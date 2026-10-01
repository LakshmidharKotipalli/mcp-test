# LLM Web Tester

Give it a URL and a plain-English test goal. An LLM plans and executes the steps through the
[Playwright MCP server](https://github.com/microsoft/playwright-mcp) (`@playwright/mcp`), then a
verification pass judges each expected outcome and the app reports pass/fail with evidence.

## Setup

Requirements: Python 3.11+, Node.js (for `npx`), and an OpenAI-compatible chat-completions
endpoint that supports tool calling.

```bash
pip install -e .
cp .env.example .env     # then fill in the placeholders
```

Key environment variables (see `.env.example`): `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`,
`ALLOWED_DOMAINS`, and any test data your suite references (for example `TEST_USER`).

## Usage

```bash
# Single test
python main.py --url <TARGET_URL> --goal "<TEST_GOAL>" --expect "<EXPECTED_OUTCOME>"

# Batch from a YAML suite
python main.py --suite <PATH_TO_SUITE.yaml>

# Validate a suite and see the plan without launching a browser or calling the LLM
python main.py --suite <PATH_TO_SUITE.yaml> --dry-run
```

Useful flags: `--allow-domain` (repeatable), `--max-steps`, `--step-timeout`,
`--allow-destructive`, `--headed`, `--model`, `--base-url`, `--output-dir`, `--no-screenshot`.
Exit code is `0` when every test passes, `1` on any failure, `2` on invalid input.

### Live view

Watch the browser and the agent's reasoning while a test runs (single or suite runs):

```bash
python main.py --url <TARGET_URL> --goal "<TEST_GOAL>" --live --open-viewer
python main.py --suite <PATH_TO_SUITE.yaml> --live --slow-mo 500
```

The console prints a viewer URL such as `http://127.0.0.1:<PORT>/?token=<TOKEN>`. The page shows the
live browser frame with the current URL and title, an overlay labelling (and, in CDP mode,
outlining) the element about to be clicked or typed into, a step timeline (reasoning, tool,
masked arguments, status, duration), an assertion panel that fills in as verification completes,
a run header (test name, elapsed time, steps, token usage), and a test list for suites.
Controls: **Pause**, **Resume**, **Step once** and **Stop**. The agent loop waits at a gate before
every tool call, and the safety checks (domain allowlist, destructive-action guard) run after the
gate, so pausing or stepping never bypasses them. Stop ends the current test as failed and skips
the remaining tests.

| Flag | Meaning |
|---|---|
| `--live` | serve the viewer (default mode `cdp`) |
| `--live-port <port>` | viewer port (default: any free port) |
| `--live-host <addr>` | bind address (default `127.0.0.1`; warns when non-local) |
| `--live-mode cdp\|screenshot\|off` | frame source (see below) |
| `--live-fps <n>` | max frames per second pushed to viewers (default 5) |
| `--open-viewer` | open the viewer in your default browser (implies `--live`) |
| `--headed` | show a visible local browser window, alongside or instead of the viewer |
| `--slow-mo <ms>` | pause after each action so it is easy to follow |
| `--record` | save a Playwright trace (and a video when possible) per test, linked from the report |
| `--replay <run_dir>` | replay a finished run in the viewer |

Without `--live` (and without `--live-mode`) nothing changes: the browser is started by Playwright MCP
exactly as before. Frame size and quality are capped (`LIVE_MAX_WIDTH`, `LIVE_MAX_HEIGHT`,
`LIVE_QUALITY`, `LIVE_MAX_FRAME_KB`), and slow viewers skip frames instead of slowing the agent.

**CDP vs screenshot mode**

- `cdp` (preferred): the app launches Chrome or Chromium itself with a loopback remote-debugging
  port and starts Playwright MCP with `--cdp-endpoint` pointing at it. A second, read-only CDP
  connection streams `Page.startScreencast` JPEG frames and locates the element the agent is about
  to use. It needs a Chrome or Chromium executable (auto-detected from the Playwright cache or
  `PATH`, or set `BROWSER_PATH`).
  If it is unavailable, the run logs a warning and falls back to screenshot mode.
- `screenshot`: after every action the app takes one MCP screenshot and publishes it as a frame.
  Works with any Playwright MCP setup, but updates once per action and has no element outline.
- `off`: no frames. The timeline, assertions and header still work.

**Replay**

Every run writes `events.jsonl` (all events except live frames, with secrets masked) plus key
frames under `frames/`. Replay it with:

```bash
python main.py --replay results/run-<TIMESTAMP> --open-viewer
```

The replay starts when the first viewer connects; use the speed selector (0.5x to 8x), pause and restart.

**Recording**

`--record` passes `--output-dir`, `--save-trace` and (when the browser is owned by Playwright MCP)
`--save-video` to `@playwright/mcp` and links the files from `report.html`. Video cannot be
produced in `cdp` mode, because Playwright can only record contexts it creates itself. Use
`--live-mode screenshot` or `off` with `--record` when you need video. These MCP options depend
on your `@playwright/mcp` version.

**Security notes**

- The viewer binds to `127.0.0.1` unless you pass `--live-host`; a non-local bind prints a warning.
- Every request needs the random per-run token in the URL, the `Host` header must be a loopback
  name (DNS-rebinding guard) and cross-origin control requests are refused.
- Secrets are masked centrally on the event bus: values of `{{env:NAME}}` placeholders are replaced in
  every event, console line, viewer message and `events.jsonl`; text typed into fields that look
  sensitive (password, token, key, ...) is shown as `••••`. Note that screenshots show what the page
  renders, so use test accounts.
- Saved images are served only from the run directory (`.jpg` and `.png`).

### Suite format

See `examples/suite.template.yaml` (placeholders only, with comments for each field).
Each test has `name`, `start_url`, `steps`, `expected` (and optionally `goal`).
`${VAR}` in `start_url` is expanded from the environment.

### Output

Each run writes to `results/run-<timestamp>/`: one `<test>.json` per test, a failure screenshot
(when a test fails), and a self-contained `report.html` with the step log, tool calls,
verdicts, duration and token usage.

## How it works

1. `src/mcp_client.py` starts `npx @playwright/mcp@latest` over stdio via the official `mcp` SDK.
2. `src/agent.py` passes the server's tools (plus a `finish_test` tool) to the LLM, executes the
   returned tool calls, feeds results back, and repeats until `finish_test`, an optional step cap,
   or the consecutive-failure cap. The LLM observes pages with accessibility snapshots, not
   screenshots; older large observations are collapsed to save tokens.
3. `src/verifier.py` asks the LLM to judge each expected outcome against the final snapshot and
   return structured JSON (`passed`, `reason`). A test passes only if the agent reported success
   and every outcome passes.
4. `src/reporter.py` writes the JSON and HTML reports.

## Safety

- **Domain allowlist**: empty by default, supplied via `--allow-domain`, the suite's
  `allowed_domains`, or `ALLOWED_DOMAINS`. When empty, only the start URL's host is allowed.
  Navigation is checked before execution, and landing on an off-list page is reported back as an error.
- **Limits**: no tool-call cap unless you set `--max-steps`/`MAX_STEPS`; per-call timeout
  (`--step-timeout`, default 60s); consecutive-failure cap (default 3). Tool errors are returned
  to the LLM so it can adapt.
- **Destructive actions** (payments, deletion and similar wording on the targeted element) are
  blocked unless `--allow-destructive` is passed. This is a keyword heuristic, not a guarantee;
  use a test account and staging data.
- **Secrets**: reference credentials as `{{env:NAME}}`. The LLM only sees the placeholder; the value
  is substituted at execution time and redacted from logs, reports and LLM context. The MCP
  subprocess does not inherit your environment variables beyond a small allowlist.

## Adding a new LLM backend

1. Create `src/llm/<name>_backend.py` with a class extending `LLMBackend` from `src/llm/base.py`
   and implement `async complete(system, messages, tools) -> LLMResponse`. Translate the normalized
   `Message` / `ToolSpec` / `ToolCall` types to your provider's format and back, and fill in `Usage`.
2. Wire it into `get_backend()` in `src/llm/__init__.py` (for example, select it with a new
   `LLM_PROVIDER` variable read in `src/config.py`).
3. No other code changes are needed; the agent and verifier only use the `LLMBackend` interface.
