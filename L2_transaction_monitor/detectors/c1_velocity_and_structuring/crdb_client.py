"""
crdb_client.py
--------------
Data access layer for C1.

Reads from CockroachDB when CRDB_DSN is set, else falls back to
baseline_fixture.json + transactions.csv so the detector runs offline.
The function SIGNATURES are unchanged — checks.py never changes.

Two responsibilities:
  1. get_account_baseline()     — per-account 90-day stats
  2. get_rolling_transactions() — recent transactions for a sender within N hours
"""

import json
import csv
from datetime import datetime, timedelta
from pathlib import Path

from aws import db
from aws import store as crdb_store

# ---------------------------------------------------------------------------
# Load fixtures once at module import (POC only)
# ---------------------------------------------------------------------------

_FIXTURE_PATH = Path(__file__).parent / "baseline_fixture.json"
_TRANSACTIONS_PATH = Path(__file__).parent.parent.parent / "data" / "transactions.csv"

# Fallback: if running tests from repo root or alternate layout
_ALT_TRANSACTIONS_PATHS = [
    Path(__file__).parent / "transactions.csv",
    Path("/mnt/project/transactions.csv"),
]

def _load_baseline() -> dict:
    with open(_FIXTURE_PATH) as f:
        return json.load(f)

def _load_transactions() -> list[dict]:
    candidates = [_TRANSACTIONS_PATH] + _ALT_TRANSACTIONS_PATHS
    for path in candidates:
        if path.exists():
            with open(path) as f:
                rows = list(csv.DictReader(f))
                # Normalise amount to float
                for r in rows:
                    r["amount_inr"] = float(r["amount_inr"])
                return rows
    raise FileNotFoundError(
        f"transactions.csv not found. Tried: {[str(p) for p in candidates]}"
    )

_BASELINE: dict = _load_baseline()
_ALL_TRANSACTIONS: list[dict] = _load_transactions()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def get_account_baseline(account_id: str) -> dict:
    """
    Returns the 90-day baseline statistics for an account.

    CockroachDB: SELECT baseline FROM account_baselines WHERE account_id = ...
    Fallback:    baseline_fixture.json
    """
    baseline = None
    if db.available():
        try:
            baseline = crdb_store.account_baseline(account_id)
        except Exception:
            baseline = None
    if baseline is None:
        baseline = _BASELINE.get(account_id)
    if baseline is None:
        # Unknown account — return a conservative default
        return {
            "account_holder_name": "UNKNOWN",
            "account_type": "SAVINGS",
            "is_business": False,
            "account_age_days": 0,
            "kyc_level": "UNKNOWN",
            "threshold_profile": "INDIVIDUAL_SAVINGS",
            "avg_daily_tx_count": 2.0,
            "avg_daily_tx_volume_inr": 10_000.0,
            "avg_tx_amount": 5_000.0,
            "p10_amount": 500.0,
            "p90_amount": 20_000.0,
            "typical_receivers": [],
        }
    return baseline


async def get_rolling_transactions(
    account_id: str,
    hours: int,
    current_ts: datetime,
) -> list[dict]:
    """
    Returns all completed transactions by sender account_id
    within the last `hours` hours BEFORE current_ts.
    Does NOT include the current transaction itself.

    CockroachDB: indexed range scan on (sender_account_id, timestamp).
    Fallback:    filters in-memory from transactions.csv
    """
    from_ts = current_ts - timedelta(hours=hours)

    if db.available():
        try:
            return crdb_store.rolling_transactions(
                account_id, from_ts.isoformat(), current_ts.isoformat()
            )
        except Exception:
            pass

    result = []
    for t in _ALL_TRANSACTIONS:
        if t["sender_account_id"] != account_id:
            continue
        try:
            tx_ts = datetime.fromisoformat(t["timestamp"])
        except ValueError:
            continue
        if from_ts <= tx_ts < current_ts:
            result.append(t)
    return result