"""
checks.py
---------
The five deterministic sub-checks that make up T1 — Velocity.

Each function:
  - Takes only the data it needs (no global state)
  - Returns a SubCheckResult (fired, score, triggered_rule, evidence)
  - Is independently unit-testable
  - Contains NO LLM calls

Sub-checks:
  1. check_count_velocity              — rolling 1h / 24h transaction count
  2. check_amount_band_structuring     — PMLA Rule 3 just-below-Rs 50K pattern
  3. check_same_beneficiary_clustering — repeated sends to same receiver
  4. check_volume_spike                — today's volume vs 90-day baseline
  5. check_high_value_threshold        — single transaction above RBI CTR limit
"""

from datetime import datetime, timedelta
from .models import SubCheckResult
from .thresholds import (
    THRESHOLD_PROFILES,
    STRUCTURING_BAND_LOW,
    STRUCTURING_BAND_HIGH,
    STRUCTURING_MIN_COUNT,
    STRUCTURING_LOW_RISK_PURPOSES,
    SAME_BENEFICIARY_TOTAL_THRESHOLD,
    SAME_BENEFICIARY_MIN_REPEAT,
    HIGH_VALUE_THRESHOLD_INR,
    HIGH_VALUE_SCORE,
)

# Threshold for excluding one-off large transactions from volume spike calculation.
# A single CTR-level transaction (e.g. FEMA remittance) should not inflate subsequent
# same-day volume signals for unrelated small transactions.
_VOLUME_SPIKE_HIGH_VALUE_EXCLUDE = HIGH_VALUE_THRESHOLD_INR


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts)

def _same_calendar_day(ts1: datetime, ts2: datetime) -> bool:
    return ts1.date() == ts2.date()


# ---------------------------------------------------------------------------
# Sub-check 1: Rolling transaction count velocity
# ---------------------------------------------------------------------------

async def check_count_velocity(
    account_id: str,
    current_tx: dict,
    baseline: dict,
    recent_txns_1h: list[dict],
    recent_txns_24h: list[dict],
) -> SubCheckResult:
    """
    Detects abnormal burst in transaction frequency.

    Uses account threshold profile so a business current account
    gets a higher allowance than an individual savings account.

    VELOCITY_SPIKE scenario (ACC0014):
      Baseline avg_daily_tx_count = 2.6. By txn 4 within 105 minutes
      the 1h count exceeds the baseline spike threshold -> fires.
    """
    profile    = baseline.get("threshold_profile", "INDIVIDUAL_SAVINGS")
    thresholds = THRESHOLD_PROFILES[profile]
    max_1h     = thresholds["max_count_1h"]
    max_24h    = thresholds["max_count_24h"]

    count_1h  = len(recent_txns_1h)
    count_24h = len(recent_txns_24h)

    score_1h  = min(count_1h  / max_1h,  1.0) if max_1h  > 0 else 0.0
    score_24h = min(count_24h / max_24h, 1.0) if max_24h > 0 else 0.0

    BUSINESS_HOURS   = 8
    baseline_daily   = baseline.get("avg_daily_tx_count", 2.0)
    baseline_hourly  = baseline_daily / BUSINESS_HOURS
    baseline_ratio   = count_1h / baseline_hourly if baseline_hourly > 0 else 0.0
    BASELINE_SPIKE_T = 7.0
    baseline_fired   = baseline_ratio >= BASELINE_SPIKE_T
    score_baseline   = min(baseline_ratio / (BASELINE_SPIKE_T * 2), 1.0)

    score = round(max(score_1h, score_24h, score_baseline), 4)
    fired = (count_1h >= max_1h) or (count_24h >= max_24h) or baseline_fired

    return SubCheckResult(
        fired=fired,
        score=score,
        triggered_rule=None,
        evidence={
            "count_1h":             count_1h,
            "count_24h":            count_24h,
            "threshold_1h":         max_1h,
            "threshold_24h":        max_24h,
            "baseline_hourly_rate": round(baseline_hourly, 3),
            "baseline_ratio_1h":    round(baseline_ratio, 2),
            "threshold_profile":    profile,
        },
    )


# ---------------------------------------------------------------------------
# Sub-check 2: Amount band structuring (PMLA Rule 3)
# ---------------------------------------------------------------------------

async def check_amount_band_structuring(
    current_tx: dict,
    recent_txns_24h: list[dict],
) -> SubCheckResult:
    """
    Detects deliberate transaction splitting to stay below the
    Rs 50,000 PMLA cash reporting threshold (Rule 3).

    Suspicious band: Rs 40,000 - Rs 49,999.
    3 or more transactions in this band in 24h = structuring pattern.

    STRUCTURING_SMURFING scenario (ACC0007):
      5 transactions of Rs 48,964 - Rs 49,749 to Gupta & Sons -> fires on txn 3.

    FALSE POSITIVE scenario (ACC0019):
      5 transactions (salary, purpose P0014). This sub-check fires but
      with a reduced score (40% of normal) because P0014 is a low-risk
      purpose code. L3 resolves the false positive using full context.
    """
    amount  = current_tx["amount_inr"]
    in_band = STRUCTURING_BAND_LOW <= amount <= STRUCTURING_BAND_HIGH

    # Count previous band transactions in the 24h window
    prior_band_txns = [
        t for t in recent_txns_24h
        if STRUCTURING_BAND_LOW <= t["amount_inr"] <= STRUCTURING_BAND_HIGH
    ]
    prior_band_count = len(prior_band_txns)
    total_band_count = prior_band_count + (1 if in_band else 0)

    # Calculate score and fired FIRST — before any purpose code modification
    fired = in_band and (total_band_count >= STRUCTURING_MIN_COUNT)
    score = round(min(total_band_count / STRUCTURING_MIN_COUNT, 1.0), 4) if in_band else 0.0

    # Apply purpose code reduction AFTER score is calculated
    # Low-risk purposes (salary, utility, loan) reduce structuring likelihood
    is_low_risk_purpose = current_tx.get("purpose_code", "") in STRUCTURING_LOW_RISK_PURPOSES
    if is_low_risk_purpose and in_band:
        score = round(score * 0.40, 4)
        fired = total_band_count >= (STRUCTURING_MIN_COUNT * 2)  # require 6 instead of 3

    return SubCheckResult(
        fired=fired,
        score=score,
        triggered_rule="PMLA_RULE_3" if fired else None,
        evidence={
            "current_amount_inr":    round(amount, 2),
            "in_band":               in_band,
            "band_low":              STRUCTURING_BAND_LOW,
            "band_high":             STRUCTURING_BAND_HIGH,
            "prior_band_count_24h":  prior_band_count,
            "total_band_count_24h":  total_band_count,
            "min_count_threshold":   STRUCTURING_MIN_COUNT,
            "is_low_risk_purpose":   is_low_risk_purpose,
        },
    )


# ---------------------------------------------------------------------------
# Sub-check 3: Same-beneficiary clustering
# ---------------------------------------------------------------------------

async def check_same_beneficiary_clustering(
    current_tx: dict,
    recent_txns_24h: list[dict],
    typical_receivers: list[str] | None = None,
) -> SubCheckResult:
    """
    Detects multiple transfers to the same receiver within 24h
    where the cumulative amount exceeds Rs 50,000.

    STRUCTURING_SMURFING scenario (ACC0007):
      5 x ~Rs 49K all to "Gupta & Sons" -> fires on txn 2.

    FALSE POSITIVE scenario (ACC0019):
      Each salary payment goes to a DIFFERENT beneficiary -> does NOT fire.

    typical_receivers: when provided, skip clustering check for known receivers
    (e.g. a business's regular supplier). Repeat payments to typical receivers
    are expected and not indicative of structuring.
    """
    current_receiver = current_tx["receiver_name"].strip().lower()

    if typical_receivers:
        typical_lower = {r.strip().lower() for r in typical_receivers}
        if current_receiver in typical_lower:
            return SubCheckResult(
                fired=False,
                score=0.0,
                triggered_rule=None,
                evidence={
                    "receiver_name":             current_tx["receiver_name"],
                    "repeat_count_24h":          1,
                    "total_inr_to_receiver_24h": round(current_tx["amount_inr"], 2),
                    "clustering_threshold_inr":  SAME_BENEFICIARY_TOTAL_THRESHOLD,
                    "skipped_typical_receiver":  True,
                },
            )

    same_receiver_txns = [
        t for t in recent_txns_24h
        if t["receiver_name"].strip().lower() == current_receiver
    ]

    repeat_count          = len(same_receiver_txns) + 1
    total_inr_to_receiver = (
        sum(t["amount_inr"] for t in same_receiver_txns)
        + current_tx["amount_inr"]
    )

    fired = (
        repeat_count >= SAME_BENEFICIARY_MIN_REPEAT
        and total_inr_to_receiver > SAME_BENEFICIARY_TOTAL_THRESHOLD
    )

    if fired:
        count_ratio  = min(repeat_count / 3, 1.0)
        volume_ratio = min(total_inr_to_receiver / 150_000, 1.0)
        score = round(count_ratio * volume_ratio, 4)
    else:
        score = 0.0

    return SubCheckResult(
        fired=fired,
        score=score,
        triggered_rule="PMLA_RULE_3" if fired else None,
        evidence={
            "receiver_name":              current_tx["receiver_name"],
            "repeat_count_24h":           repeat_count,
            "total_inr_to_receiver_24h":  round(total_inr_to_receiver, 2),
            "clustering_threshold_inr":   SAME_BENEFICIARY_TOTAL_THRESHOLD,
        },
    )


# ---------------------------------------------------------------------------
# Sub-check 4: Volume spike vs 90-day baseline
# ---------------------------------------------------------------------------

async def check_volume_spike(
    current_tx: dict,
    baseline: dict,
    recent_txns_24h: list[dict],
) -> SubCheckResult:
    """
    Detects when today's total outflow is abnormally large compared
    to the account's 90-day average daily volume.
    """
    profile         = baseline.get("threshold_profile", "INDIVIDUAL_SAVINGS")
    spike_threshold = THRESHOLD_PROFILES[profile]["spike_ratio"]

    BASELINE_FLOOR = 10_000.0
    baseline_daily = max(
        baseline.get("avg_daily_tx_volume_inr", BASELINE_FLOOR),
        BASELINE_FLOOR
    )

    # Exclude CTR-level (high-value) prior transactions from the volume sum.
    # A large FEMA/trade-finance remittance earlier in the day would otherwise
    # inflate the spike ratio for all subsequent unrelated small transactions.
    today_volume = (
        sum(t["amount_inr"] for t in recent_txns_24h
            if t["amount_inr"] < _VOLUME_SPIKE_HIGH_VALUE_EXCLUDE)
        + current_tx["amount_inr"]
    )

    spike_ratio = round(today_volume / baseline_daily, 4) if baseline_daily > 0 else 0.0
    fired       = spike_ratio >= spike_threshold

    if spike_ratio > 1.0:
        score = round(min((spike_ratio - 1.0) / (spike_threshold * 2), 1.0), 4)
    else:
        score = 0.0

    return SubCheckResult(
        fired=fired,
        score=score,
        triggered_rule=None,
        evidence={
            "today_volume_inr":          round(today_volume, 2),
            "baseline_daily_volume_inr": baseline_daily,
            "spike_ratio":               spike_ratio,
            "spike_threshold":           spike_threshold,
        },
    )


# ---------------------------------------------------------------------------
# Sub-check 5: High-value absolute threshold (RBI CTR)
# ---------------------------------------------------------------------------

async def check_high_value_threshold(
    current_tx: dict,
) -> SubCheckResult:
    """
    Detects single transactions above the RBI CTR threshold of Rs 10,00,000.

    Fires regardless of account baseline, velocity history, or structuring band.
    A single cross-border RTGS payment of Rs 1.85 crore must always be flagged.

    CROSS_BORDER_HIGH_VALUE scenario (ACC0027 - Reddy Tech LLP):
      Rs 1,85,00,000 RTGS to Singapore — no structuring pattern, no velocity
      spike, but the absolute amount mandates a CTR report under PMLA Rule 3.
      This is the only sub-check that catches it.

    Score scales with distance above threshold:
      Rs 10L  (1x threshold)  -> 0.40
      Rs 1Cr  (10x threshold) -> 0.80
      Rs 2Cr+ (20x threshold) -> 1.0
    """
    amount = current_tx["amount_inr"]
    fired  = amount >= HIGH_VALUE_THRESHOLD_INR

    if fired:
        ratio = amount / HIGH_VALUE_THRESHOLD_INR
        score = round(min(HIGH_VALUE_SCORE * (ratio ** 0.5), 1.0), 4)
    else:
        score = 0.0

    return SubCheckResult(
        fired=fired,
        score=score,
        triggered_rule="PMLA_RULE_3_CTR" if fired else None,
        evidence={
            "amount_inr":           round(amount, 2),
            "high_value_threshold": HIGH_VALUE_THRESHOLD_INR,
            "above_threshold":      fired,
            "threshold_ratio":      round(amount / HIGH_VALUE_THRESHOLD_INR, 2) if fired else 0.0,
        },
    )

# ---------------------------------------------------------------------------
# Sub-check 6: Credit-line limit-probing (T8, merged into C1)
# ---------------------------------------------------------------------------

from .thresholds import (
    CREDIT_LINE_PURPOSE_CODES,
    CREDIT_LINE_MIN_COUNT,
    CREDIT_LINE_SMALL_RATIO,
)

async def check_credit_line_probing(
    current_tx: dict,
    baseline: dict,
    recent_txns_24h: list[dict],
) -> SubCheckResult:
    """
    Detects deliberate probing of credit limits via repeated small
    drawdowns with credit/loan purpose codes within a 24h window.

    Regulatory anchor: RBI FRM Master Directions 2024 EWS (Clause 8.3)
    — real-time monitoring of credit transactions for unusual patterns.
    Also PMLA Rule 3(1)(B): integrally connected series of transactions.

    Pattern: 3+ transactions with purpose code in {P0013, P0022, P0023},
    each individually small (< 40% of account's p90_amount), summing to
    a meaningful total within 24 hours.

    Example: ACC0014 (Kapoor Enterprises) sending 4× ₹9,800 loan
    repayments in a day when p90 is ₹80K — each below radar, aggregate
    deliberate.
    """
    is_credit_purpose = current_tx.get("purpose_code", "") in CREDIT_LINE_PURPOSE_CODES
    p90 = baseline.get("p90_amount", 0.0)
    small_threshold = p90 * CREDIT_LINE_SMALL_RATIO if p90 > 0 else float("inf")
    is_small_drawdown = current_tx["amount_inr"] < small_threshold

    # Count prior credit-purpose transactions in the 24h window
    prior_credit_txns = [
        t for t in recent_txns_24h
        if t.get("purpose_code", "") in CREDIT_LINE_PURPOSE_CODES
        and t["amount_inr"] < small_threshold
    ]
    prior_credit_count = len(prior_credit_txns)
    total_credit_count = prior_credit_count + (1 if (is_credit_purpose and is_small_drawdown) else 0)

    # Aggregate amount across the probing series
    prior_credit_total = sum(t["amount_inr"] for t in prior_credit_txns)
    total_credit_amount = prior_credit_total + (
        current_tx["amount_inr"] if (is_credit_purpose and is_small_drawdown) else 0
    )

    fired = (
        is_credit_purpose
        and is_small_drawdown
        and total_credit_count >= CREDIT_LINE_MIN_COUNT
    )

    if is_credit_purpose and is_small_drawdown and total_credit_count > 0:
        score = round(min(total_credit_count / CREDIT_LINE_MIN_COUNT, 1.0), 4)
    else:
        score = 0.0

    return SubCheckResult(
        fired=fired,
        score=score,
        triggered_rule="PMLA_RULE_3_CREDIT_PROBING" if fired else None,
        evidence={
            "is_credit_purpose_code":      is_credit_purpose,
            "purpose_code":                current_tx.get("purpose_code", ""),
            "current_amount_inr":          round(current_tx["amount_inr"], 2),
            "small_drawdown_threshold":    round(small_threshold, 2),
            "is_small_drawdown":           is_small_drawdown,
            "prior_credit_txns_24h":       prior_credit_count,
            "total_credit_txns_24h":       total_credit_count,
            "total_credit_amount_24h":     round(total_credit_amount, 2),
            "min_count_threshold":         CREDIT_LINE_MIN_COUNT,
        },
    )