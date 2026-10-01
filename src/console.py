"""Console subscriber: prints run progress from bus events (replaces direct print calls)."""
from __future__ import annotations

from rich.console import Console

from .events import Event, EventBus

console = Console()
THOUGHT_CHARS = 110


def _colour(status: str) -> str:
    return {"passed": "green", "failed": "red"}.get(status, "yellow")


def handle(ev: Event) -> None:
    t = ev.type
    if t == "test_started":
        console.rule(f"[bold]{ev.test}")                                   # type: ignore[attr-defined]
    elif t == "llm_thought" and ev.text:                                    # type: ignore[attr-defined]
        console.print(f"  [dim]{ev.text[:THOUGHT_CHARS]}[/]")             # type: ignore[attr-defined]
    elif t == "tool_result":
        mark = "x" if ev.is_error else "-"                                  # type: ignore[attr-defined]
        console.print(f"  {mark} {ev.name}")                                # type: ignore[attr-defined]
    elif t == "assertion_result":
        tag = "[green]PASS" if ev.passed else "[red]FAIL"                   # type: ignore[attr-defined]
        console.print(f"  {tag}[/] {ev.outcome}: {ev.reason}")             # type: ignore[attr-defined]
    elif t == "control_state" and ev.state != "running":                    # type: ignore[attr-defined]
        console.print(f"  [yellow]{ev.state}[/]")                           # type: ignore[attr-defined]
    elif t == "error":
        label = "error" if ev.fatal else "warning"                          # type: ignore[attr-defined]
        console.print(f"  [yellow]{label}:[/] {ev.message}")               # type: ignore[attr-defined]
    elif t == "test_finished":
        total = ev.tokens_in + ev.tokens_out                                # type: ignore[attr-defined]
        console.print(f"[{_colour(ev.status)}]{ev.status.upper()}[/] {ev.test} "   # type: ignore[attr-defined]
                      f"({ev.duration_s}s, {total} tokens, stop: {ev.stop_reason or 'n/a'})")  # type: ignore[attr-defined]


def attach(bus: EventBus) -> None:
    bus.consume("console", handle)
