"""Agent loop: LLM plans, Playwright MCP executes, results are fed back until done."""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .llm.base import LLMBackend, Message, ToolCall, ToolSpec
from .mcp_client import PlaywrightMCP
from .models import SafetyConfig, StepRecord, TestCase, TokenUsage, ToolCallRecord
from .safety import SafetyGuard
from .secrets import SecretError, SecretStore

FINISH_TOOL = "finish_test"
PREVIEW_CHARS = 600
ELIDE_OVER = 1500            # older tool results longer than this are collapsed
PAGE_URL = re.compile(r"Page URL:\s*(\S+)")

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
                 test: TestCase, secrets: SecretStore, progress=None) -> None:
        self.llm, self.mcp, self.cfg, self.test = llm, mcp, safety_cfg, test
        self.secrets = secrets
        self.guard = SafetyGuard(safety_cfg, test.start_url)
        self.progress = progress or (lambda msg: None)

    async def run(self) -> AgentOutcome:
        out = AgentOutcome()
        tools = await self.mcp.list_tools() + [FINISH_SPEC]
        messages = [Message("user", build_task_prompt(self.test))]
        tool_calls_made = 0
        failures = 0
        nudges = 0

        while True:
            if self.cfg.max_steps is not None and tool_calls_made >= self.cfg.max_steps:
                out.stop_reason = "max_steps"
                break
            self._elide_old_results(messages)
            resp = await self.llm.complete(SYSTEM_PROMPT, messages, tools)
            out.usage.add(resp.usage.input_tokens, resp.usage.output_tokens)
            thought = self.secrets.redact(resp.text.strip())
            messages.append(Message("assistant", resp.text, resp.tool_calls))
            step = StepRecord(index=len(out.steps) + 1, thought=thought)
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
                text, is_error, record = await self._execute(call)
                step.tool_calls.append(record)
                messages.append(Message("tool", text, tool_call_id=call.id))
                self.progress(f"{'x' if is_error else '-'} {call.name}")
                failures = failures + 1 if is_error else 0
                if failures >= self.cfg.max_consecutive_failures:
                    out.stop_reason = "too_many_failures"
                    break
            if out.stop_reason:
                break
        return out

    async def _execute(self, call: ToolCall) -> tuple[str, bool, ToolCallRecord]:
        start = time.monotonic()
        text, is_error = await self._run_tool(call)
        text = self.secrets.redact(text)  # nothing unredacted leaves this method
        record = ToolCallRecord(
            name=call.name, arguments=call.arguments,  # placeholders, not resolved values
            result_preview=text[:PREVIEW_CHARS], is_error=is_error,
            duration_s=round(time.monotonic() - start, 3))
        return text, is_error, record

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
