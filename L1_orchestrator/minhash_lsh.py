import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import logging
from pathlib import Path
from datasketch import MinHash

from aws import db, store as crdb_store

log = logging.getLogger(__name__)

MINHASH_PERMS        = 128
SIMILARITY_THRESHOLD = 0.80
CASE_MEMORY_FILE     = Path(__file__).parent.parent / "data" / "case_memory.json"


def extract_features(tx: dict) -> set:
    """
    Updated for new 31-column schema.
    Uses beneficiary_id when available (more precise than receiver_account_external).
    Uses is_cross_border flag directly.
    """
    amount_inr  = float(tx.get('amount_inr', 0))
    amount_band = f"amt_{int(amount_inr // 1000)}k"

    # Use beneficiary_id if present (new field), else fall back to receiver_account_external
    receiver_id = (
        tx.get('beneficiary_id')
        or tx.get('receiver_account_id')
        or tx.get('receiver_account_external')
        or 'UNK'
    )
    # Normalise empty string to UNK
    if not receiver_id:
        receiver_id = 'UNK'

    features = {
        amount_band,
        f"channel_{tx.get('channel', 'UNK')}",
        f"purpose_{tx.get('purpose_code', 'UNK')}",
        f"rcv_{receiver_id}",
        f"sender_{tx.get('sender_account_id', 'UNK')}",
    }

    # Add cross-border flag as a feature — SWIFT/cross-border transactions
    # should never short-circuit against domestic ones
    if tx.get('is_cross_border') is True or tx.get('is_cross_border') == '1':
        features.add("cross_border_YES")

    return features


def make_minhash(features: set) -> MinHash:
    m = MinHash(num_perm=MINHASH_PERMS)
    for f in sorted(features):
        m.update(f.encode("utf-8"))
    return m


def load_case_memory() -> list:
    """Loads completed cases from the CockroachDB case_memory table.

    Rows marked stale by L7 (decided under a superseded regulation hash) are
    excluded at the query level, so they can never win a short-circuit.
    """
    if db.available():
        return crdb_store.load_cases()

    db.warn_unavailable("L1 case memory")
    if not CASE_MEMORY_FILE.exists():
        return []
    with open(CASE_MEMORY_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_case_memory(cases: list) -> None:
    """Fallback writer for local-file mode. CRDB mode writes one row at a time."""
    CASE_MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CASE_MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(cases, f, indent=2)


def query_case_memory(tx: dict) -> dict | None:
    """
    Searches case memory for the most similar past transaction.
    Returns the best match if similarity >= 0.80, else None.

    Short-circuit fires when:
      1. A match is found (score >= 0.80)
      2. AND the regulation hash hasn't changed since that verdict
    """
    cases = load_case_memory()
    if not cases:
        return None

    new_features = extract_features(tx)
    new_mh       = make_minhash(new_features)
    best, best_score = None, 0.0

    for case in cases:
        stored_features = set(case.get("feature_set", []))
        if not stored_features:
            continue
        stored_mh = make_minhash(stored_features)
        score     = new_mh.jaccard(stored_mh)
        if score > best_score:
            best_score, best = score, case

    if best_score >= SIMILARITY_THRESHOLD:
        best["_similarity_score"] = best_score
        log.info(
            f"Memory hit: {tx.get('tx_id')} matched "
            f"{best.get('tx_id')} (score={best_score:.3f})"
        )
        return best

    return None


def store_case(state: dict) -> None:
    """
    Saves a completed case to memory so future similar transactions
    can match against it. Called after a case reaches final_status.
    """
    record = {
        "tx_id":                   state["tx_id"],
        "case_id":                 state["case_id"],
        "feature_set":             build_feature_set(state["tx_payload"]),
        "regulation_version_hash": state.get("regulation_hash_current"),
        "final_status":            state.get("final_status") or state.get("verdict"),
        "confidence":              state.get("confidence"),
        "str_pdf_url":             state.get("str_pdf_url"),
    }

    if db.available():
        crdb_store.store_case_row(record)
        log.info(f"Stored case {state['tx_id']} in CockroachDB case memory")
        return

    cases = load_case_memory()
    cases.append(record)
    save_case_memory(cases)
    log.info(f"Stored case {state['tx_id']} in memory ({len(cases)} total)")


def build_feature_set(tx: dict) -> list:
    """Returns sorted feature list for storing in case memory."""
    return sorted(extract_features(tx))