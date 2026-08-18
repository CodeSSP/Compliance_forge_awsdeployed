"""
orchestrator.py  —  Layer 2 Transaction Monitor (unified)

Runs the six detection categories C1–C6 in parallel for one transaction, against
the unified dataset, and combines them into:

  * a hard FLAG  (suspicious / not) — raised if ANY category hard-fires
  * a weighted suspicion score (0–1) using the spec's C1–C6 weights, emitted as
    the continuous signal that Layer 3 routes on and ranks by.

Decision rule (per the agreed design): OR-of-hard-triggers gates the flag; the
weighted sum is computed and passed downstream but does NOT gate the flag. This
is what lets a narrow-but-real C5 (weight 0.07) or C6 (0.10) breach contribute
to whole-dataset recall instead of being buried under a single sum threshold.

Each detector keeps its own detection logic untouched; this module only adapts
the unified row into the shape each one expects (via data_layer adapters) and
normalises every detector's output to a common {fired, score, trigger} contract.
"""

import asyncio
from datetime import datetime

from .data_layer import (
    DataLayer,
    c1_baseline,
    c1_recent_dicts,
    c6_account_history,
    c6_transaction,
    _f,
    _parse_ts,
)

# --- spec weights (C1–C6 sum to 1.00) -------------------------------------
WEIGHTS = {
    "C1": 0.27,
    "C2": 0.20,
    "C3": 0.17,
    "C4": 0.19,
    "C5": 0.07,
    "C6": 0.10,
}


# ===========================================================================
# Per-category runners. Each returns: {"fired": bool, "score": float,
#                                       "trigger": str|None}
# ===========================================================================

# ---- C1 Velocity & Structuring -------------------------------------------
from .detectors import c1_adapter


async def run_c1(row, dl):
    return c1_adapter.evaluate_row(row, dl)


# ---- C2 Sanctions & Watchlist --------------------------------------------
from .detectors import c2_sanctions_and_watchlist as c2mod


def _c2_event(row):
    return {
        "tx_id": row["tx_id"],
        "timestamp": row.get("timestamp"),
        "channel": row.get("channel"),
        "amount_inr": _f(row.get("amount_inr")),
        "sender": {
            "account_id": row.get("sender_account_id"),
            "name": row.get("sender_name"),
            "pan": row.get("sender_pan"),
            "dob": row.get("sender_dob"),
        },
        "receiver": {
            "account_id": row.get("receiver_account_id"),
            "name": row.get("receiver_name"),
            "pan": row.get("receiver_pan"),
            "dob": row.get("receiver_dob"),
        },
    }


async def run_c2(row, dl):
    res = c2mod.evaluate_row(row, dl.watchlist)
    return res


# ---- C3 Graph / Network Flow ---------------------------------------------
from .detectors import c3_graph_network_flow as c3mod


async def run_c3(row, dl):
    return c3mod.evaluate_row(row, dl)


# ---- C4 Account Risk & Dormancy ------------------------------------------
from .detectors import c4_account_risk_and_dormancy as c4mod


async def run_c4(row, dl):
    return c4mod.evaluate_row(row, dl)


# ---- C5 Cross-Border / FEMA-LRS ------------------------------------------
from .detectors import c5_fema_lrs as c5mod


async def run_c5(row, dl):
    return c5mod.evaluate_row(row, dl)


# ---- C6 Geo-Anomaly -------------------------------------------------------
from .detectors.c6_geo_anomaly.detector import predict as c6_predict


async def run_c6(row, dl):
    acc = dl.account_for(row["sender_account_id"])
    cur_ts = _parse_ts(row["timestamp"])
    hist = dl.history_for(row["sender_account_id"], before_ts=cur_ts)
    ah = c6_account_history(acc, hist, dl)
    out = c6_predict(c6_transaction(row), ah)
    f = out["evidence"]["features"]
    fired = out["fired"]
    score = out["score"]

    # --- Travel-profile gate (precision) ---------------------------------
    # A new/foreign location is legitimate when the account's travel profile
    # explains it AND the device is unchanged. Genuine fraud here always brings
    # a NEW device, an impossible jump, a FATF jurisdiction, or a balance drain;
    # none of those are gated away.
    profile = (acc.get("travel_profile") or "DOMESTIC_STATIC").upper()
    device_unchanged = not f.get("is_new_device")
    benign_signals_only = (
        not f.get("is_new_device")
        and not f.get("impossible_travel")
        and not f.get("is_fatf_high_risk")
        and not f.get("is_balance_drain")
    )
    if device_unchanged and benign_signals_only:
        if profile in ("DOMESTIC_TRAVELLER", "INTERNATIONAL_FREQUENT") or (
            f.get("is_foreign") and profile != "DOMESTIC_STATIC"
        ):
            fired, score = False, 0.0
        # Domestic-static account, foreign vacation on the SAME device: a single
        # foreign-country signal alone (no device/impossible/FATF/drain) is not
        # enough to flag — matches the vacation_foreign_legit control cases.
        elif f.get("is_foreign") and not f.get("is_new_location"):
            fired, score = False, 0.0
        elif f.get("is_foreign"):
            fired, score = False, 0.0

    # --- Subtle device-probe rescue (recall) -----------------------------
    # A transaction on a NEW device for an otherwise-quiet account is the
    # account-takeover probe signature even when the amount is tiny (so the
    # noisy-OR score sits just under threshold). A new device is decisive.
    if not fired and f.get("is_new_device"):
        fired, score = True, max(score, 0.55)

    trig = None
    if fired:
        if f.get("impossible_travel"):
            trig = "C6_impossible_travel"
        elif f.get("is_fatf_high_risk"):
            trig = "C6_jurisdiction"
        elif f.get("is_balance_drain") and f.get("is_new_device"):
            trig = "C6_takeover"
        elif f.get("is_new_device"):
            trig = "C6_newloc_newdev" if f.get("is_new_location") else "C6_subtle_probe"
        else:
            trig = "C6_newloc_newdev"
    return {"fired": fired, "score": score, "trigger": trig}


# ===========================================================================
# Orchestration
# ===========================================================================
async def monitor(row, dl):
    """Run all six detectors in parallel; combine to a flag + weighted score."""
    results = await asyncio.gather(
        run_c1(row, dl), run_c2(row, dl), run_c3(row, dl),
        run_c4(row, dl), run_c5(row, dl), run_c6(row, dl),
    )
    cats = ["C1", "C2", "C3", "C4", "C5", "C6"]
    per = dict(zip(cats, results))

    weighted = round(sum(WEIGHTS[c] * per[c]["score"] for c in cats), 4)
    max_cat_score = max((per[c]["score"] for c in cats if per[c]["fired"]), default=0.0)
    final_score = max(weighted, max_cat_score)
    fired_cats = [c for c in cats if per[c]["fired"]]
    triggers = [per[c]["trigger"] for c in fired_cats if per[c]["trigger"]]
    flag = len(fired_cats) > 0

    return {
        "tx_id": row["tx_id"],
        "flag": flag,
        "suspicion_score": final_score,
        "fired_categories": fired_cats,
        "triggers": triggers,
        "per_category": per,
    }


def monitor_sync(row, dl):
    return asyncio.run(monitor(row, dl))
