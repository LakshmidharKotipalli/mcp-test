"""Frame sources: where the most recent page image comes from, per live mode."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class FrameSnapshot:
    data: bytes
    mime: str = "image/jpeg"
    width: int = 0
    height: int = 0
    url: str = ""
    title: str = ""


class FrameSource(Protocol):
    async def current(self) -> FrameSnapshot | None:
        """The newest frame (may be None if nothing has rendered yet)."""
        ...


class Highlighter(Protocol):
    async def locate(self, role: str, name: str) -> dict | None:
        """Bounding box of an element: {"rect": [x, y, w, h], "viewport": [w, h]} or None."""
        ...


class ScreenshotFrameSource:
    """Fallback source: one MCP screenshot after every action (also published as a live frame)."""

    def __init__(self, mcp) -> None:   # PlaywrightMCP (duck typed to avoid an import cycle)
        self.mcp = mcp

    async def current(self) -> FrameSnapshot | None:
        return await self.mcp.capture_frame()
