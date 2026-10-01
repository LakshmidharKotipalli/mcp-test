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
