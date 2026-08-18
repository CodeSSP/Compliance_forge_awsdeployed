"""
thresholds.py  (C6 — Geo-Anomaly)
---------------------------------
All C6 threshold values in one place, so a policy change is a config change,
not a code change (same philosophy as t1_velocity/thresholds.py).

C6 absorbs the old T4 geo-location anomaly check. It answers ONE question:
"is the LOCATION / device context of this transaction unusual for this account,
given where and how the account normally transacts?"

Regulatory note: C6 is a *derived behavioural signal* with NO dedicated clause.
It supports the general EWS detection mandate of the RBI FRM Master Directions,
2024 (Clause 8.3, per advisory summaries) and is consistent with the risk-based
/ contextual authentication approach in the RBI Authentication Mechanisms for
Digital Payment Transactions Directions, 2025 (location + device attributes as
risk parameters). It must NOT be cited as a discrete requirement.
"""

# --- Signal score contributions (combined via noisy-OR, capped at 1.0) -------
SCORE_NEW_LOCATION       = 0.50   # location never seen in account history
SCORE_RARE_LOCATION_MAX  = 0.30   # max contribution when location is seen but rare
SCORE_FOREIGN_COUNTRY    = 0.40   # current country != home country
SCORE_FATF_HIGH_RISK     = 0.85   # foreign AND a FATF high-risk jurisdiction
SCORE_IMPOSSIBLE_TRAVEL  = 0.95   # physically implausible movement
SCORE_NEW_DEVICE         = 0.35   # device never seen for this account
SCORE_BALANCE_DRAIN      = 0.45   # txn drains a large share of balance
SCORE_ODD_HOUR           = 0.15   # transaction at an unusual hour for the account

# A location seen in fewer than this share of past transactions counts as "rare".
RARE_LOCATION_SHARE = 0.05        # < 5% of history

# Fire (flag for investigation) at or above this combined score.
FIRED_THRESHOLD = 0.50

# Impossible travel: implied speed above this (km/h) is implausible for legit use.
MAX_PLAUSIBLE_SPEED_KMH = 900.0   # ~commercial jet cruise

# Balance-drain: a single txn taking more than this share of available balance.
BALANCE_DRAIN_SHARE = 0.80

# Odd-hour window (local): transactions between these hours are "unusual" for a
# retail account unless the account's profile says otherwise.
ODD_HOUR_START = 1   # 01:00
ODD_HOUR_END   = 5   # 05:00

# FATF "high-risk and other monitored jurisdictions" (illustrative POC subset).
# In production, source this from L7 Regulatory Watch instead of hard-coding.
FATF_HIGH_RISK_COUNTRIES = {"KP", "IR", "MM", "SY", "AF", "YE"}

DEFAULT_HOME_COUNTRY = "IN"
