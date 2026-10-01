"""Replay a finished run's events.jsonl through the same viewer."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from ..events import EventBus, Reset, parse_event
from .server import LiveServer

MAX_GAP_S = 3.0           # long idle gaps (LLM latency) are shortened
SPEEDS = (0.25, 0.5, 1, 2, 4, 8, 16)


def load_events(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "events.jsonl"
    if not path.is_file():
        raise FileNotFoundError(f"{path} not found (is this a run directory?)")
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class ReplayDriver:
    """Feeds recorded events to the bus with their original timing, scaled by ``speed``."""

    def __init__(self, bus: EventBus, events: list[dict[str, Any]], speed: float = 1.0) -> None:
        self.bus, self.events, self.speed = bus, events, speed
        self.server: LiveServer | None = None
        self._playing = asyncio.Event()
        self._playing.set()
        self._task: asyncio.Task | None = None

    async def handle(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action == "pause":
            self._playing.clear()
        elif action in ("resume", "play"):
            self._playing.set()
        elif action == "speed":
            self.speed = min(max(float(payload.get("speed", 1)), 0.1), 64)
        elif action == "restart":
            self.restart()
        else:
            raise KeyError(action)
        return {"ok": True, "speed": self.speed, "playing": self._playing.is_set()}

    def restart(self) -> None:
        if self._task:
            self._task.cancel()
        if self.server:
            self.server.reset()
        self.bus.emit(Reset())
        self._playing.set()
        self._task = asyncio.create_task(self._play())

    async def run(self) -> None:
        """Wait for a viewer to connect (so nothing is missed), then play once."""
        if self.server:
            await self.server.client_connected.wait()
        self._task = asyncio.create_task(self._play())
        await asyncio.gather(self._task, return_exceptions=True)

    async def _play(self) -> None:
        prev: float | None = None
        for raw in self.events:
            ev = parse_event(raw)
            if ev is None:
                continue
            if prev is not None:
                await asyncio.sleep(min(max(ev.ts - prev, 0), MAX_GAP_S) / self.speed)
            prev = ev.ts
            await self._playing.wait()
            self.bus.emit(ev, restamp=False)   # keep recorded timestamps
