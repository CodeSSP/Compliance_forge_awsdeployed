"""Amazon SQS. Replaces Azure Queue Storage as the L0 transport.

Falls back to an in-process deque when SQS_QUEUE_URL is unset, so the demo runs
end to end without AWS credentials. That was already the behaviour with the
Azure queue — L0 caught the connection error and continued in-process.
"""
import os
import json
import logging
from collections import deque

log = logging.getLogger(__name__)

QUEUE_URL = os.environ.get("SQS_QUEUE_URL", "")
AWS_REGION = os.environ.get("AWS_REGION", "ap-south-1")

_sqs = None
_local = deque()


def _client():
    global _sqs
    if _sqs is None:
        import boto3
        _sqs = boto3.client("sqs", region_name=AWS_REGION)
    return _sqs


def available() -> bool:
    if not QUEUE_URL:
        return False
    try:
        import boto3  # noqa: F401
        return True
    except ImportError:
        return False


class LocalMessage:
    """Stands in for an SQS message when running without AWS."""
    def __init__(self, body):
        self.body = body
        self.receipt_handle = None

    @property
    def content(self):
        return self.body


def send(payload: dict) -> None:
    body = json.dumps(payload)
    if available():
        _client().send_message(QueueUrl=QUEUE_URL, MessageBody=body)
    else:
        _local.append(body)


def receive(max_messages: int = 1, visibility_timeout: int = 60):
    """Yields (handle, body_str) tuples."""
    if available():
        resp = _client().receive_message(
            QueueUrl=QUEUE_URL,
            MaxNumberOfMessages=min(max_messages, 10),
            VisibilityTimeout=visibility_timeout,
            WaitTimeSeconds=1,
        )
        for m in resp.get("Messages", []):
            yield m["ReceiptHandle"], m["Body"]
    else:
        for _ in range(max_messages):
            if not _local:
                return
            yield None, _local.popleft()


def delete(receipt_handle) -> None:
    if available() and receipt_handle:
        _client().delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=receipt_handle)


def length() -> int:
    if available():
        attrs = _client().get_queue_attributes(
            QueueUrl=QUEUE_URL,
            AttributeNames=["ApproximateNumberOfMessages"],
        )
        return int(attrs["Attributes"]["ApproximateNumberOfMessages"])
    return len(_local)


def describe() -> str:
    return f"SQS {QUEUE_URL}" if available() else "in-process queue (SQS_QUEUE_URL unset)"
