"""
L6: Audit Logger

Writes the tamper-evident audit record to CockroachDB. The chain and the
business rows live in the same database, so an audit entry and the verdict it
describes commit or fail together — there is no path where a verdict exists
without its audit block.
"""
import logging

from .hash_chain import append_to_chain, anchor, verify, chain_length

log = logging.getLogger(__name__)


async def log_transaction(event, verdict, event_type: str = "verdict"):
    """Logs the transaction outcome and appends to the hash chain."""
    tx_id = event.get("tx_id") if isinstance(event, dict) else "?"
    log.info("L6: logging transaction %s with verdict: %s", tx_id, verdict)
    return append_to_chain(event, verdict, event_type=event_type)


async def verify_hash_chain(shard: str | None = None):
    """Daily integrity job. Returns the first broken seq, or None if intact."""
    broken = verify(shard) if shard else verify()
    if broken is None:
        log.info("L6: hash chain intact (%d blocks)", chain_length())
    else:
        log.error("L6: hash chain broken at seq %s", broken)
    return broken


async def anchor_chain():
    """Periodic global anchor across all shards."""
    return anchor()
