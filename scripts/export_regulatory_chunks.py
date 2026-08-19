"""
Export regulatory_chunks from CockroachDB to a portable seed file.

Replaces chroma_db/ as the corpus bootstrap source now that CockroachDB is the
system of record — a fresh cluster no longer needs ChromaDB at all, migrated
or otherwise.

Usage:
    python scripts/export_regulatory_chunks.py
    python scripts/export_regulatory_chunks.py --out infra/regulatory_chunks_seed.jsonl.gz
"""
import argparse
import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from aws import db  # noqa: E402

COLS = [
    "chunk_id", "document_id", "title", "regulator", "document_type",
    "effective_date", "url", "tags", "section_id", "section_heading",
    "content", "searchable_text", "key_phrases", "content_sha256",
    "superseded_by", "embedding",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="infra/regulatory_chunks_seed.jsonl.gz")
    args = ap.parse_args()

    if not db.available():
        print("CRDB_DSN is not set (or psycopg is missing). Aborting.")
        sys.exit(1)

    sql = f"SELECT {', '.join(COLS)} FROM regulatory_chunks"
    rows = db.query(sql)
    print(f"Read {len(rows)} chunks from regulatory_chunks")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out_path, "wt", encoding="utf-8") as f:
        for r in rows:
            record = dict(zip(COLS, [r[c] for c in COLS]))
            emb = record["embedding"]
            if emb is not None:
                # CockroachDB's VECTOR type comes back over the wire as its text
                # literal ("[0.1,0.2,...]"), not a native sequence — psycopg has
                # no built-in adapter for it.
                record["embedding"] = [float(x) for x in emb.strip("[]").split(",")]
            f.write(json.dumps(record) + "\n")

    print(f"Wrote {out_path} ({out_path.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
