"""
detector.py  (C6 — Geo-Anomaly)
-------------------------------
The DETERMINISTIC baseline detector. This is the "rules-only" predictor that the
SLM is benchmarked against in the F1 evaluation.

It combines the measured geo/device signals into one 0.0–1.0 score via noisy-OR
(P = 1 - Π(1 - s_i)) and fires when the score crosses FIRED_THRESHOLD.

By design this baseline is *context-blind*: it cannot tell a frequent business
traveller's legitimate new-city login from an account takeover, nor an NRE
account's expected foreign transfer from a suspicious one. That blind spot is
exactly what the SLM classifier is meant to close — see slm_classifier.py.
"""

from .features import extract_features
from .thresholds import (
    FIRED_THRESHOLD,
    SCORE_BALANCE_DRAIN,
    SCORE_FATF_HIGH_RISK,
    SCORE_FOREIGN_COUNTRY,
    SCORE_IMPOSSIBLE_TRAVEL,
    SCORE_NEW_DEVICE,
    SCORE_NEW_LOCATION,
    SCORE_ODD_HOUR,
    SCORE_RARE_LOCATION_MAX,
    RARE_LOCATION_SHARE,
)


def score_from_features(f: dict) -> tuple[float, dict, str | None]:
    """Combine features into a noisy-OR score. Returns (score, components, rule)."""
    components: dict[str, float] = {}
    triggered_rule = None

    if f["is_new_location"]:
        components["new_location"] = SCORE_NEW_LOCATION
    elif f["is_rare_location"] and f["location_share"] is not None:
        rarity = 1.0 - (f["location_share"] / RARE_LOCATION_SHARE)
        components["rare_location"] = round(SCORE_RARE_LOCATION_MAX * max(rarity, 0.0), 4)

    if f["is_fatf_high_risk"]:
        components["fatf_high_risk"] = SCORE_FATF_HIGH_RISK
        triggered_rule = "FATF_HIGH_RISK_JURISDICTION"
    elif f["is_foreign"]:
        components["foreign_country"] = SCORE_FOREIGN_COUNTRY
        triggered_rule = triggered_rule or "FEMA_CROSS_BORDER"

    if f["impossible_travel"]:
        components["impossible_travel"] = SCORE_IMPOSSIBLE_TRAVEL
        triggered_rule = "GEO_IMPOSSIBLE_TRAVEL"

    if f["is_new_device"]:
        components["new_device"] = SCORE_NEW_DEVICE

    if f["is_balance_drain"]:
        components["balance_drain"] = SCORE_BALANCE_DRAIN

    if f["is_odd_hour"]:
        components["odd_hour"] = SCORE_ODD_HOUR

    product = 1.0
    for s in components.values():
        product *= (1.0 - s)
    score = round(min(1.0 - product, 1.0), 4)
    return score, components, triggered_rule


def predict(transaction: dict, account_history: dict | None = None) -> dict:
    """
    Deterministic prediction for one transaction.

    Returns the L2 sub-check contract shape plus a binary `label` field
    (1 = SUSPICIOUS / fired, 0 = NORMAL) used directly by the F1 harness.
    """
    f = extract_features(transaction, account_history)
    score, components, rule = score_from_features(f)
    fired = score >= FIRED_THRESHOLD

    return {
        "check": "C6_GEO_ANOMALY",
        "predictor": "deterministic",
        "label": 1 if fired else 0,
        "fired": fired,
        "score": score,
        "triggered_rule": rule if fired else None,
        "evidence": {
            "score_components": components,
            "features": f,
            "fired_threshold": FIRED_THRESHOLD,
        },
    }
