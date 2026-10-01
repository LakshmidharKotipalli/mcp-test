"""Pydantic models for configuration, test definitions and results."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# ----------------------------------------------------------------- config
class LLMConfig(BaseModel):
    base_url: str = ""          # OpenAI-compatible root, e.g. <LLM_BASE_URL>
    api_key: str = ""           # optional for local servers
    model: str = ""
    temperature: float = 0.0
    request_timeout_s: float = 300.0


class SafetyConfig(BaseModel):
    # Empty by default; supplied by the user at runtime. When empty, only the
    # start URL's host is permitted (derived per test, never hardcoded).
    allowed_domains: list[str] = Field(default_factory=list)
    max_steps: int | None = None        # None = unlimited tool calls
    step_timeout_s: float = 60.0
    max_consecutive_failures: int = 3
    allow_destructive: bool = False


class MCPConfig(BaseModel):
    command: str = "npx"
    args: list[str] = Field(default_factory=lambda: ["@playwright/mcp@latest"])
    headless: bool = True


class LiveConfig(BaseModel):
    """Live view settings. Everything is overridable via env vars or CLI flags."""
    enabled: bool = False                 # start the local web viewer
    mode: Literal["cdp", "screenshot", "off"] = "off"   # how live frames are produced
    host: str = "127.0.0.1"               # viewer bind address (localhost unless overridden)
    port: int = 0                         # 0 = pick any free port
    open_viewer: bool = False             # open the viewer in the default browser
    fps: float = 5.0                      # max frames per second pushed to viewers
    max_width: int = 1280                 # screencast frame size cap
    max_height: int = 800
    quality: int = 60                     # JPEG quality for screencast frames
    max_frame_kb: int = 600               # frames larger than this are dropped
    slow_mo_ms: int = 0                   # delay after each action
    record: bool = False                  # save video + Playwright trace per test
    browser_path: str = ""                # Chrome/Chromium executable for cdp mode (auto-detected if empty)


class AppConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    live: LiveConfig = Field(default_factory=LiveConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    output_dir: str = "results"
    screenshot_on_failure: bool = True


# ------------------------------------------------------------------ tests
class TestCase(BaseModel):
    __test__ = False  # not a pytest class

    name: str
    start_url: str
    goal: str = ""                                   # free-form goal (CLI --goal)
    steps: list[str] = Field(default_factory=list)   # natural-language steps
    expected: list[str] = Field(default_factory=list)  # plain-English assertions


class Suite(BaseModel):
    allowed_domains: list[str] = Field(default_factory=list)
    tests: list[TestCase]


# ---------------------------------------------------------------- results
class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


class ToolCallRecord(BaseModel):
    name: str
    arguments: dict = Field(default_factory=dict)   # placeholders, never secret values
    result_preview: str = ""
    is_error: bool = False
    duration_s: float = 0.0


class StepRecord(BaseModel):
    index: int
    thought: str = ""
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)


class Verdict(BaseModel):
    outcome: str
    passed: bool
    reason: str = ""


class TestResult(BaseModel):
    __test__ = False

    name: str
    start_url: str
    status: Literal["passed", "failed", "error"]
    stop_reason: str = ""            # finished | max_steps | too_many_failures | ...
    agent_summary: str = ""
    verdicts: list[Verdict] = Field(default_factory=list)
    steps: list[StepRecord] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    duration_s: float = 0.0
    screenshot_path: str | None = None
    recordings: list[str] = Field(default_factory=list)   # video/trace files, relative to run dir
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.status == "passed"
