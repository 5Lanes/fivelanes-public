"""
Cross-source briefing: synthesizes lanes, thread plans, and meetings into what Alfred (see
``services/gai/chat.py`` for his persona — the same "personal assistant to a busy
professional," concise and specific, never inventing data) says to the user, most-urgent-first,
as one flowing sequence — not a categorized dashboard. There are no section headers or lane
groupings in the output; it reads like someone talking to you as you walk in.

Purely a read/summarize layer over data the user has already curated (lane membership,
conversation linking, plan creation are all manual — see project policy against
auto-tagging/auto-merging). This module never writes to lanes/conversations; it only reads
existing rows and asks the LLM to narrate them.

Neuro-symbolic split (see PRD.md): the LLM's only job is turning a lane's real conversation
content into one natural, second-person sentence — that's the one input here that's actual
free-form language needing synthesis. Everything else is formatted deterministically in code,
because it already comes as structured facts (an action string, a due date, a meeting
title/time/location) that don't need — and shouldn't get — a paraphrase:
- Plans (overdue / due soon) become a plain templated sentence; formatting them in code means
  the item can never drift from what the plan row actually says.
- A lane whose summary was itself produced deterministically by
  ``deterministic_calendar_only_lane_summary`` (tagged ``tone_overview == "calendar only"``)
  gets its item from ``_calendar_only_lane_item`` — whether its dates are past or future is a
  date-comparison question with one right answer, not a judgment call.
- Meetings never reach the LLM. Today's and tomorrow's meetings are stated as plain facts
  (title/time/location) via ``_format_meeting_item`` — that's just restating calendar data, no
  invention involved. Meetings further out only surface if their own title already names a
  pending action (``_deterministic_meeting_item``); the meetings table has no description/
  agenda field, so asking an LLM "is this meeting actionable and what's needed" would be asking
  it to invent an unstated task from the title alone, which it has been observed doing
  (fabricating "prepare an agenda" for a bare "Planning Meeting").

Each returned item carries ``votable`` and ``inbox_thread_id``: only the LLM-narrated lane
sentences are votable (the rest are just restated facts with nothing to act on) and only they
carry a thread id — that's what lets the frontend's "Add to plans" button turn a sentence into
a real ``thread_plans`` row via the existing ``POST /api/plans/create`` (see
``_first_thread_by_lane``: since plans are stored per-thread, not per-lane, the item is
attached to one representative thread in that lane).

One briefing is generated per calendar day (``services/digest/store.py``) — see
``build_digest_payload`` — and both "Clear" and "Add to plans" permanently dismiss an item via
that same store, so a cleared/added item never comes back for the rest of the day.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from services.digest.store import item_id, load_daily_digest, save_daily_digest
from services.llm_service import get_llm_backend
from services.prompts import PromptMessages
from utils.database import (
    fetch_meetings_rows,
    load_all_lane_summaries,
    load_all_lanes,
    load_all_thread_plans,
    load_lane_thread_memberships,
)

RECENT_LANE_WINDOW_HOURS = 24
UPCOMING_MEETING_DAYS = 7
DUE_SOON_DAYS = 3

_CALENDAR_HIGHLIGHT_RE = re.compile(r"^(.*?)\s*—\s*on the calendar for\s*(.+)$")

# Phrases asserting a scheduling conflict are never verified against the calendar (see
# PRD.md's scheduling-availability rationale) — a lane's free text may claim one, but the
# digest prompt has been observed to restate that claim as fact even when told not to. Since
# "did this text make an unverified conflict claim" has one right answer, it's enforced here
# in code rather than trusted to model compliance.
_UNVERIFIED_CONFLICT_RE = re.compile(
    r"conflict|double.?book|already (?:has|have) a commitment", re.IGNORECASE
)

# The prompt already tells the model to omit a lane entirely when it has nothing unresolved,
# but it has been observed emitting a filler item instead (e.g. "Divorce: No items, all
# actions completed or past.") — pure noise in a "scannable" briefing. Whether an item states
# there's nothing to act on is a fixed set of phrasings, so it's caught here rather than
# re-relied on the model to self-censor.
_NO_ACTION_ITEM_RE = re.compile(
    r"\bno (?:items?|actions?|further action)\b|\ball actions? (?:are |is )?(?:completed|"
    r"resolved|done)\b|\bnothing (?:to report|further)\b",
    re.IGNORECASE,
)


# Meetings rows carry no description/agenda field — only title, time, location, attendees
# (see fetch_meetings_rows in utils/database.py). Asking the LLM "is this meeting actionable
# and what's needed" therefore asks it to invent an unstated task from the title alone; it has
# been observed doing exactly that (fabricating "prepare an agenda" for a bare "Planning
# Meeting", or generating an item for a plain "Camp dropoff" despite being told to skip
# routine ones). Whether a title itself already names a pending action, or is one of a small
# set of routine kinds, is regex-determinable — one right answer, not a judgment call — so it's
# resolved here in code and no meeting ever reaches the digest LLM prompt.
_ROUTINE_MEETING_TITLE_RE = re.compile(
    r"\b(drop.?off|pick.?up|flight)\b", re.IGNORECASE
)

_MEETING_ACTION_TITLE_RE = re.compile(
    r"\b(reschedule|resched|confirm|rsvp|tbd|tentative|unconfirmed|to be confirmed)\b",
    re.IGNORECASE,
)


def _deterministic_meeting_item(meeting: Dict[str, Any]) -> Optional[str]:
    """
    Beyond today/tomorrow (see ``_format_meeting_item``), a meeting only gets a digest item
    when its own title already names the pending action (e.g. "Reschedule call with X", "TBD -
    venue") — copied verbatim, never elaborated. No other signal about a meeting's
    actionability exists in the data, so nothing else qualifies.
    """
    summary = str(meeting.get("summary") or "").strip()
    if not summary or _ROUTINE_MEETING_TITLE_RE.search(summary):
        return None
    if not _MEETING_ACTION_TITLE_RE.search(summary):
        return None
    local_start = _meeting_local_start(meeting)
    when = local_start.strftime("%a %b %-d") if local_start else ""
    return f"{summary} is on {when}." if when else f"{summary} is coming up."


def _meeting_local_start(meeting: Dict[str, Any]) -> Optional[datetime]:
    """Parsed with the offset the row already carries, so display shows the meeting's actual
    local wall-clock time rather than a UTC-shifted one."""
    raw = str(meeting.get("start_iso") or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _format_meeting_item(meeting: Dict[str, Any], *, day_label: str) -> Optional[str]:
    """Today's/tomorrow's meetings are stated as plain facts — title, time, location — with no
    judgment about whether they're "actionable". That's just restating the calendar, and it's
    the single highest-value thing a daily briefing can show without any risk of invention."""
    summary = str(meeting.get("summary") or "").strip()
    if not summary:
        return None
    local_start = _meeting_local_start(meeting)
    time_str = local_start.strftime("%-I:%M %p") if local_start else ""
    location_raw = str(meeting.get("location") or "").strip()
    location = location_raw.splitlines()[0] if location_raw else ""
    text = f"You've got {summary} {day_label} at {time_str}." if time_str else f"You've got {summary} {day_label}."
    if location:
        text = text[:-1] + f", at {location}."
    return text


def _parse_ymd_or_datetime(raw: str) -> Optional[datetime]:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            parsed = datetime.strptime(s, "%Y-%m-%d")
        else:
            parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _partition_plans(plans: List[Dict[str, Any]], *, now: datetime) -> Dict[str, List[Dict[str, Any]]]:
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    due_soon_end = today_start + timedelta(days=DUE_SOON_DAYS + 1)
    overdue: List[Dict[str, Any]] = []
    due_soon: List[Dict[str, Any]] = []
    for plan in plans:
        due = _parse_ymd_or_datetime(str(plan.get("by_when") or ""))
        if due is None:
            continue
        if due < today_start:
            overdue.append(plan)
        elif due < due_soon_end:
            due_soon.append(plan)
    return {"overdue": overdue, "due_soon": due_soon}


def _format_plan_item(
    plan: Dict[str, Any], *, overdue: bool, lane_name: Optional[str] = None
) -> Optional[str]:
    action = str(plan.get("action") or "").strip()
    if not action:
        return None
    by_when = str(plan.get("by_when") or "").strip()
    lead_in = f"{lane_name} — " if lane_name else ""
    if overdue:
        tail = f", that's overdue (it was due {by_when})." if by_when else ", that's overdue."
    else:
        tail = f", due {by_when}." if by_when else "."
    return f"{lead_in}{action}{tail}"


# The digest prompt requires every LLM sentence to start with a plain imperative verb (see
# _digest_prompt) — once that's guaranteed, extracting an action-type pill from the leading
# word is a one-right-answer parsing problem, not a judgment call, so it's done here rather
# than asking the model to separately self-classify (another chance to drift/invent).
_ACTION_VERB_LABELS: Dict[str, str] = {
    "follow": "Follow Up",
    "followup": "Follow Up",
    "prepare": "Prepare",
    "confirm": "Confirm",
    "respond": "Respond",
    "reply": "Respond",
    "review": "Review",
    "reschedule": "Reschedule",
    "propose": "Propose",
    "send": "Send",
    "pay": "Pay",
    "sign": "Sign",
    "wait": "Wait",
    "schedule": "Schedule",
    "validate": "Validate",
    "offer": "Offer",
    "focus": "Focus",
}


def _sentence_action_label(text: str) -> Optional[str]:
    words = re.findall(r"[A-Za-z']+", str(text or "").strip())
    if not words:
        return None
    return _ACTION_VERB_LABELS.get(words[0].lower(), words[0].capitalize())


def _digest_entry(
    text: str, *, action: Optional[str], person: Optional[str], lane: Optional[str]
) -> Dict[str, Any]:
    return {
        "text": text,
        "votable": False,
        "inbox_thread_id": None,
        "action": action,
        "person": person,
        "lane": lane,
    }


def _person_pill(raw_person: Any, *, lane_name: Optional[str]) -> Optional[str]:
    """Only shown when it's a plausible short name and not just a restatement of the lane
    pill — e.g. skip a "person" of "Sergey Kizyan" when the lane itself is already named
    Sergey Kizyan, since that's one pill's worth of information, not two."""
    person = str(raw_person or "").strip()
    if not person or len(person.split()) > 4:
        return None
    if lane_name and person.lower() == lane_name.strip().lower():
        return None
    return person


def _lane_name_by_thread(db_path: str) -> Dict[str, str]:
    """``inbox_thread_id`` -> lane name, for grouping plan items (which only carry a thread id)
    under the same lane header as that thread's other digest items. A thread in more than one
    lane keeps the first membership found — same ambiguity the rest of the dashboard accepts."""
    lanes_by_id = {lane["id"]: lane.get("name") or "" for lane in load_all_lanes(db_path)}
    memberships = load_lane_thread_memberships(db_path)
    by_thread: Dict[str, str] = {}
    for lane_id_str, thread_ids in memberships.items():
        try:
            name = lanes_by_id[int(lane_id_str)]
        except (KeyError, ValueError):
            continue
        if not name:
            continue
        for thread_id in thread_ids:
            by_thread.setdefault(thread_id, name)
    return by_thread


def _first_thread_by_lane(db_path: str) -> Dict[int, str]:
    """``lane_id`` -> one representative ``inbox_thread_id`` in that lane, for "Add to plans" on
    a lane-narrated digest item — thread_plans are stored per-thread, not per-lane, so turning a
    lane sentence into a trackable plan needs *a* thread to attach it to. Picks the first
    membership (same ambiguity ``_lane_name_by_thread`` already accepts for a multi-thread
    lane)."""
    memberships = load_lane_thread_memberships(db_path)
    out: Dict[int, str] = {}
    for lane_id_str, thread_ids in memberships.items():
        if not thread_ids:
            continue
        try:
            out[int(lane_id_str)] = thread_ids[0]
        except ValueError:
            continue
    return out


def _meetings_by_day(
    meetings: List[Dict[str, Any]], *, now: datetime
) -> Dict[str, List[Dict[str, Any]]]:
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_start = today_start + timedelta(days=1)
    day_after_start = tomorrow_start + timedelta(days=1)
    today: List[Dict[str, Any]] = []
    tomorrow: List[Dict[str, Any]] = []
    later: List[Dict[str, Any]] = []
    for meeting in meetings:
        start = _parse_ymd_or_datetime(str(meeting.get("start_iso") or ""))
        if start is None:
            continue
        if today_start <= start < tomorrow_start:
            today.append(meeting)
        elif tomorrow_start <= start < day_after_start:
            tomorrow.append(meeting)
        else:
            later.append(meeting)
    today.sort(key=lambda m: str(m.get("start_iso") or ""))
    tomorrow.sort(key=lambda m: str(m.get("start_iso") or ""))
    return {"today": today, "tomorrow": tomorrow, "later": later}


def _recently_active_lanes(*, db_path: str, now: datetime) -> List[Dict[str, Any]]:
    lanes_by_id = {lane["id"]: lane for lane in load_all_lanes(db_path) if not lane.get("archived")}
    summaries = load_all_lane_summaries(db_path)
    cutoff = now - timedelta(hours=RECENT_LANE_WINDOW_HOURS)
    active: List[Dict[str, Any]] = []
    for lane_id_str, summary in summaries.items():
        try:
            lane_id = int(lane_id_str)
        except ValueError:
            continue
        lane = lanes_by_id.get(lane_id)
        if not lane:
            continue
        updated = _parse_ymd_or_datetime(str(summary.get("updated_at") or ""))
        if updated is None or updated < cutoff:
            continue
        active.append(
            {
                "lane_id": lane_id,
                "name": lane.get("name") or "",
                "summary": summary.get("summary") or "",
                "highlights": summary.get("highlights") or [],
                "is_calendar_only": summary.get("tone_overview") == "calendar only",
                "updated_at": summary.get("updated_at") or "",
            }
        )
    active.sort(key=lambda item: item["updated_at"], reverse=True)
    return active


def _calendar_only_lane_item(lane: Dict[str, Any], *, now: datetime) -> Optional[str]:
    """
    Deterministic item for a calendar-only lane (see module docstring): parse each
    "<label> — on the calendar for <iso>" highlight, keep only future-dated ones, and state
    them as plain facts. Returns ``None`` (no item, no LLM call) when every date has passed.
    """
    upcoming: List[str] = []
    for highlight in lane.get("highlights") or []:
        m = _CALENDAR_HIGHLIGHT_RE.match(str(highlight))
        if not m:
            continue
        label, when_iso = m.group(1).strip(), m.group(2).strip()
        when = _parse_ymd_or_datetime(when_iso)
        if when is not None and when >= now:
            upcoming.append(f"{label} on {when_iso}")
    if not upcoming:
        return None
    return f"For {lane.get('name')}, you've got {'; '.join(upcoming)} on the calendar."


def _upcoming_meetings(*, db_path: str) -> List[Dict[str, Any]]:
    rows = fetch_meetings_rows(db_path, days=UPCOMING_MEETING_DAYS)
    return [
        {
            "summary": row.get("summary") or "",
            "start_iso": row.get("start_iso") or "",
            "location": row.get("location") or "",
            "attendees": row.get("attendees") or [],
        }
        for row in rows
    ]


def _digest_prompt(*, today: datetime, active_lanes: List[Dict[str, Any]]) -> PromptMessages:
    """
    The LLM sees only lane content now (see module docstring) — plans and meetings are
    formatted deterministically elsewhere and never enter this prompt. Items come back as
    natural sentences (Alfred's voice, see services/gai/chat.py), not "LaneName: ..." labels —
    there's no grouping downstream anymore for a prefix to feed.
    """
    lane_count = len(active_lanes)
    system = (
        f"Today's date is {today.strftime('%Y-%m-%d')}. You are Alfred, a personal assistant "
        "speaking directly to a busy professional as they walk in the door, telling them what "
        "they need to know to get started — the same voice as Alfred's chat persona: "
        "concise, specific, and grounded only in the data given. Never invent a name, date, or "
        "request not present in the input. Plain text only — no markdown, no asterisks, no "
        "bold, no headers.\n\n"
        f"You are given {lane_count} 'Recently updated lanes' below. Write exactly one natural, "
        f"second-person sentence for each of the {lane_count} lanes, in the same order, even if "
        "brief — unless a lane's only content is one or more dates that have already passed "
        "with no unresolved action mentioned, in which case omit that lane's sentence "
        "entirely. Every date in a lane's text is relative to today's date above; never phrase "
        "a past date as something to prepare for, confirm, or schedule — a lane whose only "
        "content is past calendar entries with nothing left to resolve gets no sentence at "
        "all.\n\n"
        "Never use the words 'conflict', 'double-book', 'double-booked', or say the user "
        "'already has a commitment' at a given time, in any sentence — a lane's text may claim "
        "this, but it has not been checked against the actual calendar and restating that "
        "claim, even without a label, has produced false alarms. If a lane says a meeting "
        "needs to be rescheduled or confirmed, state only the action needed ('reschedule the "
        "July 7 meeting with X') without stating or implying a reason why.\n\n"
        "Each sentence should read like you're actually talking to the person, not filling out "
        "a form: mention who or what it's about by name, naturally, inside the sentence — not "
        "as a 'Label:' prefix. For example write 'Paul Rios still hasn't sent those three "
        "company profiles from July 6.' rather than 'Paul Rios: follow up on profiles.' Start "
        "every sentence with a plain imperative verb (Follow up, Prepare, Confirm, Respond, "
        "Review, Reschedule, Propose, Send, Pay, Sign, Wait, ...) describing the action itself.\n\n"
        "Return each sentence tagged with: the lane_id it's about (copy the number exactly as "
        "given below, don't renumber it), and a 'person' field. Set person whenever the "
        "sentence names a specific individual to act on/with — including inside a "
        "company-or-case-named lane, e.g. a lane named 'MPL Risk' whose sentence is about "
        "Eric LeBlanc still gets person='Eric LeBlanc'. Only leave person empty when the lane "
        "itself is already a person's name (so the person and the lane would be the same "
        "name) or no individual is named at all."
    )
    lanes_block = "\n".join(
        f"{i}. [lane_id={lane['lane_id']}] {lane['name']}: {lane['summary']}"
        for i, lane in enumerate(active_lanes, 1)
    ) or "(no lanes updated recently)"
    user = (
        f"Recently updated lanes ({lane_count} total — one sentence each, see rules above):\n{lanes_block}\n\n"
        "Example: lane_id=6, lane name 'MPL Risk', summary mentions Eric LeBlanc → "
        '{"lane_id": 6, "person": "Eric LeBlanc", "text": "Follow up with Eric LeBlanc about next steps by EOD July 13th."}\n'
        "Example: lane_id=13, lane name 'Sergey Kizyan' (the lane IS the person) → "
        '{"lane_id": 13, "person": "", "text": "Confirm Monday in Back Bay with Sergey Kizyan."}\n\n'
        'Return JSON: {"items": [{"lane_id": 9, "person": "Jane Doe", "text": "..."}, ...]}'
    )
    return PromptMessages(system=system, user=user)


def _generate_digest_payload(db_path: str, *, env_path: str = ".env") -> Dict[str, Any]:
    """
    Read-only synthesis across lanes/thread_plans/meetings. Does not create or modify any
    lane, conversation, or plan row.

    Returns ``items``: ``[{"text", "votable", "inbox_thread_id", "action", "person", "lane"},
    ...]``, one flat sequence in the order Alfred would actually say it — overdue plans,
    due-soon plans, today's meetings, tomorrow's meetings, lane updates (the one LLM-written
    tier), calendar-only lane items, then meetings further out whose title itself names a
    pending action. No section headers or lane grouping in the output (see module docstring).
    ``action``/``person``/``lane`` are pill labels for the frontend — ``None`` when not
    applicable (meeting/calendar-fact items have none; plan items get action+lane but no
    person; lane items can get all three). See ``_sentence_action_label``/``_person_pill`` for
    why each is grounded rather than left to the model to self-report.

    This is the actual (LLM-calling) generation step — called at most once per calendar day by
    ``build_digest_payload``, never directly.
    """
    now = datetime.now(timezone.utc)
    plans = _partition_plans(load_all_thread_plans(db_path), now=now)
    active_lanes = _recently_active_lanes(db_path=db_path, now=now)
    upcoming_meetings = _upcoming_meetings(db_path=db_path)
    day_meetings = _meetings_by_day(upcoming_meetings, now=now)
    thread_to_lane = _lane_name_by_thread(db_path)
    thread_by_lane = _first_thread_by_lane(db_path)

    calendar_only_lanes = [lane for lane in active_lanes if lane["is_calendar_only"]]
    content_lanes = [lane for lane in active_lanes if not lane["is_calendar_only"]]

    def lane_for(plan: Dict[str, Any]) -> Optional[str]:
        return thread_to_lane.get(str(plan.get("inbox_thread_id") or ""))

    def plan_entry(plan: Dict[str, Any], *, overdue: bool) -> Optional[Dict[str, Any]]:
        lane_name = lane_for(plan)
        text = _format_plan_item(plan, overdue=overdue, lane_name=lane_name)
        if not text:
            return None
        step_type = str(plan.get("step_type") or "").strip().lower()
        action = "Respond" if step_type == "response required" else "Follow Up"
        return _digest_entry(text, action=action, person=None, lane=lane_name)

    def bare_entry(text: Optional[str]) -> Optional[Dict[str, Any]]:
        return _digest_entry(text, action=None, person=None, lane=None) if text else None

    overdue_entries = [e for e in (plan_entry(p, overdue=True) for p in plans["overdue"]) if e]
    due_soon_entries = [e for e in (plan_entry(p, overdue=False) for p in plans["due_soon"]) if e]
    today_entries = [
        e
        for e in (bare_entry(_format_meeting_item(m, day_label="today")) for m in day_meetings["today"])
        if e
    ]
    tomorrow_entries = [
        e
        for e in (
            bare_entry(_format_meeting_item(m, day_label="tomorrow")) for m in day_meetings["tomorrow"]
        )
        if e
    ]
    calendar_only_entries = [
        e
        for e in (bare_entry(_calendar_only_lane_item(lane, now=now)) for lane in calendar_only_lanes)
        if e
    ]
    later_action_entries = [
        e for e in (bare_entry(_deterministic_meeting_item(m)) for m in day_meetings["later"]) if e
    ]

    valid_lane_ids = {lane["lane_id"] for lane in content_lanes}
    lane_names_by_id = {lane["lane_id"]: lane["name"] for lane in content_lanes}
    llm_entries: List[Dict[str, Any]] = []
    if content_lanes:
        prompt = _digest_prompt(today=now, active_lanes=content_lanes)
        llm = get_llm_backend(env_path=env_path)
        result = llm.submit_digest(prompt)
        raw_items = result.get("items")
        if isinstance(raw_items, list):
            for raw in raw_items:
                if not isinstance(raw, dict):
                    continue
                text = str(raw.get("text") or "").strip()
                if not text:
                    continue
                if _UNVERIFIED_CONFLICT_RE.search(text) or _NO_ACTION_ITEM_RE.search(text):
                    continue
                try:
                    lane_id = int(raw.get("lane_id"))
                except (TypeError, ValueError):
                    lane_id = None
                if lane_id not in valid_lane_ids:
                    lane_id = None
                lane_name = lane_names_by_id.get(lane_id) if lane_id else None
                entry = _digest_entry(
                    text,
                    action=_sentence_action_label(text),
                    person=_person_pill(raw.get("person"), lane_name=lane_name),
                    lane=lane_name,
                )
                entry["votable"] = True
                entry["inbox_thread_id"] = thread_by_lane.get(lane_id) if lane_id else None
                llm_entries.append(entry)

    items = (
        overdue_entries
        + due_soon_entries
        + today_entries
        + tomorrow_entries
        + llm_entries
        + calendar_only_entries
        + later_action_entries
    )

    return {
        "ok": True,
        "items": items,
        "overdue_plans": plans["overdue"],
        "due_soon_plans": plans["due_soon"],
        "upcoming_meetings": upcoming_meetings,
        "active_lanes": active_lanes,
        "generated_at": now.isoformat(),
    }


def build_digest_payload(db_path: str, *, env_path: str = ".env") -> Dict[str, Any]:
    """
    One briefing per calendar day: if today's batch is already stored (``services/digest/
    store.py``), serve it — the LLM/deterministic build in ``_generate_digest_payload`` never
    runs twice in the same day. Each item gets a stable ``id`` (a hash of its text) so
    "Clear"/"Add to plans" (``dismiss_item``) can permanently remove it from every future
    response for the rest of the day, not just hide it client-side. Dismissed items are
    filtered out of what's returned but kept in storage so they don't come back.
    """
    stored = load_daily_digest()
    if stored is None:
        fresh = _generate_digest_payload(db_path, env_path=env_path)
        for entry in fresh["items"]:
            entry["id"] = item_id(entry["text"])
            entry["dismissed"] = False
        stored = save_daily_digest(fresh)

    out = dict(stored)
    out["items"] = [
        dict(entry) for entry in stored.get("items", []) if not entry.get("dismissed")
    ]
    return out
