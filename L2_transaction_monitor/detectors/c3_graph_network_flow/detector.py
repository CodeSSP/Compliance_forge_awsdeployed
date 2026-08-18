"""
detector.py  (C3 — Graph / Network Flow)
----------------------------------------
The DETERMINISTIC baseline detector — rules only, the benchmark the SLM is
measured against in the F1 evaluation.

Fan-in/out fires when the strict T5 thresholds are ALL met. Round-trip fires
when a return occurs within <= 3 hops sharing an identity attribute, with enough
amount preserved. The label is the OR of the two patterns.

This baseline is context-blind on purpose: it cannot tell a payroll aggregator
or a registered merchant settlement from a real mule, nor a 4-hop layering loop
(one hop past its fire depth) from no loop at all. Closing those gaps is the
SLM classifier's job — see slm_classifier.py.
"""

from .graph_builder import TxGraph
from .patterns import fan_in_out_features, round_trip_features
from .thresholds import (
    FANIN_BASE_SCORE,
    FANIN_MIN_INBOUND,
    FANOUT_MIN_RATIO,
    FIRED_THRESHOLD,
    PRESERVATION_MIN_FLAG,
    ROUNDTRIP_FIRE_MAX_DEPTH,
    ROUNDTRIP_HOP_SCORE,
)


def _fan_score(fan: dict) -> tuple[float, str | None]:
    fires = (
        fan["inbound_count"] >= FANIN_MIN_INBOUND
        and fan["distinct_vpas"] >= FANIN_MIN_INBOUND
        and fan["all_under_5k"]
        and fan["fanout_within_window"]
        and fan["outbound_ratio"] > FANOUT_MIN_RATIO
    )
    if fires:
        return FANIN_BASE_SCORE, "RBI_FRM_EWS_MULE_FANOUT"
    return 0.0, None


def _round_trip_score(rt: dict) -> tuple[float, str | None]:
    if (
        rt["returns"]
        and rt["hop_count"] is not None
        and rt["hop_count"] <= ROUNDTRIP_FIRE_MAX_DEPTH
        and rt["amount_preservation_ratio"] >= PRESERVATION_MIN_FLAG
        and rt["shared_attribute"]
    ):
        base = ROUNDTRIP_HOP_SCORE.get(rt["hop_count"], 0.5)
        score = round(min(base * (0.5 + rt["amount_preservation_ratio"] / 2), 1.0), 4)
        return score, "PMLA_S3_LAYERING_ROUNDTRIP"
    return 0.0, None


def predict(case: dict) -> dict:
    """
    Deterministic prediction for one transaction-cluster case.

    Returns the L2 sub-check contract shape plus a binary `label`
    (1 = SUSPICIOUS / fired, 0 = NORMAL) used directly by the F1 harness.
    """
    g = TxGraph.from_case(case)
    fan = fan_in_out_features(g)
    rt = round_trip_features(g)

    fan_score, fan_rule = _fan_score(fan)
    rt_score, rt_rule = _round_trip_score(rt)

    score = max(fan_score, rt_score)
    fired = score >= FIRED_THRESHOLD
    rules = [r for r in (fan_rule, rt_rule) if r]

    return {
        "check": "C3_GRAPH_FLOW",
        "predictor": "deterministic",
        "label": 1 if fired else 0,
        "fired": fired,
        "score": round(score, 4),
        "triggered_rules": rules,
        "evidence": {
            "fan_in_out": fan,
            "round_trip": rt,
            "fan_score": fan_score,
            "round_trip_score": rt_score,
            "fired_threshold": FIRED_THRESHOLD,
        },
    }
