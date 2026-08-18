"""
c1_adapter.py  —  C1 Velocity & Structuring (unified-pipeline adapter)

The C1 patterns live as SIBLING transactions in transactions.csv (same sender,
clustered in time), not in case_history. This adapter detects them with three
calibrated rules, anchored to the dataset's actual signatures:

  Velocity spike      : >= VEL_MIN_1H outbound legs from the sender within a
                        rolling 1h window. (Suspicious senders: 11-17/h;
                        clean senders: 1/h.)

  Structuring         : >= STRUCT_MIN_LEGS legs to the SAME beneficiary, each in
                        the structuring band, within a rolling window.
                        (Smurfing: ~5 legs of ~Rs 2L each to one EXT beneficiary.)

  Credit-line probing : >= CREDIT_MIN_24H legs with a credit purpose code
                        (P0013) within 24h. Recurring salary (P0008, monthly
                        cadence) shares the amount band but a different purpose
                        code and never clusters in time -> correctly excluded.

Regulatory anchor: PML (Maintenance of Records) Rules 2005, Rule 3(1)(B)
integrally-connected sub-threshold series; anti-structuring proviso PMLA s.12;
RBI FRM MD 2024 EWS (Clause 8.3). (Per Layer2.pdf C1 citation index.)
"""

from datetime import datetime, timedelta
from collections import defaultdict

# --- velocity ---
VEL_MIN_1H = 3                 # >= this many legs in a rolling 1h window

# --- structuring ---
STRUCT_BAND_LO = 10_000.0     # band the smurfing legs fall in
STRUCT_BAND_HI = 500_000.0
STRUCT_MIN_LEGS = 2            # >= this many legs to the same beneficiary
STRUCT_WINDOW_DAYS = 7

# --- credit-line probing ---
CREDIT_PURPOSES = {"P0013", "P0022", "P0023"}
CREDIT_MIN_24H = 4            # >= this many credit-purpose legs within 24h
CREDIT_BAND_HI = 100_000.0    # each drawdown is "small" (below this)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _ts(s):
    try:
        return datetime.fromisoformat(s) if s else None
    except (ValueError, TypeError):
        return None


def evaluate_row(row, dl):
    """Return {fired, score, trigger} for one unified transactions.csv row."""
    sender = row.get("sender_account_id", "")
    siblings = dl.tx_out.get(sender, [])
    cur_ts = _ts(row.get("timestamp"))
    cur_amt = _f(row.get("amount_inr"))
    cur_purpose = row.get("purpose_code", "")
    cur_receiver = row.get("receiver_account_id", "")

    # ---- High Value Transaction (>10 Lakhs) ----
    if cur_amt >= 1_000_000.0:
        score = round(min(0.4 + 0.05 * (cur_amt / 1_000_000.0), 1.0), 4)
        return {"fired": True, "score": score, "trigger": "C1_high_value"}

    # ---- Credit-line probing (most specific, check first) ----
    if cur_purpose in CREDIT_PURPOSES and cur_amt < CREDIT_BAND_HI and cur_ts is not None:
        credit_legs = [
            s for s in siblings
            if s.get("purpose_code") in CREDIT_PURPOSES
            and _f(s.get("amount_inr")) < CREDIT_BAND_HI
            and _within_hours(s.get("timestamp"), cur_ts, 24)
        ]
        if len(credit_legs) >= CREDIT_MIN_24H:
            score = round(min(0.5 + 0.1 * len(credit_legs), 1.0), 4)
            return {"fired": True, "score": score, "trigger": "C1_creditline"}

    # ---- Structuring: repeated legs to same beneficiary in band ----
    if STRUCT_BAND_LO <= cur_amt <= STRUCT_BAND_HI and cur_ts is not None and cur_receiver:
        same_bene = [
            s for s in siblings
            if s.get("receiver_account_id") == cur_receiver
            and STRUCT_BAND_LO <= _f(s.get("amount_inr")) <= STRUCT_BAND_HI
            and _within_days(s.get("timestamp"), cur_ts, STRUCT_WINDOW_DAYS)
        ]
        if len(same_bene) >= STRUCT_MIN_LEGS:
            score = round(min(0.5 + 0.1 * len(same_bene), 1.0), 4)
            return {"fired": True, "score": score, "trigger": "C1_structuring"}

    # ---- Velocity spike: many legs in a rolling 1h window ----
    if cur_ts is not None:
        window = [
            s for s in siblings
            if _within_hours(s.get("timestamp"), cur_ts, 1)
        ]
        if len(window) >= VEL_MIN_1H:
            score = round(min(0.4 + 0.04 * len(window), 1.0), 4)
            return {"fired": True, "score": score, "trigger": "C1_velocity"}

    return {"fired": False, "score": 0.0, "trigger": None}


def _within_hours(ts, ref, hours):
    t = _ts(ts)
    return t is not None and abs((t - ref).total_seconds()) <= hours * 3600


def _within_days(ts, ref, days):
    t = _ts(ts)
    return t is not None and abs((t - ref).days) <= days
