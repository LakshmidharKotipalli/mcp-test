"""Typed events and an asyncio event bus.

Producers (agent, MCP client, verifier, runner) call ``bus.emit(...)``; consumers
(console, JSONL recorder, report builder, live viewer) subscribe. ``emit`` never
blocks and never awaits, so a slow consumer cannot stall the agent:
  * live ``frame`` events are kept in a single "latest frame" slot per subscriber,
    so slow viewers simply skip frames;
  * every other event is queued (bounded, oldest dropped as a last resort).
All string fields pass through the bus redactor, so secrets are masked once,
centrally, before any subscriber (console, JSONL, viewer) sees them.
"""
from __future__ import annotations

import asyncio
import inspect
import sys
import time
from collections import deque
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, Field


class Event(BaseModel):
    type: str
    ts: float = 0.0       # epoch seconds, stamped by the bus
    seq: int = 0          # monotonically increasing per run
    test: str = ""        # test the event belongs to ("" for run-level events)


class RunStarted(Event):
    type: Literal["run_started"] = "run_started"
    run_id: str = ""
    run_dir: str = ""
    tests: list[str] = Field(default_factory=list)
    live_mode: str = "off"


class TestStarted(Event):
    __test__ = False
    type: Literal["test_started"] = "test_started"
    index: int = 0
    total: int = 0
    start_url: str = ""
    goal: str = ""
    expected: list[str] = Field(default_factory=list)


class StepStarted(Event):
    type: Literal["step_started"] = "step_started"
    index: int = 0


class LLMThought(Event):
    type: Literal["llm_thought"] = "llm_thought"
    step: int = 0
    text: str = ""
    tokens_in: int = 0      # cumulative for the test
    tokens_out: int = 0


class ToolCall(Event):
    type: Literal["tool_call"] = "tool_call"
    step: int = 0
    call_id: str = ""
    name: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)   # secrets masked
    action: str = ""        # click | type | navigate | wait | ...
    label: str = ""         # human readable, e.g. "click button 'Sign in'"
    target: dict[str, Any] | None = None   # {ref, role, name, rect?, viewport?}


class ToolResult(Event):
    type: Literal["tool_result"] = "tool_result"
    call_id: str = ""
    name: str = ""
    is_error: bool = False
    duration_s: float = 0.0
    preview: str = ""


class PageSnapshot(Event):
    type: Literal["page_snapshot"] = "page_snapshot"
    url: str = ""
    title: str = ""
    snapshot: str = ""      # truncated accessibility snapshot


class Frame(Event):
    """Live frame. High volume: never persisted to JSONL."""
    type: Literal["frame"] = "frame"
    data: str = ""          # base64 image
    mime: str = "image/jpeg"
    width: int = 0
    height: int = 0
    url: str = ""
    title: str = ""
    viewport: list[float] = Field(default_factory=list)   # CSS viewport [w, h] for overlay scaling


class Screenshot(Event):
    """A saved image (key frame per step, or failure screenshot)."""
    type: Literal["screenshot"] = "screenshot"
    path: str = ""          # relative to the run directory
    kind: Literal["step", "failure"] = "step"
    step: int = 0
    url: str = ""
    title: str = ""


class AssertionResult(Event):
    type: Literal["assertion_result"] = "assertion_result"
    index: int = 0
    outcome: str = ""
    passed: bool = False
    reason: str = ""


class ControlState(Event):
    type: Literal["control_state"] = "control_state"
    state: Literal["running", "paused", "stopping"] = "running"


class TestFinished(Event):
    __test__ = False
    type: Literal["test_finished"] = "test_finished"
    status: str = ""
    stop_reason: str = ""
    summary: str = ""
    duration_s: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    error: str | None = None
    recordings: list[str] = Field(default_factory=list)


class RunFinished(Event):
    type: Literal["run_finished"] = "run_finished"
    passed: int = 0
    failed: int = 0
    errors: int = 0
    stopped: bool = False
    report: str = ""


class ErrorEvent(Event):
    type: Literal["error"] = "error"
    message: str = ""
    fatal: bool = False


class Reset(Event):
    """Replay only: tells viewers to clear their state."""
    type: Literal["reset"] = "reset"


EVENT_TYPES: dict[str, type[Event]] = {
    cls.model_fields["type"].default: cls
    for cls in (RunStarted, TestStarted, StepStarted, LLMThought, ToolCall, ToolResult,
                PageSnapshot, Frame, Screenshot, AssertionResult, ControlState,
                TestFinished, RunFinished, ErrorEvent, Reset)
}


def parse_event(data: dict[str, Any]) -> Event | None:
    cls = EVENT_TYPES.get(data.get("type", ""))
    return cls.model_validate(data) if cls else None


def _walk(obj: Any, fn: Callable[[str], str]) -> Any:
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, dict):
        return {k: _walk(v, fn) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk(v, fn) for v in obj]
    return obj


class Subscriber:
    """One consumer's mailbox. Pushing never blocks."""

    def __init__(self, name: str, wants_frames: bool, max_events: int) -> None:
        self.name = name
        self.wants_frames = wants_frames
        self._max = max_events
        self._events: deque[Event] = deque()
        self._frame: Event | None = None        # only the newest frame is kept
        self._wake = asyncio.Event()
        self._closed = False

    def push(self, ev: Event) -> None:
        if ev.type == "frame":
            if not self.wants_frames:
                return
            self._frame = ev                    # overwrites: slow consumers skip frames
        else:
            if len(self._events) >= self._max:
                self._events.popleft()
            self._events.append(ev)
        self._wake.set()

    def close(self) -> None:
        self._closed = True
        self._wake.set()

    @property
    def pending(self) -> int:
        return len(self._events) + (self._frame is not None)

    async def get(self) -> Event | None:
        """Next event, or None once closed and drained."""
        while True:
            if self._events:
                return self._events.popleft()
            if self._frame is not None:
                ev, self._frame = self._frame, None
                return ev
            if self._closed:
                return None
            self._wake.clear()
            await self._wake.wait()

    def __aiter__(self) -> "Subscriber":
        return self

    async def __anext__(self) -> Event:
        ev = await self.get()
        if ev is None:
            raise StopAsyncIteration
        return ev


Handler = Callable[[Event], Awaitable[None] | None]


class EventBus:
    def __init__(self, max_events: int = 100_000) -> None:
        self._subs: list[Subscriber] = []
        self._taps: list[Callable[[Event], None]] = []
        self._tasks: list[asyncio.Task] = []
        self._max_events = max_events
        self._seq = 0
        self.current_test = ""                       # stamped onto events that don't set one
        self.redactor: Callable[[str], str] | None = None

    def subscribe(self, name: str = "", wants_frames: bool = False) -> Subscriber:
        sub = Subscriber(name, wants_frames, self._max_events)
        self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        if sub in self._subs:
            self._subs.remove(sub)
        sub.close()

    def tap(self, fn: Callable[[Event], None]) -> None:
        """Synchronous observer, called inside emit (used for viewer history)."""
        self._taps.append(fn)

    def consume(self, name: str, handler: Handler, wants_frames: bool = False) -> asyncio.Task:
        """Run ``handler`` for every event in a background task. A failing handler never breaks the run."""
        sub = self.subscribe(name, wants_frames)

        async def loop() -> None:
            async for ev in sub:
                try:
                    res = handler(ev)
                    if inspect.isawaitable(res):
                        await res
                except Exception as exc:  # noqa: BLE001
                    print(f"[events] subscriber '{name}' failed: {exc}", file=sys.stderr)

        task = asyncio.create_task(loop())
        self._tasks.append(task)
        return task

    def emit(self, event: Event, restamp: bool = True) -> Event:
        if restamp:
            self._seq += 1
            event.seq = self._seq
            event.ts = time.time()
            if not event.test:
                event.test = self.current_test
        event = self._mask(event)
        for tap in self._taps:
            tap(event)
        for sub in list(self._subs):
            sub.push(event)
        return event

    def _mask(self, ev: Event) -> Event:
        if self.redactor is None or ev.type == "frame":
            return ev
        data = ev.model_dump(mode="json")
        masked = _walk(data, self.redactor)
        return ev if masked == data else type(ev).model_validate(masked)

    async def aclose(self) -> None:
        """Close all mailboxes and wait for consumers to drain them."""
        for sub in list(self._subs):
            sub.close()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
