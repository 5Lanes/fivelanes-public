"""GAI chat: answer questions over Fivelanes timeline data."""

from __future__ import annotations

import json
import queue
import re
import threading
from typing import Any, Callable, Dict, Iterator, List, Optional

from services.gai.db_context import (
    database_schema_summary,
    execute_readonly_sql,
    format_arrivals_context,
    format_person_heard_answer,
    format_person_response_answer,
    format_person_response_context,
    format_person_said_answer,
    format_query_result,
    is_heard_from_question,
    is_what_said_question,
    load_arrivals_today,
    load_person_response_context,
    local_timezone_name,
    resolve_person_reference,
    structured_snapshot,
)
from services.llama_service import (
    MODEL_SUMMARY,
    _extract_first_json_object,
    _resolve_ollama_model,
    call_ollama_json,
    stream_ollama_text,
)
from services.prompts import PromptMessages
from utils.owner_config import owner_name
from utils.runtime_paths import env_file

EmitFn = Callable[[Dict[str, Any]], None]

_SQL_RESPONSE_FORMAT = {
    "type": "object",
    "properties": {
        "sql": {"type": "string"},
        "reasoning": {"type": "string"},
    },
    "required": ["sql", "reasoning"],
}

_SQL_SYSTEM = """You help query a Fivelanes SQLite database. Given a user question and schema context,
write exactly one read-only SELECT query to answer it.

Rules:
- Output JSON only with keys: sql, reasoning.
- Use only SELECT statements. No INSERT, UPDATE, DELETE, DDL, or PRAGMA.
- Prefer joins and filters over SELECT * on large tables.
- Avoid selecting full message bodies (body, raw_text) unless the user explicitly asks for message text.
- Use COLLATE NOCASE for case-insensitive name matches when helpful.
- If the question can be answered from the structured snapshot alone, still write a simple SELECT
  that confirms the fact (e.g. COUNT or a filtered list).

Date and message-source rules:
- Only filter by today / local midnight when the user explicitly asks about today, arrivals, or
  what is new. Do NOT assume "today" for general questions like "did X respond?" or "what did Y say?"
- "Today" means message_activity.local_today from the snapshot.
- "Arrived today" means timeline_entries since message_activity.since_local_midnight_utc, plus
  message_activity.new_since_refresh_total for chat channels.
- timeline_entries.datetime uses ISO 8601 UTC (e.g. 2026-07-06T17:53:32+00:00).
- message_outputs holds processed messages from email, Slack, SMS, LinkedIn, etc.
  Slack threads use thread_id like 'slack:...'. For chat questions, search this table.
- For "did X respond?" / "has X replied?": find recent messages in the relevant thread(s),
  filter by sender or subject matching X, order by datetime DESC, and compare who sent the latest
  message. Do not restrict to today unless the user asked about today.
"""

_ANSWER_SYSTEM = """You are Alfred, a helpful assistant for the user {owner_name}. You have access to their emails, calendar, Slack, SMS, and LinkedIn messages.
- Answer only from the query result rows provided. Do not use query reasoning, conversation history, or guess a person's name when rows are empty.
- If query results have zero rows, say you found no matching messages in the database. Do not name a person unless their name appears in the result rows.
- Do not assume a date/time unless the user explicitly asks for it.
- Database datetimes are stored in UTC. When rows include *_local fields or user_timezone, state times in the user's local timezone ({user_timezone}). Never report a UTC clock time as if it were local.
- Respond as a personal assistant to a busy professional would. Be concise and specific.
- Do not invent data that is not in the query results."""


def _env_path() -> str:
    return str(env_file())


def _llm_model() -> str:
    return _resolve_ollama_model(_env_path(), "OLLAMA_MODEL_SUMMARY", MODEL_SUMMARY)


def _is_arrivals_today_question(question: str) -> bool:
    q = question.lower()
    return bool(re.search(r"\btoday\b", q) and re.search(r"\b(email|message|arriv)", q))


def _emit(emit: Optional[EmitFn], event: Dict[str, Any]) -> None:
    if emit is not None:
        emit(event)


def _sql_prompt(
    *,
    question: str,
    schema: str,
    snapshot: Dict[str, Any],
    history: List[Dict[str, str]],
    sql_error: str = "",
) -> PromptMessages:
    history_block = ""
    if history:
        history_block = "Recent conversation:\n" + "\n".join(
            f"- {turn['role']}: {turn['content']}" for turn in history[-6:]
        )
    error_block = ""
    if sql_error:
        error_block = f"\nPrevious SQL failed with error: {sql_error}\nFix the query and try again.\n"
    return PromptMessages(
        system=_SQL_SYSTEM,
        user=(
            f"{history_block}\n"
            f"{error_block}\n"
            f"Database schema:\n{schema}\n\n"
            f"Structured snapshot:\n{json.dumps(snapshot, indent=2, default=str)}\n\n"
            f"User question: {question}\n\n"
            'Return JSON: {"sql": "...", "reasoning": "..."}'
        ),
    )


def _answer_prompt(
    *,
    question: str,
    sql: str,
    reasoning: str,
    query_result: Dict[str, Any] | None,
    arrivals: Dict[str, Any] | None,
    person_response: Dict[str, Any] | None,
    snapshot: Dict[str, Any],
) -> PromptMessages:
    if arrivals is not None:
        data_block = f"Arrivals data:\n{format_arrivals_context(arrivals)}"
        sql_block = "(structured arrivals lookup — not raw SQL)"
    elif person_response is not None:
        data_block = f"Person response data:\n{format_person_response_context(person_response)}"
        sql_block = "(structured person-response lookup — not raw SQL)"
    else:
        data_block = f"Query results:\n{format_query_result(query_result or {})}"
        sql_block = sql
    return PromptMessages(
        system=_ANSWER_SYSTEM.format(
            owner_name=owner_name() or "the user",
            user_timezone=local_timezone_name(),
        ),
        user=(
            f"User question: {question}\n\n"
            f"User timezone: {local_timezone_name()}\n"
            f"Query reasoning: {reasoning}\n"
            f"SQL executed:\n{sql_block}\n\n"
            f"{data_block}\n\n"
            f"Structured snapshot (for reference):\n"
            f"{json.dumps(snapshot, indent=2, default=str)}\n\n"
            "Write a direct answer in plain text. Use datetime_local values when present."
        ),
    )


def _ask_llm_for_sql(
    *,
    question: str,
    schema: str,
    snapshot: Dict[str, Any],
    history: List[Dict[str, str]],
    sql_error: str = "",
    emit: Optional[EmitFn] = None,
) -> Dict[str, Any]:
    _emit(emit, {"type": "progress", "message": "Generating SQL query with Llama…"})
    _emit(emit, {"type": "stage", "stage": "sql", "message": "Model output"})
    prompt = _sql_prompt(
        question=question,
        schema=schema,
        snapshot=snapshot,
        history=history,
        sql_error=sql_error,
    )
    if emit is not None:
        combined = ""
        for kind, chunk in stream_ollama_text(
            prompt,
            model=_llm_model(),
            max_tokens=800,
            env_path=_env_path(),
            response_format=_SQL_RESPONSE_FORMAT,
        ):
            if kind != "response":
                continue
            combined += chunk
            _emit(emit, {"type": "token", "stage": "sql", "text": chunk})
        extracted = _extract_first_json_object(combined)
        if extracted:
            return extracted
        return {"raw_text": combined}

    return call_ollama_json(
        prompt,
        model=_llm_model(),
        max_tokens=800,
        env_path=_env_path(),
        response_format=_SQL_RESPONSE_FORMAT,
    )


def _ask_llm_for_answer(
    *,
    question: str,
    sql: str,
    reasoning: str,
    query_result: Dict[str, Any] | None,
    arrivals: Dict[str, Any] | None,
    person_response: Dict[str, Any] | None,
    snapshot: Dict[str, Any],
    emit: Optional[EmitFn] = None,
) -> tuple[str, str]:
    """Returns ``(answer, thinking)``. ``thinking`` is the model's chain-of-thought trace,
    empty if the model doesn't support reasoning."""
    _emit(emit, {"type": "progress", "message": "Writing answer with Llama…"})
    _emit(emit, {"type": "stage", "stage": "answer", "message": "Model output"})
    prompt = _answer_prompt(
        question=question,
        sql=sql,
        reasoning=reasoning,
        query_result=query_result,
        arrivals=arrivals,
        person_response=person_response,
        snapshot=snapshot,
    )
    if emit is not None:
        combined = ""
        thinking = ""
        for kind, chunk in stream_ollama_text(
            prompt,
            model=_llm_model(),
            max_tokens=1200,
            env_path=_env_path(),
            response_format=None,
            think=True,
            high_priority=True,
        ):
            if kind == "thinking":
                thinking += chunk
                _emit(emit, {"type": "token", "stage": "answer", "kind": "thinking", "text": chunk})
            else:
                combined += chunk
                _emit(emit, {"type": "token", "stage": "answer", "text": chunk})
        return combined.strip() or "No answer returned.", thinking.strip()

    result = call_ollama_json(
        prompt,
        model=_llm_model(),
        max_tokens=1200,
        env_path=_env_path(),
        response_format=None,
        think=True,
        high_priority=True,
    )
    thinking = str(result.get("_thinking") or "").strip()
    raw = (result.get("raw_text") or result.get("answer") or "").strip()
    if raw:
        return raw, thinking
    return json.dumps(result, indent=2, default=str), thinking


def _person_lookup_result(
    *,
    db_path: str,
    question: str,
    person: str,
    emit: Optional[EmitFn] = None,
) -> Dict[str, Any]:
    _emit(emit, {"type": "progress", "message": f"Looking up messages involving {person}…"})
    person_response = load_person_response_context(db_path, person)
    if is_what_said_question(question):
        reasoning = f"Resolved follow-up person {person!r} and loaded their latest message."
        answer = format_person_said_answer(person_response)
        lookup = "(structured what-they-said lookup)"
    elif is_heard_from_question(question):
        reasoning = f"Matched heard-from question for {person!r}."
        answer = format_person_heard_answer(person_response)
        lookup = "(structured heard-from lookup)"
    elif re.search(r"\brespond", question, re.IGNORECASE):
        reasoning = f"Matched person-response question for {person!r}."
        answer = format_person_response_answer(person_response)
        lookup = "(structured person-response lookup)"
    else:
        reasoning = f"Resolved person reference {person!r}."
        answer = format_person_said_answer(person_response)
        lookup = "(structured person lookup)"

    _emit(emit, {"type": "progress", "message": reasoning})
    _emit(emit, {"type": "token", "stage": "answer", "text": answer})
    return {
        "ok": True,
        "question": question,
        "sql": lookup,
        "reasoning": reasoning,
        "row_count": person_response.get("chat_message_count", 0),
        "truncated": False,
        "answer": answer,
        "last_person": person.lower(),
    }


def _should_use_person_lookup(question: str, person: str | None) -> bool:
    if not person:
        return False
    if is_what_said_question(question) or is_heard_from_question(question):
        return True
    if re.search(r"\brespond", question, re.IGNORECASE):
        return True
    if re.search(r"\b(said|wrote|message|email|slack|text)\b", question, re.IGNORECASE):
        return True
    return False


def _run_gai_chat(
    db_path: str,
    question: str,
    *,
    history: List[Dict[str, str]] | None = None,
    session_context: Dict[str, Any] | None = None,
    emit: Optional[EmitFn] = None,
) -> Dict[str, Any]:
    schema = database_schema_summary(db_path)
    snapshot = structured_snapshot(db_path)
    turns = list(history or [])
    session = session_context if session_context is not None else {}

    if _is_arrivals_today_question(question):
        _emit(emit, {"type": "progress", "message": "Checking today's arrivals…"})
        arrivals = load_arrivals_today(db_path)
        reasoning = (
            "Used Fivelanes arrival semantics: timeline_entries since local midnight plus "
            "new_since_refresh for chat channels."
        )
        answer, thinking = _ask_llm_for_answer(
            question=question,
            sql="",
            reasoning=reasoning,
            query_result=None,
            arrivals=arrivals,
            person_response=None,
            snapshot=snapshot,
            emit=emit,
        )
        out = {
            "ok": True,
            "question": question,
            "sql": "(structured arrivals lookup)",
            "reasoning": reasoning,
            "row_count": arrivals["timeline_arrivals_count"] + arrivals["new_since_refresh_total"],
            "truncated": False,
            "answer": answer,
            "last_person": session.get("last_person"),
        }
        if thinking:
            out["thinking"] = thinking
        return out

    person = resolve_person_reference(question, turns, session_context=session)
    if person and _should_use_person_lookup(question, person):
        return _person_lookup_result(
            db_path=db_path,
            question=question,
            person=person,
            emit=emit,
        )

    _emit(emit, {"type": "progress", "message": "Loading database schema…"})
    sql_plan = _ask_llm_for_sql(
        question=question,
        schema=schema,
        snapshot=snapshot,
        history=turns,
        emit=emit,
    )
    sql = (sql_plan.get("sql") or "").strip()
    reasoning = (sql_plan.get("reasoning") or "").strip()
    if not sql:
        raise RuntimeError(f"LLM did not return SQL: {json.dumps(sql_plan, default=str)}")

    _emit(emit, {"type": "sql", "sql": sql, "reasoning": reasoning})
    _emit(emit, {"type": "progress", "message": "Running SQL query…"})

    try:
        query_result = execute_readonly_sql(db_path, sql)
    except ValueError as exc:
        return {
            "ok": False,
            "question": question,
            "sql": sql,
            "reasoning": reasoning,
            "error": str(exc),
        }
    except Exception as exc:
        _emit(emit, {"type": "progress", "message": f"SQL failed ({exc}); retrying…"})
        sql_plan = _ask_llm_for_sql(
            question=question,
            schema=schema,
            snapshot=snapshot,
            history=turns,
            sql_error=str(exc),
            emit=emit,
        )
        sql = (sql_plan.get("sql") or "").strip()
        reasoning = (sql_plan.get("reasoning") or "").strip()
        if not sql:
            return {
                "ok": False,
                "question": question,
                "sql": sql,
                "reasoning": reasoning,
                "error": str(exc),
            }
        _emit(emit, {"type": "sql", "sql": sql, "reasoning": reasoning})
        _emit(emit, {"type": "progress", "message": "Running SQL query (retry)…"})
        try:
            query_result = execute_readonly_sql(db_path, sql)
        except Exception as retry_exc:
            return {
                "ok": False,
                "question": question,
                "sql": sql,
                "reasoning": reasoning,
                "error": str(retry_exc),
            }

    _emit(
        emit,
        {
            "type": "progress",
            "message": f"Query returned {query_result['row_count']} row(s)…",
        },
    )

    if query_result["row_count"] == 0:
        fallback_person = person or resolve_person_reference(question, turns, session_context=session)
        if fallback_person:
            _emit(
                emit,
                {
                    "type": "progress",
                    "message": f"No SQL rows; trying person lookup for {fallback_person}…",
                },
            )
            return _person_lookup_result(
                db_path=db_path,
                question=question,
                person=fallback_person,
                emit=emit,
            )

    answer, thinking = _ask_llm_for_answer(
        question=question,
        sql=query_result["sql"],
        reasoning=reasoning,
        query_result=query_result,
        arrivals=None,
        person_response=None,
        snapshot=snapshot,
        emit=emit,
    )
    out = {
        "ok": True,
        "question": question,
        "sql": query_result["sql"],
        "reasoning": reasoning,
        "row_count": query_result["row_count"],
        "truncated": query_result["truncated"],
        "answer": answer,
        "last_person": person or session.get("last_person"),
    }
    if thinking:
        out["thinking"] = thinking
    return out


def iter_gai_chat_events(
    db_path: str,
    question: str,
    *,
    history: List[Dict[str, str]] | None = None,
    session_context: Dict[str, Any] | None = None,
) -> Iterator[Dict[str, Any]]:
    """Yield progress/token events in real time, ending with type=done or type=error."""
    event_queue: queue.Queue = queue.Queue()
    result_holder: Dict[str, Any] = {}
    error_holder: List[BaseException] = []

    def emit(event: Dict[str, Any]) -> None:
        event_queue.put(event)

    def worker() -> None:
        try:
            result_holder["result"] = _run_gai_chat(
                db_path,
                question,
                history=history,
                session_context=session_context,
                emit=emit,
            )
        except Exception as exc:
            error_holder.append(exc)
        finally:
            event_queue.put(None)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    while True:
        event = event_queue.get()
        if event is None:
            break
        yield event

    thread.join()

    if error_holder:
        yield {"type": "error", "ok": False, "error": str(error_holder[0])}
        return

    result = result_holder.get("result", {})
    if result.get("ok"):
        yield {"type": "done", **result}
    else:
        yield {"type": "error", **result}


def answer_question(
    db_path: str,
    question: str,
    *,
    history: List[Dict[str, str]] | None = None,
    session_context: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Run the full GAI pipeline for one question."""
    return _run_gai_chat(
        db_path,
        question,
        history=history,
        session_context=session_context,
        emit=None,
    )
