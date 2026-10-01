"""Provider-agnostic LLM interface. Backends translate these types to/from a vendor API."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Message:
    role: Literal["user", "assistant", "tool"]
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)  # assistant only
    tool_call_id: str = ""                                    # tool only


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)


class LLMBackend(ABC):
    """Implement ``complete`` to add a new provider (see README, 'Adding a backend')."""

    @abstractmethod
    async def complete(self, system: str, messages: list[Message],
                       tools: list[ToolSpec]) -> LLMResponse:
        ...

    async def aclose(self) -> None:  # optional cleanup
        return None
