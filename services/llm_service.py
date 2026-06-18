"""Unified LLM backend facade for Claude and Ollama."""

from __future__ import annotations

from typing import Any, Dict, Optional, Protocol

from services.prompts import PromptMessages
from utils.backend_config import BackendName, get_backend


class LlmBackend(Protocol):
    name: BackendName

    def submit_segmentation(self, prompt: PromptMessages) -> Dict[str, Any]: ...

    def submit_summary(self, prompt: PromptMessages) -> Dict[str, Any]: ...

    def submit_incremental_summary(self, prompt: PromptMessages) -> Dict[str, Any]: ...

    def submit_email_reply(self, prompt: PromptMessages) -> Dict[str, Any]: ...

    def submit_meeting_prep(self, prompt: PromptMessages) -> Dict[str, Any]: ...

    def submit_person_summary(self, prompt: PromptMessages) -> Dict[str, Any]: ...


class _ClaudeBackend:
    name = "claude"

    def submit_segmentation(self, prompt: PromptMessages) -> Dict[str, Any]:
        from services import claude_service

        return claude_service.submit_segmentation_prompt(prompt)

    def submit_summary(self, prompt: PromptMessages) -> Dict[str, Any]:
        from services import claude_service

        return claude_service.submit_summary_prompt(prompt)

    def submit_incremental_summary(self, prompt: PromptMessages) -> Dict[str, Any]:
        from services import claude_service

        return claude_service.submit_incremental_summary_prompt(prompt)

    def submit_email_reply(self, prompt: PromptMessages) -> Dict[str, Any]:
        from services import claude_service

        return claude_service.submit_email_reply_prompt(prompt)

    def submit_meeting_prep(self, prompt: PromptMessages) -> Dict[str, Any]:
        from services import claude_service

        return claude_service.submit_meeting_prep_prompt(prompt)

    def submit_person_summary(self, prompt: PromptMessages) -> Dict[str, Any]:
        from services import claude_service

        return claude_service.submit_person_summary_prompt(prompt)


class _LlamaBackend:
    name = "llama"

    def __init__(self, env_path: str = ".env") -> None:
        self._env_path = env_path

    def submit_segmentation(self, prompt: PromptMessages) -> Dict[str, Any]:
        from services import llama_service

        return llama_service.submit_segmentation_prompt(prompt, env_path=self._env_path)

    def submit_summary(self, prompt: PromptMessages) -> Dict[str, Any]:
        from services import llama_service

        return llama_service.submit_summary_prompt(prompt, env_path=self._env_path)

    def submit_incremental_summary(self, prompt: PromptMessages) -> Dict[str, Any]:
        from services import llama_service

        return llama_service.submit_incremental_summary_prompt(prompt, env_path=self._env_path)

    def submit_email_reply(self, prompt: PromptMessages) -> Dict[str, Any]:
        from services import llama_service

        return llama_service.submit_email_reply_prompt(prompt, env_path=self._env_path)

    def submit_meeting_prep(self, prompt: PromptMessages) -> Dict[str, Any]:
        from services import llama_service

        return llama_service.submit_meeting_prep_prompt(prompt, env_path=self._env_path)

    def submit_person_summary(self, prompt: PromptMessages) -> Dict[str, Any]:
        from services import llama_service

        return llama_service.submit_person_summary_prompt(prompt, env_path=self._env_path)


def get_llm_backend(*, backend: Optional[str] = None, env_path: str = ".env") -> LlmBackend:
    active = (backend or get_backend()).strip().lower()
    if active == "claude":
        return _ClaudeBackend()
    if active == "llama":
        return _LlamaBackend(env_path=env_path)
    raise ValueError(f"Invalid FIVELANES_BACKEND: {active!r}")
