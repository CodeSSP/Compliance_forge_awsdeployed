"""All CockroachDB reads and writes the pipeline needs.

Replaces:
  data/case_memory.json          -> case_memory table
  data/regulation_meta.json      -> regulation_meta table
  L2 .../data/*.csv              -> transactions / account_details / case_history / watchlist
  ui_transactions.csv            -> transactions rows with source='ui'
  baseline_fixture.json          -> account_baselines table

Detector logic is untouched. Rows come back as plain dicts with the same keys
the CSV readers produced, so every downstream field access still works.
"""
import json
import logging

from aws import db

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# L2 reference data
# ---------------------------------------------------------------------------

_TX_COLS = [
    "tx_id", "timestamp", "channel", "amount_inr", "sender_account_id", "sender_name",
    "sender_pan", "sender_dob", "sender_bank", "sender_ifsc", "sender_vpa",
    "receiver_account_id", "receiver_name", "receiver_pan", "receiver_dob",
    "receiver_bank", "receiver_vpa", "receiver_state", "receiver_city",
    "tx_location_city", "tx_location_state", "tx_location_country",
    "tx_location_lat", "tx_location_lon", "device_id", "purpose_code",
    "is_cross_border", "fx_usd_inr", "usd_equiv", "beneficiary_id", "tx_status",
]

_ACC_COLS = [
    "account_id", "pan", "holder_name", "dob", "account_age_days", "account_type",
    "kyc_status", "home_state", "home_city", "typical_device_id",
    "avg_monthly_txn_count", "avg_monthly_txn_value_inr", "avg_tx_amount_inr",
    "balance_inr", "previous_flags", "previous_strs", "linked_accounts_count",
    "occupation_category", "is_pep", "negative_news_flag", "account_dormancy_days",
    "onboarding_channel", "is_registered_merchant", "travel_profile", "home_country",
]

_HIST_COLS = [
    "account_id", "timestamp", "amount_inr", "channel", "counterparty_id",
    "direction", "tx_location_lat", "tx_location_lon", "tx_location_city",
    "tx_location_state", "tx_location_country",
]

_WL_COLS = [
    "watchlist_id", "primary_name", "aliases", "entity_type", "dob_or_incorp",
    "nationality_or_country", "pan", "passport", "cin_or_din", "national_id_last4",
    "last_known_address", "phone", "listing_source", "reference_number",
    "reason_narrative", "listed_date", "risk_tier",
]


def _stringify(rows, numeric=()):
    """CSV readers gave every field as str. Preserve that so detectors see no change."""
    out = []
    for r in rows:
        d = {}
        for k, v in r.items():
            if k in numeric:
                d[k] = float(v) if v is not None else 0.0
            else:
                d[k] = "" if v is None else str(v)
        out.append(d)
    return out


def all_transactions() -> list:
    rows = db.query(f"SELECT {', '.join(_TX_COLS)} FROM transactions")
    return _stringify(rows)


def all_accounts() -> list:
    rows = db.query(f"SELECT {', '.join(_ACC_COLS)} FROM account_details")
    return _stringify(rows)


def all_case_history() -> list:
    rows = db.query(
        f"SELECT {', '.join(_HIST_COLS)} FROM case_history ORDER BY account_id, timestamp"
    )
    return _stringify(rows, numeric=("amount_inr",))


def all_watchlist() -> list:
    rows = db.query(f"SELECT {', '.join(_WL_COLS)} FROM watchlist")
    return _stringify(rows)


def add_ui_transaction(tx: dict) -> None:
    """Replaces the append to ui_transactions.csv. UI-submitted rows persist here."""
    cols = [c for c in _TX_COLS if c in tx or c == "tx_id"]
    vals = [tx.get(c, "") for c in cols]
    placeholders = ", ".join(["%s"] * len(cols))
    db.execute(
        f"UPSERT INTO transactions ({', '.join(cols)}, source) "
        f"VALUES ({placeholders}, 'ui')",
        vals,
    )


def rolling_transactions(account_id: str, from_ts: str, to_ts: str) -> list:
    """C1's window query. Timestamps are ISO strings, ordered lexicographically."""
    rows = db.query(
        f"""SELECT {', '.join(_TX_COLS)} FROM transactions
            WHERE sender_account_id = %s AND timestamp >= %s AND timestamp < %s""",
        (account_id, from_ts, to_ts),
    )
    return _stringify(rows, numeric=("amount_inr",))


def account_baseline(account_id: str):
    r = db.one("SELECT baseline FROM account_baselines WHERE account_id = %s", (account_id,))
    return r["baseline"] if r else None


# ---------------------------------------------------------------------------
# L1 case memory
# ---------------------------------------------------------------------------

def load_cases() -> list:
    rows = db.query(
        """SELECT case_id, tx_id, feature_set, regulation_version_hash,
                  final_status, confidence, str_pdf_url
           FROM case_memory WHERE stale = false"""
    )
    return [dict(r) for r in rows]


@db.retry()
def store_case_row(case: dict) -> None:
    db.execute(
        """UPSERT INTO case_memory
             (case_id, tx_id, feature_set, regulation_version_hash,
              final_status, confidence, str_pdf_url)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (
            case["case_id"], case["tx_id"], list(case.get("feature_set") or []),
            case.get("regulation_version_hash"), case.get("final_status"),
            case.get("confidence"), case.get("str_pdf_url"),
        ),
    )


def invalidate_stale_cases(current_hash: str) -> int:
    """L7 closes the loop: any verdict decided under an older regulation hash
    is marked stale, so L1 can no longer short-circuit against it."""
    with db.get_pool().connection() as conn:
        cur = conn.execute(
            """UPDATE case_memory SET stale = true
               WHERE stale = false
                 AND (regulation_version_hash IS DISTINCT FROM %s)""",
            (current_hash,),
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# L1 / L7 regulation freshness
# ---------------------------------------------------------------------------

def get_regulation_hash(default: str) -> str:
    r = db.one("SELECT composite_hash FROM regulation_meta WHERE id = 1")
    if r:
        return r["composite_hash"]
    db.execute(
        "UPSERT INTO regulation_meta (id, composite_hash, sources) VALUES (1, %s, %s)",
        (default, json.dumps({})),
    )
    return default


@db.retry()
def set_regulation_hash(new_hash: str, sources: dict | None = None) -> None:
    db.execute(
        """UPSERT INTO regulation_meta (id, composite_hash, sources, updated_at)
           VALUES (1, %s, %s, now())""",
        (new_hash, json.dumps(sources or {})),
    )


# ---------------------------------------------------------------------------
# L3 / L4 verdicts
# ---------------------------------------------------------------------------

@db.retry()
def save_verdict(tx_id, case_id, confidence, band, verdict,
                 sub_scores=None, citations=None, str_pdf_url=None, str_s3_key=None) -> None:
    db.execute(
        """UPSERT INTO verdicts
             (tx_id, case_id, confidence, band, verdict,
              sub_scores, citations, str_pdf_url, str_s3_key)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (tx_id, case_id, confidence, band, verdict,
         json.dumps(sub_scores or {}), json.dumps(citations or []),
         str_pdf_url, str_s3_key),
    )


def get_verdict(tx_id: str):
    return db.one("SELECT * FROM verdicts WHERE tx_id = %s", (tx_id,))


def verdict_as_of(tx_id: str, timestamp: str):
    """What the system believed at a past instant. Pairs with the audit chain
    to answer 'why did you file this STR on 14 March'."""
    rows = db.as_of(
        "SELECT * FROM verdicts {aost} WHERE tx_id = %s", (tx_id,), timestamp
    )
    return rows[0] if rows else None


def detectors_as_of(tx_id: str, timestamp: str):
    """The L2 evidence as it stood at `timestamp`, for the same replay."""
    return db.as_of(
        "SELECT * FROM case_memory {aost} WHERE tx_id = %s", (tx_id,), timestamp
    )


# ---------------------------------------------------------------------------
# L5 maker-checker queue
# ---------------------------------------------------------------------------

@db.retry()
def enqueue_review(tx_id, case_id, band, fiu_deadline=None) -> None:
    db.execute(
        """UPSERT INTO review_queue (tx_id, case_id, band, fiu_deadline, updated_at)
           VALUES (%s, %s, %s, %s, now())""",
        (tx_id, case_id, band, fiu_deadline),
    )


@db.retry()
def claim_review(tx_id: str, user: str, role: str) -> bool:
    """Maker-checker. SERIALIZABLE means two reviewers cannot both claim the
    same role on the same case; the loser retries and sees it taken."""
    col = "maker" if role == "maker" else "checker"
    with db.get_pool().connection() as conn, conn.transaction():
        row = conn.execute(
            f"SELECT {col} FROM review_queue WHERE tx_id = %s FOR UPDATE", (tx_id,)
        ).fetchone()
        if row is None or row[col] is not None:
            return False
        conn.execute(
            f"UPDATE review_queue SET {col} = %s, updated_at = now() WHERE tx_id = %s",
            (user, tx_id),
        )
        return True


def open_reviews() -> list:
    return db.query(
        "SELECT * FROM review_queue WHERE state = 'open' ORDER BY fiu_deadline NULLS LAST"
    )


# ---------------------------------------------------------------------------
# L7 watch state
# ---------------------------------------------------------------------------

def get_watch_state(source_key: str):
    return db.one("SELECT * FROM watch_state WHERE source_key = %s", (source_key,))


@db.retry()
def set_watch_state(source_key: str, last_url: str, sha: str | None = None) -> None:
    db.execute(
        """UPSERT INTO watch_state
             (source_key, last_processed_url, content_sha256, last_checked, last_changed)
           VALUES (%s, %s, %s, now(), now())""",
        (source_key, last_url, sha),
    )


def is_document_processed(document_key: str) -> bool:
    return db.one(
        "SELECT 1 FROM processed_documents WHERE document_key = %s", (document_key,)
    ) is not None


def mark_document_processed(document_key: str) -> None:
    db.execute(
        "UPSERT INTO processed_documents (document_key) VALUES (%s)", (document_key,)
    )
