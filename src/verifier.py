"""Judge each plain-English expected outcome against the final page snapshot."""
from __future__ import annotations

import json
import re

from pydantic import ValidationError

from .llm.base import LLMBackend, Message
from .models import TokenUsage, Verdict
from .secrets import SecretStore

MAX_SNAPSHOT_CHARS = 24000

SYSTEM = """You are a strict QA judge. Given the final page snapshot (accessibility tree) \
and a list of expected outcomes, decide for each whether it is satisfied by the evidence \
in the snapshot. Respond with ONLY a JSON array, one object per outcome, in the same order:
[{"outcome": "<text>", "passed": true|false, "reason": "<short evidence-based reason>"}]
If the snapshot lacks evidence, mark passed=false."""


def _extract_json(text: str):
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        raise ValueError("no JSON array found")
    return json.loads(text[start:end + 1])


def _parse(text: str, outcomes: list[str]) -> list[Verdict]:
    raw = _extract_json(text)
    verdicts = [Verdict.model_validate(item) for item in raw]
    if len(verdicts) != len(outcomes):
        raise ValueError(f"expected {len(outcomes)} verdicts, got {len(verdicts)}")
    for v, o in zip(verdicts, outcomes):
        v.outcome = o  # keep the original wording
    return verdicts


async def verify(llm: LLMBackend, outcomes: list[str], snapshot: str, agent_summary: str,
                 secrets: SecretStore, usage: TokenUsage) -> list[Verdict]:
    if not outcomes:
        return []
    snap = secrets.redact(snapshot)[:MAX_SNAPSHOT_CHARS]
    prompt = (f"Expected outcomes:\n" + "\n".join(f"{i}. {o}" for i, o in enumerate(outcomes, 1))
              + f"\n\nAgent's own summary (not evidence):\n{agent_summary}"
              + f"\n\nFinal page snapshot:\n{snap}")
    messages = [Message("user", prompt)]
    last_error = ""
    for _ in range(2):  # one repair retry
        resp = await llm.complete(SYSTEM, messages, [])
        usage.add(resp.usage.input_tokens, resp.usage.output_tokens)
        try:
            return _parse(resp.text, outcomes)
        except (ValueError, ValidationError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            messages += [Message("assistant", resp.text),
                         Message("user", f"Invalid response ({last_error}). "
                                         "Reply with ONLY the JSON array.")]
    return [Verdict(outcome=o, passed=False, reason=f"Judge output unparseable: {last_error}")
            for o in outcomes]
