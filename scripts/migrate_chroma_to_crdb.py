"""
Migrate the existing ChromaDB corpus into CockroachDB.

Reads the 768-dim nomic vectors straight out of chroma_db/ and writes them to
regulatory_chunks. Nothing is re-embedded, so retrieval results after the
migration are identical to before it — which is the point: if L3 citations
change, that is a bug in the port, not a modelling difference.

Usage:
    python scripts/migrate_chroma_to_crdb.py
    python scripts/migrate_chroma_to_crdb.py --dry-run
    python scripts/migrate_chroma_to_crdb.py --chroma-path chroma_db --batch 200
"""
import argparse
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from aws import db, vectors  # noqa: E402

COLLECTION = "compliance_regulations"


def load_from_chroma(chroma_path: str):
    import chromadb

    client = chromadb.PersistentClient(path=chroma_path)
    collection = client.get_collection(COLLECTION)

    total = collection.count()
    print(f"Chroma collection '{COLLECTION}' holds {total} chunks")

    got = collection.get(include=["embeddings", "documents", "metadatas"])
    ids = got["ids"]
    embeddings = got.get("embeddings")
    embeddings = [] if embeddings is None else embeddings
    documents = got.get("documents") or []
    metadatas = got.get("metadatas") or []

    rows = []
    for i, chunk_id in enumerate(ids):
        meta = metadatas[i] if i < len(metadatas) else {}
        content = documents[i] if i < len(documents) else ""
        embedding = embeddings[i] if i < len(embeddings) else None

        rows.append({
            "chunk_id": chunk_id,
            "document_id": meta.get("document_id", ""),
            "title": meta.get("title", ""),
            "regulator": meta.get("regulator", "RBI"),
            "document_type": meta.get("document_type", ""),
            "effective_date": meta.get("effective_date", ""),
            "url": meta.get("url", ""),
            "tags": [],
            "section_id": meta.get("section_id", ""),
            "section_heading": meta.get("section_heading", ""),
            "content": content,
            "searchable_text": meta.get("searchable_text") or content,
            "key_phrases": [],
            "content_sha256": hashlib.sha256((content or "").encode("utf-8")).hexdigest(),
            "embedding": list(embedding) if embedding is not None else None,
        })

    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chroma-path", default="chroma_db")
    ap.add_argument("--batch", type=int, default=200)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.dry_run and not db.available():
        print("CRDB_DSN is not set (or psycopg is missing). Aborting.")
        sys.exit(1)

    rows = load_from_chroma(args.chroma_path)
    if not rows:
        print("Nothing to migrate.")
        return

    dims = {len(r["embedding"]) for r in rows if r["embedding"]}
    print(f"Loaded {len(rows)} chunks, embedding dimensions present: {dims or 'none'}")
    if dims and dims != {768}:
        print(f"WARNING: expected 768 dims to match VECTOR(768) in schema.sql, found {dims}")

    missing = sum(1 for r in rows if not r["embedding"])
    if missing:
        print(f"WARNING: {missing} chunks have no embedding; they will be keyword-only")

    if args.dry_run:
        sample = rows[0]
        print("\nDry run. First chunk:")
        for k in ("chunk_id", "document_id", "title", "section_heading"):
            print(f"  {k}: {sample[k]!r}")
        print(f"  content: {sample['content'][:120]!r}...")
        return

    total = 0
    for i in range(0, len(rows), args.batch):
        batch = rows[i:i + args.batch]
        total += vectors.upsert_chunks(batch)
        print(f"  committed {total}/{len(rows)}")

    print(f"\nMigration complete. regulatory_chunks holds {vectors.chunk_count()} chunks.")
    print("Next: run the same L3 query before and after and diff the citations.")


if __name__ == "__main__":
    main()
