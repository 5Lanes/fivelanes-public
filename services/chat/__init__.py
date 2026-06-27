"""Shared helpers for chat-channel threads (Slack, SMS, etc.)."""

from services.chat.coalesce import coalesce_chat_turns

__all__ = ["coalesce_chat_turns"]
