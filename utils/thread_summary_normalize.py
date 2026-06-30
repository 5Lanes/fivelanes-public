"""Normalize LLM thread-summary JSON into the dashboard schema."""

from __future__ import annotations

from typing import Any, Dict, List, Set

from utils.api_error_detection import (
    summary_updates_look_like_verbatim_email,
    thread_summary_is_valid,
    update_looks_like_verbatim_email,
)
from utils.counterparty_availability_normalize import normalize_counterparty_availability
from utils.next_step_normalize import normalize_next_steps


def _append_update(updates: List[str], seen: Set[str], line: str) -> None:
    text = str(line or "").strip()
    if text and text not in seen:
        seen.add(text)
        updates.append(text)


def _coerce_next_steps(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, list):
        out: List[Dict[str, Any]] = []
        for item in raw:
            if isinstance(item, dict):
                action = str(item.get("action") or item.get("description") or "").strip()
                if not action:
                    continue
                out.append(
                    {
                        "type": str(item.get("type") or "response required").strip(),
                        "action": action,
                        "by_when": str(item.get("by_when") or item.get("due_date") or "").strip(),
                    }
                )
            else:
                action = str(item or "").strip()
                if action:
                    out.append(
                        {
                            "type": "response required",
                            "action": action,
                            "by_when": "",
                        }
                    )
        return out
    if isinstance(raw, str) and raw.strip():
        return [{"type": "response required", "action": raw.strip(), "by_when": ""}]
    return []


def _updates_from_cleaned(cleaned: List[Dict[str, Any]]) -> List[str]:
    updates: List[str] = []
    seen: Set[str] = set()
    for row in reversed(cleaned):
        line = str(row.get("cleaned_content") or "").strip()
        if line and line != "(attachment)":
            _append_update(updates, seen, line[:240])
            break
    if updates:
        return updates

    for row in reversed(cleaned):
        parts: List[str] = []
        subject = str(row.get("subject") or "").strip()
        sender = str(row.get("sender") or row.get("forwarded_from") or "").strip()
        dt = str(row.get("datetime") or "").strip()
        if subject:
            parts.append(subject)
        if sender:
            parts.append(f"from {sender}")
        if dt:
            parts.append(f"({dt})")
        raw = str(row.get("raw_text") or "").strip()
        if raw and not parts:
            parts.append(raw[:240])
        if parts:
            _append_update(updates, seen, "; ".join(parts))
            break
    return updates


def _sender_display_name(sender: str) -> str:
    raw = str(sender or "").strip()
    if not raw:
        return ""
    if "<" in raw:
        name = raw.split("<", 1)[0].strip().strip('"')
        if name:
            return name
    if "@" in raw:
        return raw.split("@", 1)[0].strip()
    return raw


def _attributed_fallback_updates(cleaned: List[Dict[str, Any]]) -> List[str]:
    """Metadata-only bullets when the model pasted message bodies verbatim."""
    for row in reversed(cleaned):
        sender = str(row.get("sender") or "").strip()
        subject = str(row.get("subject") or "").strip()
        dt = str(row.get("datetime") or "").strip()[:10]
        name = _sender_display_name(sender)
        if not name and not subject:
            continue
        who = name or "Counterparty"
        line = f"{who} emailed"
        if subject:
            line += f" re: {subject}"
        if dt:
            line += f" ({dt})"
        line += "."
        return [line]
    return []


def _chat_attributed_fallback_updates(
    cleaned: List[Dict[str, Any]],
    *,
    display_label: str = "",
) -> List[str]:
    """Short attribution line for chat when the model pasted message text verbatim."""
    contact = (display_label.split(" · ")[0] if display_label else "").strip()
    for row in reversed(cleaned):
        sender = str(row.get("sender") or "").strip().lower()
        body = str(row.get("cleaned_content") or "").strip()
        if not body or body == "(attachment)":
            continue
        if sender == "me":
            return [f"You messaged {contact}." if contact else "You sent a message."]
        who = _sender_display_name(str(row.get("sender") or "")) or contact or "Contact"
        return [f"{who} messaged you."]
    return []


def _chat_fallback_updates(
    cleaned: List[Dict[str, Any]],
    *,
    display_label: str = "",
    channel: str = "",
) -> List[str]:
    if channel in ("slack", "text"):
        return _chat_attributed_fallback_updates(cleaned, display_label=display_label)
    return _attributed_fallback_updates(cleaned)


def _display_label_from_cleaned(cleaned: List[Dict[str, Any]]) -> str:
    if not cleaned:
        return ""
    latest = cleaned[-1]
    return str(latest.get("subject") or latest.get("suggested_thread_label") or "").strip()


def normalize_thread_summary(
    summary: Dict[str, Any],
    cleaned: List[Dict[str, Any]],
    *,
    display_label: str = "",
    channel: str = "",
) -> Dict[str, Any]:
    """Map common non-schema LLM shapes into dashboard fields."""
    out = dict(summary)
    label = (display_label or _display_label_from_cleaned(cleaned)).strip()
    if channel:
        out["channel"] = channel
    incoming_api_error = str(out.get("api_error") or "").strip()

    raw_updates = out.get("latest_updates")
    llm_provided_updates = False
    if isinstance(raw_updates, str) and raw_updates.strip():
        llm_provided_updates = True
        out["latest_updates"] = [raw_updates.strip()]
    elif isinstance(raw_updates, list) and raw_updates:
        llm_provided_updates = True

    updates: List[str] = []
    seen: Set[str] = set()
    if llm_provided_updates:
        for item in out.get("latest_updates") or []:
            _append_update(updates, seen, str(item))
        if cleaned and updates:
            filtered = [line for line in updates if not update_looks_like_verbatim_email(line, cleaned)]
            if filtered:
                updates = filtered
                seen = set(filtered)
            else:
                fallback = _chat_fallback_updates(
                    cleaned, display_label=label, channel=channel
                )
                if fallback:
                    updates = fallback
                    seen = set(fallback)
                else:
                    updates = []
                    seen = set()
    else:
        for key in ("latest_updates", "key_points", "pending_items"):
            raw = out.get(key)
            if isinstance(raw, list):
                for item in raw:
                    _append_update(updates, seen, str(item))

        for key in ("thread_summary", "summary"):
            raw = out.get(key)
            if isinstance(raw, str):
                _append_update(updates, seen, raw)
            elif isinstance(raw, dict):
                for nested_key in ("latest_updates", "key_points"):
                    nested = raw.get(nested_key)
                    if isinstance(nested, list):
                        for item in nested:
                            _append_update(updates, seen, str(item))
                for msg in raw.get("messages") or []:
                    if not isinstance(msg, dict):
                        continue
                    line = str(msg.get("content") or "").strip()
                    if line and line != "(attachment)":
                        _append_update(updates, seen, line)

        for item in out.get("action_items") or []:
            if isinstance(item, dict):
                _append_update(updates, seen, str(item.get("description") or item.get("action") or ""))
            else:
                _append_update(updates, seen, str(item))

        last_content = str(out.get("last_message_content") or "").strip()
        if last_content and last_content != "(attachment)":
            _append_update(updates, seen, last_content[:240])

        if not updates and not incoming_api_error:
            if channel in ("slack", "text"):
                updates = _chat_attributed_fallback_updates(cleaned, display_label=label)
            else:
                updates = _updates_from_cleaned(cleaned)

    if updates:
        out["latest_updates"] = updates[:8]
        out.pop("api_error", None)
    elif llm_provided_updates:
        out["latest_updates"] = []

    if not out.get("next_steps") and out.get("action_items"):
        out["next_steps"] = _coerce_next_steps(out.get("action_items"))
    elif out.get("next_steps") and not isinstance(out.get("next_steps"), list):
        out["next_steps"] = _coerce_next_steps(out.get("next_steps"))

    next_steps, passive_updates = normalize_next_steps(out.get("next_steps"))
    out["next_steps"] = next_steps

    if "counterparty_availability" in out:
        slots = normalize_counterparty_availability(out.get("counterparty_availability"))
        if slots:
            out["counterparty_availability"] = slots
        else:
            out.pop("counterparty_availability", None)

    if passive_updates:
        if not updates:
            updates = [str(item) for item in (out.get("latest_updates") or []) if str(item).strip()]
            seen = set(updates)
        for line in passive_updates:
            _append_update(updates, seen, line)
        out["latest_updates"] = updates[:8]

    if not str(out.get("last_sender") or "").strip():
        sender = str(out.get("sender") or "").strip()
        if sender:
            out["last_sender"] = sender

    if not isinstance(out.get("parties"), dict) and isinstance(out.get("participants"), list):
        speakers: List[str] = []
        for person in out.get("participants") or []:
            if not isinstance(person, dict):
                continue
            name = str(person.get("name") or "").strip()
            email = str(person.get("email") or "").strip()
            speakers.append(name or email)
        if speakers:
            out["parties"] = {"active_speakers": speakers, "audience": []}

    subject = str(out.get("subject") or "").strip()
    if subject and subject not in ("(none)", "(unknown)"):
        out.setdefault("suggested_thread_label", subject)
    if label:
        out.setdefault("suggested_thread_label", label)

    return out


def finalize_thread_summary(
    summary: Dict[str, Any],
    cleaned: List[Dict[str, Any]],
    *,
    display_label: str = "",
    channel: str = "",
) -> Dict[str, Any]:
    """Normalize LLM output, attach counts, and set ``api_error`` when still invalid."""
    if not isinstance(summary, dict):
        summary = {"payload": summary}

    updates = summary.get("latest_updates")
    if (
        str(summary.get("raw_text") or "").strip()
        and not (isinstance(updates, list) and len(updates) > 0)
    ):
        summary.setdefault(
            "api_error",
            "Model returned prose instead of JSON; re-run with Claude or a smaller thread.",
        )

    verbatim_on_input = (
        cleaned
        and isinstance(summary.get("latest_updates"), list)
        and summary_updates_look_like_verbatim_email(summary, cleaned)
    )

    out = normalize_thread_summary(
        summary,
        cleaned,
        display_label=display_label,
        channel=channel,
    )

    if not thread_summary_is_valid(out, cleaned=cleaned):
        if verbatim_on_input:
            out.setdefault(
                "api_error",
                "Model returned verbatim email text in latest_updates; re-run summary.",
            )
        else:
            out.setdefault(
                "api_error",
                str(out.get("api_error") or "Model returned invalid summary JSON (missing latest_updates)."),
            )
    elif str(out.get("api_error") or "").strip():
        out.pop("api_error", None)

    from utils.summary_timeliness import reframe_summary_temporal_fields

    out = reframe_summary_temporal_fields(out)

    out["message_count"] = len(cleaned)
    out["summarized_message_count"] = len(cleaned)
    return out
