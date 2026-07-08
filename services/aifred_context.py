"""Builds the tracked-activity snapshot fed to the 'Ask AIFred' chat prompt."""

from __future__ import annotations

from typing import Any, Dict, List

CONTEXT_CHAR_LIMIT = 12000


def _join_latest_updates(updates: Any) -> str:
    if not isinstance(updates, list):
        return ""
    return "; ".join(str(u).strip() for u in updates[:3] if str(u).strip())


def _join_next_steps(next_steps: Any) -> str:
    if not isinstance(next_steps, list):
        return ""
    parts: List[str] = []
    for step in next_steps:
        if not isinstance(step, dict):
            continue
        action = str(step.get("action") or "").strip()
        if not action:
            continue
        by_when = str(step.get("by_when") or "").strip() or "no date"
        parts.append(f"{action} ({by_when})")
    return "; ".join(parts)


def build_aifred_context(db_path: str, *, max_threads: int = 40, max_plans: int = 40) -> str:
    """Plain-text snapshot of tracked threads, open plans, and lane rollups for the chat prompt."""
    from utils.database import (
        build_summaries_bundle,
        load_all_lane_summaries,
        load_all_lanes,
        load_all_thread_plans,
    )

    bundle = build_summaries_bundle(db_path)
    summaries: List[Dict[str, Any]] = list(bundle.get("summary") or [])
    summaries.sort(key=lambda row: str(row.get("datetime") or ""), reverse=True)
    active = [row for row in summaries if not row.get("snoozed")]

    thread_label_map: Dict[str, str] = {}
    lines: List[str] = ["Tracked threads (most recent activity first):"]
    for row in active[:max_threads]:
        thread_id = str(row.get("thread_id") or "")
        label = (
            str(row.get("suggested_thread_label") or "").strip()
            or str(row.get("subject") or "").strip()
            or thread_id
            or "(untitled thread)"
        )
        if thread_id:
            thread_label_map[thread_id] = label
        tone = str(row.get("tone") or "").strip()
        last_sender = str(row.get("last_sender") or "").strip()
        header = f"- {label} (last update: {row.get('datetime') or 'unknown'}"
        if last_sender:
            header += f", last sender: {last_sender}"
        if tone:
            header += f", tone: {tone}"
        header += ")"
        lines.append(header)
        updates_text = _join_latest_updates(row.get("latest_updates"))
        if updates_text:
            lines.append(f"  Latest: {updates_text}")
        steps_text = _join_next_steps(row.get("next_steps"))
        if steps_text:
            lines.append(f"  Next steps: {steps_text}")

    plans = load_all_thread_plans(db_path)
    if plans:
        lines.append("\nOpen follow-up plans:")
        for plan in plans[:max_plans]:
            thread_id = str(plan.get("inbox_thread_id") or "")
            label = thread_label_map.get(thread_id, thread_id or "(unknown thread)")
            action = str(plan.get("action") or "").strip()
            if not action:
                continue
            by_when = str(plan.get("by_when") or "").strip() or "no date set"
            step_type = str(plan.get("step_type") or "follow up needed").strip()
            lines.append(f"- [{step_type}] {action} — by {by_when} (thread: {label})")

    lane_names = {int(lane["id"]): lane["name"] for lane in load_all_lanes(db_path)}
    lane_summaries = load_all_lane_summaries(db_path)
    if lane_summaries:
        lane_lines: List[str] = []
        for lane_id, payload in lane_summaries.items():
            summary_text = str(payload.get("summary") or "").strip()
            priorities = payload.get("current_priorities") or []
            waiting = payload.get("waiting_on_others") or []
            if not summary_text and not priorities and not waiting:
                continue
            name = lane_names.get(int(lane_id), f"Lane {lane_id}")
            lane_lines.append(f"- {name}: {summary_text}")
            if isinstance(priorities, list) and priorities:
                lane_lines.append(f"  Priorities: {'; '.join(str(p) for p in priorities)}")
            if isinstance(waiting, list) and waiting:
                lane_lines.append(f"  Waiting on others: {'; '.join(str(w) for w in waiting)}")
        if lane_lines:
            lines.append("\nLane rollups:")
            lines.extend(lane_lines)

    text = "\n".join(lines)
    if len(text) > CONTEXT_CHAR_LIMIT:
        text = text[:CONTEXT_CHAR_LIMIT].rstrip() + "\n...(truncated)"
    return text
