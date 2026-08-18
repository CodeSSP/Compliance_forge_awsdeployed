"""L7 archival to S3. Replaces blob_storage.py (Azure Blob Storage)."""
from aws import s3_store
from config import get_config

config = get_config()


def upload_to_s3(document: dict):
    """Archives the raw scraped document text to S3 for reference and replay."""
    doc_id = document.get("document_id", "unknown_doc")
    key = f"{config.S3_RAW_PREFIX}{doc_id}.txt"

    result = s3_store.put_text(key, document.get("text", ""))
    if result:
        print(f"L7: Archived raw text to s3://{s3_store.BUCKET}/{key}")
    else:
        print("L7: S3_BUCKET not configured — skipping raw archive.")
    return result


# Backwards-compatible alias so existing call sites keep working.
upload_to_blob = upload_to_s3
