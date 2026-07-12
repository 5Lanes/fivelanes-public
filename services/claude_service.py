import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from services.llm_inference_lock import llm_inference_slot
from services.prompts import (
    EMAIL_REPLY_MAX_MESSAGES,
    PromptMessages,
    format_email_reply_prompt,
    format_image_description_prompt,
    format_parse_emails_prompt,
    format_thread_summary_prompt,
    parse_emails,
)


def _load_key_from_env_file(env_path: str = ".env") -> str:
    env_file = Path(env_path)
    if not env_file.exists():
        return ""
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() == "CLAUDE_API_KEY":
            return value.strip().strip('"').strip("'")
    return ""


def _extract_first_json_object(text: str) -> Dict[str, Any]:
    if not text:
        return {}
    candidates: List[str] = []
    stripped = text.strip()
    if stripped:
        candidates.append(stripped)

    # Prefer fenced JSON blocks when present (Claude often returns ```json ... ```).
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

    for candidate in candidates:
        if not candidate:
            continue
        try:
            loaded = json.loads(candidate)
            if isinstance(loaded, dict):
                return loaded
        except json.JSONDecodeError:
            continue
    return {}


MODEL_SUMMARY = "claude-sonnet-4-6"
MODEL_SEGMENTATION = "claude-haiku-4-5-20251001"
MODEL_EMAIL_REPLY = "claude-sonnet-4-6"


def _model_fallback_chain(primary: str) -> List[str]:
    """Try primary first, then sensible alternates (Haiku chain stays cheap; Sonnet chain stays capable)."""
    sonnet = "claude-sonnet-4-6"
    sonnet_alt = "claude-sonnet-4-5-20250929"
    haiku = "claude-haiku-4-5-20251001"
    primary = (primary or "").strip()
    if "haiku" in primary.lower():
        candidates = [primary, haiku, sonnet, sonnet_alt]
    else:
        candidates = [primary, sonnet, sonnet_alt, haiku]
    seen: set[str] = set()
    ordered: List[str] = []
    for m in candidates:
        if m and m not in seen:
            seen.add(m)
            ordered.append(m)
    return ordered


def _claude_prompt_parts(prompt: str | PromptMessages) -> tuple[str, List[Dict[str, Any]]]:
    """Return Anthropic ``system`` text and user ``messages`` (system is top-level, not a message role)."""
    if isinstance(prompt, PromptMessages):
        system = (prompt.system or "").strip()
        user = (prompt.user or "").strip()
    else:
        system = ""
        user = str(prompt or "").strip()
    return system, [{"role": "user", "content": user}]


def call_claude_json(
    prompt: str | PromptMessages,
    *,
    model: str = MODEL_SUMMARY,
    max_tokens: int = 1200,
    env_path: str = ".env",
) -> Dict[str, Any]:
    """
    Call Claude Messages API and parse a JSON object from the text response.
    """
    # Prefer project-local .env over inherited shell vars to avoid stale keys.
    api_key = _load_key_from_env_file(env_path) or (os.getenv("CLAUDE_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("CLAUDE_API_KEY is not set in environment or .env")

    models_to_try = _model_fallback_chain(model)
    attempted: List[str] = []
    raw = ""
    last_error = ""
    with llm_inference_slot():
        for model_name in models_to_try:
            if model_name in attempted:
                continue
            attempted.append(model_name)
            system, messages = _claude_prompt_parts(prompt)
            payload: Dict[str, Any] = {
                "model": model_name,
                "max_tokens": max_tokens,
                "messages": messages,
            }
            if system:
                payload["system"] = system
            request = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "content-type": "application/json",
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    raw = response.read().decode("utf-8", errors="replace")
                break
            except urllib.error.HTTPError as exc:
                err_body = exc.read().decode("utf-8", errors="replace")
                last_error = f"Claude API error ({exc.code}) for {model_name}: {err_body}"
                if exc.code == 404:
                    continue
                raise RuntimeError(last_error) from exc
            except urllib.error.URLError as exc:
                raise RuntimeError(f"Claude API request failed: {exc}") from exc
    if not raw:
        raise RuntimeError(last_error or "Claude API request failed for all candidate models.")

    parsed = json.loads(raw)
    chunks = parsed.get("content") or []
    text_parts = [c.get("text", "") for c in chunks if isinstance(c, dict) and c.get("type") == "text"]
    combined = "\n".join(p for p in text_parts if p).strip()
    extracted = _extract_first_json_object(combined)
    if extracted:
        return extracted
    return {"raw_text": combined}


def submit_prompt(
    prompt: str | PromptMessages, *, model: str = MODEL_SUMMARY, max_tokens: int = 1200
) -> Dict[str, Any]:
    """
    Submit a prompt to Claude and return parsed JSON output.
    Defaults to the summary model (Sonnet-class). Prefer ``submit_segmentation_prompt`` /
    ``submit_summary_prompt`` for clearer cost behavior.
    """
    return call_claude_json(prompt, model=model, max_tokens=max_tokens)


def submit_segmentation_prompt(
    prompt: str | PromptMessages,
    *,
    model: str = MODEL_SEGMENTATION,
    max_tokens: int = 2048,
) -> Dict[str, Any]:
    """Email body segmentation (content / quoted / signature): use cheaper Haiku by default."""
    return call_claude_json(prompt, model=model, max_tokens=max_tokens)


def submit_summary_prompt(
    prompt: str | PromptMessages,
    *,
    model: str = MODEL_SUMMARY,
    max_tokens: int = 1200,
) -> Dict[str, Any]:
    """Thread summaries: use Sonnet-class by default for quality."""
    return call_claude_json(prompt, model=model, max_tokens=max_tokens)


def submit_incremental_summary_prompt(
    prompt: str | PromptMessages,
    *,
    model: str = MODEL_SUMMARY,
    max_tokens: int = 1200,
) -> Dict[str, Any]:
    """Incremental thread summary updates."""
    return call_claude_json(prompt, model=model, max_tokens=max_tokens)


def submit_email_reply_prompt(
    prompt: str | PromptMessages,
    *,
    model: str = MODEL_EMAIL_REPLY,
    max_tokens: int = 2500,
) -> Dict[str, Any]:
    """Draft a reply in the user's voice: Sonnet-class by default, higher token budget for full bodies."""
    return call_claude_json(prompt, model=model, max_tokens=max_tokens)


def submit_lane_summary_prompt(
    prompt: str | PromptMessages,
    *,
    model: str = MODEL_SUMMARY,
    max_tokens: int = 1500,
) -> Dict[str, Any]:
    """Roll-up summary across threads assigned to one lane."""
    return call_claude_json(prompt, model=model, max_tokens=max_tokens)


def submit_scheduling_ask_prompt(
    prompt: str | PromptMessages,
    *,
    model: str = MODEL_SUMMARY,
    max_tokens: int = 500,
) -> Dict[str, Any]:
    """Small, focused classification: does the last message ask about availability, and
    what window(s) does it name?"""
    return call_claude_json(prompt, model=model, max_tokens=max_tokens)


def submit_meeting_prep_prompt(
    prompt: str | PromptMessages,
    *,
    model: str = MODEL_SUMMARY,
    max_tokens: int = 2000,
) -> Dict[str, Any]:
    """Meeting prep brief from calendar event + email thread context."""
    return call_claude_json(prompt, model=model, max_tokens=max_tokens)


def submit_digest_prompt(
    prompt: str | PromptMessages,
    *,
    model: str = MODEL_SUMMARY,
    max_tokens: int = 2000,
) -> Dict[str, Any]:
    """Cross-source briefing narrative across lanes, plans, and meetings."""
    return call_claude_json(prompt, model=model, max_tokens=max_tokens)


def claude_supported_image_media_type(mime_type: str) -> Optional[str]:
    """
    Map a Gmail / HTTP image MIME type to an Anthropic Messages API ``media_type`` string.
    Returns ``None`` when Claude cannot accept that type as a base64 image source.
    """
    raw = (mime_type or "").strip().lower().split(";", 1)[0].strip()
    if raw in ("image/jpg", "image/jpeg", "image/pjpeg"):
        return "image/jpeg"
    if raw == "image/png":
        return "image/png"
    if raw == "image/gif":
        return "image/gif"
    if raw == "image/webp":
        return "image/webp"
    return None


def describe_image_with_claude(
    *,
    media_type: str,
    base64_data: str,
    context: str = "",
    model: str = MODEL_SUMMARY,
    max_tokens: int = 2048,
    env_path: str = ".env",
) -> Dict[str, Any]:
    """
    Run Claude vision on one base64-encoded image (standard Base64, not URL-safe).

    Returns a dict with at least ``description`` (merged narrative + visible text when JSON
    parsing succeeds) and ``raw_text`` (model text output). Compatible with inbox
    ``inline_image_descriptions`` extraction in :mod:`utils.gmail_message_images`.
    """
    api_key = _load_key_from_env_file(env_path) or (os.getenv("CLAUDE_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("CLAUDE_API_KEY is not set in environment or .env")

    prompt_msgs = format_image_description_prompt(context=context)
    text_prompt = prompt_msgs.as_single_prompt()

    content_blocks: List[Dict[str, Any]] = [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": base64_data},
        },
        {"type": "text", "text": text_prompt},
    ]

    models_to_try = _model_fallback_chain(model)
    attempted: List[str] = []
    raw = ""
    last_error = ""
    with llm_inference_slot():
        for model_name in models_to_try:
            if model_name in attempted:
                continue
            attempted.append(model_name)
            payload = {
                "model": model_name,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": content_blocks}],
            }
            request = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "content-type": "application/json",
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=180) as response:
                    raw = response.read().decode("utf-8", errors="replace")
                break
            except urllib.error.HTTPError as exc:
                err_body = exc.read().decode("utf-8", errors="replace")
                last_error = f"Claude API error ({exc.code}) for {model_name}: {err_body}"
                if exc.code == 404:
                    continue
                raise RuntimeError(last_error) from exc
            except urllib.error.URLError as exc:
                raise RuntimeError(f"Claude API request failed: {exc}") from exc

    if not raw:
        raise RuntimeError(last_error or "Claude API request failed for all candidate models.")

    parsed = json.loads(raw)
    chunks = parsed.get("content") or []
    text_parts = [c.get("text", "") for c in chunks if isinstance(c, dict) and c.get("type") == "text"]
    combined = "\n".join(p for p in text_parts if p).strip()

    extracted = _extract_first_json_object(combined)
    desc = str(extracted.get("description") or "").strip()
    vis = str(extracted.get("visible_text") or "").strip()
    merged = desc
    if vis:
        merged = (desc + "\n\nVisible text:\n" + vis).strip() if desc else vis
    if merged:
        return {"description": merged, "visible_text": vis, "raw_text": combined}
    return {"description": combined, "visible_text": "", "raw_text": combined}


def list_available_models(env_path: str = ".env") -> List[str]:
    """
    Return model IDs available to the current API key/account.
    """
    api_key = _load_key_from_env_file(env_path) or (os.getenv("CLAUDE_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("CLAUDE_API_KEY is not set in environment or .env")
    request = urllib.request.Request(
        "https://api.anthropic.com/v1/models",
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Claude API error ({exc.code}): {err_body}") from exc
    parsed = json.loads(raw)
    data = parsed.get("data") or []
    return [str(item.get("id", "")).strip() for item in data if isinstance(item, dict) and item.get("id")]