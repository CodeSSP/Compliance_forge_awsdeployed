"""
LLM client for L3 reasoning.

All model traffic goes through aws/model_gateway, which selects the backend from
MODEL_BACKEND: bedrock | sagemaker | ollama. Gemini is retained only as an
explicit escape hatch (MODEL_BACKEND=gemini) and is not part of the AWS path.

Embeddings stay on nomic-embed-text at 768 dimensions, because that is what the
regulatory_chunks corpus in CockroachDB was built with.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List
from urllib import request
from urllib.error import HTTPError

from dotenv import load_dotenv
load_dotenv()


def is_llm_configured() -> bool:
    backend = os.environ.get("MODEL_BACKEND", "ollama").lower()
    if backend == "gemini":
        return bool(os.environ.get("GEMINI_API_KEY"))
    return True


def _extract_text_from_gemini_response(payload: Dict[str, Any]) -> str:
    try:
        candidates = payload.get("candidates") or []
        if not candidates:
            raise ValueError("No candidates returned from Gemini API.")
        parts = candidates[0].get("content", {}).get("parts", [])
        text_content = "".join(part.get("text", "") for part in parts).strip()
        
        # Clean up markdown code blocks if the model wrapped the JSON
        if text_content.startswith("```"):
            lines = text_content.splitlines()
            valid_lines = [line for line in lines if not line.strip().startswith("```")]
            text_content = "\n".join(valid_lines).strip()
            
        return text_content
    except Exception as e:
        raise ValueError(f"Unable to extract text content from Gemini API response: {e}")


def _extract_text_from_ollama_response(payload: Dict[str, Any]) -> str:
    message = payload.get("message") or {}
    content = message.get("content", "")
    text_content = str(content).strip()
    
    if text_content.startswith("```"):
        lines = text_content.splitlines()
        valid_lines = [line for line in lines if not line.strip().startswith("```")]
        text_content = "\n".join(valid_lines).strip()
        
    return text_content


def _call_gemini(
    api_key: str,
    chosen_model: str,
    system_prompt: str,
    user_prompt: str,
) -> Dict[str, Any]:
    """Attempt Gemini API call with exponential backoff on 429 errors."""
    max_retries = 3
    delays = [2, 4, 8]

    # Gemini REST API Format
    # https://ai.google.dev/api/rest/v1beta/models/generateContent
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{chosen_model}:generateContent?key={api_key}"
    
    payload = {
        "system_instruction": {
            "parts": [{"text": system_prompt}]
        },
        "contents": [{
            "parts": [{"text": user_prompt}]
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.0
        }
    }

    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            req = request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with request.urlopen(req, timeout=120) as response:
                raw_payload = json.loads(response.read().decode("utf-8"))
            return json.loads(_extract_text_from_gemini_response(raw_payload))
            
        except HTTPError as e:
            last_error = e
            if e.code in (429, 503) and attempt < max_retries:
                delay = delays[attempt - 1]
                print(f"L3: Gemini API attempt {attempt}/{max_retries} failed ({e.code}). Retrying in {delay}s...")
                time.sleep(delay)
            else:
                if e.code == 429:
                    print(
                        f"L3: Gemini API attempt {attempt}/{max_retries} failed (429).\n"
                        f"    [TIP] You have hit the Gemini API rate limit.\n"
                    )
                raise
        except Exception:
            raise

    raise last_error  # type: ignore[misc]


def _call_ollama(
    system_prompt: str,
    user_prompt: str,
) -> Dict[str, Any]:
    """Direct Ollama path. Retained for local debugging; chat_json uses the gateway."""
    ollama_base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    ollama_model = os.environ.get("OLLAMA_MODEL", "phi4:latest")

    print(
        f"L3: Primary API call failed or unavailable. "
        f"Falling back to Ollama ({ollama_model})..."
    )

    augmented_system = system_prompt + "\n\nYou must return ONLY valid JSON."

    payload = {
        "model": ollama_model,
        "messages": [
            {"role": "system", "content": augmented_system},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.0,
        "stream": False
    }

    req = request.Request(
        f"{ollama_base.rstrip('/')}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
        },
        method="POST",
    )

    with request.urlopen(req, timeout=300) as response:
        raw_payload = json.loads(response.read().decode("utf-8"))

    raw_text = _extract_text_from_ollama_response(raw_payload)

    try:
        return json.loads(raw_text)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"L3: Ollama response was not valid JSON: {exc}")
        return {"raw_response": raw_text, "error": "ollama_json_parse_failed"}


def generate_ollama_embedding(text: str) -> List[float]:
    """768-dim nomic embedding via the gateway (SageMaker endpoint or Ollama).

    Name kept because four modules import it. The unicode cleaning that stopped
    the tokenizer 500-ing on scanned-PDF artefacts lives in the gateway now.
    """
    from aws.model_gateway import embed
    return embed(text)


def chat_json(
    system_prompt: str, user_prompt: str, model: str | None = None
) -> Dict[str, Any]:
    """Primary reasoning call. Routes to Bedrock / SageMaker / Ollama."""
    from aws import model_gateway

    backend = os.environ.get("MODEL_BACKEND", "ollama").lower()

    if backend == "gemini":
        api_key = os.environ.get("GEMINI_API_KEY")
        chosen_model = model or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        if api_key:
            try:
                return _call_gemini(api_key, chosen_model, system_prompt, user_prompt)
            except Exception as e:
                print(f"L3: Gemini call failed: {e}")
        else:
            print("L3: MODEL_BACKEND=gemini but GEMINI_API_KEY is unset.")

    try:
        return model_gateway.chat_json(system_prompt, user_prompt, model=model)
    except Exception as e:
        print(f"L3: {model_gateway.describe()} call failed: {e}")
        return {"error": "model_call_failed", "detail": str(e)}
