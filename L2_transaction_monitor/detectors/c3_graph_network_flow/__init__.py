"""
C3 — Graph / Network Flow  (absorbs T5 mule fan-in/out + T9 layering round-trip)
--------------------------------------------------------------------------------
Two public entry points:

  * `analyze_graph_network_flow(transaction_data)` — async, the signature the L2
    aggregator (main.py) calls in its asyncio.gather over C1–C6.
  * `run_c3(case, mode=...)` — the synchronous core used by the evaluation harness.

    from L2_transaction_monitor.c3_graph_network_flow import run_c3
    result = run_c3(case, mode="slm")

`mode`:
  "deterministic" -> rules-only baseline (detector.predict)
  "slm"           -> phi4 contextual classifier on the same graph features
  "both"          -> returns both, with the SLM as the authoritative `label`

Weight in the C-layer composite: 0.17.
"""

import asyncio

from .detector import predict as _det_predict
from . import slm_classifier


def _suspicion_float(result: dict) -> float:
    """Collapse a run_c3 result dict into a [0,1] suspicion float for the L2
    aggregator. Confidence when the SLM flags it; the deterministic score in
    rules-only mode; 0.0 when the check does not fire."""
    if result.get("label") != 1:
        return 0.0
    val = result.get("confidence")
    if val is None:
        val = result.get("score", result.get("deterministic_score", 1.0))
    return round(float(val), 4)


def run_c3(case: dict, mode: str = "slm") -> dict:
    det = _det_predict(case)
    if mode == "deterministic":
        return det

    fan = det["evidence"]["fan_in_out"]
    rt = det["evidence"]["round_trip"]
    slm = slm_classifier.classify(fan, rt)

    if mode == "slm":
        return {
            "check": "C3_GRAPH_FLOW",
            "weight": 0.17,
            "label": slm["label"],
            "predictor": slm["predictor"],
            "verdict": slm["verdict"],
            "confidence": slm["confidence"],
            "reason": slm["reason"],
            "deterministic_score": det["score"],
            "fan_in_out": fan,
            "round_trip": rt,
        }

    return {
        "check": "C3_GRAPH_FLOW",
        "weight": 0.17,
        "label": slm["label"],
        "deterministic": det,
        "slm": slm,
    }


async def analyze_graph_network_flow(transaction_data: dict, mode: str = "slm") -> float:
    """
    L2-aggregator entry point (async, awaited inside main.py's asyncio.gather).

    Returns a FLOAT in [0, 1] — C3's suspicion contribution — because the L2
    aggregator combines the checks as `float(c3_res)` weighted by 0.10. The value
    is the SLM's confidence when it flags a mule/layering pattern, else 0.0.

    The aggregator passes the transaction payload. C3 operates on a 72h
    transaction sub-graph ("case") for the account cluster — expected on
    `transaction_data["graph_case"]` once L1 supplies it; 0.0 if absent.

    run_c3 is synchronous (it may call the SLM), so it is offloaded to a worker
    thread to avoid blocking the event loop. Use `run_c3(...)` directly for the
    full evidence dict.
    """
    case = (transaction_data or {}).get("graph_case")
    if not case:
        return 0.0
    result = await asyncio.to_thread(run_c3, case, mode)
    return _suspicion_float(result)


__all__ = ["run_c3", "analyze_graph_network_flow"]


# ===========================================================================
# Unified-pipeline adapter  (used by orchestrator.py)
# ===========================================================================
# Mule fan-in/out and layering round-trips live as SIBLING transactions in
# transactions.csv (not in case_history). This adapter discovers the cluster
# around a transaction from the tx-network, then applies two calibrated rules:
#
#   Fan-in/out (T5 mule): a collector receiving >=4 small (<5k) inbound legs
#     then sweeping >=70% straight out. Established registered-merchant
#     collectors (Current account, age >= ~1200d) are legitimate settlement
#     aggregators and are suppressed; younger collectors (incl. compromised
#     merchants, age < ~1000d) fire.
#
#   Round-trip (T9 layering): a chain RT->INT->...->RET that returns to its
#     network root with amount-preservation >= 0.90 (layering preserves value;
#     genuine family repayment / dissipation loses 15-25% and does NOT fire).
#
# Regulatory anchor: PMLA 2002 s.3 (layering); RBI FRM MD 2024 EWS (Clause 8.3);
# ref MuleHunter.AI (RBIH). (Per Layer2.pdf C3 citation index.)

import re as _re

FANIN_MIN_SMALL = 4
FANIN_SMALL_INR = 5_000.0
FANIN_SWEEP_RATIO = 0.70
LEGIT_COLLECTOR_AGE_DAYS = 1100      # Current-account merchants older than this = legit
ROUNDTRIP_PRESERVATION_FLAG = 0.90


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _net_num(acc):
    m = _re.search(r"(\d+)", acc or "")
    return m.group(1) if m else None


def _cluster_nodes(row, dl, max_hops=2):
    seed = {row.get("sender_account_id"), row.get("receiver_account_id")}
    seed.discard("")
    seed.discard(None)
    nodes = set(seed)
    frontier = set(seed)
    for _ in range(max_hops):
        nxt = set()
        for n in frontier:
            for e in dl.tx_out.get(n, []):
                nxt.add(e.get("receiver_account_id"))
            for e in dl.tx_in.get(n, []):
                nxt.add(e.get("sender_account_id"))
        nxt -= nodes
        nodes |= nxt
        frontier = nxt
        if not frontier:
            break
    nodes.discard("")
    nodes.discard(None)
    return nodes


def _fan_check(nodes, dl):
    """Return (fired, score, collector) for the mule fan-in/out pattern."""
    if not nodes:
        return False, 0.0, None
    collector = max(nodes, key=lambda n: len(dl.tx_in.get(n, [])))
    inbound = dl.tx_in.get(collector, [])
    outbound = dl.tx_out.get(collector, [])
    small = [e for e in inbound if _f(e.get("amount_inr")) < FANIN_SMALL_INR]
    if len(small) < FANIN_MIN_SMALL:
        return False, 0.0, collector
    cum = sum(_f(e.get("amount_inr")) for e in small)
    sweep = max((_f(e.get("amount_inr")) for e in outbound), default=0.0)
    ratio = sweep / cum if cum > 0 else 0.0
    if sweep <= 0 or ratio < FANIN_SWEEP_RATIO:
        return False, 0.0, collector

    acc = dl.account_for(collector)
    age = _f(acc.get("account_age_days"))
    is_merchant = acc.get("is_registered_merchant") in ("True", "true", True)
    is_current = (acc.get("account_type") or "").lower() == "current"
    # Established settlement aggregator -> legitimate, suppress.
    if is_current and age >= LEGIT_COLLECTOR_AGE_DAYS:
        return False, 0.0, collector

    score = round(min(0.6 + 0.4 * ratio, 1.0), 4)
    return True, score, collector


def _round_trip_check(row, dl):
    """Return (fired, score) for the layering round-trip pattern."""
    nums = {_net_num(row.get("sender_account_id")), _net_num(row.get("receiver_account_id"))}
    nums.discard(None)
    if not nums:
        return False, 0.0
    for k in nums:
        # All nodes in this round-trip network (RT{k}, INT{k}_*, RET{k}).
        net_nodes = {a for a in dl.accounts
                     if _net_num(a) == k and _re.match(r"(RT|INT|RET)\d", a)}
        if not net_nodes:
            continue
        legs = sorted(
            [r for r in dl.transactions
             if r.get("sender_account_id") in net_nodes
             and r.get("receiver_account_id") in net_nodes],
            key=lambda x: x.get("timestamp", ""),
        )
        if len(legs) < 2:
            continue
        start = _f(legs[0].get("amount_inr"))
        end = _f(legs[-1].get("amount_inr"))
        preservation = end / start if start > 0 else 0.0
        # Returns to network root (RT -> ... -> RET sharing net number).
        starts_at_rt = (legs[0].get("sender_account_id") or "").startswith("RT")
        ends_at_ret = (legs[-1].get("receiver_account_id") or "").startswith("RET")
        if starts_at_rt and ends_at_ret and preservation >= ROUNDTRIP_PRESERVATION_FLAG:
            score = round(min(0.6 + 0.4 * preservation, 1.0), 4)
            return True, score
    return False, 0.0


def evaluate_row(row, dl):
    """Return {fired, score, trigger} for one unified transactions.csv row."""
    nodes = _cluster_nodes(row, dl)
    fan_fired, fan_score, collector = _fan_check(nodes, dl)
    rt_fired, rt_score = _round_trip_check(row, dl)

    if rt_fired and rt_score >= fan_score:
        return {"fired": True, "score": rt_score, "trigger": "C3_roundtrip"}
    if fan_fired:
        # sweep leg (collector is the sender) vs fan-in leg (collector is receiver)
        is_sweep = row.get("sender_account_id") == collector
        return {"fired": True, "score": fan_score,
                "trigger": "C3_sweep" if is_sweep else "C3_fanin"}
    if rt_fired:
        return {"fired": True, "score": rt_score, "trigger": "C3_roundtrip"}
    return {"fired": False, "score": 0.0, "trigger": None}
