"""Run one test end to end: browser -> agent -> verification -> failure screenshot -> TestResult."""
from __future__ import annotations

import time
from contextlib import AsyncExitStack
from pathlib import Path

from .agent import Agent
from .control import RunControl
from .events import ErrorEvent, EventBus, Screenshot, TestFinished, TestStarted
from .live.cdp import BrowserProcess, CDPScreencaster, LiveUnavailable, find_browser
from .live.frames import FrameSource, Highlighter, ScreenshotFrameSource
from .llm.base import LLMBackend
from .mcp_client import PlaywrightMCP
from .models import AppConfig, TestCase, TestResult, TokenUsage
from .secrets import SecretStore
from .util import slugify
from .verifier import verify

__all__ = ["run_test", "slugify"]

RECORD_SUFFIXES = {".webm", ".mp4", ".zip"}     # videos and Playwright traces


async def run_test(test: TestCase, cfg: AppConfig, llm: LLMBackend, run_dir: Path,
                   progress=None, *, bus: EventBus | None = None,
                   control: RunControl | None = None, index: int = 1, total: int = 1) -> TestResult:
    started = time.monotonic()
    live = cfg.live
    secrets = SecretStore()
    # Register secrets referenced anywhere in the test so redaction is active from step one.
    secrets.register_from_text("\n".join([test.goal, *test.steps, *test.expected]))
    usage = TokenUsage()
    result = TestResult(name=test.name, start_url=test.start_url, status="error", usage=usage)
    rec_dir = run_dir / "recordings" / slugify(test.name) if live.record else None
    cdp_used = False

    if bus:
        bus.current_test = test.name
        bus.redactor = secrets.redact       # every event is masked centrally
        bus.emit(TestStarted(index=index, total=total, start_url=test.start_url, goal=test.goal,
                             expected=test.expected))
    try:
        async with AsyncExitStack() as stack:
            mode = live.mode
            endpoint: str | None = None
            caster: CDPScreencaster | None = None
            if mode == "cdp":
                try:
                    exe = find_browser(live.browser_path)
                    if not exe:
                        raise LiveUnavailable("no Chrome/Chromium found (set BROWSER_PATH)")
                    browser = await stack.enter_async_context(
                        BrowserProcess(exe, headless=cfg.mcp.headless))
                    assert bus is not None
                    caster = await stack.enter_async_context(
                        CDPScreencaster(browser.endpoint, bus, live))
                    endpoint = browser.endpoint
                    cdp_used = True
                except LiveUnavailable as exc:
                    mode = "screenshot"
                    if bus:
                        bus.emit(ErrorEvent(message=f"CDP live view unavailable ({exc}); "
                                                    "falling back to screenshot mode"))
            mcp = await stack.enter_async_context(PlaywrightMCP(
                cfg.mcp, cdp_endpoint=endpoint, output_dir=rec_dir, record=live.record, bus=bus,
                max_frame_bytes=live.max_frame_kb * 1024))
            frames: FrameSource | None = (caster if caster else
                                          ScreenshotFrameSource(mcp) if mode == "screenshot" else None)
            highlighter: Highlighter | None = caster

            outcome = await Agent(llm, mcp, cfg.safety, test, secrets, progress, bus=bus,
                                  control=control, slow_mo_ms=live.slow_mo_ms, frames=frames,
                                  highlighter=highlighter, run_dir=run_dir).run()
            usage.add(outcome.usage.input_tokens, outcome.usage.output_tokens)
            result.steps = outcome.steps
            result.stop_reason = outcome.stop_reason
            result.agent_summary = outcome.summary or (
                "Stopped by user." if outcome.stop_reason == "stopped" else "")

            if outcome.stop_reason == "stopped":
                result.status = "failed"       # no verification for an aborted test
            else:
                try:
                    snapshot = await mcp.snapshot(cfg.safety.step_timeout_s)
                except Exception as exc:  # judge still runs, but will lack evidence
                    snapshot = f"(final snapshot unavailable: {exc})"
                result.verdicts = await verify(llm, test.expected, snapshot, outcome.summary,
                                               secrets, usage, bus)
                ok = outcome.finished and outcome.claimed_passed and all(
                    v.passed for v in result.verdicts)
                result.status = "passed" if ok else "failed"

            if result.status != "passed" and cfg.screenshot_on_failure:
                try:
                    png = await mcp.screenshot(cfg.safety.step_timeout_s)
                    if png:
                        path = run_dir / f"{slugify(test.name)}.png"
                        path.write_bytes(png)
                        result.screenshot_path = path.name
                        if bus:
                            bus.emit(Screenshot(path=path.name, kind="failure",
                                                step=len(result.steps), url=mcp.last_url,
                                                title=mcp.last_title))
                except Exception:
                    pass  # evidence is best effort
    except Exception as exc:
        result.status = "error"
        result.error = secrets.redact(f"{type(exc).__name__}: {exc}")
        if bus:
            bus.emit(ErrorEvent(message=result.error, fatal=True))

    if rec_dir and rec_dir.is_dir():   # traces/videos are finalized when the browser closes
        result.recordings = sorted(
            p.relative_to(run_dir).as_posix() for p in rec_dir.rglob("*")
            if p.suffix.lower() in RECORD_SUFFIXES)
        if not any(r.endswith((".webm", ".mp4")) for r in result.recordings) and bus:
            why = ("video needs a browser owned by Playwright MCP, so use --live-mode screenshot "
                   "or off" if cdp_used else "this @playwright/mcp version may not support --save-video")
            bus.emit(ErrorEvent(message=f"--record: no video was produced ({why})"))
    result.duration_s = round(time.monotonic() - started, 2)
    if bus:
        bus.emit(TestFinished(status=result.status, stop_reason=result.stop_reason,
                              summary=result.agent_summary, duration_s=result.duration_s,
                              tokens_in=usage.input_tokens, tokens_out=usage.output_tokens,
                              error=result.error, recordings=result.recordings))
    return result
