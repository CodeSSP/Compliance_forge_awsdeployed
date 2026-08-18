"""
L0: Event Ingestion

Reads transactions from CockroachDB (or a CSV during seeding) and publishes each
row as a JSON message to Amazon SQS. Also provides a receiver for L1 to consume.

Function signatures are unchanged from the Azure Queue Storage version, so
api.py and L1 need no edits beyond the import path staying the same.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import csv
import json
import logging
from pathlib import Path

from aws import sqs_queue
from config import get_config

VALID_CHANNELS = {"UPI", "NEFT", "RTGS", "IMPS", "SWIFT"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [L0] %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)
config = get_config()


class QueueClient:
    """Thin wrapper preserving the Azure QueueClient surface the callers used."""

    def send_message(self, message: str) -> None:
        sqs_queue.send(json.loads(message) if isinstance(message, str) else message)

    def receive_messages(self, max_messages: int = 1, visibility_timeout: int = 60):
        for handle, body in sqs_queue.receive(max_messages, visibility_timeout):
            yield _Msg(handle, body)

    def delete_message(self, msg) -> None:
        sqs_queue.delete(getattr(msg, "receipt_handle", None))

    def get_queue_properties(self):
        return _Props(sqs_queue.length())


class _Msg:
    def __init__(self, receipt_handle, content):
        self.receipt_handle = receipt_handle
        self.content = content
        self.id = receipt_handle


class _Props:
    def __init__(self, count):
        self.approximate_message_count = count


def get_queue_client() -> QueueClient:
    """Returns the SQS-backed queue client. No credentials needed in dev mode."""
    return QueueClient()


def publish_transactions(csv_path: str = "data/transactions.csv") -> dict:
    """
    Publishes every transaction to SQS.

    Reads from CockroachDB when CRDB_DSN is set, else falls back to the CSV so
    the seed path still works before the database is populated.

    Returns a summary dict: {total, published, errors}
    """
    stats = {"total": 0, "published": 0, "errors": 0}
    client = get_queue_client()

    rows = []
    from aws import db
    if db.available():
        from aws import store
        rows = store.all_transactions()
        log.info(f"Loaded {len(rows)} transactions from CockroachDB")
    else:
        path = Path(csv_path)
        if not path.exists():
            raise FileNotFoundError(f"CSV not found: {csv_path}")
        with open(path, newline="", encoding="utf-8") as f:
            rows = [{k: v.strip() for k, v in r.items()} for r in csv.DictReader(f)]
        log.info(f"CRDB_DSN unset — loaded {len(rows)} transactions from {csv_path}")

    for row in rows:
        stats["total"] += 1
        try:
            client.send_message(json.dumps(row))
            stats["published"] += 1
            log.info(f"Published {row['tx_id']} | {row['channel']} | Rs {row['amount_inr']}")
        except Exception as e:
            log.error(f"Failed to publish {row.get('tx_id', '?')}: {e}")
            stats["errors"] += 1

    log.info(f"Done. published={stats['published']} errors={stats['errors']}")
    return stats


def receive_message():
    client = get_queue_client()
    for msg in client.receive_messages(max_messages=1, visibility_timeout=60):
        try:
            tx = json.loads(msg.content)
            # Numeric conversions
            tx['amount_inr']      = float(tx.get('amount_inr', 0))
            tx['is_cross_border'] = tx.get('is_cross_border', '0') == '1'
            tx['usd_equiv']       = float(tx['usd_equiv']) if tx.get('usd_equiv') else None
            tx['fx_usd_inr']      = float(tx['fx_usd_inr']) if tx.get('fx_usd_inr') else None
            return msg, tx
        except Exception as e:
            log.error(f'Parse error: {e}')
            client.delete_message(msg)
            return None
    return None


def delete_message(msg) -> None:
    """Deletes (acks) a message after successful processing."""
    get_queue_client().delete_message(msg)


def get_queue_length() -> int:
    """Returns the approximate number of messages in the queue."""
    return sqs_queue.length()


if __name__ == "__main__":
    print(f"L0 transport: {sqs_queue.describe()}")
    stats = publish_transactions()
    print(f"\nPublished {stats['published']}/{stats['total']} transactions")
    print(f"Queue length: {get_queue_length()}")
