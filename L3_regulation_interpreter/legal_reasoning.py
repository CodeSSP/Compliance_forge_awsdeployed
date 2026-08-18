"""
L3: GPT-5.1 Legal Reasoning

Computes the 4 sub-scores and generates a citation trail.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Sequence

from .llm_client import chat_json, is_llm_configured


SYSTEM_PROMPT = """
You are the legal reasoning engine for an RBI/NPCI/PMLA compliance pipeline.

Your job is to reason over:
- transaction facts
- retrieved regulatory chunks
- suspicious transaction typologies

You must return ONLY valid JSON.
Be conservative, cite the retrieved material, and never invent a regulation that
is not present in the retrieved chunks.

CRITICAL INSTRUCTION FOR L2 TRIGGERS:
If the transaction has an 'l2_triggers_fired' flag (e.g., C1_velocity, structuring), you MUST mathematically assume that the backend already verified the aggregated sums. For example, if a rule states "monthly aggregate exceeds 10 lakhs", and the L2 velocity trigger fired, you must assume the 10 lakh threshold was successfully breached by the customer's history, even if the single transaction amount is smaller. Score the transaction highly (e.g., 0.80+) if the rule conceptually matches the trigger.

CRITICAL INSTRUCTION ON JARGON:
When writing the "explanation", DO NOT use internal pipeline jargon or abbreviations such as C1, C2, L1, L2, L3, or L4. The final reader of the report will not understand these terms. Instead of saying "L2 trigger C1_velocity fired", write "The transaction history triggered a high-velocity screening alert" or describe the actual behavior. Use clear, professional, plain language to explain the reasoning and the patterns observed.
""".strip()


USE_CASES = [
    {
        "name": "smurfing",
        "description": "Repeated smaller transactions within a short timeframe intended to avoid reporting thresholds.",
    },
    {
        "name": "mule_accounts",
        "description": "Recently opened accounts showing immediate, sustained, high-volume inflows and outflows suggestive of layering.",
    },
    {
        "name": "inconsistent_geographic_activity",
        "description": "Sudden cross-border or geographically inconsistent activity for a customer profile.",
    },
    {
        "name": "ghost_accounts",
        "description": "Dormant or long-idle accounts suddenly receiving or sending large-value funds.",
    },
]


def _serialize_chunks(regulation_chunks: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # Limit to top 2 chunks and truncate content to save tokens
    return [
        {
            "chunk_id": chunk.get("chunk_id"),
            "document_id": chunk.get("document_id"),
            "title": chunk.get("title"),
            "section_id": chunk.get("section_id"),
            "content": str(chunk.get("content", ""))[:3000],  # Increased to prevent cutting off rules
            "retrieval_score": chunk.get("retrieval_score"),
        }
        for chunk in regulation_chunks[:3]
    ]


def _build_reasoning_prompt(event: Dict[str, Any], regulation_chunks: Sequence[Dict[str, Any]]) -> str:
    return f"""
Analyze this transaction case against the provided regulations.

Transaction:
{json.dumps(event)}

Suspicious Typologies:
{json.dumps(USE_CASES)}

Retrieved Regulations (Top Matches):
{json.dumps(_serialize_chunks(regulation_chunks))}

Return ONLY JSON with this exact structure:
{{
  "retrieval_match": <0 to 1, how well retrieved chunks match case facts>,
  "rule_applicability": <0 to 1, how clearly retrieved rules apply>,
  "evidence_sufficiency": <0 to 1, transaction facts support defensible decision>,
  "precedent_confidence": <0 to 1, pattern resembles suspicious typologies>,
  "applicable_use_cases": ["..."],
  "applicable_rules": [
    {{
      "document_id": "...",
      "section_id": "...",
      "reason": "..."
    }}
  ],
  "citation_trail": [
    {{
      "chunk_id": "...",
      "excerpt": "short excerpt starting with the rule designation (e.g., Article 22.1) followed by the rule text",
      "why_it_matters": "..."
    }}
  ],
  "verdict": "clear" | "review" | "suspicious",
  "explanation": "...",
  "final_score": <0 to 1>
}}

Scoring guidance:
Final score MUST reflect the weighted legal confidence after reasoning. 
If the rules are weakly related or evidence is thin, lower the score.
""".strip()

def _enrich_citation_trail(citation_trail, all_chunks):
    """Add rule designation to each citation entry.
    citation_trail: list of dicts with at least "chunk_id" and "excerpt".
    all_chunks: list of chunk dicts (keyword + vector) containing metadata.
    Returns a new list with an added "rule_designation" field and ensures the excerpt starts with it.
    """
    # Build a mapping from chunk_id to chunk metadata for quick lookup
    chunk_map = {c.get("chunk_id"): c for c in all_chunks}
    enriched = []
    for entry in citation_trail:
        chunk_id = entry.get("chunk_id")
        chunk = chunk_map.get(chunk_id, {})
        # Prefer section_id, fallback to document_id, then title
        designation = chunk.get("section_id") or chunk.get("document_id") or chunk.get("title") or ""
        # Clean designation (strip whitespace)
        designation = str(designation).strip()
        # Ensure excerpt starts with designation
        excerpt = entry.get("excerpt", "")
        if designation and not excerpt.startswith(designation):
            excerpt = f"{designation} – {excerpt}" if designation else excerpt
        enriched_entry = {
            **entry,
            "rule_designation": designation,
            "excerpt": excerpt,
        }
        enriched.append(enriched_entry)
    return enriched


def generate_legal_analysis(event, retrieval):
    """
    Uses a large language model to analyze the transaction against the retrieved regulations.
    Performs Dual-Evaluation: runs the model independently on the CockroachDB keyword-arm
    chunks and the CockroachDB vector-arm chunks,
    and returns the verdict with the highest confidence score.
    """
    print("L3: Applying dual legal reasoning (CRDB keyword arm vs CRDB vector arm)...")

    keyword_chunks = retrieval.get("chunks", [])
    nomic_chunks = retrieval.get("nomic_chunks", [])

    try:
        # The existing try/except blocks for each backend remain unchanged.
        keyword_analysis = None
        nomic_analysis = None
        fallback_reason = "LLM not configured."

        # chat_json() already falls back to local Ollama when GEMINI_API_KEY
        # is unset, so always attempt the call rather than gating on
        # is_llm_configured() (which only checks for the Gemini key).
        try:
            if keyword_chunks:
                keyword_analysis = chat_json(
                    system_prompt=SYSTEM_PROMPT,
                    user_prompt=_build_reasoning_prompt(event, keyword_chunks),
                )
                keyword_analysis["backend_used"] = "crdb_keyword"
        except Exception as exc:
            print(f"L3: keyword-arm evaluation failed: {exc}")

        try:
            if nomic_chunks:
                nomic_analysis = chat_json(
                    system_prompt=SYSTEM_PROMPT,
                    user_prompt=_build_reasoning_prompt(event, nomic_chunks),
                )
                nomic_analysis["backend_used"] = "crdb_vector"
        except Exception as exc:
            print(f"L3: vector-arm evaluation failed: {exc}")

        # Determine the winner (highest final_score)
        winner = None
        if keyword_analysis and nomic_analysis:
            if float(nomic_analysis.get("final_score", 0)) > float(keyword_analysis.get("final_score", 0)):
                print(f"L3 Dual-Eval: vector arm scored higher confidence ({nomic_analysis.get('final_score')} vs {keyword_analysis.get('final_score')}). Using vector arm.")
                winner = nomic_analysis
            else:
                print("L3 Dual-Eval: keyword arm scored higher or equal confidence. Using keyword arm.")
                winner = keyword_analysis
        elif keyword_analysis:
            winner = keyword_analysis
        elif nomic_analysis:
            winner = nomic_analysis

        if winner:
            # Enrich citation_trail if present as a list
            if isinstance(winner.get("citation_trail"), list):
                all_chunks = keyword_chunks + nomic_chunks
                winner["citation_trail"] = _enrich_citation_trail(winner.get("citation_trail", []), all_chunks)
            return winner

        # No successful analysis – fallback
        print("L3: Fallback triggered – no LLM analysis succeeded.")
        return {
            "retrieval_match": 0.0,
            "rule_applicability": 0.0,
            "evidence_sufficiency": 0.0,
            "precedent_confidence": 0.0,
            "citation_trail": f"Fallback: {fallback_reason}",
            "final_score": 0.0,
            "verdict": "review",
        }
    except Exception as outer_exc:
        # Catch any unexpected error and return a safe default
        print(f"L3: Unexpected error during legal analysis: {outer_exc}")
        return {
            "retrieval_match": 0.0,
            "rule_applicability": 0.0,
            "evidence_sufficiency": 0.0,
            "precedent_confidence": 0.0,
            "citation_trail": f"Unexpected error: {outer_exc}",
            "final_score": 0.0,
            "verdict": "error",
        }
