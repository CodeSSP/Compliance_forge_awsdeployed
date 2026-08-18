"""
data_layer.py  —  Unified data access for Layer 2 (all six detectors)

ONE source of truth that loads the four reference CSVs once and builds, for any
transaction, the context each detector needs:

  transactions.csv     the 2000 cases to classify (the "current" transaction)
  account_details.csv  per-account metadata (age, dormancy, kyc, geo, device)
  case_history.csv     90-day prior activity per account (DEBIT/CREDIT legs)
  watchlist.csv        sanctions / watchlist entities (for C2)

The detectors were originally written against bespoke per-category schemas. This
layer adapts the unified dataset to each detector's expected input shape WITHOUT
touching the detection logic, so every category sees data in the form it expects.

Source of truth is CockroachDB when CRDB_DSN is set; the CSVs remain as an
offline fallback. The public methods (history_for, account_for, add_to_history)
keep identical signatures either way, so no detector changed.
"""

import csv
import os
from collections import defaultdict
from datetime import datetime, timedelta

from aws import db
from aws import store as crdb_store

_HERE = os.path.dirname(os.path.abspath(__file__))


def _data_dir():
    """Locate the CSVs: prefer the local 'data' dir, else this dir, else cwd."""
    for cand in (
        os.path.join(_HERE, "data"),               # L2_transaction_monitor/data
        os.path.join(_HERE, "..", "..", ".."),     # repo-root style
        os.path.join(_HERE, "..", ".."),
        _HERE,
        os.getcwd(),
    ):
        if os.path.exists(os.path.join(cand, "transactions.csv")):
            return os.path.abspath(cand)
    return os.getcwd()


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


class DataLayer:
    """Loads reference data once; serves per-transaction context to detectors."""

    def __init__(self, data_dir=None):
        self.dir = data_dir or _data_dir()
        self.backed_by_crdb = db.available()

        if self.backed_by_crdb:
            self.transactions = crdb_store.all_transactions()
            self.accounts = {r["account_id"]: r for r in crdb_store.all_accounts()}
            self.watchlist = crdb_store.all_watchlist()
            history_rows = crdb_store.all_case_history()
        else:
            db.warn_unavailable("L2 data layer")
            self.transactions = self._read("transactions.csv")
            self.accounts = {r["account_id"]: r for r in self._read("account_details.csv")}
            self.watchlist = self._read("watchlist.csv")
            history_rows = self._read("case_history.csv")

        # history grouped by account_id, each sorted by timestamp
        self.history = defaultdict(list)
        for r in history_rows:
            r["amount_inr"] = _f(r["amount_inr"])
            r["_ts"] = _parse_ts(r["timestamp"])
            self.history[r["account_id"]].append(r)
        for acc in self.history:
            self.history[acc].sort(key=lambda x: x["_ts"] or datetime.min)

        # cross-border legs grouped by sender PAN (for C5 YTD aggregation)
        self.xborder_by_pan = defaultdict(list)
        for r in self.transactions:
            if r.get("is_cross_border") == "1":
                self.xborder_by_pan[r.get("sender_pan", "")].append(r)

        # transactions.csv network adjacency (for C3 graph flow). Mule fan-in/out
        # and layering round-trips live as SIBLING transactions in transactions.csv
        # (not in case_history), so C3 builds its graph from these edges. Index
        # outgoing and incoming legs per account.
        self.tx_out = defaultdict(list)   # account -> [tx rows where it is sender]
        self.tx_in = defaultdict(list)    # account -> [tx rows where it is receiver]
        for r in self.transactions:
            s = r.get("sender_account_id")
            d = r.get("receiver_account_id")
            if s:
                self.tx_out[s].append(r)
            if d:
                self.tx_in[d].append(r)

        # Persisted UI-submitted transactions. In CRDB mode these are already
        # part of self.transactions (source='ui'), so nothing extra to load.
        if not self.backed_by_crdb:
            ui_cache_path = os.path.join(self.dir, "ui_transactions.csv")
            if os.path.exists(ui_cache_path):
                with open(ui_cache_path, newline="", encoding="utf-8") as fh:
                    for r in csv.DictReader(fh):
                        self.add_to_history(r)

    def _read(self, name):
        path = os.path.join(self.dir, name)
        with open(path, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def add_to_history(self, tx: dict, persist: bool = False):
        """Append a transaction to the in-memory history so velocity detectors see it immediately.

        With persist=True the row is also written to CockroachDB, which replaces
        the old append to ui_transactions.csv.
        """
        if persist and self.backed_by_crdb:
            try:
                crdb_store.add_ui_transaction(tx)
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning("Failed to persist UI tx to CRDB: %s", exc)

        s = tx.get("sender_account_id")
        d = tx.get("receiver_account_id") or tx.get("receiver_account_external")
        amt = _f(tx.get("amount_inr"))
        ts = _parse_ts(tx.get("timestamp"))
        channel = tx.get("channel")
        
        # Sender (Debit leg)
        if s:
            r = {
                "account_id": s, "timestamp": tx.get("timestamp"),
                "amount_inr": amt, "channel": channel,
                "counterparty_id": d, "direction": "DEBIT", "_ts": ts
            }
            self.history[s].append(r)
            self.history[s].sort(key=lambda x: x["_ts"] or datetime.min)
            self.tx_out[s].append(tx)
            
        # Receiver (Credit leg)
        if d:
            r = {
                "account_id": d, "timestamp": tx.get("timestamp"),
                "amount_inr": amt, "channel": channel,
                "counterparty_id": s, "direction": "CREDIT", "_ts": ts
            }
            self.history[d].append(r)
            self.history[d].sort(key=lambda x: x["_ts"] or datetime.min)
            self.tx_in[d].append(tx)
            
        # Cross border legs
        if tx.get("is_cross_border") == "1":
            pan = tx.get("sender_pan", "") or tx.get("sender_account_id", "")
            if pan:
                self.xborder_by_pan[pan].append(tx)

    # -- per-account rolling history -----------------------------------------
    def history_for(self, account_id, before_ts=None, hours=None, direction=None):
        """
        Prior legs for an account. Optionally bounded to the `hours` immediately
        before `before_ts`, and/or filtered by DEBIT/CREDIT direction.
        """
        rows = self.history.get(account_id, [])
        out = rows
        if before_ts is not None:
            bt = before_ts if isinstance(before_ts, datetime) else _parse_ts(before_ts)
            if bt is not None:
                lo = bt - timedelta(hours=hours) if hours else None
                out = [r for r in out if r["_ts"] is not None and r["_ts"] < bt
                       and (lo is None or r["_ts"] >= lo)]
        if direction:
            out = [r for r in out if r.get("direction") == direction]
        return out

    def account_for(self, account_id):
        return self.accounts.get(account_id, {})


# ---------------------------------------------------------------------------
# Adapters: unified row  ->  each detector's expected input shape
# ---------------------------------------------------------------------------

def _threshold_profile(acc):
    """Map account metadata to one of C1's threshold profiles."""
    atype = (acc.get("account_type") or "").lower()
    if "current" in atype or (acc.get("is_registered_merchant") in ("True", "true", True)):
        return "BUSINESS_CURRENT"
    if "nre" in atype or "nro" in atype or acc.get("home_country", "IN") != "IN":
        return "NRE_NRO"
    return "INDIVIDUAL_SAVINGS"


def c1_baseline(acc, history_debit):
    """Build C1's `baseline` dict from account metadata + debit history."""
    amounts = sorted(t["amount_inr"] for t in history_debit) if history_debit else []
    p90 = amounts[int(0.9 * (len(amounts) - 1))] if amounts else _f(acc.get("avg_tx_amount_inr"))
    avg_mo_cnt = _f(acc.get("avg_monthly_txn_count"), 30)
    avg_mo_val = _f(acc.get("avg_monthly_txn_value_inr"), 100_000)
    return {
        "account_holder_name": acc.get("holder_name", "UNKNOWN"),
        "account_type": acc.get("account_type", "Savings"),
        "threshold_profile": _threshold_profile(acc),
        "avg_daily_tx_count": max(avg_mo_cnt / 30.0, 0.5),
        "avg_daily_tx_volume_inr": max(avg_mo_val / 30.0, 1000.0),
        "p90_amount": p90 or 50_000.0,
        "typical_receivers": [],
    }


def c1_recent_dicts(history_rows):
    """C1 sub-checks expect rows with amount_inr / receiver_name / purpose_code."""
    out = []
    for t in history_rows:
        out.append({
            "amount_inr": t["amount_inr"],
            "timestamp": t.get("timestamp"),
            "receiver_name": t.get("counterparty_id", ""),  # history has no name; id stands in
            "purpose_code": t.get("purpose_code", ""),
        })
    return out


def c6_account_history(acc, history_rows, dl=None):
    """Build C6's account_history dict (locations, devices, balance, last_location)."""
    home_city = acc.get("home_city")
    home_state = acc.get("home_state")
    # POC: the account's home city is its dominant known location; history rows
    # carry no geo, so the home location anchors "known" geography.
    known = {}
    if home_city:
        known[home_city.strip().lower()] = max(len(history_rows), 5)
    return {
        "known_locations": known,
        "last_location": {},          # no per-leg geo in history -> impossible-travel off
        "typical_devices": [acc.get("typical_device_id")] if acc.get("typical_device_id") else [],
        "balance_inr": _f(acc.get("balance_inr")) or None,
        "home_country": acc.get("home_country", "IN"),
        "account_type": acc.get("account_type", "SAVINGS"),
        "travel_profile": acc.get("travel_profile", "DOMESTIC_STATIC"),
        "avg_tx_amount": _f(acc.get("avg_tx_amount_inr")) or None,
        "home_city": home_city,
        "home_state": home_state,
    }


def c6_transaction(row):
    """Build C6's transaction dict from a unified transactions.csv row."""
    lat = row.get("tx_location_lat")
    lon = row.get("tx_location_lon")
    return {
        "tx_id": row["tx_id"],
        "timestamp": row.get("timestamp"),
        "sender_account_id": row.get("sender_account_id"),
        "amount_inr": _f(row.get("amount_inr")),
        "channel": row.get("channel"),
        "purpose_code": row.get("purpose_code"),
        "device_id": row.get("device_id"),
        "location": {
            "city": row.get("tx_location_city"),
            "country": row.get("tx_location_country"),
            "lat": _f(lat) if lat else None,
            "lon": _f(lon) if lon else None,
        },
    }
