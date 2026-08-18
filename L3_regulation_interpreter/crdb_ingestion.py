"""
L3 corpus ingestion: S3 -> chunk -> embed -> CockroachDB.

Replaces chroma_ingestion.py (Blob Storage -> ChromaDB) and azure_ingestion.py
(Blob Storage -> Azure AI Search). One script now, because there is one store.

Usage:
    python -m L3_regulation_interpreter.crdb_ingestion

Reads PDFs from s3://$S3_BUCKET/regulations/pdf/, chunks with the existing
chunk_regulation_document(), embeds with nomic-embed-text (768 dims), and upserts
into regulatory_chunks. Already-processed documents are skipped via the
processed_documents table rather than a local JSON tracker.
"""
import hashlib
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv

sys.path.append(str(Path(__file__).parent.parent))

from aws import db, s3_store, store as crdb_store, vectors
from L3_regulation_interpreter.hybrid_retrieval import chunk_regulation_document
from L3_regulation_interpreter.llm_client import generate_ollama_embedding
from L3_regulation_interpreter.corpus_builder import pdf_to_document

load_dotenv()

PDF_PREFIX = os.environ.get("S3_REGULATION_PREFIX", "regulations/pdf/")
BATCH_SIZE = 200


def _embed_with_retry(text: str) -> List[float]:
    for attempt in range(5):
        vector = generate_ollama_embedding(text)
        if vector:
            return vector
        time.sleep(2 ** attempt)
    return []


def download_s3_corpus() -> List[Dict[str, Any]]:
    """Pulls every unprocessed regulation PDF from S3 and parses it."""
    keys = [k for k in s3_store.list_keys(PDF_PREFIX) if k.lower().endswith(".pdf")]
    if not keys:
        print(f"No PDFs found under s3://{s3_store.BUCKET}/{PDF_PREFIX}")
        return []

    documents = []
    for key in keys:
        if crdb_store.is_document_processed(key):
            print(f"Skipping '{key}' (already ingested)")
            continue

        print(f"Downloading '{key}'...")
        data = s3_store.get_bytes(key)
        if not data:
            continue

        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                tmp.write(data)
                tmp_path = Path(tmp.name)

            doc = pdf_to_document(tmp_path)
            doc_id = Path(key).stem
            doc["document_id"] = doc_id
            doc["title"] = doc.get("title") or doc_id
            doc["url"] = f"s3://{s3_store.BUCKET}/{key}"
            doc["_s3_key"] = key
            documents.append(doc)
            os.remove(tmp_path)
        except Exception as e:
            print(f"Skipping {key} due to PDF parse error: {e}")

    return documents


def ingest_documents(documents: List[Dict[str, Any]]) -> int:
    """Chunks, embeds and upserts. Row + embedding + index commit together."""
    total = 0
    for doc in documents:
        chunks = chunk_regulation_document(doc)
        print(f"  {doc['document_id']}: {len(chunks)} chunks")

        batch = []
        for chunk in chunks:
            text = chunk.get("searchable_text") or chunk.get("content", "")
            vector = _embed_with_retry(text)
            if not vector:
                print(f"    Warning: failed to embed {chunk['chunk_id']}, skipping")
                continue

            chunk["embedding"] = vector
            chunk["content_sha256"] = hashlib.sha256(
                chunk.get("content", "").encode("utf-8")
            ).hexdigest()
            batch.append(chunk)

            if len(batch) >= BATCH_SIZE:
                total += vectors.upsert_chunks(batch)
                print(f"    Committed {total} chunks so far")
                batch = []

        if batch:
            total += vectors.upsert_chunks(batch)

        crdb_store.mark_document_processed(doc.get("_s3_key") or doc["document_id"])
        print(f"  {doc['document_id']}: done ({total} chunks committed)")

    return total


def main():
    if not db.available():
        print("CRDB_DSN is not set (or psycopg is missing). Nothing to ingest into.")
        sys.exit(1)
    if not s3_store.available():
        print("S3_BUCKET is not set. Nothing to ingest from.")
        sys.exit(1)

    documents = download_s3_corpus()
    if not documents:
        print("No new documents. Corpus already current.")
        print(f"regulatory_chunks currently holds {vectors.chunk_count()} chunks.")
        sys.exit(0)

    print(f"Ingesting {len(documents)} documents into CockroachDB...")
    total = ingest_documents(documents)
    print(f"\nIngestion complete. {total} chunks committed.")
    print(f"regulatory_chunks now holds {vectors.chunk_count()} chunks.")


if __name__ == "__main__":
    main()
