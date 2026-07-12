"""
Cross-source briefing: synthesizes lanes, thread plans, and meetings into one narrative.

Purely a read/summarize layer over data the user has already curated (lane membership,
conversation linking, plan creation are all manual — see project policy against
auto-tagging/auto-merging). This module never writes to lanes/conversations; it only reads
existing rows and asks the LLM to narrate them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from services.llm_service import get_llm_backend
from services.prompts import PromptMessages
from utils.database import (
    fetch_meetings_rows,
    load_all_lane_summaries,
    load_all_lanes,
    load_all_thread_plans,
)

RECENT_LANE_WINDOW_HOURS = 24
UPCOMING_MEETING_DAYS = 7
DUE_SOON_DAYS = 3


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
                "updated_at": summary.get("updated_at") or "",
            }
        )
    active.sort(key=lambda item: item["updated_at"], reverse=True)
    return active


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


def _digest_prompt(
    *,
    active_lanes: List[Dict[str, Any]],
    overdue_plans: List[Dict[str, Any]],
    due_soon_plans: List[Dict[str, Any]],
    upcoming_meetings: List[Dict[str, Any]],
) -> PromptMessages:
    system = (
        "You write a short daily briefing for a busy professional, synthesizing what changed "
        "and what's coming up across their lanes, action items, and meetings. Ground every "
        "sentence in the data given — never invent names, dates, or requests not present in the "
        "input. 3-6 sentences, plain prose, no headers or bullet lists."
    )
    lanes_block = "\n".join(
        f"- {lane['name']}: {lane['summary']}" for lane in active_lanes
    ) or "(no lanes updated recently)"
    overdue_block = "\n".join(
        f"- {plan.get('action')} (due {plan.get('by_when')})" for plan in overdue_plans
    ) or "(none)"
    due_soon_block = "\n".join(
        f"- {plan.get('action')} (due {plan.get('by_when')})" for plan in due_soon_plans
    ) or "(none)"
    meetings_block = "\n".join(
        f"- {m.get('summary')} at {m.get('start_iso')}" for m in upcoming_meetings[:10]
    ) or "(none)"
    user = (
        f"Recently updated lanes:\n{lanes_block}\n\n"
        f"Overdue action items:\n{overdue_block}\n\n"
        f"Action items due within {DUE_SOON_DAYS} days:\n{due_soon_block}\n\n"
        f"Upcoming meetings (next {UPCOMING_MEETING_DAYS} days):\n{meetings_block}\n\n"
        "Write the briefing now."
    )
    return PromptMessages(system=system, user=user)


def build_digest_payload(db_path: str, *, env_path: str = ".env") -> Dict[str, Any]:
    """
    Read-only synthesis across lanes/thread_plans/meetings. Does not create or modify any
    lane, conversation, or plan row.
    """
    now = datetime.now(timezone.utc)
    plans = _partition_plans(load_all_thread_plans(db_path), now=now)
    active_lanes = _recently_active_lanes(db_path=db_path, now=now)
    upcoming_meetings = _upcoming_meetings(db_path=db_path)

    narrative = ""
    if active_lanes or plans["overdue"] or plans["due_soon"] or upcoming_meetings:
        prompt = _digest_prompt(
            active_lanes=active_lanes,
            overdue_plans=plans["overdue"],
            due_soon_plans=plans["due_soon"],
            upcoming_meetings=upcoming_meetings,
        )
        llm = get_llm_backend(env_path=env_path)
        result = llm.submit_digest(prompt)
        narrative = str(result.get("narrative") or "").strip()

    return {
        "ok": True,
        "narrative": narrative,
        "overdue_plans": plans["overdue"],
        "due_soon_plans": plans["due_soon"],
        "upcoming_meetings": upcoming_meetings,
        "active_lanes": active_lanes,
        "generated_at": now.isoformat(),
    }
