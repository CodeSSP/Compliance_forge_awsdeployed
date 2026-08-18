"""
models.py
---------
Pydantic models for T1 input payload and output contract.

The output model is the interface contract between T1 and:
  - L2 aggregator (consumes composite_score + flags)
  - L3 Regulation Interpreter (consumes evidence + triggered_rule_refs)
  - L6 Audit Logger (consumes the full dict)
"""

from pydantic import BaseModel
from typing import Optional


# ---------------------------------------------------------------------------
# INPUT
# ---------------------------------------------------------------------------

class TransactionPayload(BaseModel):
    """
    Subset of the full event payload that T1 needs.
    The L2 orchestrator passes the full payload; T1 reads only these fields.
    """
    tx_id: str
    timestamp: str                   # ISO-8601 e.g. "2026-05-22T11:01:00"
    channel: str                     # UPI / NEFT / RTGS / IMPS
    amount_inr: float
    sender_account_id: str
    sender_name: str
    receiver_name: str
    receiver_account_external: str
    purpose_code: str
    tx_status: str


# ---------------------------------------------------------------------------
# OUTPUT — sub-check results (internal, not exposed outside T1)
# ---------------------------------------------------------------------------

class SubCheckResult(BaseModel):
    fired: bool
    score: float                     # 0.0 – 1.0
    triggered_rule: Optional[str]    # e.g. "PMLA_RULE_3", None if not fired
    evidence: dict


# ---------------------------------------------------------------------------
# OUTPUT — final T1 result (this is the contract with the rest of the system)
# ---------------------------------------------------------------------------

class T1Result(BaseModel):
    check: str = "T1_VELOCITY"
    fired: bool
    flags: list[str]
    sub_scores: dict[str, float]
    composite_score: float
    evidence: dict
    triggered_rule_refs: list[str]
    processing_time_ms: float

    # --- NEW: SLM reasoning (None if score below threshold or SLM disabled) ---
    slm_reasoning: dict | None = None
    slm_false_positive_likelihood: str | None = None   # LOW / MEDIUM / HIGH
    slm_recommended_action: str | None = None          # PASS_TO_L2 / DEPRIORITISE / ESCALATE