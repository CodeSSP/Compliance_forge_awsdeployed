"""
L6: SHA-256 Hash Chain on CockroachDB.

The chain is sharded so appends do not all serialise on one range, and anchored
periodically so there is still a single provable root.

Why this is safe here and was not before: CockroachDB runs SERIALIZABLE by
default. Two concurrent appends cannot both read the same head and link to the
same prev_hash — one of them hits a 40001 retry instead. Under read-committed
that race silently forks the chain, in the one component whose entire job is
being tamper-evident.
"""
import hashlib
import json
import logging
import os

from aws import db

log = logging.getLogger(__name__)

SHARD = os.environ.get("AUDIT_SHARD", os.environ.get("AWS_REGION", "ap-south-1"))
GENESIS = "genesis_hash_placeholder"


def calculate_hash(data):
    """Calculates the SHA-256 hash of a dictionary."""
    encoded_data = json.dumps(data, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded_data).hexdigest()


def get_previous_hash(shard: str = SHARD) -> str:
    """Head of this shard's chain."""
    if not db.available():
        return GENESIS
    row = db.one(
        "SELECT entry_hash FROM audit_chain WHERE shard = %s ORDER BY seq DESC LIMIT 1",
        (shard,),
    )
    return row["entry_hash"] if row else GENESIS


@db.retry()
def append_to_chain(event, verdict, event_type: str = "verdict", shard: str = SHARD) -> dict:
    """
    Appends a new block. Read of the head and write of the new entry happen in
    one serializable transaction, so the chain cannot fork under concurrency.
    """
    layer_data = {"event": event, "verdict": verdict}

    if not db.available():
        db.warn_unavailable("L6 audit chain")
        block = {"prev_hash": GENESIS, "layer_data": layer_data}
        new_hash = calculate_hash(block)
        log.info("L6: (local mode) block hash %s", new_hash)
        return {"shard": shard, "seq": None, "entry_hash": new_hash, "prev_hash": GENESIS}

    tx_id = (event or {}).get("tx_id") if isinstance(event, dict) else None
    case_id = (event or {}).get("case_id") if isinstance(event, dict) else None

    with db.get_pool().connection() as conn, conn.transaction():
        head = conn.execute(
            "SELECT seq, entry_hash FROM audit_chain WHERE shard = %s ORDER BY seq DESC LIMIT 1",
            (shard,),
        ).fetchone()

        seq = (head["seq"] + 1) if head else 1
        prev_hash = head["entry_hash"] if head else GENESIS

        entry_hash = calculate_hash({
            "shard": shard,
            "seq": seq,
            "prev_hash": prev_hash,
            "event_type": event_type,
            "layer_data": layer_data,
        })

        conn.execute(
            """INSERT INTO audit_chain
                 (shard, seq, event_type, tx_id, case_id, payload, prev_hash, entry_hash)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (shard, seq, event_type, tx_id, case_id,
             json.dumps(layer_data, default=str), prev_hash, entry_hash),
        )

    log.info("L6: appended block %s/%s hash=%s", shard, seq, entry_hash[:16])
    return {"shard": shard, "seq": seq, "entry_hash": entry_hash, "prev_hash": prev_hash}


@db.retry()
def anchor() -> dict:
    """
    Hashes the head of every shard into one global anchor. Run on a schedule
    (EventBridge -> Lambda). Per-shard chains scale; anchors give one root.
    """
    if not db.available():
        return {}

    with db.get_pool().connection() as conn, conn.transaction():
        heads = conn.execute(
            "SELECT shard, max(seq) AS seq FROM audit_chain GROUP BY shard ORDER BY shard"
        ).fetchall()

        detail = {}
        for h in heads:
            row = conn.execute(
                "SELECT entry_hash FROM audit_chain WHERE shard = %s AND seq = %s",
                (h["shard"], h["seq"]),
            ).fetchone()
            detail[h["shard"]] = {"seq": h["seq"], "hash": row["entry_hash"]}

        prev = conn.execute(
            "SELECT anchor_hash FROM audit_anchors ORDER BY anchored_at DESC LIMIT 1"
        ).fetchone()
        prev_anchor = prev["anchor_hash"] if prev else None

        anchor_hash = calculate_hash({"prev": prev_anchor, "heads": detail})

        conn.execute(
            "INSERT INTO audit_anchors (heads, anchor_hash, prev_anchor) VALUES (%s, %s, %s)",
            (json.dumps(detail), anchor_hash, prev_anchor),
        )

    log.info("L6: anchored %d shards, root=%s", len(detail), anchor_hash[:16])
    return {"heads": detail, "anchor_hash": anchor_hash}


def verify(shard: str = SHARD):
    """
    Recomputes the chain from scratch. Returns the first broken seq, or None if
    the chain is intact. This is what you run on stage after killing a node.
    """
    if not db.available():
        return None

    rows = db.query(
        """SELECT seq, event_type, payload, prev_hash, entry_hash
           FROM audit_chain WHERE shard = %s ORDER BY seq""",
        (shard,),
    )

    prev = GENESIS
    for r in rows:
        if r["prev_hash"] != prev:
            return r["seq"]
        recomputed = calculate_hash({
            "shard": shard,
            "seq": r["seq"],
            "prev_hash": prev,
            "event_type": r["event_type"],
            "layer_data": r["payload"],
        })
        if recomputed != r["entry_hash"]:
            return r["seq"]
        prev = r["entry_hash"]
    return None


def chain_length(shard: str = SHARD) -> int:
    if not db.available():
        return 0
    row = db.one("SELECT count(*) AS n FROM audit_chain WHERE shard = %s", (shard,))
    return int(row["n"]) if row else 0
