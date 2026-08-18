"""
L3: Hybrid Retrieval

Both retrieval arms now read the same CockroachDB table:

  vector arm   (was ChromaDB)        -> VECTOR INDEX over regulatory_chunks
  keyword arm  (was Azure AI Search) -> BM25 over the same rows

That collapse is the point of the migration, not a simplification. Previously
the two arms were separate systems that were updated separately, so there was a
window in which L3 could reason against a circular one arm had already
superseded. With one table, the chunk row, its embedding and the vector index
commit in a single transaction: the arms cannot disagree.

The dual-evaluation contract is unchanged — search_regulations() still returns
`chunks` and `nomic_chunks`, and legal_reasoning still runs the model over each
and takes the higher-confidence verdict.
"""
from __future__ import annotations

import json
import math
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from config import get_config

config = get_config()

DEFAULT_LOCAL_CORPUS_PATH = os.environ.get(
    "LOCAL_REGULATION_CORPUS_PATH",
    str(Path(__file__).with_name("regulation_corpus.json")),
)
DEFAULT_TOP_K = int(os.environ.get("L3_TOP_K", "5"))
DEFAULT_CHUNK_WORDS = int(os.environ.get("L3_CHUNK_WORDS", "400"))
DEFAULT_CHUNK_OVERLAP = int(os.environ.get("L3_CHUNK_OVERLAP", "100"))


def _tokenize(text: Any) -> List[str]:
    return re.findall(r"[a-z0-9]+", str(text or "").lower())


def _dedupe_preserve_order(items: Iterable[str]) -> List[str]:
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def build_search_query(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Converts the transaction/event context into a retrieval query object.

    Intentionally simple and auditable: `keywords` drives the BM25 arm,
    `query_text` is embedded for the vector arm.
    """
    channel = str(event.get("channel", "")).upper()
    amount = event.get("amount")
    amount_band = "small"
    try:
        numeric_amount = float(amount)
        if numeric_amount >= 1_000_000:
            amount_band = "very_large"
        elif numeric_amount >= 200_000:
            amount_band = "large"
        elif numeric_amount >= 50_000:
            amount_band = "medium"
    except (TypeError, ValueError):
        numeric_amount = None

    keywords = [
        channel.lower(),
        "rbi",
        "compliance",
        "transaction",
        "payment",
        amount_band,
    ]

    for key in ("sender_bank", "receiver_bank", "receiver_type", "purpose_code", "channel"):
        value = event.get(key)
        if value:
            keywords.extend(_tokenize(value))

    for key in ("scenario_tag", "funds_in_out_pattern", "l3_investigation_notes", "purpose_code_declared"):
        value = event.get(key)
        if value:
            keywords.extend(_tokenize(value))

    for key in ("t2_watchlist_hit", "t3_risk_label", "geo_country", "transaction_type"):
        value = event.get(key)
        if value:
            keywords.extend(_tokenize(value))

    # Add L2 triggers to keywords
    l2_triggers = event.get("l2_triggers_fired", [])
    if l2_triggers:
        for t in l2_triggers:
            keywords.extend(_tokenize(t))

    scenario = event.get("scenario_tag", "")
    if not scenario:
        if any("C5" in t for t in l2_triggers):
            scenario = "CROSS_BORDER_LRS Liberalised Remittance Scheme LRS limits"
        elif any("C1_high_value" in t for t in l2_triggers):
            scenario = "high value transaction enhanced due diligence monitoring"
        elif any("C1" in t for t in l2_triggers):
            scenario = "structuring smurfing transaction splitting"
        else:
            scenario = " ".join(l2_triggers).replace("_", " ")
    scenario = scenario.replace("_", " ")
    
    investigation = event.get("l3_investigation_notes", "")
    channel_str = event.get("channel", channel)
    txn_type = event.get("transaction_type", "").replace("_", " ")
    
    query_text = f"search_query: What are the RBI regulatory guidelines, reporting thresholds, and KYC requirements for suspected {scenario} via {channel_str} {txn_type}? Context: {investigation}"

    return {
        "channel": channel,
        "amount": numeric_amount,
        "amount_band": amount_band,
        "keywords": _dedupe_preserve_order(keywords),
        "query_text": query_text,
    }


def _window_words(text: str, chunk_words: int, overlap_words: int) -> List[str]:
    words = text.split()
    if len(words) <= chunk_words:
        return [text.strip()] if text.strip() else []

    chunks = []
    start = 0
    stride = max(chunk_words - overlap_words, 1)
    while start < len(words):
        window = words[start : start + chunk_words]
        if not window:
            break
        chunks.append(" ".join(window).strip())
        start += stride
    return chunks


def chunk_regulation_document(
    document: Dict[str, Any],
    chunk_words: int = DEFAULT_CHUNK_WORDS,
    overlap_words: int = DEFAULT_CHUNK_OVERLAP,
) -> List[Dict[str, Any]]:
    """
    Converts one regulation document into searchable chunks.

    Expected source shape:
    {
      "document_id": "...",
      "title": "...",
      "regulator": "RBI",
      "document_type": "master_direction",
      "effective_date": "2026-01-01",
      "url": "...",
      "tags": ["KYC", "AML", "UPI"],
      "sections": [
        {
          "section_id": "...",
          "heading": "...",
          "text": "...",
          "clauses": ["...", "..."]
        }
      ]
    }
    """
    document_id = document.get("document_id") or document.get("id") or document.get("doc_id")
    title = document.get("title", "")
    tags = document.get("tags") or []
    regulator = document.get("regulator", "RBI")
    document_type = document.get("document_type", "regulation")
    effective_date = document.get("effective_date")
    url = document.get("url")

    chunked_rows: List[Dict[str, Any]] = []
    sections = document.get("sections") or []
    if not sections and document.get("text"):
        sections = [{"section_id": "full_text", "heading": title, "text": document["text"], "clauses": []}]

    for section in sections:
        section_id = section.get("section_id") or section.get("id") or "section"
        heading = section.get("heading", "")
        clauses = section.get("clauses") or []

        candidate_texts: List[str] = []
        if section.get("text"):
            candidate_texts.extend(_window_words(str(section["text"]), chunk_words, overlap_words))
        for clause in clauses:
            if clause:
                candidate_texts.extend(_window_words(str(clause), chunk_words, overlap_words))

        for index, chunk_text in enumerate(candidate_texts, start=1):
            searchable_text = " ".join(part for part in [title, heading, chunk_text, " ".join(tags)] if part)
            chunked_rows.append(
                {
                    "chunk_id": f"{document_id}:{section_id}:{index}",
                    "document_id": document_id,
                    "title": title,
                    "regulator": regulator,
                    "document_type": document_type,
                    "effective_date": effective_date,
                    "url": url,
                    "tags": tags,
                    "section_id": section_id,
                    "section_heading": heading,
                    "content": chunk_text,
                    "searchable_text": searchable_text,
                }
            )

    return chunked_rows


def load_regulation_corpus(
    corpus_path: Optional[str] = None,
    chunk_words: int = DEFAULT_CHUNK_WORDS,
    overlap_words: int = DEFAULT_CHUNK_OVERLAP,
) -> List[Dict[str, Any]]:
    path = Path(corpus_path or DEFAULT_LOCAL_CORPUS_PATH)
    if not path.exists():
        raise FileNotFoundError(
            f"Regulation corpus not found at {path}. Add your JSON corpus or set LOCAL_REGULATION_CORPUS_PATH."
        )

    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        documents = payload.get("documents") or payload.get("regulations") or [payload]
    elif isinstance(payload, list):
        documents = payload
    else:
        raise ValueError("Regulation corpus JSON must be a list or an object with a 'documents' field.")

    chunks: List[Dict[str, Any]] = []
    for document in documents:
        chunks.extend(chunk_regulation_document(document, chunk_words=chunk_words, overlap_words=overlap_words))
    return chunks


def _compute_retrieval_match(top_chunks: Sequence[Dict[str, Any]]) -> float:
    """
    Produces the retrieval-match sub-score expected by L3.

    Current interpretation:
    - high if the top result is strong and the top few results are consistent
    - low if results are weak or sparse
    """
    if not top_chunks:
        return 0.0

    top_score = float(top_chunks[0].get("retrieval_score", 0.0))
    avg_top = sum(float(row.get("retrieval_score", 0.0)) for row in top_chunks[:3]) / min(len(top_chunks), 3)

    # Both arms are normalised to roughly 0-1 before they reach here; the /2.0
    # is kept from the Azure-era formula so sub-scores stay comparable with the
    # pre-migration runs used to calibrate the confidence bands.
    match_score = min(1.0, ((0.65 * top_score) + (0.35 * avg_top)) / 2.0)
    return round(match_score, 4)


def search_regulations(
    event: Dict[str, Any],
    corpus_path: Optional[str] = None,
    top_k: int = DEFAULT_TOP_K,
) -> Dict[str, Any]:
    """
    Dual retrieval against CockroachDB: BM25 keyword arm plus native vector arm.
    Returns both sets of chunks for dual model evaluation, same as before.
    """
    query = build_search_query(event)

    from aws import db, vectors
    from L3_regulation_interpreter.llm_client import generate_ollama_embedding

    if not db.available():
        db.warn_unavailable("L3 retrieval")
        return {"query": query, "retrieval_match": 0.0,
                "chunks": [], "nomic_chunks": [], "backend": "unavailable"}

    keyword_chunks: List[Dict[str, Any]] = []
    vector_chunks: List[Dict[str, Any]] = []

    print("L3: Searching regulations in CockroachDB (BM25 + vector index)...")

    # 1. Keyword arm — BM25 over regulatory_chunks.searchable_text
    try:
        keyword_chunks = vectors.keyword_search(query.get("keywords", []), top_k=top_k)
    except Exception as exc:
        print(f"L3: CockroachDB keyword search failed: {exc}")

    # 2. Vector arm — native VECTOR INDEX, cosine distance
    try:
        query_vector = generate_ollama_embedding(query.get("query_text", ""))
        if query_vector:
            vector_chunks = vectors.vector_search(query_vector, top_k=top_k)
        else:
            print("L3: Embedding returned empty; vector arm skipped.")
    except Exception as exc:
        print(f"L3: CockroachDB vector search failed: {exc}")

    retrieval_match = _compute_retrieval_match(keyword_chunks if keyword_chunks else vector_chunks)

    return {
        "query": query,
        "retrieval_match": retrieval_match,
        "chunks": keyword_chunks,
        "nomic_chunks": vector_chunks,
        "backend": "cockroachdb_dual_retrieval",
    }
