# LLM Web Tester

Give it a URL and a plain-English test goal. An LLM plans and runs the steps through the
[Playwright MCP server](https://github.com/microsoft/playwright-mcp), a verification pass judges each expected
outcome, and you get a pass/fail report with evidence.

## Setup

Requires Python 3.11+, Node.js (for `npx`), and an OpenAI-compatible endpoint with tool calling.

```bash
pip install -e .
cp .env.example .env   # set LLM_BASE_URL, LLM_API_KEY, LLM_MODEL, ALLOWED_DOMAINS
```

## Usage

```bash
# Single test
python main.py --url <URL> --goal "<GOAL>" --expect "<EXPECTED_OUTCOME>"

# YAML suite (see examples/suite.template.yaml)
python main.py --suite <SUITE.yaml>

# Validate a suite without launching a browser or calling the LLM
python main.py --suite <SUITE.yaml> --dry-run
```

Common flags: `--allow-domain`, `--max-steps`, `--step-timeout`, `--allow-destructive`, `--headed`, `--model`.
Exit codes: `0` all passed, `1` any failure, `2` invalid input.

Each run writes to `results/run-<timestamp>/`: per-test JSON, failure screenshots, and a self-contained
`report.html` with steps, tool calls, verdicts and token usage.

## Live view and replay

```bash
python main.py --url <URL> --goal "<GOAL>" --live --open-viewer
python main.py --replay results/run-<TIMESTAMP> --open-viewer
```

The viewer streams the browser alongside the agent's step timeline, assertions and token usage, with
**Pause**, **Resume**, **Step** and **Stop** controls. Safety checks run after the pause gate, so stepping never
bypasses them.

| Flag | Purpose |
|---|---|
| `--live-mode cdp\|screenshot\|off` | Frame source. `cdp` streams frames and outlines the target element; falls back to `screenshot` |
| `--slow-mo <ms>` | Pause after each action |
| `--record` | Save a Playwright trace (and video outside `cdp` mode), linked from the report |
| `--live-host`, `--live-port`, `--live-fps` | Viewer binding and frame rate |

The viewer binds to `127.0.0.1`, requires a per-run token, and rejects cross-origin and non-loopback requests.

## How it works

1. `src/mcp_client.py` starts `@playwright/mcp` over stdio.
2. `src/agent.py` gives the LLM the MCP tools plus `finish_test` and loops until it finishes or hits a limit.
   The LLM reads pages through accessibility snapshots, not screenshots.
3. `src/verifier.py` has the LLM judge each expected outcome against the final page and return structured JSON.
4. `src/reporter.py` writes the JSON and HTML reports.

## Safety

- **Domain allowlist:** defaults to the start URL's host; navigation is checked before every action.
- **Destructive actions** (payments, deletion and similar) are blocked unless `--allow-destructive` is set.
  This is a keyword heuristic, so use test accounts.
- **Secrets:** reference credentials as `{{env:NAME}}`. The LLM only sees the placeholder, and values are
  masked in logs, reports and the viewer.

## Adding an LLM backend

Subclass `LLMBackend` in `src/llm/<name>_backend.py`, implement `complete()`, and register it in
`get_backend()` in `src/llm/__init__.py`. The agent and verifier only use that interface.
