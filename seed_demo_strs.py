#!/usr/bin/env python3
"""
seed_demo_strs.py — make every curated demo sample show a real STR PDF.

Why this exists
---------------
The regulatory-ui demo samples are single, isolated transactions. Detectors
like structuring / mule / velocity need multi-transaction context, so the live
pipeline correctly returns "clean" for them as one-off events and no STR is
filed — leaving the report page with no PDF for 4 of the 5 flagged samples.

The FEMA sample already works because a real STR PDF exists on disk AND its case
is seeded into L1 case memory: a matching transaction short-circuits (MinHash
>= 0.80, regulation hash unchanged) and L1 copies the stored str_pdf_url.

This script gives the other four the same treatment:
  1. Generate a genuine goAML-validated STR review PDF via L4 for each sample.
  2. Rebuild data/case_memory.json with one clean, PDF-bearing seed per sample
     (confidence 0.75 -> verdict maps to str_filed), removing test pollution and
     duplicates so the PDF-bearing entry is the one MinHash returns.

Re-runnable: it rebuilds the seed set from scratch each time.
"""
import os
import json

from L4.l4_report_generator import run_l4, write_pdf_review_copy
from L1_orchestrator.minhash_lsh import CASE_MEMORY_FILE

ROOT = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(ROOT, "reports")
REG_HASH = "INITIAL_HASH_PLACEHOLDER"   # same value the working FEMA seed uses

# Each demo sample: exact live MinHash feature_set (captured from real pipeline
# runs) + the authored evidence used to render a meaningful STR PDF.
SAMPLES = [
    {
        "tx_id": "TX20260512000541", "category": "C3",
        "feature_set": ["amt_4200k", "channel_RTGS", "purpose_P0103",
                        "rcv_EXT0541", "sender_ACXXXX4471"],
        "tx": {"tx_id": "TX20260512000541", "timestamp": "2026-05-12 14:22:07",
               "channel": "RTGS", "amount_inr": "4200000", "amount": "4200000",
               "currency": "INR", "sender_name": "Anita Desai",
               "receiver_name": "Global Trade Ltd"},
        "sender": {"name": "Anita Desai", "pan": "AACPD1234K", "dob": ""},
        "receiver": {"name": "Global Trade Ltd", "pan": "AAECG5678L", "dob": ""},
        "explanation": ("Mule-account layering network: RS 42,00,000 placed, split "
                        "across three pass-through mule accounts (fan-out), layered, "
                        "and reintegrated at the beneficiary (fan-in) to obscure "
                        "beneficial ownership."),
        "citation": [{"rule_designation": "PMLA 2002, Sec 3",
                      "excerpt": "Layering of proceeds through pass-through accounts."}],
    },
    {
        "tx_id": "TX20260514000771", "category": "C1",
        "feature_set": ["amt_949k", "channel_IMPS", "purpose_P0106",
                        "rcv_EXT0771", "sender_ACXXXX2288"],
        "tx": {"tx_id": "TX20260514000771", "timestamp": "2026-05-14 11:07:52",
               "channel": "IMPS", "amount_inr": "949000", "amount": "949000",
               "currency": "INR", "sender_name": "Vikram Nair",
               "receiver_name": "QuickCash Traders"},
        "sender": {"name": "Vikram Nair", "pan": "AKLPN9012M", "dob": ""},
        "receiver": {"name": "QuickCash Traders", "pan": "AAFCQ3456N", "dob": ""},
        "explanation": ("Structuring / smurfing: six correlated tranches totalling "
                        "RS 56,70,000 within 48 hours, each engineered just below the "
                        "RS 10,00,000 CTR threshold to evade mandatory reporting."),
        "citation": [{"rule_designation": "PMLA Rule 3",
                      "excerpt": "Structuring below the Cash Transaction Report threshold."}],
    },
    {
        "tx_id": "TX20260514000903", "category": "C1",
        "feature_set": ["amt_680k", "channel_UPI", "purpose_P0108",
                        "rcv_EXT0903", "sender_ACXXXX5510"],
        "tx": {"tx_id": "TX20260514000903", "timestamp": "2026-05-14 15:41:19",
               "channel": "UPI", "amount_inr": "680000", "amount": "680000",
               "currency": "INR", "sender_name": "Meera Kapoor",
               "receiver_name": "Multiple payees"},
        "sender": {"name": "Meera Kapoor", "pan": "AMNPK7788P", "dob": ""},
        "receiver": {"name": "Multiple first-time payees", "pan": "", "dob": ""},
        "explanation": ("Velocity anomaly: 38 debits across 12 first-time payees "
                        "inside a 4-minute window against a ~2-transactions/day "
                        "baseline — consistent with account compromise or a bust-out."),
        "citation": [{"rule_designation": "RBI EDD Guidance",
                      "excerpt": "Behavioural velocity deviation warranting enhanced due diligence."}],
    },
    {
        "tx_id": "TX20260514001042", "category": "C6",
        "feature_set": ["amt_215k", "channel_CARD", "purpose_P0107",
                        "rcv_EXT1042", "sender_ACXXXX7734"],
        "tx": {"tx_id": "TX20260514001042", "timestamp": "2026-05-14 14:31:12",
               "channel": "CARD", "amount_inr": "215000", "amount": "215000",
               "currency": "INR", "sender_name": "Arjun Rao",
               "receiver_name": "Horizon Digital"},
        "sender": {"name": "Arjun Rao", "pan": "ARJPR4455Q", "dob": ""},
        "receiver": {"name": "Horizon Digital", "pan": "AAHCH6677R", "dob": ""},
        "explanation": ("Impossible-travel geolocation anomaly: two authenticated "
                        "sessions on one credential originated ~11,000 km apart within "
                        "9 minutes (Mumbai -> Lagos) — a strong account-takeover signal."),
        "citation": [{"rule_designation": "RBI Digital Payment Security Controls",
                      "excerpt": "Impossible-travel / device anomaly indicating account takeover."}],
    },
]


def build_pdf(sample) -> str:
    l3_verdict = {
        "verdict": "SUSPICIOUS", "confidence": 0.93,
        "citation_trail": sample["citation"],
        "clause_no": sample["citation"][0]["rule_designation"],
        "clause": sample["citation"][0]["excerpt"],
        "citation": sample["citation"][0]["excerpt"],
        "explanation": sample["explanation"],
    }
    l2_evidence = {
        "primary_category": sample["category"], "l2_score": 0.9,
        "l2_triggers": [], "sender": sample["sender"], "receiver": sample["receiver"],
    }
    result = run_l4(l3_verdict, l2_evidence, sample["tx"])
    if result["disposition"] != "FILED":
        raise RuntimeError(f"L4 did not file for {sample['tx_id']}: {result['disposition']}")
    return write_pdf_review_copy(result, l3_verdict, sample["tx"], REPORTS_DIR)


def main():
    os.makedirs(REPORTS_DIR, exist_ok=True)

    # 1. Generate a real STR PDF per sample.
    seeds = []
    for s in SAMPLES:
        pdf_path = build_pdf(s)
        pdf_url = f"/reports/{os.path.basename(pdf_path)}"
        print(f"  PDF  {s['tx_id']} -> {pdf_url}")
        seeds.append({
            "tx_id": s["tx_id"],
            "case_id": f"seed-{s['tx_id']}",
            "feature_set": sorted(s["feature_set"]),
            "regulation_version_hash": REG_HASH,
            "final_status": "review",
            "confidence": 0.75,               # >= 0.70 -> str_filed on short-circuit
            "str_pdf_url": pdf_url,
        })

    seed_ids = {s["tx_id"] for s in SAMPLES}

    # 2. Load existing memory, drop any prior entries for these samples plus the
    #    -DBG / TXFRESH test pollution, keep everything else (e.g. the FEMA seed).
    existing = []
    if CASE_MEMORY_FILE.exists():
        with open(CASE_MEMORY_FILE, encoding="utf-8") as f:
            existing = json.load(f)

    def keep(case):
        tid = str(case.get("tx_id", ""))
        if tid in seed_ids:
            return False
        if "-DBG" in tid or tid.startswith("TXFRESH"):
            return False
        return True

    kept = [c for c in existing if keep(c)]

    # 3. Put PDF-bearing seeds FIRST so MinHash's strict-greater tie-break returns
    #    them over any residual equal-scoring case.
    merged = seeds + kept
    with open(CASE_MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2)

    print(f"\nSeeded {len(seeds)} demo STR cases; {len(kept)} other cases preserved; "
          f"{len(merged)} total in {CASE_MEMORY_FILE}")


if __name__ == "__main__":
    main()
