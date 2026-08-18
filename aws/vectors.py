"""L3 retrieval against CockroachDB. Replaces both ChromaDB and Azure AI Search.

The dual-retrieval shape is preserved exactly:

  vector arm   (was ChromaDB)        -> CRDB VECTOR INDEX, cosine distance
  keyword arm  (was Azure AI Search) -> BM25 over CRDB rows via rank_bm25

Both arms now read the same table, which is the point: the chunk row, its
embedding and the index commit in one transaction, so the two arms can never
disagree about what the current regulation is. Under the old split there was a
window where Chroma had a circular that Azure Search did not.
"""
import json
import logging
import re

from aws import db

log = logging.getLogger(__name__)

_bm25_cache = None   # (bm25_index, rows)


def _tokenize(text):
    return re.findall(r"[a-z0-9]+", str(text or "").lower())


# ---------------------------------------------------------------------------
# Vector arm
# ---------------------------------------------------------------------------

def vector_search(query_vector, top_k: int = 5, regulator: str | None = None) -> list:
    """Nearest chunks by cosine distance. Returns the same field shape the
    ChromaDB branch produced, so hybrid_retrieval's caller is unchanged."""
    v = db.vec(query_vector)
    where, params = "", []
    if regulator:
        where = "WHERE regulator = %s"
        params.append(regulator)

    sql = f"""
        SELECT chunk_id, document_id, title, content, section_id, section_heading,
               embedding <=> '{v}'::VECTOR AS distance
        FROM regulatory_chunks
        {where}
        ORDER BY embedding <=> '{v}'::VECTOR
        LIMIT %s
    """
    params.append(top_k)
    rows = db.query(sql, params)

    out = []
    for r in rows:
        # Cosine distance in [0, 2] -> similarity, matching the old Chroma maths.
        similarity = 1.0 - (float(r["distance"]) / 2.0)
        out.append({
            "chunk_id": r["chunk_id"],
            "document_id": r["document_id"] or "",
            "title": r["title"] or "",
            "content": r["content"],
            "section_id": r["section_id"] or "",
            "section_heading": r["section_heading"] or "",
            "retrieval_score": round(similarity, 4),
        })
    return out


# ---------------------------------------------------------------------------
# Keyword arm
# ---------------------------------------------------------------------------

def _load_bm25():
    """Build the BM25 index once per process from the CRDB corpus.

    Rebuilt on demand rather than cached forever, because L7 can change the
    corpus underneath us; call invalidate_keyword_index() after an L7 write.
    """
    global _bm25_cache
    if _bm25_cache is not None:
        return _bm25_cache

    from rank_bm25 import BM25Okapi

    rows = db.query(
        """SELECT chunk_id, document_id, title, content, section_id,
                  section_heading, searchable_text
           FROM regulatory_chunks"""
    )
    if not rows:
        _bm25_cache = (None, [])
        return _bm25_cache

    corpus = [_tokenize(r["searchable_text"] or r["content"]) for r in rows]
    _bm25_cache = (BM25Okapi(corpus), [dict(r) for r in rows])
    log.info("L3: BM25 keyword index built over %d chunks from CockroachDB", len(rows))
    return _bm25_cache


def invalidate_keyword_index() -> None:
    """Called by L7 after the corpus changes so the next query rebuilds."""
    global _bm25_cache
    _bm25_cache = None


def keyword_search(keywords, top_k: int = 5) -> list:
    """BM25 ranking over the CRDB corpus. Replaces the Azure AI Search arm."""
    bm25, rows = _load_bm25()
    if bm25 is None:
        return []

    tokens = []
    for kw in keywords or []:
        tokens.extend(_tokenize(kw))
    if not tokens:
        return []

    scores = bm25.get_scores(tokens)
    ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]

    top = scores[ranked[0]] if ranked else 0.0
    out = []
    for i in ranked:
        if scores[i] <= 0:
            continue
        r = rows[i]
        # Normalise to 0-1 against the best hit, matching the old score handling.
        normalised = (scores[i] / top) if top > 0 else 0.0
        out.append({
            "chunk_id": r["chunk_id"],
            "document_id": r["document_id"] or "",
            "title": r["title"] or "",
            "content": r["content"],
            "section_id": r["section_id"] or "",
            "section_heading": r["section_heading"] or "",
            "retrieval_score": round(normalised, 4),
        })
    return out


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

@db.retry()
def upsert_chunks(chunks) -> int:
    """Chunk row + embedding + vector index commit atomically.

    This is what removes the staleness window. Previously the Chroma write and
    the Azure Search write were two independent systems, so a crash between
    them left L3 reasoning against a superseded circular.
    """
    if not chunks:
        return 0

    cols = ("chunk_id, document_id, title, regulator, document_type, "
            "effective_date, url, tags, section_id, section_heading, "
            "content, searchable_text, key_phrases, content_sha256, superseded_by")

    with db.get_pool().connection() as conn, conn.transaction():
        for c in chunks:
            emb = c.get("embedding")
            params = [
                c["chunk_id"], c.get("document_id", ""), c.get("title", ""),
                c.get("regulator", "RBI"), c.get("document_type", ""),
                str(c.get("effective_date") or ""), c.get("url", ""),
                list(c.get("tags") or []), c.get("section_id", ""),
                c.get("section_heading", ""), c.get("content", ""),
                c.get("searchable_text", c.get("content", "")),
                list(c.get("key_phrases") or []), c.get("content_sha256"),
                c.get("superseded_by"),
            ]
            placeholders = ", ".join(["%s"] * len(params))

            # The embedding is interpolated as a literal rather than bound as a
            # parameter: CockroachDB will not infer VECTOR for an untyped
            # placeholder. db.vec() emits digits and commas only, so there is
            # nothing injectable in it.
            if emb:
                sql = (f"UPSERT INTO regulatory_chunks ({cols}, embedding) "
                       f"VALUES ({placeholders}, '{db.vec(emb)}'::VECTOR)")
            else:
                sql = (f"UPSERT INTO regulatory_chunks ({cols}) "
                       f"VALUES ({placeholders})")

            conn.execute(sql, params)
    invalidate_keyword_index()
    return len(chunks)


@db.retry()
def delete_document(document_id: str) -> int:
    """L7's RAG-hunter. Removes every chunk of a superseded circular."""
    with db.get_pool().connection() as conn:
        cur = conn.execute(
            "DELETE FROM regulatory_chunks WHERE document_id = %s", (document_id,)
        )
        deleted = cur.rowcount
    invalidate_keyword_index()
    return deleted


def find_document_id(search_term: str) -> str | None:
    """Locate a circular by id or title fragment before wiping it."""
    r = db.one(
        """SELECT document_id FROM regulatory_chunks
           WHERE document_id ILIKE %s OR title ILIKE %s LIMIT 1""",
        (f"%{search_term}%", f"%{search_term}%"),
    )
    return r["document_id"] if r else None


def document_exists(title: str) -> bool:
    return db.one(
        "SELECT 1 FROM regulatory_chunks WHERE title ILIKE %s LIMIT 1", (f"%{title}%",)
    ) is not None


def chunk_count() -> int:
    r = db.one("SELECT count(*) AS n FROM regulatory_chunks")
    return int(r["n"]) if r else 0
