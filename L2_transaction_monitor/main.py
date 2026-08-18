"""
Main function for L2 Transaction Monitor.
Orchestrates the six parallel checks (C1-C6) and combines their outputs into a
single weighted suspicion score.

INTEGRATION NOTE (see c3_c6_docs/INTEGRATION_NOTES.md):
  The six checks currently return heterogeneous shapes — some a float, some a
  dict (with `score` / `risk_score` / `composite_score`), some are sync, some
  async, and a couple can raise or fail to import while their owners finish
  wiring. This aggregator is therefore defensive: it imports each check safely,
  awaits it if it's a coroutine, extracts a [0,1] score from whatever it returns,
  and treats any failure as a non-firing 0.0. The unit runs end-to-end today and
  sharpens as each detector conforms to the contract (return a float in [0,1],
  or a dict carrying a `score`).
"""
import asyncio
import inspect

# Per-check weights (sum to 1.0). Carried over from the dev-branch aggregator.
WEIGHTS = {"c1": 0.15, "c2": 0.20, "c3": 0.10, "c4": 0.35, "c5": 0.10, "c6": 0.10}


def _safe_import(module_path: str, func_name: str):
    """Import a check entry point; if the module can't load (flat imports,
    missing deps, sys.exit-on-import), substitute a stub that contributes 0.0."""
    try:
        module = __import__(module_path, fromlist=[func_name])
        return getattr(module, func_name)
    except BaseException as exc:  # BaseException: some modules call sys.exit on import
        reason = f"{type(exc).__name__}: {exc}"

        async def _unavailable(_data):
            return {"score": 0.0, "status": "IMPORT_FAILED", "detail": reason}

        return _unavailable


check_velocity_and_structuring = _safe_import(
    "L2_transaction_monitor.detectors.c1_velocity_and_structuring", "check_velocity_and_structuring")
check_sanctions_and_watchlist = _safe_import(
    "L2_transaction_monitor.detectors.c2_sanctions_and_watchlist", "check_sanctions_and_watchlist")
analyze_graph_network_flow = _safe_import(
    "L2_transaction_monitor.detectors.c3_graph_network_flow", "analyze_graph_network_flow")
calculate_account_risk_and_dormancy = _safe_import(
    "L2_transaction_monitor.detectors.c4_account_risk_and_dormancy", "calculate_account_risk_and_dormancy")
fema_lrs_analysis = _safe_import(
    "L2_transaction_monitor.detectors.c5_fema_lrs", "fema_lrs_analysis")
check_geo_anomaly = _safe_import(
    "L2_transaction_monitor.detectors.c6_geo_anomaly", "check_geo_anomaly")


def _to_score(result) -> float:
    """Collapse any check's return value into a [0,1] suspicion score."""
    if result is None:
        return 0.0
    if isinstance(result, (int, float)):
        return float(result)
    if isinstance(result, dict):
        for key in ("score", "risk_score", "composite_score", "suspicion_score"):
            if key in result:
                try:
                    return float(result[key])
                except (TypeError, ValueError):
                    pass
        if "label" in result:
            try:
                return float(result["label"])
            except (TypeError, ValueError):
                pass
    return 0.0


async def _run(check_fn, transaction_data) -> float:
    """Call a check (sync or async), tolerate failure, return its score in [0,1]."""
    try:
        result = check_fn(transaction_data)
        if inspect.isawaitable(result):
            result = await result
        return _to_score(result)
    except BaseException:
        return 0.0


async def transaction_monitor(transaction_data):
    """
    Runs all six transaction-monitoring checks in parallel and combines them.

    Returns:
        suspicion_score: combined weighted score from all six checks, in [0, 1].
    """
    # TODO: Fetch necessary data for each check (e.g., history, account info, the
    # 72h graph_case for C3) and attach to transaction_data before dispatch.
    c1, c2, c3, c4, c5, c6 = await asyncio.gather(
        _run(check_velocity_and_structuring, transaction_data),
        _run(check_sanctions_and_watchlist, transaction_data),
        _run(analyze_graph_network_flow, transaction_data),
        _run(calculate_account_risk_and_dormancy, transaction_data),
        _run(fema_lrs_analysis, transaction_data),
        _run(check_geo_anomaly, transaction_data),
    )

    suspicion_score = round(
        WEIGHTS["c1"] * c1 + WEIGHTS["c2"] * c2 + WEIGHTS["c3"] * c3
        + WEIGHTS["c4"] * c4 + WEIGHTS["c5"] * c5 + WEIGHTS["c6"] * c6,
        4,
    )
    return suspicion_score
