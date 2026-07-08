import base64
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent

from services.llm_inference_lock import llm_inference_slot
try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

from services.prompts import (
    EMAIL_REPLY_MAX_MESSAGES,
    PromptMessages,
    format_email_reply_prompt,
    format_image_description_prompt,
    format_parse_emails_prompt,
    format_thread_summary_prompt,
    parse_emails,
)


def _parse_env_file(env_path: str = ".env") -> Dict[str, str]:
    """Load key=value pairs from a project-local ``.env`` file (simple parser, no export syntax)."""
    env_file = Path(env_path)
    out: Dict[str, str] = {}
    if not env_file.is_file():
        return out
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        k = key.strip()
        v = value.strip().strip('"').strip("'")
        out[k] = v
    return out


def _ollama_base_url(env_path: str = ".env") -> str:
    pairs = _parse_env_file(env_path)
    raw = (
        pairs.get("OLLAMA_HOST")
        or (os.getenv("OLLAMA_HOST") or "").strip()
        or "http://127.0.0.1:11434"
    )
    return raw.rstrip("/")


def _ollama_auth_headers(env_path: str = ".env") -> Dict[str, str]:
    pairs = _parse_env_file(env_path)
    user = (pairs.get("OLLAMA_HOST_USERNAME") or (os.getenv("OLLAMA_HOST_USERNAME") or "").strip()).strip()
    password = (pairs.get("OLLAMA_HOST_PASSWORD") or (os.getenv("OLLAMA_HOST_PASSWORD") or "").strip()).strip()
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if user or password:
        token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    return headers


def _resolve_ollama_model(env_path: str, env_key: str, fallback: str) -> str:
    pairs = _parse_env_file(env_path)
    return (pairs.get(env_key) or (os.getenv(env_key) or "").strip() or fallback).strip()


def _resolve_ollama_image_description_model(env_path: str) -> str:
    """``OLLAMA_MODEL_IMAGE_DESCRIPTION``; legacy ``OLLAMA_MODEL_VISION`` if unset."""
    pairs = _parse_env_file(env_path)
    for key in ("OLLAMA_MODEL_IMAGE_DESCRIPTION", "OLLAMA_MODEL_VISION"):
        val = (pairs.get(key) or (os.getenv(key) or "").strip())
        if val:
            return val
    return MODEL_IMAGE_DESCRIPTION


def _extract_first_json_object(text: str) -> Dict[str, Any]:
    """
    Parse the first JSON object from model text.

    Ollama models often ignore "JSON only" instructions: they wrap output in markdown fences,
    omit the closing fence when ``num_predict`` cuts off, or append prose after the object.
    Claude path (``claude_service``) uses a simpler extractor because the API output is steadier.
    """
    if not text:
        return {}
    candidates: List[str] = []
    stripped = text.strip()
    # Opening fence without a closing ``` (truncation) — complete-fence regex never matches; strip prefix.
    if stripped.startswith("```"):
        inner = re.sub(r"^```(?:json)?\s*", "", stripped, count=1, flags=re.I)
        inner = re.sub(r"\s*```\s*$", "", inner).strip()
        if inner:
            candidates.append(inner)
    if stripped:
        candidates.append(stripped)

    # Prefer fenced JSON blocks when present (complete fences).
    for m in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE):
        block = (m.group(1) or "").strip()
        if block:
            candidates.append(block)

    # Fallback: collect balanced { ... } spans without greedy over-capture.
    starts = [i for i, ch in enumerate(text) if ch == "{"]
    for start in starts:
        depth = 0
        in_string = False
        escape = False
        for idx in range(start, len(text)):
            ch = text[idx]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : idx + 1].strip())
                    break

    decoder = json.JSONDecoder()
    for candidate in candidates:
        if not candidate:
            continue
        s = candidate.strip()
        try:
            loaded = json.loads(s)
            if isinstance(loaded, dict):
                return loaded
        except json.JSONDecodeError:
            pass
        brace = s.find("{")
        if brace < 0:
            continue
        try:
            loaded, _ = decoder.raw_decode(s[brace:])
            if isinstance(loaded, dict):
                return loaded
        except json.JSONDecodeError:
            continue
    return {}


MODEL_SUMMARY = "mistral-small3.2:latest"
MODEL_SEGMENTATION = "llama3:latest"
MODEL_EMAIL_REPLY = "mistral-small3.2:latest"

# Ollama structured output for email segmentation (requires ``content`` key).
SEGMENTATION_RESPONSE_FORMAT: Dict[str, Any] = {
    "type": "object",
    "properties": {"content": {"type": "string"}},
    "required": ["content"],
}
# Ollama structured output for thread summaries (requires ``latest_updates``).
THREAD_SUMMARY_RESPONSE_FORMAT: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "latest_updates": {"type": "array", "items": {"type": "string"}},
        "next_steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "action": {"type": "string"},
                    "by_when": {"type": "string"},
                },
            },
        },
        "last_sender": {"type": "string"},
        "tone": {"type": "string"},
        "suggested_thread_label": {"type": "string"},
        "parties": {
            "type": "object",
            "properties": {
                "active_speakers": {"type": "array", "items": {"type": "string"}},
                "audience": {"type": "array", "items": {"type": "string"}},
            },
        },
        "counterparty_availability": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "party": {"type": "string"},
                    "label": {"type": "string"},
                },
            },
        },
    },
    "required": ["latest_updates"],
}
AIFRED_CHAT_RESPONSE_FORMAT: Dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}
LANE_SUMMARY_RESPONSE_FORMAT: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "highlights": {"type": "array", "items": {"type": "string"}},
        "current_priorities": {"type": "array", "items": {"type": "string"}},
        "waiting_on_others": {"type": "array", "items": {"type": "string"}},
        "tone_overview": {"type": "string"},
    },
    "required": ["summary"],
}
# Multimodal vision (Ollama): ``ollama pull llava`` (or another LLaVA-tagged model).
MODEL_IMAGE_DESCRIPTION = "llava:latest"


def describe_image_with_llava(
    *,
    base64_data: str,
    context: str = "",
    model: Optional[str] = None,
    max_tokens: int = 2048,
    env_path: str = ".env",
) -> Dict[str, Any]:
    """
    Run a local/remote Ollama LLaVA-class model on one image (standard Base64 of raw image bytes).

    Uses ``/api/generate`` with an ``images`` array. Configure host via ``OLLAMA_HOST`` and model
    via ``OLLAMA_MODEL_IMAGE_DESCRIPTION`` (default ``llava:latest``, see ``MODEL_IMAGE_DESCRIPTION``).

    Return shape matches :func:`services.claude_service.describe_image_with_claude` for callers.
    """
    base = _ollama_base_url(env_path)
    if not base:
        raise RuntimeError("OLLAMA_HOST is not set in environment or .env")
    model_name = (model or "").strip() or _resolve_ollama_image_description_model(env_path)
    if not model_name:
        raise RuntimeError("Ollama vision model name is empty")

    prompt_msgs = format_image_description_prompt(context=context)
    text_prompt = prompt_msgs.as_single_prompt()

    url = f"{base}/api/generate"
    headers = _ollama_auth_headers(env_path)
    payload: Dict[str, Any] = {
        "model": model_name,
        "prompt": text_prompt,
        "images": [base64_data],
        "stream": False,
        "options": {"num_predict": max_tokens},
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with llm_inference_slot():
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Ollama API error ({exc.code}) for {model_name}: {err_body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Ollama request failed ({base}): {exc}") from exc

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "description": "",
            "visible_text": "",
            "raw_text": raw[:4000],
        }
    combined = str(parsed.get("response") or "").strip()
    extracted = _extract_first_json_object(combined)
    desc = str(extracted.get("description") or "").strip()
    vis = str(extracted.get("visible_text") or "").strip()
    merged = desc
    if vis:
        merged = (desc + "\n\nVisible text:\n" + vis).strip() if desc else vis
    if merged:
        return {"description": merged, "visible_text": vis, "raw_text": combined}
    return {"description": combined, "visible_text": "", "raw_text": combined}


def _ollama_timeout_sec(env_path: str = ".env") -> int:
    pairs = _parse_env_file(env_path)
    raw = (pairs.get("OLLAMA_TIMEOUT_SEC") or os.getenv("OLLAMA_TIMEOUT_SEC") or "300").strip() or "300"
    try:
        return max(30, min(3600, int(raw)))
    except ValueError:
        return 300


def _ollama_generate_text(
    *,
    base: str,
    headers: Dict[str, str],
    model_name: str,
    prompt: str,
    system: str = "",
    max_tokens: int,
    response_format: Any | None,
    env_path: str = ".env",
) -> str:
    url = f"{base}/api/generate"
    payload: Dict[str, Any] = {
        "model": model_name,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": max_tokens},
    }
    if (system or "").strip():
        payload["system"] = system.strip()
    if response_format is not None:
        payload["format"] = response_format
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    timeout_sec = _ollama_timeout_sec(env_path)
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except TimeoutError as exc:
        raise RuntimeError(
            f"Ollama timed out after {timeout_sec}s ({base}, model={model_name})"
        ) from exc
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollama API error ({exc.code}) for {model_name}: {err_body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Ollama request failed ({base}): {exc}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:4000]
    return str(parsed.get("response") or "").strip()


def iter_ollama_generate_text(
    *,
    base: str,
    headers: Dict[str, str],
    model_name: str,
    prompt: str,
    system: str = "",
    max_tokens: int,
    response_format: Any | None,
    env_path: str = ".env",
):
    """Stream text chunks from Ollama ``/api/generate`` (``stream: true``)."""
    url = f"{base}/api/generate"
    payload: Dict[str, Any] = {
        "model": model_name,
        "prompt": prompt,
        "stream": True,
        "options": {"num_predict": max_tokens},
    }
    if (system or "").strip():
        payload["system"] = system.strip()
    if response_format is not None:
        payload["format"] = response_format
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    timeout_sec = _ollama_timeout_sec(env_path)
    with llm_inference_slot():
        try:
            with urllib.request.urlopen(request, timeout=timeout_sec) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    chunk = str(obj.get("response") or "")
                    if chunk:
                        yield chunk
                    if obj.get("done"):
                        break
        except TimeoutError as exc:
            raise RuntimeError(
                f"Ollama timed out after {timeout_sec}s ({base}, model={model_name})"
            ) from exc
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Ollama API error ({exc.code}) for {model_name}: {err_body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Ollama request failed ({base}): {exc}") from exc


def stream_ollama_text(
    prompt: str | PromptMessages,
    *,
    model: str,
    max_tokens: int = 1200,
    env_path: str = ".env",
    response_format: Any | None = None,
):
    """Yield streamed text chunks from Ollama for a prompt."""
    base = _ollama_base_url(env_path)
    if not base:
        raise RuntimeError("OLLAMA_HOST is not set in environment or .env")
    model_name = (model or "").strip()
    if not model_name:
        raise RuntimeError("Ollama model name is empty")
    headers = _ollama_auth_headers(env_path)
    system, user = _resolve_ollama_prompt(prompt)
    if not user and system:
        user = system
        system = ""
    yield from iter_ollama_generate_text(
        base=base,
        headers=headers,
        model_name=model_name,
        prompt=user,
        system=system,
        max_tokens=max_tokens,
        response_format=response_format,
        env_path=env_path,
    )


def _resolve_ollama_prompt(prompt: str | PromptMessages) -> tuple[str, str]:
    if isinstance(prompt, PromptMessages):
        return (prompt.system or "").strip(), (prompt.user or "").strip()
    return "", str(prompt or "").strip()


def call_ollama_json(
    prompt: str | PromptMessages,
    *,
    model: str,
    max_tokens: int = 1200,
    env_path: str = ".env",
    response_format: Any | None = "json",
) -> Dict[str, Any]:
    """
    Call a remote Ollama ``/api/generate`` endpoint and parse a JSON object from the model text.
    Uses ``OLLAMA_HOST``, ``OLLAMA_HOST_USERNAME``, and ``OLLAMA_HOST_PASSWORD`` from ``.env`` (or process env).

    ``response_format`` is passed to Ollama as ``format`` (``"json"`` or a JSON-schema dict). Pass ``None`` to
    disable structured output (legacy behavior).
    """
    base = _ollama_base_url(env_path)
    if not base:
        raise RuntimeError("OLLAMA_HOST is not set in environment or .env")
    model_name = (model or "").strip()
    if not model_name:
        raise RuntimeError("Ollama model name is empty")

    headers = _ollama_auth_headers(env_path)
    system, user = _resolve_ollama_prompt(prompt)
    if not user and system:
        user = system
        system = ""
    formats_to_try: List[Any] = []
    if response_format is None:
        formats_to_try = [None]
    elif response_format == "json":
        formats_to_try = ["json"]
    else:
        formats_to_try = [response_format, "json", None]

    combined = ""
    last_http_error: Optional[RuntimeError] = None
    with llm_inference_slot():
        for fmt in formats_to_try:
            try:
                combined = _ollama_generate_text(
                    base=base,
                    headers=headers,
                    model_name=model_name,
                    prompt=user,
                    system=system,
                    max_tokens=max_tokens,
                    response_format=fmt,
                    env_path=env_path,
                )
                last_http_error = None
                break
            except RuntimeError as exc:
                if fmt is formats_to_try[-1]:
                    raise
                last_http_error = exc
    if last_http_error is not None:
        raise last_http_error

    extracted = _extract_first_json_object(combined)
    if extracted:
        return extracted
    return {
        "raw_text": combined,
        "api_error": "Model returned prose instead of JSON; summary not structured.",
    }


def submit_prompt(
    prompt: str | PromptMessages,
    *,
    model: Optional[str] = None,
    max_tokens: int = 1200,
    env_path: str = ".env",
) -> Dict[str, Any]:
    """
    Submit a prompt to Ollama and return parsed JSON output.
    Defaults to ``OLLAMA_MODEL_SUMMARY`` from env when ``model`` is omitted.
    """
    resolved = model or _resolve_ollama_model(env_path, "OLLAMA_MODEL_SUMMARY", MODEL_SUMMARY)
    return call_ollama_json(prompt, model=resolved, max_tokens=max_tokens, env_path=env_path)


def submit_segmentation_prompt(
    prompt: str | PromptMessages,
    *,
    model: Optional[str] = None,
    max_tokens: int = 2048,
    env_path: str = ".env",
) -> Dict[str, Any]:
    """Email body segmentation: default model from ``OLLAMA_MODEL_SEGMENTATION``."""
    resolved = model or _resolve_ollama_model(env_path, "OLLAMA_MODEL_SEGMENTATION", MODEL_SEGMENTATION)
    return call_ollama_json(
        prompt,
        model=resolved,
        max_tokens=max_tokens,
        env_path=env_path,
        response_format=SEGMENTATION_RESPONSE_FORMAT,
    )


def submit_summary_prompt(
    prompt: str | PromptMessages,
    *,
    model: Optional[str] = None,
    max_tokens: int = 1200,
    env_path: str = ".env",
) -> Dict[str, Any]:
    """Thread summaries: default model from ``OLLAMA_MODEL_SUMMARY``."""
    resolved = model or _resolve_ollama_model(env_path, "OLLAMA_MODEL_SUMMARY", MODEL_SUMMARY)
    return call_ollama_json(
        prompt,
        model=resolved,
        max_tokens=max_tokens,
        env_path=env_path,
        response_format=THREAD_SUMMARY_RESPONSE_FORMAT,
    )


def submit_incremental_summary_prompt(
    prompt: str | PromptMessages,
    *,
    model: Optional[str] = None,
    max_tokens: int = 1200,
    env_path: str = ".env",
) -> Dict[str, Any]:
    """Incremental thread summary updates."""
    resolved = model or _resolve_ollama_model(env_path, "OLLAMA_MODEL_SUMMARY", MODEL_SUMMARY)
    return call_ollama_json(
        prompt,
        model=resolved,
        max_tokens=max_tokens,
        env_path=env_path,
        response_format=THREAD_SUMMARY_RESPONSE_FORMAT,
    )


def submit_email_reply_prompt(
    prompt: str | PromptMessages,
    *,
    model: Optional[str] = None,
    max_tokens: int = 2500,
    env_path: str = ".env",
) -> Dict[str, Any]:
    """Draft reply: default model from ``OLLAMA_MODEL_EMAIL_REPLY``."""
    resolved = model or _resolve_ollama_model(env_path, "OLLAMA_MODEL_EMAIL_REPLY", MODEL_EMAIL_REPLY)
    return call_ollama_json(prompt, model=resolved, max_tokens=max_tokens, env_path=env_path)


def submit_lane_summary_prompt(
    prompt: str | PromptMessages,
    *,
    model: Optional[str] = None,
    max_tokens: int = 1500,
    env_path: str = ".env",
) -> Dict[str, Any]:
    """Lane roll-up summaries across assigned threads."""
    resolved = model or _resolve_ollama_model(env_path, "OLLAMA_MODEL_SUMMARY", MODEL_SUMMARY)
    return call_ollama_json(
        prompt,
        model=resolved,
        max_tokens=max_tokens,
        env_path=env_path,
        response_format=LANE_SUMMARY_RESPONSE_FORMAT,
    )


def submit_meeting_prep_prompt(
    prompt: str | PromptMessages,
    *,
    model: Optional[str] = None,
    max_tokens: int = 2000,
    env_path: str = ".env",
) -> Dict[str, Any]:
    """Meeting prep: default model from ``OLLAMA_MODEL_SUMMARY``."""
    resolved = model or _resolve_ollama_model(env_path, "OLLAMA_MODEL_SUMMARY", MODEL_SUMMARY)
    return call_ollama_json(prompt, model=resolved, max_tokens=max_tokens, env_path=env_path)


def submit_aifred_chat_prompt(
    prompt: str | PromptMessages,
    *,
    model: Optional[str] = None,
    max_tokens: int = 1200,
    env_path: str = ".env",
) -> Dict[str, Any]:
    """Ask AIFred chat turn: default model from ``OLLAMA_MODEL_AIFRED`` (falls back to summary model)."""
    resolved = model or _resolve_ollama_model(env_path, "OLLAMA_MODEL_AIFRED", MODEL_SUMMARY)
    return call_ollama_json(
        prompt,
        model=resolved,
        max_tokens=max_tokens,
        env_path=env_path,
        response_format=AIFRED_CHAT_RESPONSE_FORMAT,
    )


def list_available_models(env_path: str = ".env") -> List[str]:
    """Return model names reported by Ollama ``GET /api/tags``."""
    base = _ollama_base_url(env_path)
    if not base:
        raise RuntimeError("OLLAMA_HOST is not set in environment or .env")
    url = f"{base}/api/tags"
    headers = _ollama_auth_headers(env_path)
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollama API error ({exc.code}): {err_body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Ollama request failed ({base}): {exc}") from exc
    parsed = json.loads(raw)
    models = parsed.get("models") or []
    names: List[str] = []
    for item in models:
        if isinstance(item, dict):
            name = str(item.get("name", "")).strip()
            if name:
                names.append(name)
    return names