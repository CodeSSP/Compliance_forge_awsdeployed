import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import uuid
import logging
import datetime
from config import get_config
from L0_event_ingestion.event_receiver import (
    receive_message, delete_message, get_queue_length
)
from L1_orchestrator.minhash_lsh import (
    query_case_memory, build_feature_set, store_case
)
from L1_orchestrator.regulation_hash import get_current_hash, is_stale

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [L1] %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)
config = get_config()


def build_initial_state(tx: dict) -> dict:
    """
    Builds the CaseState object that flows through all pipeline layers.
    Field names match what L2's transaction_monitor() and downstream
    layers expect.
    """
    return {
        # Core identifiers
        "tx_id":             tx["tx_id"],
        "case_id":           str(uuid.uuid4()),
        "tx_payload":        tx,
        "pipeline_start_ts": datetime.datetime.utcnow().isoformat() + "Z",

        # Audit chain — genesis block added here, each layer appends
        "audit_blocks": [{
            "block_index": 0,
            "layer":       "L0",
            "tx_id":       tx["tx_id"],
            "timestamp":   tx.get("timestamp", ""),
            "data":        {"tx_payload": tx},
            "prev_hash":   None,
        }],

        # L1 routing fields
        "memory_match":            None,
        "memory_similarity_score": None,
        "regulation_hash_current": None,
        "regulation_hash_cached":  None,
        "regulation_stale":        None,
        "short_circuit":           False,
        "route":                   None,

        # L2 fields — populated by transaction_monitor()
        "suspicion_score":  None,
        "composite_score":  None,
        "triggers_fired":   None,
        "evidence":         None,

        # L3 fields — populated by regulation interpreter
        "confidence":       None,
        "confidence_band":  None,
        "verdict":          None,
        "citation_trail":   None,

        # L4 fields
        "xml_path":         None,
        "xml_valid":        None,

        # L5 fields
        "reviewer_decision": None,
        "reviewer_id":       None,

        # Final outcome
        "final_status":      None,
        "pipeline_end_ts":   None,
    }


def run_l1_routing(state: dict) -> dict:
    """
    Core L1 logic — case memory check + regulation freshness check.
    Sets state['route'] to either 'l2' or 'l6_short_circuit'.
    """
    tx = state["tx_payload"]

    # Step 1 — check case memory for similar past transaction
    match = query_case_memory(tx)
    state["memory_match"]            = match
    state["memory_similarity_score"] = (
        match.get("_similarity_score") if match else None
    )

    # Step 2 — check regulation freshness
    current_hash = get_current_hash()
    cached_hash  = match.get("regulation_version_hash") if match else None
    stale        = is_stale(cached_hash, current_hash)

    state["regulation_hash_current"] = current_hash
    state["regulation_hash_cached"]  = cached_hash
    state["regulation_stale"]        = stale

    # Step 3 — routing decision
    cached_conf = match.get("confidence") if match else None
    if match and not stale:
        # Memory hit + regulation unchanged → short-circuit L2+L3 and reuse
        # whatever the past case decided — clear, review, or STR — instead of
        # re-running detectors and legal reasoning on a transaction the agent
        # already has a verdict for. Carry the cached verdict onto state so
        # it (a) audits accurately at L6 and (b) chains correctly if this
        # case itself becomes someone else's memory match later.
        state["short_circuit"] = True
        state["route"]         = "l6_short_circuit"
        state["final_status"]  = match.get("final_status") or match.get("verdict")
        state["verdict"]       = state["final_status"]
        state["confidence"]    = cached_conf
        log.info(
            f"SHORT-CIRCUIT: {tx['tx_id']} matched {match.get('tx_id')} "
            f"(similarity={state['memory_similarity_score']:.3f}, "
            f"hash_unchanged=True, conf={cached_conf})"
        )
    else:
        state["short_circuit"] = False
        state["route"]         = "l2"
        reason = "no memory match" if not match else "regulation hash changed"
        log.info(f"FULL PIPELINE: {tx['tx_id']} ({reason})")

    # Append L1 audit block
    state["audit_blocks"].append({
        "block_index": 1,
        "layer":       "L1",
        "tx_id":       tx["tx_id"],
        "timestamp":   datetime.datetime.utcnow().isoformat() + "Z",
        "data": {
            "case_id":                 state["case_id"],
            "short_circuit":           state["short_circuit"],
            "route":                   state["route"],
            "memory_match_tx_id":      match.get("tx_id") if match else None,
            "memory_similarity_score": state["memory_similarity_score"],
            "regulation_hash_current": current_hash,
            "regulation_hash_cached":  cached_hash,
        },
        "prev_hash": None,
    })

    return state


# Global DataLayer for L2
_dl = None

async def call_l2(state: dict) -> dict:
    """
    Calls L2 transaction_monitor with the raw transaction payload.
    L2 runs its 6 parallel checks and returns a suspicion score.
    """
    tx = state["tx_payload"]

    try:
         global _dl
         from L2_transaction_monitor.orchestrator import monitor
         from L2_transaction_monitor.data_layer import DataLayer
         
         if _dl is None:
             _dl = DataLayer()

         # The new L2 orchestrator expects the row and the DataLayer
         l2_result = await monitor(tx, _dl)
         
         state["suspicion_score"] = l2_result["suspicion_score"]
         state["composite_score"] = l2_result["suspicion_score"]
         state["triggers_fired"]  = l2_result["triggers"]
         state["l2_evidence"]     = l2_result.get("evidence", {})
         state["flag"]            = l2_result.get("flag", len(state["triggers_fired"]) > 0)
         
         # Inject into tx so L3 can use them!
         tx["l2_suspicion_score"] = state["suspicion_score"]
         tx["l2_triggers_fired"]  = state["triggers_fired"]
         tx["l2_evidence"]        = state["l2_evidence"]

    except Exception as e:
        log.error(f"L2 call failed for {tx['tx_id']}: {e}")
        state["suspicion_score"] = None

    return state


async def call_l3(state: dict) -> dict:
    """
    Calls L3 regulation_interpreter.
    Runs retrieval and analysis, and populates verdict and citations.
    """
    tx = state["tx_payload"]
    try:
        from L3_regulation_interpreter.hybrid_retrieval import search_regulations
        from L3_regulation_interpreter.legal_reasoning import generate_legal_analysis
        
        retrieval = search_regulations(tx)
        analysis = generate_legal_analysis(tx, retrieval)
        
        state["confidence"] = analysis.get("final_score")
        state["verdict"] = analysis.get("verdict")
        state["citation_trail"] = analysis.get("citation_trail")
        state["explanation"] = analysis.get("explanation")
        state["clause_no"] = analysis.get("clause_no")
        state["clause"] = analysis.get("clause")
        state["citation"] = analysis.get("citation")
    except Exception as e:
        log.error(f"L3 call failed for {tx['tx_id']}: {e}")
        state["verdict"] = "error"
        state["confidence"] = 0.0
        
    return state


async def handle_event(tx: dict) -> dict:
    """
    Main entry point — called by main.py for each transaction.
    Builds state, runs L1 routing, calls L2 if needed.
    """
    state = build_initial_state(tx)
    state = run_l1_routing(state)

    if state["route"] == "l2":
        state = await call_l2(state)
        
        # Route to L3 if L2 flagged it
        if state.get("flag"):
            log.info(f"L2 scored {state['suspicion_score']}. Routing to L3 for legal analysis.")
            state = await call_l3(state)
        else:
            state["verdict"] = "clear"
            state["confidence"] = 1.0
            state["citation_trail"] = []
            
    elif state["route"] == "l6_short_circuit":
        log.info(f"Short-circuit to L6: {tx['tx_id']}")
        # L6 audit call will go here once L6 is implemented

    # Store completed case in memory for future short-circuit matching
    store_case(state)

    log.info(
        f"DONE: {tx['tx_id']} → "
        f"route={state['route']} | "
        f"short_circuit={state['short_circuit']}"
    )
    return state


async def process_queue(max_messages: int = 10) -> list:
    """
    Reads up to max_messages from the L0 queue and processes
    each through L1. Returns list of completed CaseState dicts.
    """
    results   = []
    processed = 0

    log.info(f"Queue length: {get_queue_length()}")

    while processed < max_messages:
        result = receive_message()
        if not result:
            log.info("Queue empty.")
            break

        msg, tx = result
        try:
            state = await handle_event(tx)
            delete_message(msg)
            results.append(state)
            processed += 1
        except Exception as e:
            log.error(f"Error processing {tx.get('tx_id','?')}: {e}")

    # Summary
    short_circuits = sum(1 for r in results if r["short_circuit"])
    full_pipeline  = sum(1 for r in results if not r["short_circuit"])
    log.info(
        f"Processed {processed} messages — "
        f"short-circuit: {short_circuits} | "
        f"full pipeline: {full_pipeline}"
    )
    return results


if __name__ == "__main__":
    results = asyncio.run(process_queue(max_messages=2000))
    print(f"\n{'='*50}")
    print(f"Processed {len(results)} transactions")
    print(f"{'='*50}")
    for r in results:
        print(
            f"  {r['tx_id']:<25} "
            f"route={r['route']:<20} "
            f"short_circuit={r['short_circuit']}"
        )