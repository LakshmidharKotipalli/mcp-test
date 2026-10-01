"""OpenAI-compatible chat-completions backend (llama.cpp, opencode-served models, etc.).

Uses httpx directly so any server that speaks ``POST {base_url}/chat/completions``
with tool calling works, with no vendor SDK required.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from ..models import LLMConfig
from .base import LLMBackend, LLMResponse, Message, ToolCall, ToolSpec, Usage

RETRY_STATUS = {429, 500, 502, 503, 504}


def _clean_schema(node: Any) -> Any:
    """Drop JSON-schema keys some local servers choke on when building grammars."""
    if isinstance(node, dict):
        return {k: _clean_schema(v) for k, v in node.items() if k != "$schema"}
    if isinstance(node, list):
        return [_clean_schema(v) for v in node]
    return node


class OpenAICompatBackend(LLMBackend):
    def __init__(self, cfg: LLMConfig) -> None:
        if not cfg.base_url or not cfg.model:
            raise ValueError("LLM_BASE_URL and LLM_MODEL must be set")
        self.cfg = cfg
        headers = {"Content-Type": "application/json"}
        if cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"
        self._client = httpx.AsyncClient(
            base_url=cfg.base_url.rstrip("/"), headers=headers,
            timeout=cfg.request_timeout_s)

    # --- normalized -> OpenAI wire format
    @staticmethod
    def _wire_messages(system: str, messages: list[Message]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            if m.role == "assistant":
                msg: dict[str, Any] = {"role": "assistant", "content": m.text or ""}
                if m.tool_calls:
                    msg["tool_calls"] = [{
                        "id": c.id, "type": "function",
                        "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                    } for c in m.tool_calls]
                out.append(msg)
            elif m.role == "tool":
                out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.text})
            else:
                out.append({"role": "user", "content": m.text})
        return out

    async def complete(self, system: str, messages: list[Message],
                       tools: list[ToolSpec]) -> LLMResponse:
        body: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": self._wire_messages(system, messages),
            "temperature": self.cfg.temperature,
        }
        if tools:
            body["tools"] = [{"type": "function", "function": {
                "name": t.name, "description": t.description,
                "parameters": _clean_schema(t.input_schema)}} for t in tools]
            body["tool_choice"] = "auto"

        data = await self._post(body)
        choice = data["choices"][0]["message"]
        calls: list[ToolCall] = []
        for i, tc in enumerate(choice.get("tool_calls") or []):
            fn = tc.get("function", {})
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except json.JSONDecodeError:
                args = {"_unparsed_arguments": raw}
            calls.append(ToolCall(id=tc.get("id") or f"call_{i}", name=fn.get("name", ""),
                                  arguments=args))
        usage = data.get("usage") or {}
        return LLMResponse(
            text=choice.get("content") or "",
            tool_calls=calls,
            usage=Usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)),
        )

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(3):
            try:
                resp = await self._client.post("/chat/completions", json=body)
                if resp.status_code in RETRY_STATUS:
                    raise httpx.HTTPStatusError("retryable", request=resp.request, response=resp)
                resp.raise_for_status()
                return resp.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status is not None and status not in RETRY_STATUS:
                    break
                await asyncio.sleep(2 ** attempt)
        raise RuntimeError(f"LLM request failed: {last}") from last

    async def aclose(self) -> None:
        await self._client.aclose()
