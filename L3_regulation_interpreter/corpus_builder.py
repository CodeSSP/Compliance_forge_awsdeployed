"""
L3 corpus builder

Builds a local regulation corpus JSON from RBI / NPCI / AML PDFs.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Sequence

from pypdf import PdfReader


USE_CASE_TAG_RULES = {
    "smurfing": ["threshold", "cash", "structuring", "reporting", "suspicious transaction"],
    "mule_accounts": ["mule", "beneficiary", "layering", "high volume", "suspicious transaction"],
    "inconsistent_geographic_activity": ["cross border", "country", "location", "ip", "international"],
    "ghost_accounts": ["dormant", "inactive", "re-activation", "large value", "monitoring"],
    "kyc_aml": ["kyc", "client due diligence", "record", "monitoring", "reporting entity"],
}


def _extract_text_from_pdf(pdf_path: Path) -> List[str]:
    reader = PdfReader(str(pdf_path))
    pages: List[str] = []
    for page in reader.pages:
        text = page.extract_text() or ""
        text = text.replace("\x00", " ")
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        pages.append(text.strip())
    return pages


def _guess_regulator(filename: str) -> str:
    upper = filename.upper()
    if "UPI" in upper or "NPCI" in upper or "PCOMP" in upper:
        return "NPCI"
    if "MONEY LAUNDERING" in upper or "PMLA" in upper:
        return "PMLA/RBI"
    return "RBI"


def _guess_document_type(filename: str) -> str:
    upper = filename.upper()
    if "ACT" in upper:
        return "act"
    if "RULE" in upper:
        return "rule"
    if "ADDENDUM" in upper:
        return "addendum"
    if "GUIDELINE" in upper:
        return "guideline"
    if "CIRCULAR" in upper or "OC-NO" in upper or "PORTAL" in upper:
        return "circular"
    return "regulation"


def _infer_tags(text: str, filename: str) -> List[str]:
    combined = f"{filename} {text}".lower()
    tags = []

    if "upi" in combined:
        tags.append("UPI")
    if "neft" in combined:
        tags.append("NEFT")
    if "rtgs" in combined:
        tags.append("RTGS")
    if "kyc" in combined:
        tags.append("KYC")
    if "money-laundering" in combined or "money laundering" in combined or "aml" in combined:
        tags.append("AML")
    if "purpose code" in combined:
        tags.append("purpose_code")
    if "cross border" in combined or "country code" in combined or "global acceptance" in combined:
        tags.append("cross_border")
    if "gift card" in combined or "voucher" in combined or "mcc" in combined:
        tags.append("merchant_classification")
    if "record" in combined or "maintenance of records" in combined:
        tags.append("recordkeeping")
    if "suspicious transaction" in combined or "reporting entity" in combined:
        tags.append("reporting")

    for use_case, trigger_terms in USE_CASE_TAG_RULES.items():
        if any(term in combined for term in trigger_terms):
            tags.append(use_case)

    return sorted(set(tags))


def _split_into_sections(pages: Sequence[str]) -> List[Dict[str, Any]]:
    full_text = "\n".join(pages)
    lines = full_text.split("\n")
    
    sections = []
    current_heading = "Introduction"
    current_lines = []
    section_counter = 1
    
    # Matches: "12. ", "4.1 ", "CHAPTER II", "PART B", "Section 5"
    heading_pattern = re.compile(r"^(?:\d{1,3}\.|\d{1,3}\.\d{1,2}\.|CHAPTER\s+[IVXLCDM\d]+|PART\s+[A-Z\d]+|Section\s+\d+)\s+(.*)", re.IGNORECASE)
    
    for line in lines:
        line_stripped = line.strip()
        if not line_stripped:
            current_lines.append("")
            continue
            
        match = heading_pattern.match(line_stripped)
        if match:
            # Save previous section
            if current_lines and any(l.strip() for l in current_lines):
                sections.append({
                    "section_id": f"section-{section_counter}",
                    "heading": current_heading[:140],
                    "text": "\n".join(current_lines).strip(),
                    "clauses": [],
                })
                section_counter += 1
            
            current_heading = line_stripped
            current_lines = [line_stripped]
        else:
            current_lines.append(line_stripped)
            
    # Save last section
    if current_lines and any(l.strip() for l in current_lines):
        sections.append({
            "section_id": f"section-{section_counter}",
            "heading": current_heading[:140],
            "text": "\n".join(current_lines).strip(),
            "clauses": [],
        })
        
    return sections


def pdf_to_document(pdf_path: Path) -> Dict[str, Any]:
    pages = _extract_text_from_pdf(pdf_path)
    combined_text = "\n\n".join(pages[:5])
    filename = pdf_path.name

    return {
        "document_id": pdf_path.stem,
        "title": pdf_path.stem.replace("_", " "),
        "regulator": _guess_regulator(filename),
        "document_type": _guess_document_type(filename),
        "effective_date": None,
        "url": None,
        "source_file": str(pdf_path),
        "tags": _infer_tags(combined_text, filename),
        "sections": _split_into_sections(pages),
    }


def build_corpus(pdf_paths: Sequence[str]) -> Dict[str, Any]:
    documents = [pdf_to_document(Path(pdf_path)) for pdf_path in pdf_paths]
    return {
        "documents": documents,
        "metadata": {
            "document_count": len(documents),
            "builder": "corpus_builder.py",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a local regulation corpus JSON from PDFs.")
    parser.add_argument("--output", required=True, help="Output JSON path")
    parser.add_argument("pdfs", nargs="+", help="PDF paths")
    args = parser.parse_args()

    corpus = build_corpus(args.pdfs)
    output_path = Path(args.output)
    output_path.write_text(json.dumps(corpus, indent=2), encoding="utf-8")
    print(f"Wrote corpus to {output_path}")


if __name__ == "__main__":
    main()
