"""
Seed CockroachDB from the bundled CSVs and JSON fixtures.

    python scripts/seed_crdb.py

Loads transactions, account_details, case_history and watchlist, plus the C1
baseline fixture and any existing local case memory. Idempotent — re-running
upserts rather than duplicating.
"""
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from aws import db  # noqa: E402

ROOT = Path(__file__).parent.parent
DATA = ROOT / "L2_transaction_monitor" / "data"
BASELINE = ROOT / "L2_transaction_monitor" / "detectors" / "c1_velocity_and_structuring" / "baseline_fixture.json"
CASE_MEMORY = ROOT / "data" / "case_memory.json"

BATCH = 1000


def _rows(path: Path):
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            yield {k: (v.strip() if isinstance(v, str) else v) for k, v in r.items()}


def _upsert(table: str, cols: list, rows: list) -> int:
    if not rows:
        return 0
    placeholders = ", ".join(["%s"] * len(cols))
    sql = f"UPSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
    total = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        db.executemany(sql, [[r.get(c) or None for c in cols] for r in chunk])
        total += len(chunk)
        print(f"  {table}: {total}/{len(rows)}")
    return total


def seed_transactions():
    path = DATA / "transactions.csv"
    rows = list(_rows(path))
    cols = list(rows[0].keys())
    print(f"transactions.csv -> {len(rows)} rows")
    return _upsert("transactions", cols, rows)


def seed_accounts():
    path = DATA / "account_details.csv"
    rows = list(_rows(path))
    cols = list(rows[0].keys())
    print(f"account_details.csv -> {len(rows)} rows")
    return _upsert("account_details", cols, rows)


def seed_history():
    path = DATA / "case_history.csv"
    rows = list(_rows(path))
    cols = list(rows[0].keys())
    print(f"case_history.csv -> {len(rows)} rows")
    # case_history has a generated leg_id primary key, so plain INSERT is correct.
    placeholders = ", ".join(["%s"] * len(cols))
    sql = f"INSERT INTO case_history ({', '.join(cols)}) VALUES ({placeholders})"
    existing = db.one("SELECT count(*) AS n FROM case_history")
    if existing and int(existing["n"]) > 0:
        print("  case_history already populated — skipping (truncate first to reload)")
        return 0
    total = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        db.executemany(sql, [[r.get(c) or None for c in cols] for r in chunk])
        total += len(chunk)
        print(f"  case_history: {total}/{len(rows)}")
    return total


def seed_watchlist():
    path = DATA / "watchlist.csv"
    rows = list(_rows(path))
    cols = list(rows[0].keys())
    print(f"watchlist.csv -> {len(rows)} rows")
    return _upsert("watchlist", cols, rows)


def seed_baselines():
    if not BASELINE.exists():
        print("baseline_fixture.json not found — skipping")
        return 0
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    rows = [(acc, json.dumps(baseline)) for acc, baseline in data.items()]
    print(f"baseline_fixture.json -> {len(rows)} accounts")
    db.executemany(
        "UPSERT INTO account_baselines (account_id, baseline) VALUES (%s, %s)", rows
    )
    return len(rows)


def seed_case_memory():
    if not CASE_MEMORY.exists():
        print("data/case_memory.json not found — skipping (fresh case memory)")
        return 0
    cases = json.loads(CASE_MEMORY.read_text(encoding="utf-8"))
    rows = [
        (
            c.get("case_id"), c.get("tx_id"), list(c.get("feature_set") or []),
            c.get("regulation_version_hash"), c.get("final_status"),
            c.get("confidence"), c.get("str_pdf_url"),
        )
        for c in cases if c.get("case_id")
    ]
    print(f"case_memory.json -> {len(rows)} cases")
    db.executemany(
        """UPSERT INTO case_memory
             (case_id, tx_id, feature_set, regulation_version_hash,
              final_status, confidence, str_pdf_url)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        rows,
    )
    return len(rows)


def main():
    if not db.available():
        print("CRDB_DSN is not set (or psycopg is missing). Aborting.")
        sys.exit(1)

    print("Seeding CockroachDB from bundled datasets...\n")
    seed_transactions()
    seed_accounts()
    seed_history()
    seed_watchlist()
    seed_baselines()
    seed_case_memory()

    print("\nCounts:")
    for t in ("transactions", "account_details", "case_history", "watchlist",
              "account_baselines", "case_memory", "regulatory_chunks"):
        try:
            n = db.one(f"SELECT count(*) AS n FROM {t}")["n"]
            print(f"  {t:22} {n}")
        except Exception as e:
            print(f"  {t:22} error: {e}")

    print("\nNext: python scripts/seed_regulatory_chunks.py")


if __name__ == "__main__":
    main()
