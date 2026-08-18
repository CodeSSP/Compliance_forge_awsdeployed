"""
slm_classifier.py  (C3 — Graph / Network Flow)
----------------------------------------------
Phi-4 prompt-based binary classifier for C3. Sees the SAME fan-in/out and
round-trip features the deterministic detector sees, and makes a CONTEXTUAL
judgement: is this graph pattern a genuine mule / layering structure, or is it a
legitimate aggregation (registered merchant, payroll) or family repayment?

Paths (mirrors the C6 classifier and t1_velocity/slm_reasoner.py):
  USE_MOCK = True   -> transparent `reference_reasoner` (features only, never the
                       label). Honest F1 measurement of the reasoning approach.
  USE_MOCK = False  -> real phi4 over Ollama; reference reasoner is the fallback.
"""

import json

USE_MOCK = False         # real phi4-mini over Ollama by default (falls back to the
                         # reference reasoner automatically if Ollama is unreachable)
SLM_MODEL = "phi4-mini"  # 3.8B — fits ~8GB RAM / CPU. Use "phi4" (14B) only with 16GB+.
SLM_TIMEOUT_S = 180.0   # CPU + low-RAM: first load is slow; keep generous


SYSTEM_PROMPT = """You are a financial-crime graph analyst for an Indian bank,
applying PMLA 2002 s.3 (layering) and the RBI FRM Master Directions 2024 EWS
mule-detection mandate (reference tool: MuleHunter.AI).

Classify ONE account cluster as SUSPICIOUS or NORMAL.

THE ONLY RULE — answer SUSPICIOUS if at least one TRIGGER is present:
  M (mule sweep): many small DISTINCT inbound credits (each < Rs 5,000) rapidly
     swept out in ONE outbound worth >80% of the total received, on an account
     that is NOT a registered merchant.
  L (layering loop): funds RETURN to an account sharing the origin's device_id or
     IFSC prefix, with HIGH amount preservation (>=85%), within 4 hops.
If NEITHER M nor L holds -> NORMAL. Ordinary activity, a registered-merchant /
payroll SETTLEMENT, a return where funds were largely spent (preservation < 85%),
or a return sharing only a common surname are all NORMAL.

The user message gives you M and L ALREADY COMPUTED. Trust them — do not re-derive.

GROUNDED EXAMPLES:
  • mule_sweep=True,  layering_loop=False -> SUSPICIOUS ("rapid small-credit sweep — mule")
  • mule_sweep=False, layering_loop=True  -> SUSPICIOUS ("high-preservation same-identity round-trip — layering")
  • registered_merchant=True with a sweep -> NORMAL ("registered-merchant settlement aggregation")
  • returns=True but preservation < 0.85   -> NORMAL ("funds dissipated — genuine spending")
  • no sweep and no return                 -> NORMAL ("ordinary transaction graph")

Respond ONLY with JSON, no preamble:
{"label": "SUSPICIOUS|NORMAL", "confidence": 0.0-1.0, "reason": "<one sentence>"}
"""


def _mule_sweep(fan: dict) -> bool:
    """Trigger M: rapid small-distinct-credit sweep on a non-merchant account."""
    return bool(
        fan.get("inbound_count", 0) >= 3
        and fan.get("distinct_vpas", 0) >= 3
        and fan.get("all_under_5k")
        and fan.get("fanout_within_window")
        and fan.get("outbound_ratio", 0) > 0.80
        and not fan.get("is_registered_merchant")
    )


def _layering_loop(rt: dict) -> bool:
    """Trigger L: high-preservation return to a same-identity account, <= 4 hops."""
    hop = rt.get("hop_count") or 99
    return bool(
        rt.get("returns")
        and rt.get("amount_preservation_ratio", 0.0) >= 0.85
        and rt.get("shared_attribute") in {"device_id", "ifsc_prefix", "same_account"}
        and hop <= 4
    )


def _features_to_prompt(fan: dict, rt: dict) -> str:
    m = _mule_sweep(fan)
    l = _layering_loop(rt)
    return f"""Trigger account: {fan.get('trigger_account')} | type={fan.get('account_type')} | age_days={fan.get('account_age_days')} | registered_merchant={fan.get('is_registered_merchant')}

Context (fan-in/out): inbound_count={fan.get('inbound_count')} distinct_vpas={fan.get('distinct_vpas')} all_under_5k={fan.get('all_under_5k')} outbound_ratio={fan.get('outbound_ratio')} swept_within_window={fan.get('fanout_within_window')}
Context (round-trip): returns={rt.get('returns')} hop_count={rt.get('hop_count')} amount_preservation_ratio={rt.get('amount_preservation_ratio')} shared_attribute={rt.get('shared_attribute')}

DECISION INPUTS — the ONLY things that matter:
- mule_sweep    = {m}
- layering_loop = {l}
At least one TRUE -> SUSPICIOUS. Both FALSE -> NORMAL.
Here, at_least_one_trigger = {m or l}.

Answer for THIS cluster (SUSPICIOUS or NORMAL):"""


# ---------------------------------------------------------------------------
# Reference reasoner (mock + fallback)
# ---------------------------------------------------------------------------

def reference_reasoner(fan: dict, rt: dict) -> dict:
    """Feature-only contextual reasoner encoding the system-prompt rules."""
    age = fan.get("account_age_days")
    is_new_account = age is not None and age < 180
    is_merchant = bool(fan.get("is_registered_merchant"))
    acct_type = (fan.get("account_type") or "").upper()
    is_aggregator = is_merchant or acct_type in {"MERCHANT", "PAYROLL", "SETTLEMENT"}

    # --- Fan-in/out judgement ---
    sweep = (
        fan.get("inbound_count", 0) >= 3
        and fan.get("all_under_5k")
        and fan.get("fanout_within_window")
        and fan.get("outbound_ratio", 0) > 0.80
        and fan.get("distinct_vpas", 0) >= 3
    )
    if sweep and not is_aggregator:
        # Strong classic mule (>=5) OR sub-threshold mule on a new account.
        if fan.get("inbound_count", 0) >= 5 or is_new_account:
            conf = 0.92 if fan.get("inbound_count", 0) >= 5 else 0.86
            return _verdict("SUSPICIOUS", conf,
                            "Rapid fan-in of small distinct credits swept out in one transfer (mule).")

    # --- Round-trip judgement ---
    if rt.get("returns"):
        preservation = rt.get("amount_preservation_ratio", 0.0)
        shared = rt.get("shared_attribute")
        hop = rt.get("hop_count") or 99
        if shared in {"device_id", "ifsc_prefix", "same_account"} and preservation >= 0.85 and hop <= 4:
            return _verdict("SUSPICIOUS", 0.90,
                            "Funds round-trip to a same-identity account with high amount preservation (layering).")
        # Common surname only, or funds largely dissipated -> legitimate.
        if shared == "holder_suffix" and preservation < 0.90:
            return _verdict("NORMAL", 0.74,
                            "Return shares only a common surname with funds partly dissipated — likely family transfer.")
        if preservation < 0.60:
            return _verdict("NORMAL", 0.70,
                            "Funds largely dissipated before returning — consistent with genuine spending.")

    if is_aggregator and sweep:
        return _verdict("NORMAL", 0.80,
                        "High-volume aggregation explained by a registered merchant / payroll account.")

    return _verdict("NORMAL", 0.66, "No mule sweep or high-preservation layering loop detected.")


def _verdict(label: str, confidence: float, reason: str) -> dict:
    return {"label": label, "confidence": confidence, "reason": reason}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify(fan: dict, rt: dict) -> dict:
    if USE_MOCK:
        return _shape(reference_reasoner(fan, rt), "phi4_mock")
    try:
        return _shape(_call_ollama(fan, rt), "phi4")
    except Exception:
        return _shape(reference_reasoner(fan, rt), "phi4_fallback")


def _shape(v: dict, predictor: str) -> dict:
    label_str = str(v.get("label", "NORMAL")).strip().upper().strip("<>")
    return {
        "check": "C3_GRAPH_FLOW",
        "predictor": predictor,
        "label": 1 if label_str.startswith("SUS") else 0,
        "verdict": "SUSPICIOUS" if label_str.startswith("SUS") else "NORMAL",
        "confidence": float(v.get("confidence", 0.5) or 0.5),
        "reason": v.get("reason", ""),
    }


def _call_ollama(fan: dict, rt: dict) -> dict:
    """Name kept for call-site compatibility; the backend is whatever
    MODEL_BACKEND selects (bedrock / sagemaker / ollama)."""
    from aws import model_gateway

    raw = model_gateway.chat(
        SYSTEM_PROMPT, _features_to_prompt(fan, rt),
        max_tokens=1024, temperature=0.0, timeout=int(SLM_TIMEOUT_S),
    )
    cleaned = raw.strip()
    if "{" in cleaned:
        cleaned = cleaned[cleaned.index("{"): cleaned.rindex("}") + 1]
    return json.loads(cleaned)
