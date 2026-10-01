"""Per-test JSON results and a single self-contained HTML summary."""
from __future__ import annotations

import base64
import html
import json
from datetime import datetime
from pathlib import Path

from .models import TestResult
from .runner import slugify

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


def _e(value: object) -> str:
    return html.escape(str(value))


def _cls(status: str) -> str:
    return {"passed": "pass", "failed": "fail"}.get(status, "err")


def _render_test(r: TestResult, run_dir: Path) -> str:
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
    if r.screenshot_path:
        png = run_dir / r.screenshot_path
        if png.is_file():
            b64 = base64.b64encode(png.read_bytes()).decode()
            out.append(f"<p>Screenshot on failure:</p><img src='data:image/png;base64,{b64}'>")
    return "\n".join(out)


def write_html(results: list[TestResult], run_dir: Path) -> Path:
    passed = sum(r.passed for r in results)
    tokens = sum(r.usage.total for r in results)
    duration = round(sum(r.duration_s for r in results), 2)
    body = "\n<hr>\n".join(_render_test(r, run_dir) for r in results)
    page = (f"<!doctype html><html><head><meta charset='utf-8'><title>Test report</title>"
            f"<style>{CSS}</style></head><body><h1>Test report</h1>"
            f"<p>{passed}/{len(results)} passed | {duration}s | {tokens} tokens</p>{body}"
            f"</body></html>")
    path = run_dir / "report.html"
    path.write_text(page)
    return path
