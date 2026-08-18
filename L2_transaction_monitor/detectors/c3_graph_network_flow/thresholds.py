"""
thresholds.py  (C3 — Graph / Network Flow)
-------------------------------------------
All C3 threshold values in one place (config-not-code, per house style).

C3 absorbs two legacy checks over a SINGLE directed graph of CockroachDB
transaction edges built once for a 72h window and traversed twice:

  * T5  mule fan-in / fan-out
  * T9  PMLA layering round-trip

Regulatory anchors:
  - Layering (moving proceeds through intermediaries to disguise origin) is the
    money-laundering process defined in PMLA, 2002, s.3 — making round-trip
    detection directly relevant to the s.3 offence and the Rule 3(1)(D) STR duty.
  - The RBI FRM Master Directions, 2024 EWS framework (Clause 8.3, per advisory
    summaries) extended monitoring to non-credit / digital-platform transactions
    to identify unusual patterns — the basis for graph-level pattern detection.
  - Reference implementation: MuleHunter.AI (Reserve Bank Innovation Hub, RBIH).
"""

# --- Graph window ---
GRAPH_WINDOW_HOURS = 72          # the single window the graph is built over

# --- Fan-in / fan-out (T5 mule) ---
FANIN_WINDOW_HOURS   = 2         # inbound credits must cluster within this window
FANIN_MIN_INBOUND    = 5         # N: minimum distinct inbound credits to flag
FANIN_MAX_CREDIT_INR = 5_000.0   # each inbound credit must be below this
FANOUT_WINDOW_MIN    = 30        # the single outbound must occur within this many minutes
FANOUT_MIN_RATIO     = 0.80      # outbound must be > this share of cumulative received

# --- Round-trip (T9 layering) ---
ROUNDTRIP_FIRE_MAX_DEPTH = 3     # deterministic fires only for returns within <= 3 hops
ROUNDTRIP_MEASURE_DEPTH  = 4     # graph is traversed one hop deeper for measurement
PRESERVATION_VERY_HIGH   = 0.90  # >= this share returned = very high suspicion
PRESERVATION_MIN_FLAG    = 0.50  # below this, a "return" is not treated as layering

# Shared-identity attributes that make a return "circular" (same beneficial owner).
SHARED_ATTRIBUTES = ("device_id", "ifsc_prefix", "holder_suffix")

# --- Round-trip score by hop count (closer return = stronger signal) ---
ROUNDTRIP_HOP_SCORE = {2: 0.90, 3: 0.70, 4: 0.45}

# --- Fan pattern score ---
FANIN_BASE_SCORE = 0.85

# Combined fire threshold for the deterministic detector.
FIRED_THRESHOLD = 0.50

# Purpose codes / account flags that LEGITIMISE a fan/round-trip pattern.
# (Used by the SLM reasoner context, not by the rules-only baseline.)
SETTLEMENT_PURPOSES = {"P0104", "P1099", "SETTLEMENT", "PAYROLL", "P0014"}
