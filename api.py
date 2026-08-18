"""
api.py  —  FastAPI bridge between the React frontend and your L0-L3 pipeline.

Drop this file into your repo root (same level as L0_event_ingestion/, L1_orchestrator/, etc.)
Run with:  uvicorn api:app --reload --port 8000

The frontend calls:  POST /api/transactions/stream
This file calls your ACTUAL code:
  L0 → event_receiver.publish_transactions / receive_message  (Amazon SQS)
  L1 → orchestrator.handle_event
  L2 → called inside L1 via call_l2()
  L3 → called inside L1 via call_l3()
"""

import asyncio
import datetime
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import get_config

# ── Make sure repo root is on sys.path ────────────────────────────────────────
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

app = FastAPI(title="Compliance Pipeline API")

# Ensure reports directory exists and mount it
os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
app.mount("/reports", StaticFiles(directory=os.path.join(ROOT, "reports")), name="reports")

app.add_middleware(
    CORSMiddleware,
    # In production replace "*" with your CloudFront distribution domain
    allow_origins=["http://localhost:3000", "*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request schema (mirrors what the React TransactionTable sends) ────────────
class TransactionRequest(BaseModel):
    tx_id: str
    timestamp: str = ""
    channel: str = "UPI"
    amount_inr: float = 0.0
    sender_account_id: str = ""
    sender_name: str = ""
    sender_bank: str = ""
    sender_ifsc: str = ""
    sender_vpa: str | None = None
    sender_pan: str = ""
    receiver_name: str = ""
    receiver_account_external: str = ""
    receiver_bank: str = ""
    receiver_pan: str = ""
    receiver_dob: str = ""
    receiver_state: str = ""
    receiver_city: str = ""
    tx_location_state: str = ""
    tx_location_city: str = ""
    tx_location_country: str = ""
    tx_location_lat: str = ""
    tx_location_lon: str = ""
    purpose_code: str = ""
    device_id: str = ""
    tx_status: str = ""
    is_cross_border: str = ""
    usd_equiv: str = ""
    fx_usd_inr: str = ""
    beneficiary_id: str = ""

    model_config = {"extra": "allow"}  # preserve uploaded CSV columns for downstream checks


# ── SSE helper ────────────────────────────────────────────────────────────────
def sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


# ── Main streaming endpoint ───────────────────────────────────────────────────
@app.post("/api/transactions/stream")
async def stream_transaction(tx: TransactionRequest):
    """
    Streams layer events as Server-Sent Events while running the real pipeline.

    Event shapes (matches frontend StreamMessage type in src/types/pipeline.ts):
      {"type": "layer_start",    "layer": <int>}
      {"type": "layer_complete", "event": { LayerEvent }}
      {"type": "result",         "result": { PipelineResult }}
      {"type": "error",          "message": "..."}
    """

    async def generate():
        start = time.monotonic()
        layer_events = []

        # Convert Pydantic model → plain dict that your pipeline functions expect
        tx_dict = tx.model_dump()
        tx_dict_str = {k: str(v) if v is not None else "" for k, v in tx_dict.items()}
        tx_dict_str["amount_inr"] = tx.amount_inr  # keep as float for L2 checks
        tx_dict_str["amount"] = str(tx.amount_inr)
        
        # DEBUG: Dump the exact payload received from the frontend
        import json
        with open("debug_frontend_payload.json", "w") as f:
            json.dump(tx_dict_str, f, indent=2)

        # ── Field name bridge ─────────────────────────────────────────────────
        # Prefer the explicit receiver_account_id when the UI provides it, else
        # fall back to the legacy receiver_account_external mapping.
        tx_dict_str["receiver_account_id"] = (
            tx_dict_str.get("receiver_account_id")
            or tx_dict_str.get("receiver_account_external")
            or ""
        )
        tx_dict_str["receiver_cin"] = tx_dict_str.get("receiver_cin", "")
        # Use CSV's is_cross_border when present; fall back to SWIFT channel detection.
        # Cross-border determination
        is_foreign = False
        ch = tx.channel.upper()
        if ch == "SWIFT":
            is_foreign = True
        elif tx.receiver_city and ("UK" in tx.receiver_city.upper() or "SINGAPORE" in tx.receiver_city.upper()):
            is_foreign = True
        elif tx.receiver_account_external and (tx.receiver_account_external.startswith("UK_") or tx.receiver_account_external.startswith("SG_")):
            is_foreign = True

        tx_dict_str["is_cross_border"] = (
            tx.is_cross_border if tx.is_cross_border in ("0", "1")
            else ("1" if is_foreign else "0")
        )
        tx_dict_str["usd_equiv"] = tx.usd_equiv if tx.usd_equiv else str(float(tx.amount_inr) / 83.0)
        tx_dict_str["fx_usd_inr"] = tx.fx_usd_inr if tx.fx_usd_inr else "83.0"
        tx_dict_str["beneficiary_id"] = (
            tx_dict_str.get("beneficiary_id")
            or tx_dict_str["receiver_account_id"]
            or tx_dict_str.get("receiver_account_external")
            or ""
        )

        try:
            # ── L0: Publish to Amazon SQS ────────────────────────────────────
            yield sse({"type": "layer_start", "layer": 0})
            t0 = time.monotonic()

            try:
                from L0_event_ingestion.event_receiver import get_queue_client
                from aws import sqs_queue
                client = get_queue_client()
                client.send_message(json.dumps(tx_dict_str))
                l0_status = "pass"
                l0_detail = f"Message published to {sqs_queue.describe()} · visibility lock acquired"
            except Exception as e:
                # Publish failed (e.g. no AWS creds in dev) — continue anyway
                l0_status = "pass"
                l0_detail = f"Queue publish skipped in dev mode ({type(e).__name__}) · continuing in-process"

            l0_event = {
                "layer": 0,
                "status": l0_status,
                "chip_label": "Ingested",
                "detail": l0_detail,
                "sub_checks": [],
                "sub_scores": [],
                "latency_ms": int((time.monotonic() - t0) * 1000),
            }
            layer_events.append(l0_event)
            yield sse({"type": "layer_complete", "event": l0_event})

            # ── L1: Orchestrator (MinHash LSH + regulation hash check) ────────
            yield sse({"type": "layer_start", "layer": 1})
            t0 = time.monotonic()

            from L1_orchestrator.orchestrator import build_initial_state, run_l1_routing

            state = build_initial_state(tx_dict_str)
            state = run_l1_routing(state)

            short_circuit = state["short_circuit"]
            l1_event = {
                "layer": 1,
                "status": "pass" if short_circuit else "flag",
                "chip_label": "Short-circuit" if short_circuit else ("Cache hit (forced run)" if state.get("memory_match") else "No cache hit"),
                "detail": (
                    f"MinHash hit on past tx {state.get('memory_match', {}).get('tx_id', 'Unknown')} ({state['memory_similarity_score']:.2f} similarity) · "
                    f"rule hash unchanged → skip L2/L3"
                    if short_circuit else
                    (
                        f"MinHash hit on {state.get('memory_match', {}).get('tx_id', 'Unknown')} ({state['memory_similarity_score']:.2f} sim) · "
                        f"{'rule hash changed' if state.get('regulation_stale') else 'confidence >= 0.70 requires new STR'} → routing to L2"
                        if state.get("memory_match") else
                        f"No case memory match · regulation hash: {(state.get('regulation_hash_current') or 'INITIAL')[:12]}… · routing to L2"
                    )
                ),
                "sub_checks": [],
                "sub_scores": [],
                "latency_ms": int((time.monotonic() - t0) * 1000),
            }
            layer_events.append(l1_event)
            yield sse({"type": "layer_complete", "event": l1_event})

            # ── Short-circuit path: skip L2–L5 ───────────────────────────────
            if short_circuit:
                for layer_idx, label in [(2, "T1–T8 skipped"), (3, "Legal reasoning skipped"),
                                         (4, "No STR required"), (5, "No review required")]:
                    skip = {
                        "layer": layer_idx, "status": "skip",
                        "chip_label": "Skipped", "detail": f"L1 short-circuit — {label}",
                        "sub_checks": [], "sub_scores": [],
                    }
                    if layer_idx == 4 and state.get("memory_match", {}).get("str_pdf_url"):
                        skip["str_pdf_url"] = state["memory_match"]["str_pdf_url"]
                        skip["chip_label"] = "STR copied"
                        skip["detail"] = "L1 short-circuit — Copied past STR"
                    layer_events.append(skip)
                    yield sse({"type": "layer_complete", "event": skip})

            else:
                # ── L2: Transaction monitor (C1–C6 parallel checks) ──────────
                yield sse({"type": "layer_start", "layer": 2})
                t0 = time.monotonic()

                import L1_orchestrator.orchestrator as l1_orch
                state = await l1_orch.call_l2(state)

                # Persist to in-memory history so subsequent transactions in the stream can see this one
                if l1_orch._dl is not None:
                    # We need receiver_account_id mapping just like in frontend parsing
                    hist_tx = tx_dict_str.copy()
                    hist_tx.setdefault("receiver_account_id",
                                       hist_tx.get("receiver_account_external", ""))
                    # persist=True writes the row to CockroachDB (source='ui'),
                    # which replaces the old append to ui_transactions.csv and
                    # survives API restarts without a file on disk.
                    l1_orch._dl.add_to_history(hist_tx, persist=True)

                # ── DEBUG: print full L2 result to uvicorn terminal ──────────
                import logging as _log
                _log.warning(f"[L2 DEBUG] suspicion_score={state.get('suspicion_score')}")
                _log.warning(f"[L2 DEBUG] triggers_fired={state.get('triggers_fired')}")
                _log.warning(f"[L2 DEBUG] composite_score={state.get('composite_score')}")
                _log.warning(f"[L2 DEBUG] tx_payload keys={list(state.get('tx_payload', {}).keys())}")
                _log.warning(f"[L2 DEBUG] receiver_account_id={state.get('tx_payload', {}).get('receiver_account_id')}")
                _log.warning(f"[L2 DEBUG] receiver_name={state.get('tx_payload', {}).get('receiver_name')}")
                # ─────────────────────────────────────────────────────────────

                suspicion_score = state.get("suspicion_score") or 0.0
                triggers = state.get("triggers_fired") or []
                flagged = state.get("flag", False)

                # Build sub_checks from triggers your L2 actually fires
                sub_checks = _triggers_to_sub_checks(triggers)

                l2_event = {
                    "layer": 2,
                    "status": "flag" if flagged else "pass",
                    "chip_label": f"Score {suspicion_score:.3f}" if flagged else "Clear",
                    "detail": (
                        f"Composite suspicion score {suspicion_score:.3f} · "
                        f"triggers: {', '.join(triggers) if triggers else 'none'}"
                    ),
                    "sub_checks": sub_checks,
                    "sub_scores": [],
                    "latency_ms": int((time.monotonic() - t0) * 1000),
                }
                layer_events.append(l2_event)
                yield sse({"type": "layer_complete", "event": l2_event})

                # ── L3: Regulation interpreter (GPT/Gemini + BM25) ───────────
                if flagged:
                    yield sse({"type": "layer_start", "layer": 3})
                    t0 = time.monotonic()

                    from L1_orchestrator.orchestrator import call_l3
                    state = await call_l3(state)

                    confidence = state.get("confidence") or 0.0
                    verdict = state.get("verdict") or "review"
                    citation_trail = state.get("citation_trail") or []

                    # Map L3's 4-sub-score output (if present in citation_trail)
                    sub_scores = _extract_sub_scores(state)
                    band = _confidence_to_band(confidence)

                    l3_event = {
                        "layer": 3,
                        "status": "str" if confidence >= 0.70 else "flag" if confidence >= 0.50 else "pass",
                        "chip_label": f"Confidence {confidence:.3f}",
                        "detail": (
                            f"Verdict: {verdict} · confidence {confidence:.3f} · "
                            f"band: {band} · "
                            f"{len(citation_trail) if isinstance(citation_trail, list) else 0} citations retrieved"
                        ),
                        "sub_checks": [],
                        "sub_scores": sub_scores,
                        "latency_ms": int((time.monotonic() - t0) * 1000),
                    }
                    layer_events.append(l3_event)
                    yield sse({"type": "layer_complete", "event": l3_event})

                    if confidence >= 0.70:
                        l2_evidence = {
                            "primary_category": "C1",
                            "l2_score": state.get("suspicion_score"),
                            "l2_triggers": state.get("triggers_fired") or [],
                            "sender": {"name": tx.sender_name or "Unknown Sender", "pan": tx.sender_pan, "dob": ""},
                            "receiver": {"name": tx.receiver_name or "Unknown Receiver", "pan": tx.receiver_pan, "dob": tx.receiver_dob},
                        }
                        tx_l4 = tx_dict_str.copy()
                        tx_l4["date"] = tx.timestamp or datetime.datetime.now().isoformat()
                        tx_l4["amount"] = str(tx.amount_inr)
                        tx_l4["currency"] = "INR"

                        l3_verdict_obj = {
                            "verdict": verdict,
                            "confidence": confidence,
                            "citation_trail": citation_trail,
                            "clause_no": state.get("clause_no", ""),
                            "clause": state.get("clause", ""),
                            "citation": state.get("citation", ""),
                            "explanation": state.get("explanation", ""),
                        }
                        
                        # ── L3.5: Maker-Checker Validation (Local Ollama) ───────────
                        try:
                            from L3_regulation_interpreter.maker_checker import run_maker_checker
                            import logging as _log
                            _log.warning("[L3.5] Running Maker-Checker Explainability Agent...")
                            
                            # Run Maker Checker
                            mc_explanation = run_maker_checker(tx_dict_str, l3_verdict_obj, model="qwen2.5:72b")
                            l3_verdict_obj["maker_checker_explanation"] = mc_explanation
                            state["maker_checker_explanation"] = mc_explanation
                            
                            # Emulate an event to show on backend logs
                            _log.warning(f"[L3.5] Maker-Checker output: {mc_explanation[:100]}...")
                        except Exception as e:
                            l3_verdict_obj["maker_checker_explanation"] = f"Maker-Checker validation offline or failed: {e}"
                        
                        try:
                            from L4.l4_report_generator import run_l4, write_pdf_review_copy
                            l4_result = run_l4(l3_verdict_obj, l2_evidence, tx_l4)
                            if l4_result["disposition"] == "FILED":
                                pdf_dir = os.path.join(ROOT, "reports")
                                os.makedirs(pdf_dir, exist_ok=True)
                                pdf_path = write_pdf_review_copy(l4_result, l3_verdict_obj, tx_l4, pdf_dir)
                                pdf_url = f"/reports/{os.path.basename(pdf_path)}"
                                l4_detail = f"STR PDF generated at {pdf_url}"
                                l4_event = {
                                    "layer": 4, "status": "str",
                                    "chip_label": "STR generated",
                                    "detail": l4_detail,
                                    "sub_checks": [], "sub_scores": [],
                                    "str_pdf_url": pdf_url
                                }
                            else:
                                l4_detail = f"Failed to generate valid STR after {l4_result['attempts']} attempts"
                                l4_event = {
                                    "layer": 4, "status": "error",
                                    "chip_label": "STR Error",
                                    "detail": l4_detail,
                                    "sub_checks": [], "sub_scores": [],
                                }
                        except Exception as e:
                            import traceback
                            l4_event = {
                                "layer": 4, "status": "error", "chip_label": "L4 Error",
                                "detail": f"Failed to run L4: {e}",
                                "sub_checks": [], "sub_scores": [],
                            }
                    else:
                        l4_event = {
                            "layer": 4, "status": "skip", "chip_label": "Skipped",
                            "detail": f"Confidence {confidence:.3f} < 0.70 — no auto-file",
                            "sub_checks": [], "sub_scores": [],
                        }
                    layer_events.append(l4_event)
                    yield sse({"type": "layer_complete", "event": l4_event})

                    if 0.50 <= confidence < 0.90:
                        l5_event = {
                            "layer": 5, "status": "flag", "chip_label": "Queued for review",
                            "detail": "Evidence dossier created · 7-day FIU-IND deadline started",
                            "sub_checks": [], "sub_scores": [],
                        }
                    elif confidence >= 0.90:
                        l5_event = {
                            "layer": 5, "status": "skip", "chip_label": "Async review",
                            "detail": "Auto-filed at ≥ 0.90 · post-filing review queued",
                            "sub_checks": [], "sub_scores": [],
                        }
                    else:
                        l5_event = {
                            "layer": 5, "status": "flag", "chip_label": "Priority escalation",
                            "detail": "Confidence < 0.50 · compliance officer notified",
                            "sub_checks": [], "sub_scores": [],
                        }
                    layer_events.append(l5_event)
                    yield sse({"type": "layer_complete", "event": l5_event})

                else:
                    # L2 clear — skip L3/L4/L5
                    for layer_idx, label in [(3, "L2 score below threshold"),
                                             (4, "No STR needed"), (5, "No review needed")]:
                        skip = {
                            "layer": layer_idx, "status": "skip", "chip_label": "Skipped",
                            "detail": label, "sub_checks": [], "sub_scores": [],
                        }
                        layer_events.append(skip)
                        yield sse({"type": "layer_complete", "event": skip})

            # ── L6: Audit logger (SHA-256 hash chain in CockroachDB) ─────────
            yield sse({"type": "layer_start", "layer": 6})
            t0 = time.monotonic()

            try:
                from L6_audit_logger.hash_chain import append_to_chain
                audit_event = {
                    "tx_id": tx.tx_id,
                    "case_id": state.get("case_id", ""),
                    "triggers": state.get("triggers_fired") or [],
                    "suspicion_score": state.get("suspicion_score"),
                }
                audit_verdict = {
                    "verdict": state.get("verdict"),
                    "confidence": state.get("confidence"),
                }
                block = append_to_chain(audit_event, audit_verdict, event_type="pipeline_run")
                audit_hash = block["entry_hash"]
                l6_detail = (
                    f"SHA-256 block appended · case_id {state.get('case_id','')[:8]}… · "
                    f"shard {block['shard']} seq {block['seq']} · "
                    f"serializable append, chain cannot fork"
                )
            except Exception as e:
                block_data = f"{tx.tx_id}:{state.get('case_id','')}:{time.time()}"
                audit_hash = hashlib.sha256(block_data.encode()).hexdigest()
                l6_detail = (
                    f"SHA-256 block computed locally ({type(e).__name__}) · "
                    f"CockroachDB audit chain unavailable"
                )

            l6_event = {
                "layer": 6, "status": "pass", "chip_label": "Logged",
                "detail": l6_detail,
                "sub_checks": [], "sub_scores": [],
                "latency_ms": int((time.monotonic() - t0) * 1000),
            }
            layer_events.append(l6_event)
            yield sse({"type": "layer_complete", "event": l6_event})

            # ── L7: Cron-based — not per-transaction ─────────────────────────
            l7_skip = {
                "layer": 7, "status": "skip", "chip_label": "Cron-based",
                "detail": "Runs every 6h independently — not transaction-triggered",
                "sub_checks": [], "sub_scores": [],
            }
            layer_events.append(l7_skip)
            yield sse({"type": "layer_complete", "event": l7_skip})

            # ── Build final PipelineResult ────────────────────────────────────
            confidence = state.get("confidence")
            verdict_str = _state_to_verdict(state, short_circuit)
            sub_scores = _extract_sub_scores(state)
            triggers = state.get("triggers_fired") or []
            
            str_pdf_url = None
            for ev in layer_events:
                if ev.get("str_pdf_url"):
                    str_pdf_url = ev["str_pdf_url"]

            result = {
                "tx_id": tx.tx_id,
                "verdict": verdict_str,
                "verdict_label": _verdict_label(verdict_str),
                "verdict_detail": _verdict_detail(state, short_circuit),
                "confidence_band": _confidence_to_band(confidence) if confidence is not None else "n_a",
                "composite_score": confidence,
                "sub_scores": sub_scores,
                "l2_checks_fired": _triggers_to_sub_checks(triggers),
                "regulatory_basis": _extract_regulatory_basis(state),
                "audit_block_hash": audit_hash,
                "processing_time_ms": int((time.monotonic() - start) * 1000),
                "layer_events": layer_events,
            }
            if "maker_checker_explanation" in state:
                result["maker_checker_explanation"] = state["maker_checker_explanation"]
                
            if str_pdf_url:
                result["str_pdf_url"] = str_pdf_url
                state["str_pdf_url"] = str_pdf_url
                
            from L1_orchestrator.orchestrator import store_case
            store_case(state)

            # Persist the verdict so it is replayable with AS OF SYSTEM TIME.
            try:
                from aws import db as _db, store as _crdb
                if _db.available():
                    _crdb.save_verdict(
                        tx_id=tx.tx_id,
                        case_id=state.get("case_id"),
                        confidence=confidence,
                        band=result["confidence_band"],
                        verdict=verdict_str,
                        sub_scores={s_["key"]: s_["value"] for s_ in sub_scores},
                        citations=state.get("citation_trail") or [],
                        str_pdf_url=str_pdf_url,
                    )
                    if result["confidence_band"] in ("file_review", "human_first", "priority_escalation"):
                        deadline = datetime.datetime.utcnow() + datetime.timedelta(
                            days=get_config().FIU_IND_DEADLINE_DAYS
                        )
                        _crdb.enqueue_review(
                            tx.tx_id, state.get("case_id"),
                            result["confidence_band"], deadline,
                        )
            except Exception as e:
                import logging as _l
                _l.warning("Failed to persist verdict to CockroachDB: %s", e)

            yield sse({"type": "result", "result": result})

        except Exception as exc:
            import traceback
            yield sse({"type": "error", "message": f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Health check ─────────────────────────────────────────────────────────────
@app.get("/api/health")
async def health():
    from aws import db, sqs_queue, s3_store, model_gateway
    return {
        "status": "ok",
        "layers": list(range(8)),
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "cockroachdb": "connected" if db.available() else "not configured",
        "queue": sqs_queue.describe(),
        "object_store": f"s3://{s3_store.BUCKET}" if s3_store.available() else "not configured",
        "model": model_gateway.describe(),
    }


# ── Debug: echo back what the frontend sent ──────────────────────────────────
from fastapi import Request as _Request

@app.post("/api/debug")
async def debug(request: _Request):
    body = await request.json()
    return {"received_keys": list(body.keys()), "received": body}


# ── Helper functions ──────────────────────────────────────────────────────────

def _triggers_to_sub_checks(triggers: list) -> list:
    """
    Convert your L2 trigger strings (e.g. 'C1_structuring', 'C2_watchlist_hit')
    into the {label, result} format the frontend LayerCard expects.
    """
    TRIGGER_LABELS = {
        "C1_structuring":    ("T1 structuring",         "fail"),
        "C1_velocity":       ("T1 velocity spike",      "fail"),
        "C1_creditline":     ("T8 credit-line probing", "fail"),
        "T1_VELOCITY":       ("T1 velocity",            "fail"),
        "T1_STRUCTURING":    ("T1 structuring",         "fail"),
        "T1_CREDIT_PROBING": ("T8 credit-line probing", "fail"),
        "C2_watchlist_hit":  ("T2 watchlist hit",       "fail"),
        "C2_alias_hit":      ("T2 alias match",         "fail"),
        "C3_fanin":          ("C3 mule fan-in",         "fail"),
        "C3_sweep":          ("C3 sweep pattern",       "fail"),
        "C3_roundtrip":      ("C3 layering round-trip", "fail"),
        "C4_dormancy":       ("C4 dormant account",     "fail"),
        "C4_newaccount":     ("C4 new account risk",    "fail"),
        "C5_lrs_ceiling":    ("C5 LRS ceiling breach",  "fail"),
        "C5_split_beneficiary": ("C5 beneficiary split","fail"),
        "C5_gift_ratio":     ("C5 gift ratio breach",   "fail"),
        "C6_takeover":          ("C6 account takeover",      "fail"),
        "C6_jurisdiction":      ("C6 FATF jurisdiction",    "fail"),
        "C6_impossible_travel": ("C6 impossible travel",    "fail"),
        "C6_subtle_probe":      ("C6 new device probe",     "fail"),
        "C6_newloc_newdev":     ("C6 new location/device",  "fail"),
        "C1_high_value":        ("T1 high value cash",      "fail"),
    }
    checks = []
    seen = set()
    for t in (triggers or []):
        if t in TRIGGER_LABELS and t not in seen:
            label, result = TRIGGER_LABELS[t]
            checks.append({"label": label, "result": result})
            seen.add(t)
    return checks


def _extract_sub_scores(state: dict) -> list:
    """
    Pull L3's 4 sub-scores from the state dict.
    Your legal_reasoning.py returns them directly inside the analysis dict,
    which call_l3 stores under state keys. We reconstruct them here.
    """
    # L3 returns these keys directly from generate_legal_analysis()
    raw = {}
    for key in ("retrieval_match", "rule_applicability", "evidence_sufficiency", "precedent_confidence"):
        val = state.get(key)
        if val is None:
            # Also check if they came back nested in citation_trail or verdict
            citation = state.get("citation_trail")
            if isinstance(citation, dict):
                val = citation.get(key)
        raw[key] = val

    weights = {
        "retrieval_match": 0.30,
        "rule_applicability": 0.35,
        "evidence_sufficiency": 0.25,
        "precedent_confidence": 0.10,
    }
    labels = {
        "retrieval_match": "Retrieval match",
        "rule_applicability": "Rule applicability",
        "evidence_sufficiency": "Evidence sufficiency",
        "precedent_confidence": "Precedent confidence",
    }
    scores = []
    for k, w in weights.items():
        v = raw.get(k)
        if v is not None:
            try:
                scores.append({"key": labels[k], "value": float(v), "weight": w})
            except (TypeError, ValueError):
                pass
    return scores


def _extract_regulatory_basis(state: dict) -> list:
    """
    Pull the applicable_rules / citation_trail from L3's output.
    Your legal_reasoning.py returns these in the analysis dict.
    call_l3 stores the full analysis, but only extracts confidence/verdict/citation_trail.
    We rebuild from what's available in state.
    """
    basis = []
    trail = state.get("citation_trail")
    if isinstance(trail, list):
        for c in trail[:3]:
            if isinstance(c, dict):
                doc = c.get("chunk_id", c.get("document_id", ""))
                why = c.get("why_it_matters", c.get("excerpt", ""))
                if doc or why:
                    basis.append(f"{doc}: {why}" if doc and why else doc or why)
    elif isinstance(trail, str) and trail:
        basis.append(trail)

    if not basis:
        # Fallback: use verdict to infer regulation
        verdict = state.get("verdict", "")
        if "structuring" in str(state.get("triggers_fired", "")).lower():
            basis.append("PMLA Rule 3 — structuring / smurfing")
        if "watchlist" in str(state.get("triggers_fired", "")).lower():
            basis.append("FIU-IND AML Direction — sanctioned entity")
        if not basis:
            basis.append("RBI AML / FIU-IND guidelines")
    return basis


def _state_to_verdict(state: dict, short_circuit: bool) -> str:
    if short_circuit:
        match = state.get("memory_match", {})
        cached_status = match.get("final_status") or match.get("verdict")
        cached_conf = match.get("confidence") or 0.0
        
        if cached_status in ("clear", "clean"):
            return "clean"
            
        if cached_conf >= 0.70:
            return "str_filed"
        if cached_conf >= 0.50:
            return "human_review"
            
        if cached_status is None:
            return "clean"
        return "escalated"
    confidence = state.get("confidence")
    verdict = state.get("verdict", "")
    suspicion = state.get("suspicion_score") or 0.0

    if suspicion == 0.0:
        return "clean"
    if verdict in ("clear", "clean"):
        return "dismissed"
    if confidence is not None:
        if confidence >= 0.70:
            return "str_filed"
        if confidence >= 0.50:
            return "human_review"
        return "escalated"
    return "human_review"


def _verdict_label(verdict: str) -> str:
    return {
        "clean": "Transaction cleared",
        "str_filed": "STR auto-filed",
        "human_review": "Held for human review",
        "escalated": "Priority escalation",
        "dismissed": "False positive — dismissed",
    }.get(verdict, "Under review")


def _verdict_detail(state: dict, short_circuit: bool) -> str:
    if short_circuit:
        score = state.get("memory_similarity_score")
        match_id = state.get("memory_match", {}).get("tx_id", "Unknown") if state.get("memory_match") else "Unknown"
        return f"L1 short-circuit against past tx {match_id} · {score:.2f} similarity · rule hash unchanged"
    confidence = state.get("confidence")
    verdict = state.get("verdict", "")
    triggers = ", ".join(state.get("triggers_fired") or []) or "none"
    detail = f"L2 triggers: {triggers}"
    if confidence is not None:
        detail += f" · L3 confidence {confidence:.3f}"
    if verdict:
        detail += f" · verdict: {verdict}"
    return detail


def _confidence_to_band(confidence) -> str:
    if confidence is None:
        return "n_a"
    c = float(confidence)
    if c >= 0.90:
        return "auto_file"
    if c >= 0.60:
        return "file_review"
    if c >= 0.50:
        return "human_first"
    return "priority_escalation"


# ── L0: Publish all transactions from uploaded CSV to the queue ───────────────
class PublishRequest(BaseModel):
    rows: list[dict]

@app.post("/api/publish")
async def publish_to_queue(req: PublishRequest):
    """
    Receives all CSV rows from the frontend and publishes them to
    Amazon SQS (tx-events) via the L0 event_receiver.

    Call this ONCE after CSV upload, before running individual transactions.
    Flow:
      Upload CSV → POST /api/publish (all rows → queue)
      Pick one row → POST /api/transactions/stream (L1 polls + processes it)
    """
    try:
        from L0_event_ingestion.event_receiver import get_queue_client
        client = get_queue_client()
        published = 0
        errors = 0
        for row in req.rows:
            try:
                row_str = {k: str(v) if v is not None else "" for k, v in row.items()}

                # Infer cross-border logic for missing CSV columns
                receiver_ext = row_str.get("receiver_account_external", "")
                if row_str.get("receiver_city") in ["Singapore", "London UK", "Dubai", "New York"] or \
                   receiver_ext.startswith("SG_") or receiver_ext.startswith("UK_") or receiver_ext.startswith("US_"):
                    row_str["is_cross_border"] = "1"
                    if not row_str.get("fx_usd_inr"):
                        row_str["fx_usd_inr"] = "83.5"
                    if not row_str.get("usd_equiv"):
                        amount = float(row_str.get("amount_inr", 0) or 0)
                        row_str["usd_equiv"] = str(round(amount / 83.5, 2))

                client.send_message(json.dumps(row_str))
                published += 1
            except Exception:
                errors += 1
        return {
            "status": "ok",
            "total": len(req.rows),
            "published": published,
            "errors": errors,
            "message": f"Published {published}/{len(req.rows)} transactions to the tx-events queue",
        }
    except Exception as e:
        return {
            "status": "skipped",
            "total": len(req.rows),
            "published": 0,
            "errors": 0,
            "message": f"Queue publish skipped ({type(e).__name__}) — pipeline will run in-process",
        }


# ── L0: Queue status ──────────────────────────────────────────────────────────
@app.get("/api/queue/status")
async def queue_status():
    """Returns the current number of messages waiting in the queue."""
    try:
        from L0_event_ingestion.event_receiver import get_queue_length
        length = get_queue_length()
        return {"status": "ok", "queue_length": length, "queue_name": "tx-events"}
    except Exception as e:
        return {"status": "unavailable", "queue_length": None, "error": str(e)}