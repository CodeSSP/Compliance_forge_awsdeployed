"""S3. Replaces Azure Blob Storage for raw circulars, STR PDFs and goAML XML.

Buckets (one bucket, three prefixes — simpler to provision than three containers):
  regulations/raw/<document_id>.txt   scraped circular text
  reports/str/<tx_id>.pdf             generated STR review copies
  reports/goaml/<tx_id>.xml           goAML submission XML
"""
import os
import logging

log = logging.getLogger(__name__)

BUCKET = os.environ.get("S3_BUCKET", "")
AWS_REGION = os.environ.get("AWS_REGION", "ap-south-1")

_s3 = None


def _client():
    global _s3
    if _s3 is None:
        import boto3
        _s3 = boto3.client("s3", region_name=AWS_REGION)
    return _s3


def available() -> bool:
    if not BUCKET:
        return False
    try:
        import boto3  # noqa: F401
        return True
    except ImportError:
        return False


def put_text(key: str, text: str, content_type: str = "text/plain") -> str | None:
    if not available():
        log.warning("S3_BUCKET not set — skipping upload of %s", key)
        return None
    try:
        _client().put_object(
            Bucket=BUCKET, Key=key,
            Body=str(text).encode("utf-8"),
            ContentType=content_type,
            ServerSideEncryption="aws:kms" if os.environ.get("S3_KMS_KEY_ID") else "AES256",
            **({"SSEKMSKeyId": os.environ["S3_KMS_KEY_ID"]} if os.environ.get("S3_KMS_KEY_ID") else {}),
        )
        log.info("S3: wrote %s", key)
        return key
    except Exception as exc:
        log.warning("S3 upload failed for %s: %s", key, exc)
        return None


def put_file(key: str, path: str, content_type: str = "application/octet-stream") -> str | None:
    if not available():
        log.warning("S3_BUCKET not set — keeping %s on local disk only", path)
        return None
    try:
        with open(path, "rb") as fh:
            _client().put_object(Bucket=BUCKET, Key=key, Body=fh, ContentType=content_type)
        log.info("S3: uploaded %s -> %s", path, key)
        return key
    except Exception as exc:
        log.warning("S3 upload failed for %s: %s", path, exc)
        return None


def get_text(key: str) -> str | None:
    if not available():
        return None
    try:
        obj = _client().get_object(Bucket=BUCKET, Key=key)
        return obj["Body"].read().decode("utf-8")
    except Exception as exc:
        log.warning("S3 read failed for %s: %s", key, exc)
        return None


def list_keys(prefix: str) -> list:
    if not available():
        return []
    keys, token = [], None
    while True:
        kwargs = {"Bucket": BUCKET, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        resp = _client().list_objects_v2(**kwargs)
        keys.extend(o["Key"] for o in resp.get("Contents", []))
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    return keys


def get_bytes(key: str) -> bytes | None:
    if not available():
        return None
    try:
        return _client().get_object(Bucket=BUCKET, Key=key)["Body"].read()
    except Exception as exc:
        log.warning("S3 read failed for %s: %s", key, exc)
        return None
