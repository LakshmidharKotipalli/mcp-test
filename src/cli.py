"""Command-line interface: single run (--url/--goal) or batch (--suite)."""
from __future__ import annotations

import argparse
import asyncio
import sys
import webbrowser
from pathlib import Path

from rich.table import Table

from . import console as console_sub
from .config import config_from_env, load_env_file
from .console import console
from .control import RunControl
from .events import EventBus, RunFinished, RunStarted
from .live.replay import ReplayDriver, load_events
from .live.server import LiveControlHandler, LiveServer, is_loopback
from .llm import get_backend
from .models import AppConfig, TestCase, TestResult
from .reporter import JsonlRecorder, KeyFrames, new_run_dir, write_html, write_json
from .runner import run_test
from .safety import effective_allowlist
from .secrets import placeholder_names
from .suite import SuiteError, load_suite



def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="main.py", description="LLM-driven website testing via Playwright MCP")
    p.add_argument("--url", help="start URL (single run)")
    p.add_argument("--goal", help="plain-English test goal (single run)")
    p.add_argument("--expect", action="append", default=[],
                   help="expected outcome in plain English (repeatable, single run)")
    p.add_argument("--suite", help="path to a YAML suite (batch run)")
    p.add_argument("--allow-domain", action="append", default=[],
                   help="allowed domain (repeatable); default: start URL host only")
    p.add_argument("--max-steps", type=int, help="cap on tool calls (default: unlimited)")
    p.add_argument("--step-timeout", type=float, help="seconds per tool call")
    p.add_argument("--allow-destructive", action="store_true",
                   help="permit payments/deletions (off by default)")
    p.add_argument("--headed", action="store_true",
                   help="show a visible local browser window (works alongside the web viewer)")
    live = p.add_argument_group("live view")
    live.add_argument("--live", action="store_true", help="serve the live web viewer while tests run")
    live.add_argument("--live-port", type=int, help="viewer port (default: any free port)")
    live.add_argument("--live-host", help="viewer bind address (default: 127.0.0.1; "
                                          "non-local values expose the viewer, use with care)")
    live.add_argument("--live-mode", choices=["cdp", "screenshot", "off"],
                      help="frame source: cdp (screencast), screenshot (MCP screenshot per action), off")
    live.add_argument("--live-fps", type=float, help="max live frames per second (default 5)")
    live.add_argument("--open-viewer", action="store_true", help="open the viewer in the default browser")
    live.add_argument("--slow-mo", type=int, metavar="MS", help="delay after each action, in ms")
    live.add_argument("--record", action="store_true",
                      help="save a Playwright trace (and video when supported) per test")
    live.add_argument("--replay", metavar="RUN_DIR", help="replay a finished run in the viewer")
    p.add_argument("--model", help="override LLM_MODEL")
    p.add_argument("--base-url", help="override LLM_BASE_URL")
    p.add_argument("--output-dir", help="results directory (default: results)")
    p.add_argument("--env-file", default=".env", help="env file to load (default: .env)")
    p.add_argument("--no-screenshot", action="store_true", help="skip screenshot on failure")
    p.add_argument("--dry-run", action="store_true",
                   help="validate inputs and print the plan; no browser or LLM calls")
    return p


def apply_overrides(cfg: AppConfig, a: argparse.Namespace, suite_domains: list[str]) -> AppConfig:
    s = cfg.safety
    s.allowed_domains = a.allow_domain or s.allowed_domains or suite_domains
    if a.max_steps is not None:
        s.max_steps = a.max_steps
    if a.step_timeout is not None:
        s.step_timeout_s = a.step_timeout
    s.allow_destructive = a.allow_destructive
    cfg.mcp.headless = not a.headed
    if a.model:
        cfg.llm.model = a.model
    if a.base_url:
        cfg.llm.base_url = a.base_url
    if a.output_dir:
        cfg.output_dir = a.output_dir
    cfg.screenshot_on_failure = not a.no_screenshot
    apply_live_overrides(cfg, a)
    return cfg


def apply_live_overrides(cfg: AppConfig, a: argparse.Namespace) -> None:
    lv = cfg.live
    lv.enabled = a.live or a.open_viewer
    lv.open_viewer = a.open_viewer
    if a.live_host:
        lv.host = a.live_host
    if a.live_port is not None:
        lv.port = a.live_port
    if a.live_fps:
        lv.fps = a.live_fps
    if a.slow_mo is not None:
        lv.slow_mo_ms = a.slow_mo
    lv.record = a.record
    # Without --live, behaviour is unchanged (mode off) unless a mode is asked for explicitly.
    lv.mode = a.live_mode or (lv.mode if lv.mode != "off" else ("cdp" if lv.enabled else "off"))


def collect_tests(a: argparse.Namespace) -> tuple[list[TestCase], list[str]]:
    if a.suite:
        suite = load_suite(a.suite, strict=not a.dry_run)
        return suite.tests, suite.allowed_domains
    if not (a.url and a.goal):
        raise SuiteError("provide --url and --goal, or --suite")
    return [TestCase(name="adhoc", start_url=a.url, goal=a.goal, expected=a.expect)], []


def print_plan(tests: list[TestCase], cfg: AppConfig) -> int:
    import os
    problems = 0
    if not cfg.llm.base_url or not cfg.llm.model:
        console.print("[yellow]warning:[/] LLM_BASE_URL / LLM_MODEL not set (needed for real runs)")
    for t in tests:
        console.rule(t.name)
        console.print(f"start_url: {t.start_url}")
        if "${" in t.start_url:
            console.print("  [red]start_url has an unset environment variable[/]")
            problems += 1
        console.print(f"allowed domains: {effective_allowlist(cfg.safety.allowed_domains, t.start_url) or 'none'}")
        for i, s in enumerate([t.goal, *t.steps] if t.goal else t.steps, 1):
            console.print(f"  {i}. {s}")
        for e in t.expected:
            console.print(f"  expect: {e}")
        names = placeholder_names("\n".join([t.goal, *t.steps, *t.expected]))
        for n in names:
            ok = n in os.environ  # name only; the value is never printed
            console.print(f"  env {n}: {'set' if ok else '[red]NOT SET[/]'}")
            problems += not ok
    return 1 if problems else 0


def warn_if_exposed(host: str) -> None:
    if not is_loopback(host):
        console.print(f"[bold yellow]warning:[/] live viewer bound to {host}: anyone who can reach "
                      "this address and has the URL token can watch and control the run.")


def open_in_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        console.print("[yellow]could not open a browser; open the URL manually[/]")


async def run_all(tests: list[TestCase], cfg: AppConfig) -> list[TestResult]:
    run_dir = new_run_dir(cfg.output_dir)
    llm = get_backend(cfg.llm)
    bus = EventBus()
    control = RunControl(bus)
    # Console, JSONL recorder and report key frames are plain bus subscribers.
    console_sub.attach(bus)
    recorder = JsonlRecorder(bus, run_dir / "events.jsonl")
    keyframes = KeyFrames(bus)
    server: LiveServer | None = None
    if cfg.live.enabled:
        warn_if_exposed(cfg.live.host)
        server = LiveServer(bus, LiveControlHandler(control), run_dir, cfg.live.host,
                            cfg.live.port, mode="live", run_id=run_dir.name)
        url = await server.start()
        console.print(f"Live view: {url}")
        if cfg.live.open_viewer:
            open_in_browser(url)

    results: list[TestResult] = []
    bus.emit(RunStarted(run_id=run_dir.name, run_dir=str(run_dir), tests=[t.name for t in tests],
                        live_mode=cfg.live.mode))
    try:
        for i, t in enumerate(tests, 1):
            if control.stopped:
                break
            with console.status("running..."):
                r = await run_test(t, cfg, llm, run_dir, bus=bus, control=control,
                                   index=i, total=len(tests))
            write_json(r, run_dir)
            results.append(r)
    finally:
        await llm.aclose()
    bus.current_test = ""
    report = run_dir / "report.html"
    bus.emit(RunFinished(passed=sum(r.status == "passed" for r in results),
                         failed=sum(r.status == "failed" for r in results),
                         errors=sum(r.status == "error" for r in results),
                         stopped=control.stopped, report=str(report)))
    if server:
        await server.stop()            # let viewers receive the final events
    await bus.aclose()                 # drain console / recorder / key frame subscribers
    await recorder.close()
    write_html(results, run_dir, keyframes.by_test)
    table = Table(title="Summary")
    for col in ("Test", "Status", "Duration", "Tokens"):
        table.add_column(col)
    for r in results:
        table.add_row(r.name, r.status, f"{r.duration_s}s", str(r.usage.total))
    console.print(table)
    console.print(f"Report: {report}")
    return results


async def replay_run(run_dir: Path, cfg: AppConfig) -> int:
    """Serve a finished run in the viewer, replaying its recorded events."""
    events = load_events(run_dir)
    bus = EventBus()
    driver = ReplayDriver(bus, events)
    warn_if_exposed(cfg.live.host)
    server = LiveServer(bus, driver, run_dir, cfg.live.host, cfg.live.port, mode="replay",
                        run_id=run_dir.name)
    driver.server = server
    url = await server.start()
    console.print(f"Replay viewer: {url}\nPress Ctrl+C to stop.")
    if cfg.live.open_viewer:
        open_in_browser(url)
    try:
        await driver.run()                       # plays once, when the first viewer connects
        await asyncio.Event().wait()             # keep serving (restart / speed controls)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await server.stop(drain_s=0)
        await bus.aclose()
    return 0


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    load_env_file(a.env_file)
    if a.replay:
        cfg = config_from_env()
        apply_live_overrides(cfg, a)
        try:
            return asyncio.run(replay_run(Path(a.replay), cfg))
        except FileNotFoundError as exc:
            console.print(f"[red]error:[/] {exc}")
            return 2
        except KeyboardInterrupt:
            return 0
    try:
        tests, suite_domains = collect_tests(a)
    except SuiteError as exc:
        console.print(f"[red]error:[/] {exc}")
        return 2
    cfg = apply_overrides(config_from_env(), a, suite_domains)
    if a.dry_run:
        return print_plan(tests, cfg)
    if not cfg.llm.base_url or not cfg.llm.model:
        console.print("[red]error:[/] set LLM_BASE_URL and LLM_MODEL (see .env.example)")
        return 2
    results = asyncio.run(run_all(tests, cfg))
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
