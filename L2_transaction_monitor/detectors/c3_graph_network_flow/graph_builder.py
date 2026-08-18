"""
graph_builder.py  (C3 — Graph / Network Flow)
---------------------------------------------
Builds ONE directed graph of transaction edges for a 72h window, then exposes
the traversal helpers both pattern detectors share. Built once, traversed twice
(fan-in/out and round-trip) — no second pass over CockroachDB.

A `case` is the input unit the L1 orchestrator hands to C3:
    {
      "case_id": str,
      "trigger_account": str,            # the account the cluster is centred on
      "window_hours": 72,
      "edges": [                         # directed money movements
        {"src","dst","amount_inr","timestamp","src_vpa","dst_vpa"}, ...
      ],
      "accounts": {                      # node metadata
        "ACCxxx": {"holder_name","ifsc","device_id","account_type",
                   "account_age_days","is_registered_merchant","kyc_level"},
        ...
      }
    }

POC: the case is passed in directly (built from the synthetic generator or a
CockroachDB query upstream). Production: replace `from_case` with a recursive CTE
query for the trigger account's 72h neighbourhood — the rest is unchanged.
"""

from collections import defaultdict
from datetime import datetime


class TxGraph:
    """A thin directed multigraph over account nodes with money-movement edges."""

    def __init__(self, trigger_account: str, accounts: dict):
        self.trigger = trigger_account
        self.accounts = accounts or {}
        self._out = defaultdict(list)   # src -> [edge, ...]
        self._in = defaultdict(list)    # dst -> [edge, ...]
        self.edges: list[dict] = []

    @classmethod
    def from_case(cls, case: dict) -> "TxGraph":
        g = cls(case["trigger_account"], case.get("accounts", {}))
        for e in case.get("edges", []):
            g.add_edge(e)
        return g

    def add_edge(self, edge: dict) -> None:
        e = dict(edge)
        e["amount_inr"] = float(e.get("amount_inr", 0) or 0)
        try:
            e["_ts"] = datetime.fromisoformat(e["timestamp"])
        except (ValueError, TypeError, KeyError):
            e["_ts"] = None
        self.edges.append(e)
        self._out[e["src"]].append(e)
        self._in[e["dst"]].append(e)

    def out_edges(self, node: str) -> list[dict]:
        return self._out.get(node, [])

    def in_edges(self, node: str) -> list[dict]:
        return self._in.get(node, [])

    def node(self, account_id: str) -> dict:
        return self.accounts.get(account_id, {})

    # --- shared-identity helpers (used by round-trip "circularity") ---

    def shared_attribute(self, a: str, b: str) -> str | None:
        """Return the first identity attribute shared by accounts a and b, if any."""
        na, nb = self.node(a), self.node(b)
        if na.get("device_id") and na.get("device_id") == nb.get("device_id"):
            return "device_id"
        ia, ib = (na.get("ifsc") or "")[:4], (nb.get("ifsc") or "")[:4]
        if ia and ia == ib:
            return "ifsc_prefix"
        ha = _holder_suffix(na.get("holder_name"))
        hb = _holder_suffix(nb.get("holder_name"))
        if ha and ha == hb:
            return "holder_suffix"
        return None


def _holder_suffix(name: str | None) -> str:
    """Last token of a holder name, lower-cased (e.g. 'Ravi Kumar' -> 'kumar')."""
    if not name:
        return ""
    parts = [p for p in str(name).strip().split() if p]
    return parts[-1].lower() if parts else ""
