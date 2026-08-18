"""
L7: OCR via Amazon Textract. Replaces Azure AI Document Intelligence.

Scanned RBI circulars are common enough that the text layer is often missing.
Textract is used only when TEXTRACT_ENABLED=true; otherwise the pypdf text
layer from corpus_builder is used, which is correct for born-digital PDFs.
"""
import logging

import boto3

from aws import s3_store
from config import get_config

log = logging.getLogger(__name__)
config = get_config()


def extract_text_from_pdfs(pdf_source: str) -> str:
    """
    Extracts text from a PDF.

    pdf_source may be an S3 key (preferred — Textract reads it in place) or a
    local path, which is uploaded to S3 first because Textract's async API only
    accepts S3 objects.
    """
    if not config.TEXTRACT_ENABLED:
        log.info("L7: Textract disabled — relying on the pypdf text layer.")
        return ""

    if not s3_store.available():
        log.warning("L7: S3_BUCKET unset — cannot run Textract.")
        return ""

    key = pdf_source
    if not pdf_source.startswith(config.S3_REGULATION_PREFIX):
        key = f"{config.S3_REGULATION_PREFIX}{pdf_source.split('/')[-1]}"
        s3_store.put_file(key, pdf_source, content_type="application/pdf")

    client = boto3.client("textract", region_name=config.AWS_REGION)

    try:
        job = client.start_document_text_detection(
            DocumentLocation={"S3Object": {"Bucket": s3_store.BUCKET, "Name": key}}
        )
        job_id = job["JobId"]
        log.info("L7: Textract job %s started for %s", job_id, key)

        import time
        for _ in range(60):
            resp = client.get_document_text_detection(JobId=job_id)
            status = resp["JobStatus"]
            if status == "SUCCEEDED":
                break
            if status == "FAILED":
                log.error("L7: Textract job %s failed", job_id)
                return ""
            time.sleep(5)
        else:
            log.error("L7: Textract job %s timed out", job_id)
            return ""

        lines, token = [], None
        while True:
            resp = client.get_document_text_detection(
                JobId=job_id, **({"NextToken": token} if token else {})
            )
            lines.extend(
                b["Text"] for b in resp.get("Blocks", []) if b["BlockType"] == "LINE"
            )
            token = resp.get("NextToken")
            if not token:
                break

        return "\n".join(lines)

    except Exception as exc:
        log.warning("L7: Textract extraction failed for %s: %s", key, exc)
        return ""
