"""
Regression gate for the port.

Runs L2 over the same transactions twice — once against the CSVs, once against
CockroachDB — and diffs the scores and triggers. If anything moves, the port
broke data loading, not detection logic, and you want to know that before L3
is in the picture.

    python scripts/verify_parity.py --limit 200
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


async def run_l2(transactions, dl):
    from L2_transaction_monitor.orchestrator import monitor
    out = {}
    for tx in transactions:
        try:
            result = await monitor(tx, dl)
            out[tx["tx_id"]] = {
                "suspicion_score": result.get("suspicion_score"),
                "triggers": sorted(result.get("triggers") or []),
                "flag": result.get("flag"),
            }
        except Exception as exc:
            out[tx["tx_id"]] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


def build_data_layer():
    from L2_transaction_monitor.data_layer import DataLayer
    return DataLayer()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--out", default="parity_report.json")
    args = ap.parse_args()

    saved_dsn = os.environ.get("CRDB_DSN", "")
    if not saved_dsn:
        print("CRDB_DSN is not set — nothing to compare against. Aborting.")
        sys.exit(1)

    # --- Pass 1: CSV mode ---
    os.environ["CRDB_DSN"] = ""
    for mod in [m for m in list(sys.modules) if m.startswith(("aws", "L2_"))]:
        del sys.modules[mod]
    dl_csv = build_data_layer()
    txs = dl_csv.transactions[:args.limit]
    print(f"Pass 1: CSV mode over {len(txs)} transactions")
    csv_results = asyncio.run(run_l2(txs, dl_csv))

    # --- Pass 2: CockroachDB mode ---
    os.environ["CRDB_DSN"] = saved_dsn
    for mod in [m for m in list(sys.modules) if m.startswith(("aws", "L2_"))]:
        del sys.modules[mod]
    dl_crdb = build_data_layer()
    txs_crdb = {t["tx_id"]: t for t in dl_crdb.transactions}
    ordered = [txs_crdb[t["tx_id"]] for t in txs if t["tx_id"] in txs_crdb]
    print(f"Pass 2: CockroachDB mode over {len(ordered)} transactions")
    crdb_results = asyncio.run(run_l2(ordered, dl_crdb))

    # --- Diff ---
    diffs = []
    for tx_id, csv_r in csv_results.items():
        crdb_r = crdb_results.get(tx_id)
        if crdb_r is None:
            diffs.append({"tx_id": tx_id, "issue": "missing in CockroachDB"})
            continue
        if csv_r != crdb_r:
            diffs.append({"tx_id": tx_id, "csv": csv_r, "crdb": crdb_r})

    report = {
        "compared": len(csv_results),
        "identical": len(csv_results) - len(diffs),
        "differences": diffs,
    }
    Path(args.out).write_text(json.dumps(report, indent=2))

    print(f"\n{report['identical']}/{report['compared']} identical")
    if diffs:
        print(f"{len(diffs)} differences written to {args.out}")
        for d in diffs[:5]:
            print(f"  {d}")
        sys.exit(1)
    print("Parity confirmed. The port did not change detector output.")


if __name__ == "__main__":
    main()
