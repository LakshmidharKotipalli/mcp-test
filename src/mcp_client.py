"""Thin async wrapper around the official MCP Python SDK for the Playwright MCP server (stdio)."""
from __future__ import annotations

import asyncio
import base64
import os
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

from .events import ErrorEvent, EventBus, Frame, PageSnapshot
from .live.frames import FrameSnapshot
from .llm.base import ToolSpec
from .models import MCPConfig
from .util import image_mime, image_size

# Only these extra variables reach the MCP subprocess; credentials do not.
_PASSTHROUGH_PREFIXES = ("PLAYWRIGHT_", "NODE_", "SSL_CERT", "REQUESTS_CA", "XDG_")
_PASSTHROUGH_EXACT = {"HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy"}


@dataclass
class ToolResult:
    text: str
    is_error: bool = False
    images: list[bytes] = field(default_factory=list)


_PAGE_URL = re.compile(r"Page URL:\s*(\S+)")
_PAGE_TITLE = re.compile(r"Page Title:\s*(.*)")
SNAPSHOT_EVENT_CHARS = 4000     # snapshot text published to subscribers is truncated


def _subprocess_env() -> dict[str, str]:
    env = dict(get_default_environment())
    for k, v in os.environ.items():
        if k in _PASSTHROUGH_EXACT or k.startswith(_PASSTHROUGH_PREFIXES):
            env[k] = v
    return env


class PlaywrightMCP:
    """Async context manager: spawns the server, exposes list_tools / call_tool."""

    def __init__(self, cfg: MCPConfig, *, cdp_endpoint: str | None = None,
                 output_dir: Path | None = None, record: bool = False,
                 bus: EventBus | None = None, max_frame_bytes: int = 600_000) -> None:
        self.cfg = cfg
        self.cdp_endpoint = cdp_endpoint      # attach to a browser we launched (live cdp mode)
        self.output_dir = output_dir          # where --record artifacts go
        self.record = record
        self.bus = bus
        self.max_frame_bytes = max_frame_bytes
        self.last_url = ""
        self.last_title = ""
        self._stack = AsyncExitStack()
        self._session: ClientSession | None = None

    async def __aenter__(self) -> "PlaywrightMCP":
        args = list(self.cfg.args)
        if self.cdp_endpoint:
            # Browser is owned by us (so a CDP screencast can attach too); MCP just drives it.
            args += ["--cdp-endpoint", self.cdp_endpoint]
        else:
            if self.cfg.headless:
                args.append("--headless")
            args.append("--isolated")  # fresh in-memory browser profile per test
        if self.record and self.output_dir:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            args += ["--output-dir", str(self.output_dir), "--save-trace"]
            if not self.cdp_endpoint:   # video needs a browser context created by MCP itself
                args.append("--save-video=1280x720")
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
        text = "\n".join(texts)
        self._publish_page_state(text)
        return ToolResult(text, bool(res.isError), images)

    def _publish_page_state(self, text: str) -> None:
        """Publish url/title/snapshot when a tool result carries page state."""
        m = _PAGE_URL.search(text)
        if not m:
            return
        self.last_url = m.group(1)
        t = _PAGE_TITLE.search(text)
        self.last_title = t.group(1).strip() if t else self.last_title
        if self.bus:
            self.bus.emit(PageSnapshot(url=self.last_url, title=self.last_title,
                                       snapshot=text[:SNAPSHOT_EVENT_CHARS]))

    async def capture_frame(self, timeout: float = 30) -> FrameSnapshot | None:
        """Screenshot-mode live frame: one MCP screenshot, published as a Frame event."""
        try:
            res = await self.call_tool("browser_take_screenshot", {"type": "jpeg"}, timeout)
        except Exception as exc:  # noqa: BLE001 - live view must never break the test
            if self.bus:
                self.bus.emit(ErrorEvent(message=f"screenshot frame failed: {exc}"))
            return None
        if not res.images:
            return None
        raw = res.images[0]
        w, h = image_size(raw)
        snap = FrameSnapshot(raw, image_mime(raw), w, h, self.last_url, self.last_title)
        if self.bus and len(raw) <= self.max_frame_bytes:    # cap frame size
            self.bus.emit(Frame(data=base64.b64encode(raw).decode(), mime=snap.mime, width=w,
                                height=h, url=snap.url, title=snap.title))
        return snap

    async def snapshot(self, timeout: float = 60) -> str:
        """Accessibility snapshot (default page observation; far cheaper than screenshots)."""
        return (await self.call_tool("browser_snapshot", {}, timeout)).text

    async def screenshot(self, timeout: float = 60) -> bytes | None:
        res = await self.call_tool("browser_take_screenshot", {}, timeout)
        return res.images[0] if res.images else None
