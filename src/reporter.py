"""Per-test JSON results and a single self-contained HTML summary."""
from __future__ import annotations

import asyncio
import base64
import html
import json
from datetime import datetime
from pathlib import Path

from .events import Event, EventBus
from .models import TestResult
from .util import slugify

EMBED_KEYFRAMES = 12      # key frames embedded per test in the HTML report

CSS = """body{font-family:system-ui,sans-serif;margin:2rem;color:#222}
.pass{color:#1a7f37}.fail{color:#cf222e}.err{color:#9a6700}
table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:6px;text-align:left;vertical-align:top}
details{margin:.5rem 0}pre{white-space:pre-wrap;background:#f6f8fa;padding:8px;overflow:auto}
img{max-width:100%;border:1px solid #ddd}"""


def new_run_dir(base: str) -> Path:
    run_dir = Path(base) / datetime.now().strftime("run-%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def write_json(result: TestResult, run_dir: Path) -> Path:
    path = run_dir / f"{slugify(result.name)}.json"
    path.write_text(result.model_dump_json(indent=2))
    return path


class JsonlRecorder:
    """Bus subscriber that saves the event stream (minus live frames) to events.jsonl for replay.

    Events arrive already masked by the bus, so no secret value reaches the file.
    """

    def __init__(self, bus: EventBus, path: Path) -> None:
        self._fh = path.open("w", encoding="utf-8")
        self._task = bus.consume("jsonl", self._write)      # frames are not subscribed to

    def _write(self, ev: Event) -> None:
        self._fh.write(ev.model_dump_json() + "\n")
        self._fh.flush()

    async def close(self) -> None:
        """Call after bus.aclose() so the queue is fully drained first."""
        await asyncio.gather(self._task, return_exceptions=True)
        self._fh.close()


class KeyFrames:
    """Bus subscriber collecting saved key frames per test, for the HTML report."""

    def __init__(self, bus: EventBus) -> None:
        self.by_test: dict[str, list[tuple[int, str]]] = {}
        bus.consume("keyframes", self._on_event)

    def _on_event(self, ev: Event) -> None:
        if ev.type == "screenshot" and getattr(ev, "kind", "") == "step":
            self.by_test.setdefault(ev.test, []).append((ev.step, ev.path))   # type: ignore[attr-defined]


def _sample(items: list, n: int) -> list:
    if len(items) <= n:
        return items
    return [items[round(i * (len(items) - 1) / (n - 1))] for i in range(n)]


def _e(value: object) -> str:
    return html.escape(str(value))


def _cls(status: str) -> str:
    return {"passed": "pass", "failed": "fail"}.get(status, "err")


def _render_test(r: TestResult, run_dir: Path, frames: list[tuple[int, str]]) -> str:
    out = [f"<h2 class='{_cls(r.status)}'>{_e(r.name)} : {_e(r.status.upper())}</h2>",
           f"<p>Start URL: {_e(r.start_url)}<br>Duration: {r.duration_s}s | Stop reason: "
           f"{_e(r.stop_reason)} | Tokens: {r.usage.input_tokens} in / "
           f"{r.usage.output_tokens} out</p>"]
    if r.error:
        out.append(f"<p class='err'>Error: {_e(r.error)}</p>")
    if r.agent_summary:
        out.append(f"<p><b>Agent summary:</b> {_e(r.agent_summary)}</p>")
    if r.verdicts:
        out.append("<table><tr><th>Expected outcome</th><th>Result</th><th>Reason</th></tr>")
        for v in r.verdicts:
            out.append(f"<tr><td>{_e(v.outcome)}</td><td class='{'pass' if v.passed else 'fail'}'>"
                       f"{'PASS' if v.passed else 'FAIL'}</td><td>{_e(v.reason)}</td></tr>")
        out.append("</table>")
    out.append("<details><summary>Step log and tool calls</summary>")
    for s in r.steps:
        out.append(f"<p><b>Step {s.index}</b> {_e(s.thought)}</p>")
        for c in s.tool_calls:
            args = json.dumps(c.arguments, ensure_ascii=False)
            out.append(f"<details><summary class='{'fail' if c.is_error else ''}'>"
                       f"{_e(c.name)} {_e(args)} ({c.duration_s}s)</summary>"
                       f"<pre>{_e(c.result_preview)}</pre></details>")
    out.append("</details>")
    shots = []
    for step, rel in _sample(frames, EMBED_KEYFRAMES):
        f = run_dir / rel
        if f.is_file():
            mime = "image/png" if f.suffix == ".png" else "image/jpeg"
            b64 = base64.b64encode(f.read_bytes()).decode()
            shots.append(f"<figure style='margin:0'><img src='data:{mime};base64,{b64}' "
                         f"style='width:220px'><figcaption>Step {step}</figcaption></figure>")
    if shots:
        out.append("<details open><summary>Key frames</summary><div style='display:flex;"
                   "flex-wrap:wrap;gap:8px'>" + "".join(shots) + "</div></details>")
    if r.recordings:
        links = " | ".join(f"<a href='{_e(p)}'>{_e(p)}</a>" for p in r.recordings)
        out.append(f"<p>Recordings: {links}</p>")
    if r.screenshot_path:
        png = run_dir / r.screenshot_path
        if png.is_file():
            b64 = base64.b64encode(png.read_bytes()).decode()
            out.append(f"<p>Screenshot on failure:</p><img src='data:image/png;base64,{b64}'>")
    return "\n".join(out)


def write_html(results: list[TestResult], run_dir: Path,
               keyframes: dict[str, list[tuple[int, str]]] | None = None) -> Path:
    passed = sum(r.passed for r in results)
    tokens = sum(r.usage.total for r in results)
    duration = round(sum(r.duration_s for r in results), 2)
    keyframes = keyframes or {}
    body = "\n<hr>\n".join(_render_test(r, run_dir, keyframes.get(r.name, [])) for r in results)
    page = (f"<!doctype html><html><head><meta charset='utf-8'><title>Test report</title>"
            f"<style>{CSS}</style></head><body><h1>Test report</h1>"
            f"<p>{passed}/{len(results)} passed | {duration}s | {tokens} tokens"
            f" | <a href='events.jsonl'>event log</a></p>{body}"
            f"</body></html>")
    path = run_dir / "report.html"
    path.write_text(page)
    return path
