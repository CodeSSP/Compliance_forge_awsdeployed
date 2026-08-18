"""
L7: Corpus indexer on CockroachDB.

Replaces the Azure AI Search indexer. The RAG-hunter behaviour is preserved:
when a circular is updated or replaced, the old chunks are deleted before the
new ones land — except now the delete and the insert happen inside one
serializable transaction, so the corpus is never briefly missing both versions.
"""
import hashlib
import logging
import re
import uuid
from typing import Any, Dict

from aws import vectors
from L3_regulation_interpreter.hybrid_retrieval import chunk_regulation_document
from L3_regulation_interpreter.llm_client import generate_ollama_embedding

log = logging.getLogger(__name__)

_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "shall", "from", "any", "all",
    "such", "may", "not", "are", "was", "were", "has", "have", "been", "its",
    "into", "under", "upon", "which", "their", "these", "those", "other",
}


def _extract_key_phrases(text: str, limit: int = 12) -> list:
    """Cheap keyword extraction. Kept from the Azure cognitive-skill emulation
    so key_phrases stays populated for anything that reads it."""
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9\-]{3,}", str(text or "").lower())
    counts: Dict[str, int] = {}
    for t in tokens:
        if t in _STOPWORDS:
            continue
        counts[t] = counts.get(t, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [w for w, _ in ranked[:limit]]


def check_document_exists(title: str) -> bool:
    """True if any chunk of this circular is already in the corpus."""
    try:
        return vectors.document_exists(title)
    except Exception as e:
        log.warning("L7: existence check failed for '%s': %s", title, e)
        return False


def update_search_index(document: Dict[str, Any], change_info: dict):
    """
    Deletes superseded chunks if applicable, then upserts the new ones.
    """
    action = change_info.get("action")
    target_id = change_info.get("target_circular_id")
    log.info("L7: updating corpus. Action: %s", action)

    # 1. RAG-hunter: remove the superseded circular
    if action in ("update", "replacement") and target_id:
        log.info("L7: RAG-hunter searching for superseded circular: %s", target_id)
        try:
            doc_id = vectors.find_document_id(target_id)
            if doc_id:
                deleted = vectors.delete_document(doc_id)
                log.info("L7: deleted %d chunks belonging to %s", deleted, doc_id)
            else:
                log.info("L7: superseded circular %s not present in the corpus", target_id)
        except Exception as e:
            log.warning("L7: failed to delete superseded document: %s", e)

    # 2. Chunk, embed and commit the new circular
    if "document_id" not in document:
        document["document_id"] = str(uuid.uuid4())

    raw_chunks = chunk_regulation_document(document)
    batch = []

    for chunk in raw_chunks:
        chunk["chunk_id"] = re.sub(r"[^a-zA-Z0-9_\-=:]", "-", chunk["chunk_id"])
        chunk["key_phrases"] = _extract_key_phrases(chunk["searchable_text"])
        chunk["content_sha256"] = hashlib.sha256(
            chunk.get("content", "").encode("utf-8")
        ).hexdigest()

        # Embedding is what makes the vector arm see the update. Without it the
        # chunk is keyword-searchable only.
        vector = generate_ollama_embedding(chunk["searchable_text"])
        if vector:
            chunk["embedding"] = vector
        else:
            log.warning("L7: embedding failed for %s — keyword-only chunk", chunk["chunk_id"])

        if action == "replacement" and target_id:
            chunk["superseded_by"] = document["document_id"]

        batch.append(chunk)

    if batch:
        try:
            committed = vectors.upsert_chunks(batch)
            log.info("L7: committed %d chunks to CockroachDB", committed)
        except Exception as e:
            log.error("L7: failed to commit new chunks: %s", e)
