"""Agent loop: LLM plans, Playwright MCP executes, results are fed back until done."""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .control import RunControl, StopRequested
from .events import LLMThought, Screenshot, StepStarted, ToolResult
from .events import EventBus, ToolCall as ToolCallEvent
from .live.actions import RefIndex, describe
from .live.frames import FrameSource, Highlighter
from .llm.base import LLMBackend, Message, ToolCall, ToolSpec
from .mcp_client import PlaywrightMCP
from .models import SafetyConfig, StepRecord, TestCase, TokenUsage, ToolCallRecord
from .safety import SafetyGuard, sanitize_args
from .secrets import SecretError, SecretStore
from .util import slugify

FINISH_TOOL = "finish_test"
PREVIEW_CHARS = 600
ELIDE_OVER = 1500            # older tool results longer than this are collapsed
PAGE_URL = re.compile(r"Page URL:\s*(\S+)")
MAX_KEYFRAMES = 60           # key frames saved per test (report + replay)

FINISH_SPEC = ToolSpec(
    name=FINISH_TOOL,
    description="Call once when the test goal has been fully attempted. Report honestly.",
    input_schema={
        "type": "object",
        "properties": {
            "passed": {"type": "boolean", "description": "True only if the goal was achieved."},
            "summary": {"type": "string", "description": "What was done and observed."},
        },
        "required": ["passed", "summary"],
    },
)

SYSTEM_PROMPT = """You are a QA automation agent. You control a real browser through tools \
and test a website by following the user's test plan.

Rules:
- Observe the page with browser_snapshot (accessibility tree). Do not request screenshots.
- Use element refs from the latest snapshot when clicking or typing.
- For credentials or test data written as {{env:NAME}}, pass that placeholder text exactly \
as written in tool arguments. It is replaced with the real value at execution time. \
Never guess or invent credentials.
- Stay on the allowed domains. Do not perform destructive actions (payments, deleting data).
- If a tool call fails, read the error, adapt, and try a different approach.
- When done (or when the goal is impossible), call finish_test with passed and a short, \
honest summary. Do not claim success without evidence in the page."""


@dataclass
class AgentOutcome:
    steps: list[StepRecord] = field(default_factory=list)
    finished: bool = False
    claimed_passed: bool = False
    summary: str = ""
    stop_reason: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)


def build_task_prompt(test: TestCase) -> str:
    parts = [f"Start URL: {test.start_url}"]
    if test.goal:
        parts.append(f"Goal: {test.goal}")
    if test.steps:
        parts.append("Steps:\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(test.steps, 1)))
    if test.expected:
        parts.append("Expected outcomes to observe:\n" + "\n".join(f"- {e}" for e in test.expected))
    parts.append("Begin by navigating to the start URL.")
    return "\n\n".join(parts)


class Agent:
    def __init__(self, llm: LLMBackend, mcp: PlaywrightMCP, safety_cfg: SafetyConfig,
                 test: TestCase, secrets: SecretStore, progress=None, *,
                 bus: EventBus | None = None, control: RunControl | None = None,
                 slow_mo_ms: int = 0, frames: FrameSource | None = None,
                 highlighter: Highlighter | None = None, run_dir: Path | None = None) -> None:
        self.llm, self.mcp, self.cfg, self.test = llm, mcp, safety_cfg, test
        self.secrets = secrets
        self.guard = SafetyGuard(safety_cfg, test.start_url)
        self.progress = progress or (lambda msg: None)
        self.bus, self.control = bus, control
        self.slow_mo_s = max(slow_mo_ms, 0) / 1000
        self.frames, self.highlighter, self.run_dir = frames, highlighter, run_dir
        self.refs = RefIndex()
        self._keyframes = 0
        self._step = 0
        self._usage = TokenUsage()

    async def run(self) -> AgentOutcome:
        out = AgentOutcome()
        tools = await self.mcp.list_tools() + [FINISH_SPEC]
        messages = [Message("user", build_task_prompt(self.test))]
        tool_calls_made = 0
        failures = 0
        nudges = 0

        while True:
            if self.control and self.control.stopped:
                out.stop_reason = "stopped"
                break
            if self.cfg.max_steps is not None and tool_calls_made >= self.cfg.max_steps:
                out.stop_reason = "max_steps"
                break
            self._elide_old_results(messages)
            self._step = len(out.steps) + 1
            self._emit(StepStarted(index=self._step))
            resp = await self.llm.complete(SYSTEM_PROMPT, messages, tools)
            out.usage.add(resp.usage.input_tokens, resp.usage.output_tokens)
            thought = self.secrets.redact(resp.text.strip())
            self._emit(LLMThought(step=self._step, text=thought,
                                  tokens_in=out.usage.input_tokens,
                                  tokens_out=out.usage.output_tokens))
            messages.append(Message("assistant", resp.text, resp.tool_calls))
            step = StepRecord(index=self._step, thought=thought)
            out.steps.append(step)

            if not resp.tool_calls:
                nudges += 1
                if nudges > 2:
                    out.stop_reason = "no_tool_call"
                    break
                messages.append(Message("user", "Continue with tool calls, or call finish_test."))
                continue
            nudges = 0

            for call in resp.tool_calls:
                if call.name == FINISH_TOOL:
                    out.finished = True
                    out.claimed_passed = bool(call.arguments.get("passed"))
                    out.summary = self.secrets.redact(str(call.arguments.get("summary", "")))
                    out.stop_reason = "finished"
                    break
                tool_calls_made += 1
                try:
                    if self.control:
                        await self.control.gate()   # pause / step-once; before every safety check
                except StopRequested:
                    out.stop_reason = "stopped"
                    break
                text, is_error, record = await self._execute(call)
                step.tool_calls.append(record)
                messages.append(Message("tool", text, tool_call_id=call.id))
                self.progress(f"{'x' if is_error else '-'} {call.name}")
                await self._after_action()
                failures = failures + 1 if is_error else 0
                if failures >= self.cfg.max_consecutive_failures:
                    out.stop_reason = "too_many_failures"
                    break
            if out.stop_reason:
                break
        return out

    def _emit(self, event) -> None:
        if self.bus:
            self.bus.emit(event)

    async def _execute(self, call: ToolCall) -> tuple[str, bool, ToolCallRecord]:
        shown_args = sanitize_args(call.name, call.arguments)   # typed secrets masked
        await self._announce_call(call, shown_args)
        start = time.monotonic()
        text, is_error = await self._run_tool(call)
        text = self.secrets.redact(text)  # nothing unredacted leaves this method
        duration = round(time.monotonic() - start, 3)
        record = ToolCallRecord(
            name=call.name, arguments=shown_args,  # placeholders/masks, never resolved values
            result_preview=text[:PREVIEW_CHARS], is_error=is_error, duration_s=duration)
        self._emit(ToolResult(call_id=call.id, name=call.name, is_error=is_error,
                              duration_s=duration, preview=text[:PREVIEW_CHARS]))
        return text, is_error, record

    async def _announce_call(self, call: ToolCall, shown_args: dict[str, Any]) -> None:
        """Publish the upcoming action, with the target element's box when it can be located."""
        if not self.bus:
            return
        target = self.refs.lookup(str(call.arguments.get("ref") or ""))
        if target and self.highlighter:
            try:
                box = await asyncio.wait_for(
                    self.highlighter.locate(target["role"], target["name"]), 3)
            except Exception:  # noqa: BLE001 - highlight is cosmetic
                box = None
            if box:
                target = {**target, **box}
        action, label = describe(call.name, shown_args, target)
        self._emit(ToolCallEvent(step=self._step, call_id=call.id, name=call.name,
                                 arguments=shown_args, action=action, label=label, target=target))

    async def _after_action(self) -> None:
        """Slow-mo delay and key frame capture after each tool call."""
        if self.frames and self.run_dir:
            await self._save_keyframe()
        if self.slow_mo_s:
            await asyncio.sleep(self.slow_mo_s)

    async def _save_keyframe(self) -> None:
        if self._keyframes >= MAX_KEYFRAMES:
            return
        try:
            snap = await self.frames.current()   # type: ignore[union-attr]
            if not snap or not snap.data:
                return
            ext = "png" if snap.mime == "image/png" else "jpg"
            rel = Path("frames") / f"{slugify(self.test.name)}-{self._step:03d}-{self._keyframes}.{ext}"
            (self.run_dir / rel.parent).mkdir(parents=True, exist_ok=True)   # type: ignore[operator]
            (self.run_dir / rel).write_bytes(snap.data)                      # type: ignore[operator]
            self._keyframes += 1
            self._emit(Screenshot(path=rel.as_posix(), kind="step", step=self._step,
                                  url=snap.url, title=snap.title))
        except Exception:  # noqa: BLE001 - never fail a test over a key frame
            pass

    async def _run_tool(self, call: ToolCall) -> tuple[str, bool]:
        blocked = self.guard.check_call(call.name, call.arguments)
        if blocked:
            return blocked, True
        try:
            args = self.secrets.resolve(call.arguments)
        except SecretError as exc:
            return f"Error: {exc}. Do not retry with a guessed value.", True
        try:
            result = await self.mcp.call_tool(call.name, args, self.cfg.step_timeout_s)
        except asyncio.TimeoutError:
            return f"Error: tool timed out after {self.cfg.step_timeout_s}s.", True
        except Exception as exc:  # protocol/transport errors go back to the LLM too
            return f"Error: {type(exc).__name__}: {exc}", True
        text = result.text
        self.refs.update(text)
        # Off-allowlist page after a click/redirect: tell the LLM to go back.
        m = PAGE_URL.search(text)
        if m and (err := self.guard.check_url(m.group(1))):
            return f"{err} The page navigated off-domain; navigate back.", True
        return text, result.is_error

    @staticmethod
    def _elide_old_results(messages: list[Message]) -> None:
        """Keep only the latest tool results in full to save tokens (snapshots are big)."""
        last_assistant = max((i for i, m in enumerate(messages) if m.role == "assistant"),
                             default=-1)
        for i, m in enumerate(messages):
            if m.role == "tool" and i < last_assistant and len(m.text) > ELIDE_OVER:
                m.text = m.text[:200] + "\n[... earlier observation elided to save tokens ...]"
