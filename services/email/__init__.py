"""
Gmail inbox pipeline: pull, thread expansion, and timeline population.

Heavy modules (Gmail API) are lazy-loaded so lightweight imports (e.g. unit tests
for routing) do not require ``google`` packages.
"""

from __future__ import annotations

import importlib
from typing import Any

from services.email.config import (
    CREDENTIALS_DIR,
    CREDENTIALS_PATH,
    DATABASE_NAME,
    PROJECT_ROOT,
    QUERY_BATCH_SIZE,
    SOURCE_ACCOUNT,
    SOURCE_OAUTH_ACCOUNT_ID,
    TOKENS_PATH,
)
from services.email.inbox_delivery import (
    PLACEHOLDER_SUBJECTS,
    body_is_empty_except_image,
    timeline_row_needs_image_description,
    timeline_row_process_body,
)
from services.email.inbox_route import (
    InboxRoute,
    dedupe_timeline_rows_by_source_id,
    process_todo_plan,
    route_inbox_message,
)
from services.email.subject import (
    extract_todo_plan_action,
    strip_subject_prefix_chain,
    subject_core_indicates_todo,
)

__all__ = [
    "CREDENTIALS_DIR",
    "CREDENTIALS_PATH",
    "DATABASE_NAME",
    "InboxRoute",
    "PLACEHOLDER_SUBJECTS",
    "PROJECT_ROOT",
    "QUERY_BATCH_SIZE",
    "SOURCE_ACCOUNT",
    "SOURCE_OAUTH_ACCOUNT_ID",
    "TOKENS_PATH",
    "body_is_empty_except_image",
    "body_without_forward_to_source",
    "build_tracking_row",
    "collect_thread_expansion_candidates",
    "dedupe_timeline_rows_by_source_id",
    "exchange_code_for_token",
    "extract_envelope_rfc_message_id",
    "extract_inner_rfc_message_id",
    "extract_todo_plan_action",
    "expand_thread",
    "get_authorization_url",
    "guard_segmentation_content",
    "pick_best_thread_expansion",
    "populate_timeline",
    "primary_email_from_sender",
    "process_inbox_pipeline",
    "process_todo_plan",
    "pull_fivelanes_inbox_messages",
    "pull_timeline_messages_for_threads",
    "quoted_thread_start_index",
    "rewrite_inbox_seed",
    "route_inbox_message",
    "segmentation_content_from_quoted_tail_only",
    "strip_forwarded_to_source_address",
    "strip_quoted_thread_tail",
    "strip_subject_prefix_chain",
    "subject_core_indicates_todo",
    "timeline_row_needs_image_description",
    "timeline_row_process_body",
]

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "body_without_forward_to_source": (
        "services.email.forwarding",
        "body_without_forward_to_source",
    ),
    "extract_envelope_rfc_message_id": (
        "services.email.forwarding",
        "extract_envelope_rfc_message_id",
    ),
    "extract_inner_rfc_message_id": (
        "services.email.forwarding",
        "extract_inner_rfc_message_id",
    ),
    "primary_email_from_sender": (
        "services.email.forwarding",
        "primary_email_from_sender",
    ),
    "strip_forwarded_to_source_address": (
        "services.email.forwarding",
        "strip_forwarded_to_source_address",
    ),
    "build_tracking_row": ("services.email.inbox_process", "build_tracking_row"),
    "expand_thread": ("services.email.inbox_process", "expand_thread"),
    "process_inbox_pipeline": (
        "services.email.inbox_process",
        "process_inbox_pipeline",
    ),
    "rewrite_inbox_seed": ("services.email.inbox_process", "rewrite_inbox_seed"),
    "pull_fivelanes_inbox_messages": (
        "services.email.inbox_pull",
        "pull_fivelanes_inbox_messages",
    ),
    "exchange_code_for_token": ("services.email.oauth", "exchange_code_for_token"),
    "get_authorization_url": ("services.email.oauth", "get_authorization_url"),
    "guard_segmentation_content": (
        "services.email.segmentation",
        "guard_segmentation_content",
    ),
    "quoted_thread_start_index": (
        "services.email.segmentation",
        "quoted_thread_start_index",
    ),
    "segmentation_content_from_quoted_tail_only": (
        "services.email.segmentation",
        "segmentation_content_from_quoted_tail_only",
    ),
    "strip_quoted_thread_tail": (
        "services.email.segmentation",
        "strip_quoted_thread_tail",
    ),
    "collect_thread_expansion_candidates": (
        "services.email.thread_resolve",
        "collect_thread_expansion_candidates",
    ),
    "pick_best_thread_expansion": (
        "services.email.thread_resolve",
        "pick_best_thread_expansion",
    ),
    "populate_timeline": ("services.email.thread_resolve", "populate_timeline"),
    "pull_timeline_messages_for_threads": (
        "services.email.thread_resolve",
        "pull_timeline_messages_for_threads",
    ),
}


def __getattr__(name: str) -> Any:
    spec = _LAZY_EXPORTS.get(name)
    if spec is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = spec
    module = importlib.import_module(module_name)
    return getattr(module, attr_name)
