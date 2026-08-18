"""
C6 — Geo-Anomaly  (absorbs the old T4 geo-location anomaly check)
-----------------------------------------------------------------
Public entry point. Mirrors the L2 sub-check contract used by the aggregator.

    from L2_transaction_monitor.c6_geo_anomaly import run_c6
    result = run_c6(transaction, account_history, mode="slm")

The L2 aggregator (main.py) instead awaits `check_geo_anomaly(transaction_data)`.

`mode`:
  "deterministic" -> rules-only baseline (detector.predict)
  "slm"           -> phi4 contextual classifier on the same features
  "both"          -> returns both, with the SLM as the authoritative `label`

Weight in the C-layer composite: 0.10.
"""

import asyncio

from .detector import predict as _det_predict
from .features import extract_features
from . import slm_classifier


def _suspicion_float(result: dict) -> float:
    """Collapse a run_c6/run_c3 result dict into a [0,1] suspicion float for the
    L2 aggregator. Confidence when the SLM flags it; the deterministic score in
    rules-only mode; 0.0 when the check does not fire."""
    if result.get("label") != 1:
        return 0.0
    val = result.get("confidence")
    if val is None:
        val = result.get("score", result.get("deterministic_score", 1.0))
    return round(float(val), 4)


def run_c6(transaction: dict, account_history: dict | None = None, mode: str = "slm") -> dict:
    det = _det_predict(transaction, account_history)
    if mode == "deterministic":
        return det

    features = det["evidence"]["features"]
    slm = slm_classifier.classify(features)

    if mode == "slm":
        return {
            "check": "C6_GEO_ANOMALY",
            "weight": 0.10,
            "label": slm["label"],
            "predictor": slm["predictor"],
            "verdict": slm["verdict"],
            "confidence": slm["confidence"],
            "reason": slm["reason"],
            "deterministic_score": det["score"],
            "features": features,
        }

    # mode == "both"
    return {
        "check": "C6_GEO_ANOMALY",
        "weight": 0.10,
        "label": slm["label"],                 # SLM is authoritative
        "deterministic": det,
        "slm": slm,
        "features": features,
    }


async def check_geo_anomaly(transaction_data: dict, mode: str = "slm") -> float:
    """
    L2-aggregator entry point (async, awaited inside main.py's asyncio.gather).

    Returns a FLOAT in [0, 1] — C6's suspicion contribution — because the L2
    aggregator combines the checks as `float(c6_res)` weighted by 0.10. The value
    is the SLM's confidence when it flags a geo/device anomaly, else 0.0.

    The aggregator passes the transaction payload; the account's geo/device
    history is expected on `transaction_data["account_history"]` once L1 supplies
    it (the check degrades gracefully if it is absent).

    run_c6 is synchronous (it may call the SLM), so it is offloaded to a worker
    thread to avoid blocking the event loop. Use `run_c6(...)` directly for the
    full evidence dict.
    """
    account_history = (transaction_data or {}).get("account_history")
    result = await asyncio.to_thread(run_c6, transaction_data, account_history, mode)
    return _suspicion_float(result)


__all__ = ["run_c6", "extract_features", "check_geo_anomaly"]
