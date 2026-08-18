"""Single entry point for every model call in the pipeline.

Backends, chosen by MODEL_BACKEND:
  bedrock    Amazon Bedrock Converse API      (fastest to stand up, no endpoint to warm)
  sagemaker  Phi-4-mini on a SageMaker endpoint (keeps the self-hosted SLM story)
  ollama     local Ollama                     (offline dev and demo fallback)

Embeddings, chosen by EMBED_BACKEND:
  sagemaker  nomic-embed-text on a SageMaker endpoint
  ollama     local Ollama

Embeddings must stay on nomic-embed-text either way: the corpus in
regulatory_chunks is 768-dimensional nomic. Swapping the embedding model means
changing VECTOR(768) in the schema and re-embedding all 1445 chunks.
"""
import json
import os
import re
import logging
from urllib import request

log = logging.getLogger(__name__)

MODEL_BACKEND = os.environ.get("MODEL_BACKEND", "ollama").lower()
EMBED_BACKEND = os.environ.get("EMBED_BACKEND", "ollama").lower()

AWS_REGION = os.environ.get("AWS_REGION", "ap-south-1")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "")
SAGEMAKER_GEN_ENDPOINT = os.environ.get("SAGEMAKER_GEN_ENDPOINT", "")
SAGEMAKER_EMBED_ENDPOINT = os.environ.get("SAGEMAKER_EMBED_ENDPOINT", "")

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "phi4-mini:latest")
OLLAMA_EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")

_brt = None
_smr = None


def _bedrock():
    global _brt
    if _brt is None:
        import boto3
        _brt = boto3.client("bedrock-runtime", region_name=AWS_REGION)
    return _brt


def _sagemaker():
    global _smr
    if _smr is None:
        import boto3
        _smr = boto3.client("sagemaker-runtime", region_name=AWS_REGION)
    return _smr


def _strip_fences(text: str) -> str:
    text = str(text or "").strip()
    if text.startswith("```"):
        lines = [ln for ln in text.splitlines() if not ln.strip().startswith("```")]
        text = "\n".join(lines).strip()
    return text


# ---------------------------------------------------------------------------
# Text generation
# ---------------------------------------------------------------------------

def generate(prompt: str, system: str | None = None,
             max_tokens: int = 1024, temperature: float = 0.0,
             timeout: int = 300, model: str | None = None) -> str:
    """Returns raw text. Every detector's SLM call routes through here."""
    backend = MODEL_BACKEND

    if backend == "bedrock":
        kwargs = {
            "modelId": model or BEDROCK_MODEL_ID,
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
        }
        if system:
            kwargs["system"] = [{"text": system}]
        resp = _bedrock().converse(**kwargs)
        return resp["output"]["message"]["content"][0]["text"]

    if backend == "sagemaker":
        full = f"{system}\n\n{prompt}" if system else prompt
        resp = _sagemaker().invoke_endpoint(
            EndpointName=model or SAGEMAKER_GEN_ENDPOINT,
            ContentType="application/json",
            Body=json.dumps({
                "inputs": full,
                "parameters": {
                    "max_new_tokens": max_tokens,
                    "temperature": temperature,
                    "return_full_text": False,
                },
            }),
        )
        payload = json.loads(resp["Body"].read())
        if isinstance(payload, list):
            return payload[0].get("generated_text", "")
        return payload.get("generated_text", "")

    # ollama
    body = {
        "model": model or OLLAMA_MODEL,
        "prompt": f"{system}\n\n{prompt}" if system else prompt,
        "stream": False,
        "options": {"temperature": temperature},
    }
    req = request.Request(
        f"{OLLAMA_BASE_URL.rstrip('/')}/api/generate",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8")).get("response", "")


def chat(system: str, user: str, max_tokens: int = 2048,
         temperature: float = 0.0, timeout: int = 300, model: str | None = None) -> str:
    """Chat-shaped call. Ollama uses /api/chat; the AWS backends are message-based already."""
    if MODEL_BACKEND in ("bedrock", "sagemaker"):
        return generate(user, system=system, max_tokens=max_tokens,
                        temperature=temperature, timeout=timeout, model=model)

    body = {
        "model": model or OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": {"temperature": temperature},
    }
    req = request.Request(
        f"{OLLAMA_BASE_URL.rstrip('/')}/api/chat",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return (payload.get("message") or {}).get("content", "")


def chat_json(system: str, user: str, model: str | None = None) -> dict:
    """Chat call whose response must parse as JSON."""
    augmented = system + "\n\nYou must return ONLY valid JSON."
    raw = _strip_fences(chat(augmented, user, model=model))
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("Model response was not valid JSON: %s", exc)
        return {"raw_response": raw, "error": "json_parse_failed"}


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------

def embed(text: str) -> list:
    """768-dim nomic embedding. Same cleaning as before: the tokenizer 500s on
    the replacement char and on Devanagari headers lifted out of scanned PDFs."""
    clean = str(text or "").replace("\ufffd", " ")
    clean = re.sub(r"[^\x00-\x7F]+", " ", clean)

    try:
        if EMBED_BACKEND == "sagemaker":
            resp = _sagemaker().invoke_endpoint(
                EndpointName=SAGEMAKER_EMBED_ENDPOINT,
                ContentType="application/json",
                Body=json.dumps({"inputs": [clean]}),
            )
            payload = json.loads(resp["Body"].read())
            if isinstance(payload, dict):
                vectors = payload.get("embeddings") or payload.get("vectors") or []
            else:
                vectors = payload
            return vectors[0] if vectors else []

        req = request.Request(
            f"{OLLAMA_BASE_URL.rstrip('/')}/api/embeddings",
            data=json.dumps({"model": OLLAMA_EMBED_MODEL, "prompt": clean}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8")).get("embedding", [])
    except Exception as exc:
        log.warning("Embedding failed (%s backend): %s", EMBED_BACKEND, exc)
        return []


def describe() -> str:
    if MODEL_BACKEND == "bedrock":
        return f"Bedrock {BEDROCK_MODEL_ID or '(unset)'}"
    if MODEL_BACKEND == "sagemaker":
        return f"SageMaker {SAGEMAKER_GEN_ENDPOINT or '(unset)'}"
    return f"Ollama {OLLAMA_MODEL}"
