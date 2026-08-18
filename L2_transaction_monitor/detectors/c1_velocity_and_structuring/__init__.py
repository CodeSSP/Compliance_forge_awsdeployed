"""
__init__.py
-----------
Entry point for C1 — Velocity and Structuring Check.

This is the function that Person D's L2 aggregator calls
inside asyncio.gather() alongside T2, T3, T4.

CONTRACT WITH L2 AGGREGATOR:
  Input  : dict matching TransactionPayload fields
  Output : T1Result as dict (call .model_dump() before returning)

USAGE:
  from t1_velocity import run_t1

  t1_result = await run_t1(event_payload)
  # or inside L2:
  t1, t2, t3, t4 = await asyncio.gather(
      run_t1(payload), run_t2(payload), run_t3(payload), run_t4(payload)
  )
"""

import time
from datetime import datetime

from .models import TransactionPayload, T1Result
from .checks import (
    check_count_velocity,
    check_amount_band_structuring,
    check_same_beneficiary_clustering,
    check_volume_spike,
    check_high_value_threshold,
    check_credit_line_probing,
)
from .crdb_client import get_account_baseline, get_rolling_transactions
from .thresholds import T1_WEIGHTS, SLM_REASONING_THRESHOLD, HIGH_VALUE_THRESHOLD_INR

# --- NEW: Phi-4-mini SLM reasoner ---
# Import is wrapped in try/except so the pipeline still runs if slm_reasoner.py
# is not yet present (e.g. during initial setup or if SLM is disabled).
try:
    from .slm_reasoner import run_slm_reasoning
    SLM_AVAILABLE = True
except ImportError:
    SLM_AVAILABLE = False


async def run_t1(event_payload: dict) -> dict:
    """
    Main entry point. Accepts raw dict, validates it, runs all 4 sub-checks,
    aggregates into a composite score, and returns the T1Result as a dict.

    The dict return (not Pydantic model) keeps this compatible with
    asyncio.gather() and simple JSON serialisation downstream.
    """
    start = time.perf_counter()

    # --- Validate input ---
    tx = TransactionPayload(**event_payload)
    current_ts = datetime.fromisoformat(tx.timestamp)

    # --- Fetch data (one CockroachDB read per account) ---
    baseline  = await get_account_baseline(tx.sender_account_id)
    recent_1h = await get_rolling_transactions(tx.sender_account_id, hours=1,  current_ts=current_ts)
    recent_24h = await get_rolling_transactions(tx.sender_account_id, hours=24, current_ts=current_ts)

    # Build the dict T1 checks need (same shape as CSV row)
    current_tx_dict = {
        "tx_id":         tx.tx_id,
        "timestamp":     tx.timestamp,
        "amount_inr":    tx.amount_inr,
        "receiver_name": tx.receiver_name,
        "purpose_code":  tx.purpose_code,
    }

    # --- Run all 4 sub-checks ---
    r1 = await check_count_velocity(
        tx.sender_account_id, current_tx_dict, baseline, recent_1h, recent_24h
    )
    r2 = await check_amount_band_structuring(current_tx_dict, recent_24h)
    r3 = await check_same_beneficiary_clustering(
        current_tx_dict, recent_24h,
        typical_receivers=baseline.get("typical_receivers", []),
    )
    r4 = await check_volume_spike(current_tx_dict, baseline, recent_24h)
    r5 = await check_high_value_threshold(current_tx_dict)
    r6 = await check_credit_line_probing(current_tx_dict, baseline, recent_24h)
    
    # --- Composite score (weighted sum) ---
    composite = round(
        r1.score * T1_WEIGHTS["count_velocity"]
        + r2.score * T1_WEIGHTS["amount_band_structuring"]
        + r3.score * T1_WEIGHTS["same_beneficiary_clustering"]
        + r4.score * T1_WEIGHTS["volume_spike"]
        + r5.score * T1_WEIGHTS["high_value_threshold"]
        + r6.score * T1_WEIGHTS["credit_line_probing"],
        4,
    )

    # --- Flags ---
    # T1_VELOCITY: fires when ANY sub-check fires, OR when composite score crosses
    # the minimum signal threshold. The latter catches the first transaction
    # in a structuring series — even a single just-below-₹50K payment is a weak
    # velocity signal worth surfacing, even before the pattern is confirmed.
    MINIMUM_SIGNAL = 0.10   # must exceed single in-band txn score (0.133) to avoid single-txn FPs
    any_sub_fired = r1.fired or r2.fired or r3.fired or r4.fired or r5.fired or r6.fired
    flags = []
    if any_sub_fired or composite >= MINIMUM_SIGNAL:
        flags.append("T1_VELOCITY")

    # T1_STRUCTURING: fires when:
    #   (a) Amount band sub-check fires (confirmed pattern, >= STRUCTURING_MIN_COUNT txns)
    #   (b) Same-beneficiary clustering fires
    #   (c) Current amount is in the structuring band even on first occurrence
    in_band_raw      = r2.evidence.get("in_band", False)
    prior_band_count = r2.evidence.get("prior_band_count_24h", 0)

    # T1_STRUCTURING fires when:
    #   (a) amount band sub-check confirmed pattern (>= 3 in-band txns) — r2.fired
    #   (b) same-beneficiary clustering fires — r3.fired
    #   (c) in-band amount with at least 1 prior in-band txn today (pattern building)
    #   (d) in-band amount on first occurrence BUT score is strong enough (>= 0.12)
    #       This keeps salary/smurfing first txns while dropping isolated CLEAN noise
    has_prior_band_txn   = prior_band_count >= 1
    strong_first_in_band = in_band_raw and not has_prior_band_txn and composite >= 0.12

    if r2.fired or r3.fired or (in_band_raw and has_prior_band_txn) or strong_first_in_band:
        flags.append("T1_STRUCTURING")

    # Remove T1_VELOCITY flag for very low scores with no sub-check firing
    # This eliminates noise like Rs 3,428 Airtel recharge
    if not any_sub_fired and composite < 0.10:
        flags = [f for f in flags if f != "T1_VELOCITY"]

    if r6.fired:
        flags.append("T1_CREDIT_PROBING")

    # -------------------------------------------------------------------------
    # Deterministic suppression: single first in-band transaction to a
    # non-typical receiver with no sub-check signal.
    #
    # Why: a single Rs 40K-49K payment to an unknown/infrequent receiver is
    # not structuring evidence on its own. The composite signal (0.10-0.19)
    # comes purely from the amount-band score and a mild volume contribution.
    # Sending to a TYPICAL receiver (e.g. Gupta & Sons for a distributor) IS
    # genuinely suspicious even on the first occurrence, so we leave it alone.
    # Building patterns (prior_band_count > 0) and sub-check fires are also
    # excluded from this suppression.
    # -------------------------------------------------------------------------
    receiver_is_typical = tx.receiver_name.strip().lower() in {
        r.strip().lower() for r in baseline.get("typical_receivers", [])
    }
    if (
        in_band_raw
        and prior_band_count == 0
        and not any_sub_fired
        and not receiver_is_typical
        and composite < 0.20
    ):
        flags = [f for f in flags if f not in ("T1_VELOCITY", "T1_STRUCTURING")]

    # -------------------------------------------------------------------------
    # Deterministic suppression: CTR-level high-value transaction in the 24h
    # window inflating velocity signals for subsequent small transactions.
    #
    # Why: a FEMA remittance or large trade-finance payment earlier in the day
    # makes every subsequent small transaction look like a volume spike.
    # T4/r5 already own the high-value signal; T1 should not double-count it
    # by tagging the routine small payments that follow.
    # -------------------------------------------------------------------------
    recent_has_high_value = any(
        t["amount_inr"] >= HIGH_VALUE_THRESHOLD_INR for t in recent_24h
    )
    if recent_has_high_value and r5.score == 0.0 and composite < 0.30:
        flags = [f for f in flags if f not in ("T1_VELOCITY", "T1_STRUCTURING")]

    # --- Merge evidence (prefix each key with its sub-check) ---
    evidence = (
        {"sc1_" + k: v for k, v in r1.evidence.items()}
        | {"sc2_" + k: v for k, v in r2.evidence.items()}
        | {"sc3_" + k: v for k, v in r3.evidence.items()}
        | {"sc4_" + k: v for k, v in r4.evidence.items()}
        | {"sc5_" + k: v for k, v in r5.evidence.items()}
        | {"sc6_" + k: v for k, v in r6.evidence.items()}
    )

    # --- Collect triggered rule refs (deduplicated) ---
    triggered_rules = list({
        rule for rule in [
            r1.triggered_rule, r2.triggered_rule,
            r3.triggered_rule, r4.triggered_rule,
            r5.triggered_rule, r6.triggered_rule,
        ]
        if rule is not None
    })

    # -------------------------------------------------------------------------
    # NEW: Phi-4-mini SLM reasoning pass
    # Runs only when composite score crosses SLM_REASONING_THRESHOLD.
    # Never blocks the pipeline — any failure returns None silently.
    # -------------------------------------------------------------------------
    slm_result = None
    if SLM_AVAILABLE and composite >= SLM_REASONING_THRESHOLD:
        try:
            slm_result = await run_slm_reasoning(
                tx_payload      = dict(tx),
                t1_evidence     = evidence,
                composite_score = composite,
                flags           = flags,
            )
        except Exception:
            # SLM failure is silent — deterministic result stands unchanged
            slm_result = None

        # Only allow SLM to demote T1_STRUCTURING when score is substantial enough
        # that the SLM has sufficient pattern context to judge reliably.
        # Below 0.25, the pattern is too early (e.g. single in-band txn) —
        # the SLM sees weak evidence and incorrectly calls it HIGH FP likelihood.
        # Allow SLM to suppress T1_STRUCTURING when:
        #   (a) SLM says HIGH false positive likelihood
        #   (b) Score is in the weak signal range (< 0.30) — SLM has enough context
        #       to judge single in-band transactions but not confirmed patterns
        #   (c) count_velocity and volume_spike did NOT fire independently
        #       (those are stronger signals the SLM shouldn't override)
        count_vel_fired  = r1.fired
        volume_sp_fired  = r4.fired
        slm_may_suppress = (
            slm_result is not None
            and slm_result.get("false_positive_likelihood") == "HIGH"
            and "T1_STRUCTURING" in flags
            and composite < 0.30           # weak signal range only
            and not count_vel_fired        # don't suppress if count velocity fired
            and not volume_sp_fired        # don't suppress if volume spike fired
        )
        if slm_may_suppress:
            flags = [f for f in flags if f != "T1_STRUCTURING"]
            # Also suppress T1_VELOCITY if it was the only flag,
            # score is very weak, and no prior in-band pattern is building.
            # Keep T1_VELOCITY when prior_band_count > 0: the SLM should not
            # dismiss a confirmed multi-transaction structuring series even if
            # the purpose code looks like salary (RECURRING_SALARY_LOOK_LIKE_STRUCTURING).
            if (
                "T1_VELOCITY" in flags
                and composite < 0.20
                and not r1.fired
                and not r4.fired
                and prior_band_count == 0
            ):
                flags = [f for f in flags if f != "T1_VELOCITY"]
    # -------------------------------------------------------------------------

    elapsed_ms = round((time.perf_counter() - start) * 1000, 2)

    result = T1Result(
        check="T1_VELOCITY",
        fired=len(flags) > 0,
        flags=flags,
        sub_scores={
            "count_velocity":              r1.score,
            "amount_band_structuring":     r2.score,
            "same_beneficiary_clustering": r3.score,
            "volume_spike":                r4.score,
            "high_value_threshold":        r5.score,
            "credit_line_probing":         r6.score,
        },
        composite_score=composite,
        evidence=evidence,
        triggered_rule_refs=triggered_rules,
        processing_time_ms=elapsed_ms,
        # --- NEW fields (None if SLM disabled or score below threshold) ---
        slm_reasoning=slm_result,
        slm_false_positive_likelihood=(
            slm_result.get("false_positive_likelihood") if slm_result else None
        ),
        slm_recommended_action=(
            slm_result.get("recommended_action") if slm_result else None
        ),
    )

    return result.model_dump()


async def check_velocity_and_structuring(transaction_data, rail=None, threshold=None):
    """
    L2-aggregator entry point (the name main.py imports). Wraps `run_t1` and
    returns its result dict; the aggregator reads `composite_score` from it.
    Degrades to a non-firing result when the payload or baseline data is
    insufficient, so the L2 unit keeps running.
    """
    try:
        return await run_t1(transaction_data)
    except Exception as exc:
        return {
            "check": "C1_VELOCITY_STRUCTURING",
            "composite_score": 0.0,
            "status": "INSUFFICIENT_DATA",
            "detail": f"{type(exc).__name__}: {exc}",
        }