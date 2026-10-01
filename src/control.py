"""Run control shared by the live viewer and the agent loop.

The agent awaits ``gate()`` before every tool call. Because the safety checks run
*after* the gate, pausing or stepping can never bypass the allowlist or the
destructive-action guard.
"""
from __future__ import annotations

import asyncio

from .events import ControlState, EventBus


class StopRequested(Exception):
    """Raised by the gate when the user asked to stop."""


class RunControl:
    def __init__(self, bus: EventBus | None = None) -> None:
        self.bus = bus
        self._paused = False
        self._stop = False
        self._steps = 0                       # one-shot permits granted by step()
        self._wake = asyncio.Event()

    # --- state ---------------------------------------------------------
    @property
    def stopped(self) -> bool:
        return self._stop

    @property
    def state(self) -> str:
        return "stopping" if self._stop else "paused" if self._paused else "running"

    def _announce(self) -> None:
        if self.bus:
            self.bus.emit(ControlState(state=self.state))   # type: ignore[arg-type]
        self._wake.set()

    # --- commands (called from the viewer server) -----------------------
    def pause(self) -> None:
        self._paused = True
        self._announce()

    def resume(self) -> None:
        self._paused = False
        self._steps = 0
        self._announce()

    def step(self) -> None:
        """Allow exactly one tool call, then pause again."""
        self._paused = True
        self._steps += 1
        self._announce()

    def stop(self) -> None:
        self._stop = True
        self._announce()

    # --- agent side ------------------------------------------------------
    async def gate(self) -> None:
        """Block while paused; return immediately when running or when a step permit exists."""
        while True:
            if self._stop:
                raise StopRequested()
            if not self._paused:
                return
            if self._steps > 0:
                self._steps -= 1
                return
            self._wake.clear()
            await self._wake.wait()
