"""Run one test end to end: agent -> verification -> failure screenshot -> TestResult."""
from __future__ import annotations

import re
import time
from pathlib import Path

from .agent import Agent
from .llm.base import LLMBackend
from .mcp_client import PlaywrightMCP
from .models import AppConfig, TestCase, TestResult, TokenUsage
from .secrets import SecretStore
from .verifier import verify


def slugify(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-").lower() or "test"


async def run_test(test: TestCase, cfg: AppConfig, llm: LLMBackend, run_dir: Path,
                   progress=None) -> TestResult:
    started = time.monotonic()
    secrets = SecretStore()
    # Register secrets referenced anywhere in the test so redaction is active from step one.
    secrets.register_from_text("\n".join([test.goal, *test.steps, *test.expected]))
    usage = TokenUsage()
    result = TestResult(name=test.name, start_url=test.start_url, status="error", usage=usage)
    try:
        async with PlaywrightMCP(cfg.mcp) as mcp:
            outcome = await Agent(llm, mcp, cfg.safety, test, secrets, progress).run()
            usage.add(outcome.usage.input_tokens, outcome.usage.output_tokens)
            result.steps = outcome.steps
            result.stop_reason = outcome.stop_reason
            result.agent_summary = outcome.summary

            try:
                snapshot = await mcp.snapshot(cfg.safety.step_timeout_s)
            except Exception as exc:  # judge still runs, but will lack evidence
                snapshot = f"(final snapshot unavailable: {exc})"
            result.verdicts = await verify(llm, test.expected, snapshot, outcome.summary,
                                           secrets, usage)

            ok = outcome.finished and outcome.claimed_passed and all(
                v.passed for v in result.verdicts)
            result.status = "passed" if ok else "failed"

            if not ok and cfg.screenshot_on_failure:
                try:
                    png = await mcp.screenshot(cfg.safety.step_timeout_s)
                    if png:
                        path = run_dir / f"{slugify(test.name)}.png"
                        path.write_bytes(png)
                        result.screenshot_path = path.name
                except Exception:
                    pass  # evidence is best effort
    except Exception as exc:
        result.status = "error"
        result.error = secrets.redact(f"{type(exc).__name__}: {exc}")
    result.duration_s = round(time.monotonic() - started, 2)
    return result
