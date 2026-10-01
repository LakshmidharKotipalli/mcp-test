"""LLM backends. Add new ones by subclassing LLMBackend and wiring them in get_backend()."""
from __future__ import annotations

from ..models import LLMConfig
from .base import LLMBackend, LLMResponse, Message, ToolCall, ToolSpec, Usage
from .openai_compat_backend import OpenAICompatBackend


def get_backend(cfg: LLMConfig) -> LLMBackend:
    # To add a provider: import it above and return it here (e.g. based on a new
    # LLM_PROVIDER setting). The agent only depends on the LLMBackend interface.
    return OpenAICompatBackend(cfg)


__all__ = ["LLMBackend", "LLMResponse", "Message", "ToolCall", "ToolSpec", "Usage", "get_backend"]
