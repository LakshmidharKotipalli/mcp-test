"""Command-line interface: single run (--url/--goal) or batch (--suite)."""
from __future__ import annotations

import argparse
import asyncio
import sys

from rich.console import Console
from rich.table import Table

from .config import config_from_env, load_env_file
from .llm import get_backend
from .models import AppConfig, TestCase, TestResult
from .reporter import new_run_dir, write_html, write_json
from .runner import run_test
from .safety import effective_allowlist
from .secrets import placeholder_names
from .suite import SuiteError, load_suite

console = Console()


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
    p.add_argument("--headed", action="store_true", help="show the browser window")
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
    return cfg


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


async def run_all(tests: list[TestCase], cfg: AppConfig) -> list[TestResult]:
    run_dir = new_run_dir(cfg.output_dir)
    llm = get_backend(cfg.llm)
    results: list[TestResult] = []
    try:
        for t in tests:
            console.rule(f"[bold]{t.name}")
            with console.status("running..."):
                r = await run_test(t, cfg, llm, run_dir, progress=lambda m: console.print(f"  {m}"))
            write_json(r, run_dir)
            results.append(r)
            colour = {"passed": "green", "failed": "red"}.get(r.status, "yellow")
            console.print(f"[{colour}]{r.status.upper()}[/] {r.name} "
                          f"({r.duration_s}s, {r.usage.total} tokens, stop: {r.stop_reason or 'n/a'})")
            for v in r.verdicts:
                console.print(f"  {'[green]PASS' if v.passed else '[red]FAIL'}[/] {v.outcome}: {v.reason}")
            if r.error:
                console.print(f"  [yellow]error:[/] {r.error}")
    finally:
        await llm.aclose()
    report = write_html(results, run_dir)
    table = Table(title="Summary")
    for col in ("Test", "Status", "Duration", "Tokens"):
        table.add_column(col)
    for r in results:
        table.add_row(r.name, r.status, f"{r.duration_s}s", str(r.usage.total))
    console.print(table)
    console.print(f"Report: {report}")
    return results


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    load_env_file(a.env_file)
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
