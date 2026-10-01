"""Secret handling.

Steps reference credentials as ``{{env:NAME}}``. The LLM only ever sees the
placeholder; the real value is substituted right before a tool call is sent to
the browser. Every resolved value is tracked so it can be redacted from
anything that is logged, reported, or fed back to the LLM.
"""
from __future__ import annotations

import os
import re
from typing import Any

PLACEHOLDER = re.compile(r"\{\{\s*env:([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
MIN_REDACT_LEN = 4  # avoid mangling text by redacting very short values


class SecretError(Exception):
    """Raised when a referenced environment variable is missing (name only, never a value)."""


def placeholder_names(text: str) -> list[str]:
    return sorted(set(PLACEHOLDER.findall(text)))


class SecretStore:
    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def register_from_text(self, text: str) -> None:
        """Pre-load values for placeholders found in test text so redaction is active early."""
        for name in placeholder_names(text):
            value = os.environ.get(name)
            if value:
                self._values[name] = value

    def resolve(self, obj: Any) -> Any:
        """Recursively substitute placeholders inside strings of a JSON-like object."""
        if isinstance(obj, str):
            def sub(match: re.Match[str]) -> str:
                name = match.group(1)
                value = os.environ.get(name)
                if value is None:
                    raise SecretError(f"environment variable {name} is not set")
                self._values[name] = value
                return value
            return PLACEHOLDER.sub(sub, obj)
        if isinstance(obj, dict):
            return {k: self.resolve(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.resolve(v) for v in obj]
        return obj

    def redact(self, text: str) -> str:
        for name, value in self._values.items():
            if len(value) >= MIN_REDACT_LEN:
                text = text.replace(value, f"{{{{env:{name}}}}}")
        return text
