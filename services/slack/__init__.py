"""
Slack DM threads stored as JSON under ``slack_dms/``.

Tracked conversations are registered in ``thread_tracking`` (``inbox_thread_id`` prefix
``slack:``) and merged into the dashboard summaries bundle for the Threads view.
"""

from services.slack.bundle import append_unsynced_slack_threads_to_bundle
from services.slack.catalog import list_conversation_catalog
from services.slack.config import SLACK_DMS_DIR, conversation_file_path
from services.slack.format import load_messages_for_key
from services.slack.pull import pull_slack_dms
from services.slack.summarize import summarize_one_slack_thread, summarize_tracked_slack_threads
from services.slack.tracking import (
    SLACK_THREAD_PREFIX,
    fetch_tracked_conversation_keys,
    fetch_visible_conversation_keys,
    parse_slack_inbox_thread_id,
    set_tracked_conversation_keys,
    slack_inbox_thread_id,
)

__all__ = [
    "SLACK_DMS_DIR",
    "SLACK_THREAD_PREFIX",
    "append_unsynced_slack_threads_to_bundle",
    "conversation_file_path",
    "fetch_tracked_conversation_keys",
    "fetch_visible_conversation_keys",
    "list_conversation_catalog",
    "load_messages_for_key",
    "parse_slack_inbox_thread_id",
    "pull_slack_dms",
    "set_tracked_conversation_keys",
    "slack_inbox_thread_id",
    "summarize_one_slack_thread",
    "summarize_tracked_slack_threads",
]
