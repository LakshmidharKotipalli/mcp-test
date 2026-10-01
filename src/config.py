"""Configuration loading: environment variables (and an optional .env file)."""
from __future__ import annotations

import os
import shlex
from pathlib import Path

from .models import AppConfig, LLMConfig, MCPConfig, SafetyConfig


def load_env_file(path: str | Path = ".env") -> None:
    """Minimal .env loader. Existing environment variables win."""
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        # strip trailing inline comment on unquoted values
        if value and value[0] not in "\"'" and " #" in value:
            value = value.split(" #", 1)[0].strip()
        value = value.strip("\"'")
        os.environ.setdefault(key.strip(), value)


def split_list(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _opt_int(value: str | None) -> int | None:
    return int(value) if value and value.strip() else None


def config_from_env() -> AppConfig:
    """Build the base config from environment variables only (no site defaults)."""
    e = os.environ.get
    mcp = MCPConfig()
    if e("MCP_COMMAND"):
        mcp.command = e("MCP_COMMAND", mcp.command)
    if e("MCP_ARGS"):
        mcp.args = shlex.split(e("MCP_ARGS", ""))
    return AppConfig(
        llm=LLMConfig(
            base_url=e("LLM_BASE_URL", ""),
            api_key=e("LLM_API_KEY", ""),
            model=e("LLM_MODEL", ""),
        ),
        safety=SafetyConfig(
            allowed_domains=split_list(e("ALLOWED_DOMAINS")),
            max_steps=_opt_int(e("MAX_STEPS")),
            step_timeout_s=float(e("STEP_TIMEOUT", "60")),
            max_consecutive_failures=int(e("MAX_CONSECUTIVE_FAILURES", "3")),
        ),
        mcp=mcp,
    )
