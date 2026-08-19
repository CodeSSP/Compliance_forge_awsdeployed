"""
Seed regulatory_chunks from the bundled CockroachDB-native export.

Replaces migrate_chroma_to_crdb.py — CockroachDB is the system of record now,
so a fresh cluster no longer needs ChromaDB as an intermediate step.

Usage:
    python scripts/seed_regulatory_chunks.py
    python scripts/seed_regulatory_chunks.py --dry-run
    python scripts/seed_regulatory_chunks.py --file infra/regulatory_chunks_seed.jsonl.gz --batch 200
"""
import argparse
import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from aws import db, vectors  # noqa: E402


def load_from_file(path: str):
    rows = []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="infra/regulatory_chunks_seed.jsonl.gz")
    ap.add_argument("--batch", type=int, default=200)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.dry_run and not db.available():
        print("CRDB_DSN is not set (or psycopg is missing). Aborting.")
        sys.exit(1)

    rows = load_from_file(args.file)
    if not rows:
        print("Nothing to seed.")
        return

    dims = {len(r["embedding"]) for r in rows if r.get("embedding")}
    print(f"Loaded {len(rows)} chunks, embedding dimensions present: {dims or 'none'}")
    if dims and dims != {768}:
        print(f"WARNING: expected 768 dims to match VECTOR(768) in schema.sql, found {dims}")

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

    print(f"\nSeed complete. regulatory_chunks holds {vectors.chunk_count()} chunks.")


if __name__ == "__main__":
    main()
