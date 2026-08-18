"""
patterns.py  (C3 — Graph / Network Flow)
----------------------------------------
The two graph traversals, each returning a flat feature dict (measurement only —
no firing or scoring). Both share the single TxGraph built by graph_builder.

  fan_in_out_features  -> T5 mule signature: many tiny distinct inbound credits
                          in a 2h window, then one large outbound that sweeps
                          most of the received funds within 30 min.

  round_trip_features  -> T9 layering signature: BFS from the trigger account;
                          a path that returns to an account sharing identity with
                          the origin (device / IFSC prefix / holder surname),
                          scored by hop count and amount-preservation ratio.
"""

from collections import deque
from datetime import timedelta

from .graph_builder import TxGraph
from .thresholds import (
    FANIN_MAX_CREDIT_INR,
    FANIN_WINDOW_HOURS,
    FANOUT_WINDOW_MIN,
    ROUNDTRIP_MEASURE_DEPTH,
)


def fan_in_out_features(g: TxGraph) -> dict:
    """Measure the fan-in / fan-out signature around the trigger account."""
    trigger = g.trigger
    inbound = [e for e in g.in_edges(trigger) if e["_ts"] is not None]
    inbound.sort(key=lambda e: e["_ts"])

    best = {
        "inbound_count": 0, "distinct_vpas": 0, "all_under_5k": False,
        "cumulative_in": 0.0, "max_inbound": 0.0,
    }

    # Slide a FANIN_WINDOW_HOURS window over inbound credits; keep the densest burst.
    for i, anchor in enumerate(inbound):
        window_end = anchor["_ts"] + timedelta(hours=FANIN_WINDOW_HOURS)
        burst = [e for e in inbound[i:] if e["_ts"] <= window_end]
        if len(burst) <= best["inbound_count"]:
            continue
        vpas = {e.get("src_vpa") or e["src"] for e in burst}
        best = {
            "inbound_count": len(burst),
            "distinct_vpas": len(vpas),
            "all_under_5k": all(e["amount_inr"] < FANIN_MAX_CREDIT_INR for e in burst),
            "cumulative_in": round(sum(e["amount_inr"] for e in burst), 2),
            "max_inbound": round(max(e["amount_inr"] for e in burst), 2),
            "_burst_start": anchor["_ts"],
            "_burst_end": window_end,
        }

    # Largest outbound sweep within FANOUT_WINDOW_MIN after the burst starts.
    outbound_amount = 0.0
    fanout_within_window = False
    if best["inbound_count"] > 0:
        start = best["_burst_start"]
        deadline = start + timedelta(hours=FANIN_WINDOW_HOURS, minutes=FANOUT_WINDOW_MIN)
        for e in g.out_edges(trigger):
            if e["_ts"] is not None and start <= e["_ts"] <= deadline:
                if e["amount_inr"] > outbound_amount:
                    outbound_amount = e["amount_inr"]
                    fanout_within_window = True

    cumulative = best["cumulative_in"] or 0.0
    outbound_ratio = round(outbound_amount / cumulative, 3) if cumulative > 0 else 0.0
    node = g.node(trigger)

    return {
        "pattern": "fan_in_out",
        "trigger_account": trigger,
        "inbound_count": best["inbound_count"],
        "distinct_vpas": best["distinct_vpas"],
        "all_under_5k": best["all_under_5k"],
        "cumulative_in_inr": round(cumulative, 2),
        "max_inbound_inr": best["max_inbound"],
        "outbound_inr": round(outbound_amount, 2),
        "outbound_ratio": outbound_ratio,
        "fanout_within_window": fanout_within_window,
        # context for the SLM reasoner
        "account_age_days": node.get("account_age_days"),
        "is_registered_merchant": bool(node.get("is_registered_merchant")),
        "account_type": node.get("account_type"),
        "kyc_level": node.get("kyc_level"),
    }


def round_trip_features(g: TxGraph) -> dict:
    """
    BFS from the trigger account up to ROUNDTRIP_MEASURE_DEPTH hops; find the
    strongest path that returns to an account sharing identity with the origin.
    Amount-preservation = funds still moving at the return / amount that left.
    """
    trigger = g.trigger
    origin_out = g.out_edges(trigger)
    initial_outflow = max((e["amount_inr"] for e in origin_out), default=0.0)

    best = {
        "returns": False, "hop_count": None, "preservation": 0.0,
        "shared_attribute": None, "path": [],
    }

    # Each queue item: (current_node, depth, min_amount_along_path, path_list)
    queue = deque()
    for e in origin_out:
        queue.append((e["dst"], 1, e["amount_inr"], [trigger, e["dst"]]))

    while queue:
        node, depth, flow, path = queue.popleft()

        # A return = reaching an account that shares identity with the trigger,
        # without being the trigger's own immediate position (depth >= 2).
        if depth >= 2:
            shared = g.shared_attribute(trigger, node)
            if shared or node == trigger:
                preservation = round(flow / initial_outflow, 3) if initial_outflow > 0 else 0.0
                # Prefer shorter, higher-preservation returns.
                better = (
                    not best["returns"]
                    or depth < best["hop_count"]
                    or (depth == best["hop_count"] and preservation > best["preservation"])
                )
                if better:
                    best = {
                        "returns": True,
                        "hop_count": depth,
                        "preservation": preservation,
                        "shared_attribute": shared or "same_account",
                        "path": path,
                    }
                continue  # don't traverse past a closed loop

        if depth >= ROUNDTRIP_MEASURE_DEPTH:
            continue
        for e in g.out_edges(node):
            if e["dst"] in path:       # avoid trivial 2-cycles within the same path
                continue
            queue.append((e["dst"], depth + 1, min(flow, e["amount_inr"]), path + [e["dst"]]))

    return {
        "pattern": "round_trip",
        "trigger_account": trigger,
        "returns": best["returns"],
        "hop_count": best["hop_count"],
        "amount_preservation_ratio": best["preservation"],
        "shared_attribute": best["shared_attribute"],
        "path": best["path"],
        "initial_outflow_inr": round(initial_outflow, 2),
    }
