"""Thin async wrapper around the official MCP Python SDK for the Playwright MCP server (stdio)."""
from __future__ import annotations

import asyncio
import base64
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

from .llm.base import ToolSpec
from .models import MCPConfig

# Only these extra variables reach the MCP subprocess; credentials do not.
_PASSTHROUGH_PREFIXES = ("PLAYWRIGHT_", "NODE_", "SSL_CERT", "REQUESTS_CA", "XDG_")
_PASSTHROUGH_EXACT = {"HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy"}


@dataclass
class ToolResult:
    text: str
    is_error: bool = False
    images: list[bytes] = field(default_factory=list)


def _subprocess_env() -> dict[str, str]:
    env = dict(get_default_environment())
    for k, v in os.environ.items():
        if k in _PASSTHROUGH_EXACT or k.startswith(_PASSTHROUGH_PREFIXES):
            env[k] = v
    return env


class PlaywrightMCP:
    """Async context manager: spawns the server, exposes list_tools / call_tool."""

    def __init__(self, cfg: MCPConfig) -> None:
        self.cfg = cfg
        self._stack = AsyncExitStack()
        self._session: ClientSession | None = None

    async def __aenter__(self) -> "PlaywrightMCP":
        args = list(self.cfg.args)
        if self.cfg.headless:
            args.append("--headless")
        args.append("--isolated")  # fresh in-memory browser profile per test
        params = StdioServerParameters(command=self.cfg.command, args=args, env=_subprocess_env())
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._stack.aclose()

    @property
    def session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("MCP session not started")
        return self._session

    async def list_tools(self) -> list[ToolSpec]:
        res = await self.session.list_tools()
        return [ToolSpec(t.name, t.description or "", t.inputSchema or {"type": "object"})
                for t in res.tools]

    async def call_tool(self, name: str, arguments: dict[str, Any], timeout: float) -> ToolResult:
        """Call a tool; raises asyncio.TimeoutError on timeout."""
        res = await asyncio.wait_for(self.session.call_tool(name, arguments), timeout)
        texts: list[str] = []
        images: list[bytes] = []
        for block in res.content:
            kind = getattr(block, "type", "")
            if kind == "text":
                texts.append(block.text)
            elif kind == "image":
                images.append(base64.b64decode(block.data))
                texts.append("[image returned]")
        return ToolResult("\n".join(texts), bool(res.isError), images)

    async def snapshot(self, timeout: float = 60) -> str:
        """Accessibility snapshot (default page observation; far cheaper than screenshots)."""
        return (await self.call_tool("browser_snapshot", {}, timeout)).text

    async def screenshot(self, timeout: float = 60) -> bytes | None:
        res = await self.call_tool("browser_take_screenshot", {}, timeout)
        return res.images[0] if res.images else None
