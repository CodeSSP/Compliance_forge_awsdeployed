"""
slm_reasoner.py
---------------
Phi-4-mini reasoning pass for T1 flagged transactions.

Runs ONLY when composite_score > SLM_REASONING_THRESHOLD.
Takes T1's evidence dict and returns:
  - reasoning_summary          : plain English explanation of why T1 fired
  - false_positive_likelihood  : LOW / MEDIUM / HIGH
  - key_factors                : list of what drove the decision
  - recommended_action         : PASS_TO_L2 / DEPRIORITISE / ESCALATE

SWITCHING BETWEEN MOCK AND REAL:
  Development / unit tests  → keep USE_MOCK = True  (instant, no Ollama needed)
  SLM quality evaluation    → set  USE_MOCK = False  (hits Ollama, ~5s per call)
  Production (AWS)          → set  USE_MOCK = False; MODEL_BACKEND picks the endpoint
"""

import json
from .thresholds import SLM_REASONING_THRESHOLD

# ---------------------------------------------------------------------------
# Toggle this to switch between mock and real Phi-4-mini
# ---------------------------------------------------------------------------
USE_MOCK = False   # ← set False when you want real Ollama inference


SYSTEM_PROMPT = """
You are an AML compliance reasoning assistant for an Indian fintech.
You will be given evidence from a transaction velocity check.
Your job is to assess whether the flagged pattern is genuinely suspicious
or likely a false positive.

Regulations in scope:
- PMLA Rule 3: transactions just below ₹50,000 may indicate structuring
- RBI AML circular: velocity spikes relative to account baseline are suspicious
- Purpose codes: P0014=Salary, P0008=Utility, P0013=Loan, P0099=General

Respond ONLY in this exact JSON format, no preamble:
{
  "reasoning_summary": "<one sentence explaining the pattern>",
  "false_positive_likelihood": "<LOW|MEDIUM|HIGH>",
  "key_factors": ["<factor1>", "<factor2>"],
  "recommended_action": "<PASS_TO_L2|DEPRIORITISE|ESCALATE>"
}
"""


async def run_slm_reasoning(
    tx_payload: dict,
    t1_evidence: dict,
    composite_score: float,
    flags: list[str],
) -> dict | None:
    """
    Calls Phi-4-mini with T1's evidence.
    Returns structured reasoning dict, or None if score is below threshold.
    """
    if composite_score < SLM_REASONING_THRESHOLD:
        return None

    if USE_MOCK:
        return _mock_reasoning(tx_payload, composite_score, flags)

    # Build a concise evidence summary for the prompt
    user_message = f"""
Transaction: {tx_payload['tx_id']}
Amount: ₹{tx_payload['amount_inr']:,.0f}
Purpose code: {tx_payload['purpose_code']}
Receiver: {tx_payload['receiver_name']}
Channel: {tx_payload['channel']}

T1 Flags fired: {flags}
Composite score: {composite_score}

Evidence:
- Transactions in ₹40K–₹49K band today: {t1_evidence.get('sc2_total_band_count_24h', 0)}
- Same receiver count today: {t1_evidence.get('sc3_repeat_count_24h', 0)}
- Total sent to same receiver: ₹{t1_evidence.get('sc3_total_inr_to_receiver_24h', 0):,.0f}
- 1-hour transaction count: {t1_evidence.get('sc1_count_1h', 0)}
- Volume spike ratio: {t1_evidence.get('sc4_spike_ratio', 0)}
- Account type threshold profile: {t1_evidence.get('sc1_threshold_profile', 'UNKNOWN')}

Is this pattern genuinely suspicious or likely a false positive?
"""

    # --- Model call routes through the gateway (Bedrock / SageMaker / Ollama) ---
    import asyncio
    from aws import model_gateway

    raw = await asyncio.to_thread(
        model_gateway.chat, SYSTEM_PROMPT, user_message, 1024, 0.0, 60
    )

    # Parse the JSON response — if malformed, return a safe fallback
    try:
        cleaned = raw.strip()
        # Strip markdown code fences
        if "```" in cleaned:
            parts = cleaned.split("```")
            # Take the part between the first pair of fences
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                if part.startswith("{"):
                    cleaned = part
                    break
        # Strip any preamble before the first {
        if "{" in cleaned:
            cleaned = cleaned[cleaned.index("{"):]
        # Strip any trailing content after the last }
        if "}" in cleaned:
            cleaned = cleaned[:cleaned.rindex("}") + 1]
        parsed = json.loads(cleaned)
        # Strip angle brackets from values e.g. <HIGH> -> HIGH
        for key in parsed:
            if isinstance(parsed[key], str):
                parsed[key] = parsed[key].strip("<>")
        return parsed
    except (json.JSONDecodeError, ValueError):
        return {
            "reasoning_summary": "SLM response parse error — deterministic result stands",
            "false_positive_likelihood": "UNKNOWN",
            "key_factors": [],
            "recommended_action": "PASS_TO_L2",
        }


def _mock_reasoning(tx_payload: dict, score: float, flags: list[str]) -> dict:
    """
    POC mock — returns a plausible reasoning result based on purpose code.
    Runs instantly. Use during development and all unit/e2e test runs.
    Replace with real Phi-4-mini call (USE_MOCK = False) for SLM evaluation.
    """
    purpose = tx_payload.get("purpose_code", "")
    amount  = tx_payload.get("amount_inr", 0)

    if purpose == "P0014":   # salary
        return {
            "reasoning_summary": "Pattern consistent with salary disbursement — multiple recipients, same amount band, P0014 purpose code.",
            "false_positive_likelihood": "HIGH",
            "key_factors": ["purpose_code_P0014", "different_receivers", "regular_interval"],
            "recommended_action": "DEPRIORITISE",
        }
    elif 40000 <= amount <= 49999 and score > 0.60:
        return {
            "reasoning_summary": "Repeated just-below-₹50K transfers to same receiver suggest deliberate structuring.",
            "false_positive_likelihood": "LOW",
            "key_factors": ["amount_band_pattern", "same_receiver", "pmla_threshold_proximity"],
            "recommended_action": "ESCALATE",
        }
    else:
        return {
            "reasoning_summary": "Velocity pattern detected but context insufficient for high confidence.",
            "false_positive_likelihood": "MEDIUM",
            "key_factors": ["velocity_above_baseline"],
            "recommended_action": "PASS_TO_L2",
        }