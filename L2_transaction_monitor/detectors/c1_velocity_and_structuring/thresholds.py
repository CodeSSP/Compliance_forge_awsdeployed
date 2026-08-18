"""
thresholds.py
-------------
All T1 threshold values in one place.

WHY THIS EXISTS:
  PMLA thresholds (₹50K reporting limit) and RBI velocity rules have changed
  before and will change again. A regulatory update must NOT require a code
  change — only a config change here (or swap this for AWS AppConfig
  in production).

PROFILE SELECTION:
  Assigned per account in baseline_fixture.json based on account_type +
  is_business. The crdb_client returns the profile string; checks.py
  looks it up here.
"""

# ---------------------------------------------------------------------------
# STRUCTURING BAND
# ---------------------------------------------------------------------------
# Based on PMLA Rule 3: cash transactions >= ₹50,000 require reporting.
# Structuring = deliberately keeping amounts just below this threshold.
# Band captures ₹40,000 – ₹49,999 as the suspicious zone.

STRUCTURING_BAND_LOW  = 40_000.0
STRUCTURING_BAND_HIGH = 49_999.0

# Minimum number of band transactions in 24h to flag structuring
STRUCTURING_MIN_COUNT = 3

# Same-beneficiary clustering: total across repeated receivers > this → flag
SAME_BENEFICIARY_TOTAL_THRESHOLD = 50_000.0
SAME_BENEFICIARY_MIN_REPEAT      = 2        # at least 2 txns to same receiver

# ---------------------------------------------------------------------------
# VELOCITY THRESHOLDS — per threshold profile
# ---------------------------------------------------------------------------
# max_count_1h  : rolling 1-hour transaction count limit
# max_count_24h : rolling 24-hour transaction count limit
# spike_ratio   : today's total volume / baseline daily volume → flag if exceeded

THRESHOLD_PROFILES = {
    "INDIVIDUAL_SAVINGS": {
        "max_count_1h":  10,
        "max_count_24h": 20,
        "spike_ratio":   4.0,
    },
    "SALARY": {
        "max_count_1h":  5,
        "max_count_24h": 20,
        "spike_ratio":   5.0,    # salary accounts have predictable large bursts
    },
    "BUSINESS_CURRENT": {
        "max_count_1h":  10,
        "max_count_24h": 50,
        "spike_ratio":   4.0,    # raised from 3.0 — post-structuring activity was inflating FPR
    },
    "NRE_NRO": {
        "max_count_1h":  3,
        "max_count_24h": 10,
        "spike_ratio":   3.0,    # tighter — FEMA LRS rules apply
    },
}

# ---------------------------------------------------------------------------
# COMPOSITE SCORE WEIGHTS
# ---------------------------------------------------------------------------
# These four weights must sum to 1.0.
# amount_band_structuring carries the most regulatory weight (PMLA Rule 3).

T1_WEIGHTS = {
    "count_velocity":              0.20,
    "amount_band_structuring":     0.35,
    "same_beneficiary_clustering": 0.25,
    "volume_spike":                0.07,   
    "high_value_threshold":        0.10,
    "credit_line_probing":         0.03,   
}

# ---------------------------------------------------------------------------
# PURPOSE CODE EXEMPTIONS
# ---------------------------------------------------------------------------
# Transactions with these purpose codes are lower-risk for structuring.
# P0014 = Salary disbursement
# P0008 = Utility / telecom recharge
# P0013 = Loan repayment
# When a transaction is in the structuring band but has one of these
# purpose codes, the score is reduced to 40% and the fired threshold
# is doubled (requires 6 transactions instead of 3).

STRUCTURING_LOW_RISK_PURPOSES = {"P0014", "P0008", "P0013"}


# ---------------------------------------------------------------------------
# HIGH-VALUE ABSOLUTE THRESHOLD
# ---------------------------------------------------------------------------
# RBI mandates Cash Transaction Reports (CTR) for transactions >= Rs 10,00,000
# A single transaction above this amount is always flagged regardless of
# account baseline or velocity pattern.
# Source: PMLA Rule 3, RBI Master Direction on KYC 2016

HIGH_VALUE_THRESHOLD_INR = 1_000_000.0   # Rs 10 lakh
HIGH_VALUE_SCORE         = 0.40          # base score when threshold is just crossed

# ---------------------------------------------------------------------------
# SLM REASONING THRESHOLD
# ---------------------------------------------------------------------------
# Minimum composite score above which Phi-4-mini reasoning is triggered.
# Below this: deterministic result is sufficient.
# Above this: SLM adds reasoning context before passing to L2.
# Must exceed single in-band txn score (0.133) to avoid running SLM
# on every transaction with a marginal in-band amount.

SLM_REASONING_THRESHOLD = 0.04

# ---------------------------------------------------------------------------
# CREDIT-LINE LIMIT-PROBING (T8, merged into C1)
# ---------------------------------------------------------------------------
# Purpose codes that indicate credit utilisation or loan drawdown activity.
# A burst of these in a short window suggests deliberate limit-probing.
CREDIT_LINE_PURPOSE_CODES = {"P0013", "P0022", "P0023"}  
# P0013 = Loan repayment, P0022 = Credit utilisation, P0023 = Loan disbursement

# Minimum number of credit-purpose transactions in a 24h window to fire
CREDIT_LINE_MIN_COUNT = 3

# The probing window — we look at 24h, same as structuring
CREDIT_LINE_WINDOW_HOURS = 24

# Fraction of the account's p90_amount that defines "small drawdown"
# A drawdown is suspicious when it's well below the account's normal large txn size
# but repeats rapidly. We flag when each txn < CREDIT_LINE_SMALL_RATIO * p90_amount.
CREDIT_LINE_SMALL_RATIO = 0.40

# Weight contribution of T8 sub-check within C1's composite score
# Re-pools from the spec: T1 was 0.25, T8 was 0.02, pooled C1 = 0.27
# Within T1's internal weights, T8 gets a proportional slice
T8_INTERNAL_WEIGHT = 0.07  # carved out of volume_spike (was 0.10 → 0.07 after T8 added)