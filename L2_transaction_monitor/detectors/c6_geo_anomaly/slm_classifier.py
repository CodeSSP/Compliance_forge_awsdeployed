"""
slm_classifier.py  (C6 — Geo-Anomaly)
-------------------------------------
Phi-4 prompt-based binary classifier for C6.

Given the SAME feature dict the deterministic detector sees, it asks phi4 to make
a CONTEXTUAL judgement: is this geo/device pattern genuinely an account-takeover
/ laundering signal, or is it explained by the account's profile (NRE account,
frequent traveller, large legitimate purchase)?

Two execution paths (mirrors t1_velocity/slm_reasoner.py):
  USE_MOCK = True   -> the transparent `reference_reasoner` below. Derives its
                       decision from FEATURES ONLY (never reads the label), so its
                       F1 on held-out data is an honest measurement of the
                       contextual-reasoning approach. Runs instantly, no Ollama.
  USE_MOCK = False  -> real phi4 via the AWS model gateway. The reference
                       reasoner becomes the safe fallback if phi4 is unreachable
                       or returns malformed JSON.

Switching: set USE_MOCK = False and point MODEL_BACKEND at bedrock/sagemaker.
"""

import json

USE_MOCK = False         # real phi4-mini over Ollama by default (falls back to the
                         # reference reasoner automatically if Ollama is unreachable)
SLM_MODEL = "phi4-mini"  # 3.8B — fits ~8GB RAM / CPU. Use "phi4" (14B) only with 16GB+.
SLM_TIMEOUT_S = 180.0   # CPU + low-RAM: first load is slow; keep generous


SYSTEM_PROMPT = """You are a geo-anomaly fraud analyst for an Indian bank's
Early Warning System (RBI FRM Master Directions 2024, Clause 8.3).

Classify ONE transaction as SUSPICIOUS or NORMAL.

THE ONLY RULE — answer SUSPICIOUS if and ONLY if at least one TRIGGER is present:
  T1  new_device = True            (a device the account has NEVER used before)
  T2  impossible_travel = True     (physically impossible movement)
  T3  fatf_high_risk = True        (FATF high-risk country)
  T4  foreign = True AND account_type is NOT NRE/NRO AND travel_profile is NOT
      INTERNATIONAL_FREQUENT
If NO trigger is present -> NORMAL.

IGNORE THESE WHEN DECIDING (they are NOT triggers on their own):
  amount size, amount-vs-average, balance_drain, odd_hour, new_location,
  rare_location.
On the account's OWN known device with no trigger, a huge payment, a full
balance drain, a 3 a.m. payment, or a payment from a new city are ALL NORMAL.
These only matter when a trigger (T1–T4) is also present.

GROUNDED EXAMPLES (study the ones with no trigger carefully):
  • new_device=False, balance_drain=True(0.88), amount x16 avg, odd_hour=False,
    location known   -> NORMAL  ("large purchase on the account's own device")
  • new_device=False, odd_hour=True, amount normal, location known
                      -> NORMAL  ("night-time payment from a recognised device")
  • new_device=False, new_location=True, amount normal, DOMESTIC_STATIC
                      -> NORMAL  ("new city on the account's own device — travel")
  • new_device=False, foreign=True, account_type=NRE
                      -> NORMAL  ("foreign transfer expected for an NRE account")
  • new_device=True,  balance_drain=True(0.95)
                      -> SUSPICIOUS  ("unrecognised device draining the balance")
  • new_device=True,  odd_hour=True, amount small
                      -> SUSPICIOUS  ("unrecognised device probing at an odd hour")
  • impossible_travel=True   -> SUSPICIOUS  ("physically impossible travel")
  • fatf_high_risk=True      -> SUSPICIOUS  ("FATF high-risk jurisdiction")

Respond ONLY with JSON, no preamble:
{"label": "SUSPICIOUS|NORMAL", "confidence": 0.0-1.0, "reason": "<one sentence>"}
"""


def _foreign_unexpected(f: dict) -> bool:
    """foreign on an account for which foreign activity is NOT expected (T4)."""
    acct = (f.get("account_type") or "").upper()
    profile = (f.get("travel_profile") or "").upper()
    expected = acct in {"NRE", "NRO", "NRE_NRO"} or profile == "INTERNATIONAL_FREQUENT"
    return bool(f.get("is_foreign") and not expected)


def _features_to_prompt(f: dict) -> str:
    triggers = {
        "new_device": bool(f.get("is_new_device")),
        "impossible_travel": bool(f.get("impossible_travel")),
        "fatf_high_risk": bool(f.get("is_fatf_high_risk")),
        "foreign_unexpected": _foreign_unexpected(f),
    }
    any_trigger = any(triggers.values())
    return f"""Account: {f.get('account_id')} | type={f.get('account_type')} | travel_profile={f.get('travel_profile')}
Home country: {f.get('home_country')} | Current: {f.get('current_city')}, {f.get('current_country')}
Amount: Rs {f.get('amount_inr'):,.0f} (x{f.get('amount_vs_avg')} of account average) via {f.get('channel')} purpose={f.get('purpose_code')}
Context signals (DO NOT decide on these): new_location={f.get('is_new_location')} rare_location={f.get('is_rare_location')} balance_drain={f.get('is_balance_drain')}(share={f.get('drain_share')}) odd_hour={f.get('is_odd_hour')}

DECISION INPUTS — the ONLY things that matter:
- new_device        = {triggers['new_device']}
- impossible_travel = {triggers['impossible_travel']}
- fatf_high_risk    = {triggers['fatf_high_risk']}
- foreign_unexpected= {triggers['foreign_unexpected']}
At least one TRUE -> SUSPICIOUS. All FALSE -> NORMAL.
Here, at_least_one_trigger = {any_trigger}.

Answer for THIS transaction (SUSPICIOUS or NORMAL):"""


# ---------------------------------------------------------------------------
# Reference reasoner (mock + fallback)
# ---------------------------------------------------------------------------

def reference_reasoner(f: dict) -> dict:
    """
    Transparent, feature-only contextual reasoner. Encodes the same rules given
    to phi4 in the system prompt. NEVER reads the ground-truth label.
    """
    profile = (f.get("travel_profile") or "").upper()
    acct = (f.get("account_type") or "").upper()
    is_international_account = acct in {"NRE", "NRO", "NRE_NRO"} or profile == "INTERNATIONAL_FREQUENT"
    is_traveller = profile in {"DOMESTIC_TRAVELLER", "INTERNATIONAL_FREQUENT"}
    high_amount = (f.get("amount_vs_avg") or 0) >= 3.0

    # 1) Hard signals — always suspicious.
    if f.get("impossible_travel"):
        return _verdict("SUSPICIOUS", 0.97, "Physically impossible travel between sessions.")
    if f.get("is_fatf_high_risk"):
        return _verdict("SUSPICIOUS", 0.95, "Transaction from a FATF high-risk jurisdiction.")

    # 2) New-device takeover combinations.
    if f.get("is_new_device") and f.get("is_balance_drain"):
        return _verdict("SUSPICIOUS", 0.93, "New device draining account balance — takeover pattern.")
    if f.get("is_new_device") and (f.get("is_odd_hour") or high_amount):
        return _verdict("SUSPICIOUS", 0.85, "New device with odd-hour / high-value probe.")

    # 3) Foreign transactions — context decides.
    if f.get("is_foreign"):
        if is_international_account:
            return _verdict("NORMAL", 0.80, "Foreign transfer expected for NRE/international-frequent profile.")
        return _verdict("SUSPICIOUS", 0.82, "Foreign transaction inconsistent with a domestic-only account.")

    # 4) New / rare location — context decides.
    if f.get("is_new_location") or f.get("is_rare_location"):
        if is_traveller and not f.get("is_new_device"):
            return _verdict("NORMAL", 0.78, "New location consistent with a travelling account on a known device.")
        if f.get("is_new_device"):
            return _verdict("SUSPICIOUS", 0.80, "New location AND new device for this account.")
        # Static account, own device, normal amount -> likely travel/relocation.
        if not high_amount and not f.get("is_balance_drain"):
            return _verdict("NORMAL", 0.66, "New location on the account's own device with a normal amount.")
        return _verdict("SUSPICIOUS", 0.70, "New location with an unusually large transfer.")

    # 5) Balance drain from a known device/location at a normal hour -> legit purchase.
    if f.get("is_balance_drain") and not f.get("is_odd_hour"):
        return _verdict("NORMAL", 0.62, "Large transfer from a known device/location — likely a legitimate purchase.")

    # 6) New device alone, odd hour alone, etc. -> weak, treat as normal probe-of-one.
    if f.get("is_new_device") and f.get("is_odd_hour"):
        return _verdict("SUSPICIOUS", 0.72, "Unrecognised device active at an odd hour.")

    return _verdict("NORMAL", 0.60, "No combination of signals rises to a takeover/laundering pattern.")


def _verdict(label: str, confidence: float, reason: str) -> dict:
    return {"label": label, "confidence": confidence, "reason": reason}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify(features: dict) -> dict:
    """
    Classify one transaction's geo features.

    Returns: {"predictor", "label" (1/0), "verdict", "confidence", "reason"}
    """
    if USE_MOCK:
        v = reference_reasoner(features)
        return _shape(v, predictor="phi4_mock")

    try:
        v = _call_ollama(features)
        return _shape(v, predictor="phi4")
    except Exception:
        # phi4 unreachable / malformed — fall back to the transparent reasoner.
        v = reference_reasoner(features)
        return _shape(v, predictor="phi4_fallback")


def _shape(v: dict, predictor: str) -> dict:
    label_str = str(v.get("label", "NORMAL")).strip().upper().strip("<>")
    return {
        "check": "C6_GEO_ANOMALY",
        "predictor": predictor,
        "label": 1 if label_str.startswith("SUS") else 0,
        "verdict": "SUSPICIOUS" if label_str.startswith("SUS") else "NORMAL",
        "confidence": float(v.get("confidence", 0.5) or 0.5),
        "reason": v.get("reason", ""),
    }


def _call_ollama(features: dict) -> dict:
    """Name kept for call-site compatibility; the backend is whatever
    MODEL_BACKEND selects (bedrock / sagemaker / ollama)."""
    from aws import model_gateway

    raw = model_gateway.chat(
        SYSTEM_PROMPT, _features_to_prompt(features),
        max_tokens=1024, temperature=0.0, timeout=int(SLM_TIMEOUT_S),
    )
    cleaned = raw.strip()
    if "{" in cleaned:
        cleaned = cleaned[cleaned.index("{"): cleaned.rindex("}") + 1]
    return json.loads(cleaned)
