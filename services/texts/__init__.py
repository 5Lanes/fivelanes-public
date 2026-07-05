"""
iMessage / SMS threads stored as JSON under ``conversations/``.

Tracked conversations are registered in ``thread_tracking`` (``inbox_thread_id`` prefix
``text:``) and merged into the dashboard summaries bundle for the Threads view.
"""

from services.texts.bundle import append_unsynced_text_threads_to_bundle
from services.texts.summarize import summarize_one_text_thread, summarize_tracked_text_threads
from services.texts.catalog import list_conversation_catalog
from services.texts.config import CONVERSATIONS_DIR, conversation_file_path
from services.texts.format import load_conversation_messages
from services.texts.tracking import (
    TEXT_THREAD_PREFIX,
    fetch_tracked_conversation_keys,
    fetch_visible_conversation_keys,
    parse_text_inbox_thread_id,
    set_tracked_conversation_keys,
    text_inbox_thread_id,
)

__all__ = [
    "CONVERSATIONS_DIR",
    "TEXT_THREAD_PREFIX",
    "append_unsynced_text_threads_to_bundle",
    "summarize_one_text_thread",
    "summarize_tracked_text_threads",
    "conversation_file_path",
    "fetch_tracked_conversation_keys",
    "fetch_visible_conversation_keys",
    "list_conversation_catalog",
    "load_conversation_messages",
    "parse_text_inbox_thread_id",
    "set_tracked_conversation_keys",
    "text_inbox_thread_id",
]
