"""Load prompt templates from prompts.json and build model-ready messages."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple, Union

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[misc, assignment]

_PROMPTS_DIR = Path(__file__).resolve().parent
_PROMPTS_PATH = Path(
    os.getenv("FIVELANES_PROMPTS_PATH") or (_PROMPTS_DIR / "prompts.json")
)
_PROMPTS_EXAMPLE_PATH = _PROMPTS_DIR / "prompts.example.json"

EMAIL_REPLY_MAX_MESSAGES = 3
SEGMENTATION_MAX_BODY_CHARS = int(os.getenv("SEGMENTATION_MAX_BODY_CHARS") or "12000")


@dataclass(frozen=True)
class PromptMessages:
    """System and user prompt parts for LLM APIs."""

    system: str
    user: str

    def as_single_prompt(self) -> str:
        """Concatenate for backends or callers that expect one string."""
        system = (self.system or "").strip()
        user = (self.user or "").strip()
        if system and user:
            return f"{system}\n\n{user}"
        return system or user


def _prompts_missing_message() -> str:
    return (
        f"Missing prompt file at {_PROMPTS_PATH}. "
        f"Copy {_PROMPTS_EXAMPLE_PATH.name} to prompts.json and fill in your prompts."
    )


@lru_cache(maxsize=1)
def _load_config() -> Dict[str, Any]:
    if not _PROMPTS_PATH.is_file():
        raise FileNotFoundError(_prompts_missing_message())
    return json.loads(_PROMPTS_PATH.read_text(encoding="utf-8"))


def _load_prompts() -> Dict[str, Any]:
    return dict(_load_config().get("prompts") or {})


def _load_settings() -> Dict[str, Any]:
    return dict(_load_config().get("settings") or {})


def prompt_version() -> str:
    return str(_load_settings().get("prompt_version") or "1")


def _prompt_template(key: str) -> Union[str, Dict[str, str]]:
    return _load_prompts()[key]


def _format_prompt_pair(key: str, **kwargs: Any) -> PromptMessages:
    raw = _prompt_template(key)
    if isinstance(raw, dict):
        system_tpl = str(raw.get("system") or "")
        user_tpl = str(raw.get("user") or "")
    else:
        system_tpl = ""
        user_tpl = str(raw)
    return PromptMessages(
        system=system_tpl.format(**kwargs) if system_tpl else "",
        user=user_tpl.format(**kwargs) if user_tpl else "",
    )


def _scheduler_tz_name() -> str:
    return (os.getenv("FIVELANES_SCHEDULER_TZ") or "America/New_York").strip() or "America/New_York"


def summary_as_of_datetime(*, as_of: datetime | None = None) -> str:
    """Human-readable as-of stamp for summary prompts (scheduler timezone)."""
    tz_name = _scheduler_tz_name()
    tz: Any
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
            tz_name = "UTC"
    else:
        tz = timezone.utc
        tz_name = "UTC"
    if as_of is None:
        now = datetime.now(tz)
    elif as_of.tzinfo is None:
        now = as_of.replace(tzinfo=tz)
    else:
        now = as_of.astimezone(tz)
    return f"{now.strftime('%Y-%m-%d %H:%M')} ({tz_name})"


def _summary_routing_aliases() -> List[str]:
    from utils.owner_config import summary_routing_aliases

    return summary_routing_aliases()


def _sanitize_summary_text(text: Any) -> str:
    """Mask intake inbox aliases so they do not become summary content."""
    value = str(text or "")
    aliases = set(_summary_routing_aliases())
    source_account = (os.getenv("SOURCE_ACCOUNT") or "").strip().lower()
    if source_account:
        aliases.add(source_account)
    cleaned = value
    for alias in aliases:
        if not alias:
            continue
        cleaned = re.sub(re.escape(alias), "(fivelanes inbox)", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def _thread_message_block_template() -> str:
    return str(_load_prompts()["thread_message_block"])


def _thread_summary_block_template() -> str:
    return str(_load_prompts()["thread_summary_block"])


def _segmentation_email_body(body: str) -> str:
    """Cap body length for segmentation prompts (new content is usually at the top)."""
    from services.email.segmentation import strip_quoted_thread_tail

    text = strip_quoted_thread_tail(str(body or ""))
    limit = SEGMENTATION_MAX_BODY_CHARS
    if limit > 0 and len(text) > limit:
        return text[:limit]
    return text


def _build_thread_message_blocks(
    messages: Sequence[Dict[str, Any]],
    *,
    max_messages: int | None = None,
    message_template: str | None = None,
    sanitize: bool = False,
) -> Tuple[str, int]:
    """Return (joined blocks, omitted count). Messages should be newest-first."""
    msg_list = list(messages)
    omitted = 0
    if max_messages is not None and len(msg_list) > max_messages:
        omitted = len(msg_list) - max_messages
        msg_list = msg_list[:max_messages]

    block_tpl = message_template or _thread_message_block_template()
    blocks: List[str] = []
    if omitted:
        blocks.append(
            f"--- Context ---\nOnly the {max_messages} most recent messages are shown "
            f"({omitted} older message(s) omitted from this prompt).\n"
        )
    for i, msg in enumerate(msg_list, start=1):
        dt = str(msg.get("datetime") or msg.get("timestamp") or "").strip()
        if sanitize:
            sender = _sanitize_summary_text(msg.get("sender") or msg.get("from") or "")
            recipients = _sanitize_summary_text(msg.get("recipients") or "")
            subject = _sanitize_summary_text(msg.get("subject") or "")
            content = _sanitize_summary_text(msg.get("content") or "")
        else:
            sender = str(msg.get("sender") or msg.get("from") or "").strip()
            recipients = str(msg.get("recipients") or "").strip()
            subject = str(msg.get("subject") or "").strip()
            content = str(msg.get("content") or "").strip()
        blocks.append(
            block_tpl.format(
                index=i,
                datetime=dt or "(unknown)",
                sender=sender or "(unknown)",
                recipients=recipients or "(unknown)",
                subject=subject or "(none)",
                content=content,
            )
        )
    return "\n".join(blocks), omitted


def format_parse_emails_prompt(emails: List[Any]) -> List[PromptMessages]:
    """Build segmentation prompts. Each item may be a raw body string or a dict with ``body``."""
    prompts: List[PromptMessages] = []
    for email in emails:
        if isinstance(email, dict):
            body = str(email.get("body") or "")
        else:
            body = str(email or "")
        prompts.append(
            _format_prompt_pair("email_segmentation", email_body=_segmentation_email_body(body))
        )
    return prompts


def parse_emails(bodies: List[Any]) -> List[PromptMessages]:
    """Alias for :func:`format_parse_emails_prompt` (one prompt per body string or dict)."""
    return format_parse_emails_prompt(bodies)


def format_thread_summary_prompt(
    messages: Sequence[Dict[str, Any]],
    *,
    message_template: str | None = None,
    as_of: datetime | None = None,
    db_path: str | None = None,
    project_root: Path | None = None,
) -> PromptMessages:
    """
    Build the full thread-summary prompt from structured messages.

    Callers should pass messages **newest first**; only the most recent N are included.
    """
    settings = _load_settings()
    max_messages = int(settings.get("thread_summary_max_messages") or 12)
    thread_messages, _ = _build_thread_message_blocks(
        messages,
        max_messages=max_messages,
        message_template=message_template,
        sanitize=True,
    )
    aliases = ", ".join(_summary_routing_aliases())
    from services.scheduling_availability_step import calendar_context_for_summary_prompt

    calendar_events_block, calendar_timezone = calendar_context_for_summary_prompt(
        db_path=db_path,
        project_root=project_root,
    )
    return _format_prompt_pair(
        "email_thread_summary",
        thread_messages=thread_messages,
        summary_routing_aliases=aliases,
        summary_datetime=summary_as_of_datetime(as_of=as_of),
        calendar_events_block=calendar_events_block,
        calendar_timezone=calendar_timezone,
    )


def format_incremental_thread_summary_prompt(
    prior_summary: Dict[str, Any],
    new_messages: Sequence[Dict[str, Any]],
    *,
    message_template: str | None = None,
    as_of: datetime | None = None,
    db_path: str | None = None,
    project_root: Path | None = None,
) -> PromptMessages:
    """Build an incremental summary prompt from prior summary JSON and new message blocks."""
    new_thread_messages, _ = _build_thread_message_blocks(
        new_messages,
        max_messages=None,
        message_template=message_template,
        sanitize=True,
    )
    aliases = ", ".join(_summary_routing_aliases())
    from services.scheduling_availability_step import calendar_context_for_summary_prompt

    calendar_events_block, calendar_timezone = calendar_context_for_summary_prompt(
        db_path=db_path,
        project_root=project_root,
    )
    prior_json = json.dumps(prior_summary if isinstance(prior_summary, dict) else {}, indent=2)
    return _format_prompt_pair(
        "email_thread_summary_incremental",
        prior_summary_json=prior_json,
        new_thread_messages=new_thread_messages,
        summary_routing_aliases=aliases,
        summary_datetime=summary_as_of_datetime(as_of=as_of),
        calendar_events_block=calendar_events_block,
        calendar_timezone=calendar_timezone,
    )


def format_email_reply_prompt(
    messages: Sequence[Dict[str, Any]],
    response_intent: str,
    *,
    thread_subject: str = "",
    message_template: str | None = None,
) -> PromptMessages:
    """Build a prompt for a draft reply in the owner's voice."""
    intent = (response_intent or "").strip()
    if not intent:
        raise ValueError("response_intent is required: short summary of what the reply must include.")

    max_messages = int(_load_settings().get("email_reply_max_messages") or EMAIL_REPLY_MAX_MESSAGES)
    tail = list(messages)[-max_messages:]
    thread_messages, _ = _build_thread_message_blocks(
        tail,
        max_messages=None,
        message_template=message_template,
        sanitize=False,
    )
    return _format_prompt_pair(
        "email_reply_voice",
        n_messages=len(tail),
        thread_subject=(thread_subject or "").strip(),
        response_intent=intent,
        thread_messages=thread_messages,
    )


def format_image_description_prompt(*, context: str = "") -> PromptMessages:
    return _format_prompt_pair("image_description", context=(context or "").strip())


def _thread_status_label(snoozed: Any) -> str:
    return "snoozed" if int(snoozed or 0) == 1 else "active"


def _format_latest_updates_block(updates: Any) -> str:
    items = [str(u).strip() for u in (updates or []) if str(u).strip()]
    if not items:
        return "Latest updates: (none provided)"
    return "Latest updates:\n" + "\n".join(f"- {item}" for item in items)


def _format_next_steps_block(steps: Any) -> str:
    from utils.owner_config import owner_name

    owner = owner_name()
    lines: List[str] = []
    for step in steps or []:
        if isinstance(step, dict):
            action = str(step.get("action") or "").strip()
            if not action:
                continue
            step_type = str(step.get("type") or "action").strip()
            by_when = str(step.get("by_when") or "").strip()
            lines.append(f"- [{step_type}] {action} (by_when: {by_when})")
        else:
            text = str(step).strip()
            if text:
                lines.append(f"- {text}")
    if not lines:
        return f"Next steps for {owner}: (none)"
    return f"Next steps for {owner}:\n" + "\n".join(lines)


def _lane_thread_summary_block_template() -> str:
    return str(_load_prompts()["lane_thread_summary_block"])


def _summary_datetime_key(summary: Dict[str, Any]) -> str:
    return str(summary.get("datetime") or "").strip()


def _format_thread_summary_block(
    index: int,
    summary: Dict[str, Any],
    *,
    block_template: str | None = None,
    include_datetime: bool = False,
) -> str:
    label = (
        str(summary.get("suggested_thread_label") or summary.get("subject") or summary.get("thread_id") or "")
        .strip()
        or "(unknown)"
    )
    block_tpl = block_template or (
        _lane_thread_summary_block_template() if include_datetime else _thread_summary_block_template()
    )
    dt = _summary_datetime_key(summary) or "(unknown date)"
    return block_tpl.format(
        index=index,
        label=label,
        datetime=dt,
        status=_thread_status_label(summary.get("snoozed")),
        tone=str(summary.get("tone") or "").strip() or "(unknown)",
        last_sender=str(summary.get("last_sender") or "").strip(),
        latest_updates_block=_format_latest_updates_block(summary.get("latest_updates")),
        next_steps_block=_format_next_steps_block(summary.get("next_steps")),
    )


def _build_aggregate_thread_summary_blocks(
    thread_summaries: Sequence[Dict[str, Any]],
    *,
    max_threads: int | None = None,
    block_template: str | None = None,
) -> str:
    """
    Format existing thread summaries for lane/person aggregate prompts.

    Summaries are sorted by ``datetime`` ascending (oldest first). When over the cap,
    only the most recent threads are included, still shown in chronological order.
    """
    summaries = sorted(list(thread_summaries), key=_summary_datetime_key)
    omitted = 0
    if max_threads is not None and max_threads > 0 and len(summaries) > max_threads:
        omitted = len(summaries) - max_threads
        summaries = summaries[-max_threads:]

    blocks: List[str] = []
    if omitted:
        blocks.append(
            f"--- Context ---\nOnly the {max_threads} most recent threads (by email date) "
            f"are shown ({omitted} older thread(s) omitted from this prompt).\n"
        )
    block_tpl = block_template or _lane_thread_summary_block_template()
    for i, summary in enumerate(summaries, start=1):
        blocks.append(
            _format_thread_summary_block(
                i, summary, block_template=block_tpl, include_datetime=True
            )
        )
    return "\n\n".join(blocks)


def format_person_summary_prompt(
    person_name: str,
    thread_summaries: Sequence[Dict[str, Any]],
    *,
    block_template: str | None = None,
    as_of: datetime | None = None,
) -> PromptMessages:
    """
    Build a person-level summary prompt from existing thread summaries (not raw email).

    Each summary dict should include fields produced by thread summarization, such as
    ``datetime``, ``latest_updates``, ``next_steps``, ``tone``, ``suggested_thread_label``,
    and ``snoozed``.
    """
    settings = _load_settings()
    max_threads = int(settings.get("person_summary_max_threads") or 10)
    thread_summaries_text = _build_aggregate_thread_summary_blocks(
        thread_summaries,
        max_threads=max_threads,
        block_template=block_template,
    )
    return _format_prompt_pair(
        "person_summary",
        person_name=(person_name or "").strip() or "(unnamed person)",
        thread_summaries=thread_summaries_text,
        summary_datetime=summary_as_of_datetime(as_of=as_of),
    )


def format_meeting_prep_prompt(
    messages: Sequence[Dict[str, Any]],
    *,
    meeting_title: str = "",
    meeting_start: str = "",
    meeting_end: str = "",
    meeting_location: str = "",
    meeting_attendees: str = "",
    thread_label: str = "",
    message_template: str | None = None,
) -> PromptMessages:
    """Build a prompt to prepare the owner for a meeting using email thread context."""
    settings = _load_settings()
    max_messages = int(settings.get("meeting_prep_max_messages") or 10)
    thread_messages, _ = _build_thread_message_blocks(
        messages,
        max_messages=max_messages,
        message_template=message_template,
        sanitize=False,
    )
    return _format_prompt_pair(
        "meeting_prep",
        meeting_title=(meeting_title or "").strip() or "(No title)",
        meeting_start=(meeting_start or "").strip() or "(unknown)",
        meeting_end=(meeting_end or "").strip() or "(unknown)",
        meeting_location=(meeting_location or "").strip() or "(none)",
        meeting_attendees=(meeting_attendees or "").strip() or "(none)",
        thread_label=(thread_label or "").strip() or "(unknown thread)",
        thread_messages=thread_messages,
    )
