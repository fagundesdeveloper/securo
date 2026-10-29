from __future__ import annotations

import json
from typing import AsyncIterator, Optional

import httpx

from app.agents.providers.base import (
    ChatChunk,
    ChatMessage,
    LLMAuthError,
    LLMProvider,
    LLMRateLimitError,
    LLMUnavailableError,
    ToolDefinition,
    Usage,
)


def _split_system(messages: list[ChatMessage]) -> tuple[Optional[str], list[ChatMessage]]:
    """Gemini takes the system prompt out of band, in `systemInstruction`."""
    system: list[str] = []
    rest: list[ChatMessage] = []
    for message in messages:
        if message.role == "system" and message.content:
            system.append(message.content)
        else:
            rest.append(message)
    return ("\n\n".join(system) if system else None), rest


def _serialize_messages(messages: list[ChatMessage]) -> list[dict]:
    """Map the provider-agnostic messages onto Gemini's `contents`.

    Gemini calls the assistant "model", and a tool result is a `functionResponse`
    part keyed by the function's *name* rather than by a call id — it issues no
    ids of its own, so `chat_stream` synthesizes them and this maps back by name.
    """
    contents: list[dict] = []
    for message in messages:
        if message.role == "tool":
            contents.append({
                "role": "user",
                "parts": [{
                    "functionResponse": {
                        "name": message.name or "",
                        # A bare string is not valid here; Gemini wants an object.
                        "response": {"result": message.content or ""},
                    }
                }],
            })
            continue
        if message.role == "assistant" and message.tool_calls:
            parts: list[dict] = []
            if message.content:
                parts.append({"text": message.content})
            for call in message.tool_calls:
                part: dict = {"functionCall": {"name": call.name, "args": call.arguments}}
                if call.signature:
                    # Required verbatim, on the same part as the call itself.
                    part["thoughtSignature"] = call.signature
                parts.append(part)
            contents.append({"role": "model", "parts": parts})
            continue
        contents.append({
            "role": "model" if message.role == "assistant" else "user",
            "parts": [{"text": message.content or ""}],
        })
    return contents


# Gemini's function declarations take an OpenAPI 3.0 subset, not JSON Schema:
# anything outside this set makes the whole request 400, not just that tool.
# An allowlist rather than a denylist on purpose — MCP schemas here already
# brought `additionalProperties`, `$schema` and `exclusiveMinimum`, and the
# next tool will bring something else.
_GEMINI_SCHEMA_KEYS = frozenset({
    "type", "format", "title", "description", "nullable", "enum", "default",
    "items", "properties", "required", "minimum", "maximum",
    "minLength", "maxLength", "minItems", "maxItems", "pattern",
})


def _clean_schema(schema: dict) -> dict:
    """Reduce a JSON Schema to what Gemini accepts, recursively."""
    out: dict = {}
    for key, value in schema.items():
        if key not in _GEMINI_SCHEMA_KEYS:
            continue
        if key == "type" and isinstance(value, list):
            # JSON Schema writes an optional string as ["string", "null"];
            # Gemini wants one type plus the nullable flag.
            concrete = [t for t in value if t != "null"]
            out["type"] = concrete[0] if concrete else "string"
            if len(concrete) < len(value):
                out["nullable"] = True
        elif key == "properties" and isinstance(value, dict):
            out[key] = {
                name: _clean_schema(sub) if isinstance(sub, dict) else sub
                for name, sub in value.items()
            }
        elif key == "items" and isinstance(value, dict):
            out[key] = _clean_schema(value)
        else:
            out[key] = value
    return out


def _serialize_tools(tools: Optional[list[ToolDefinition]]) -> Optional[list[dict]]:
    if not tools:
        return None
    return [{
        "functionDeclarations": [{
            "name": tool.name,
            "description": tool.description,
            "parameters": _clean_schema(tool.parameters or {"type": "object", "properties": {}}),
        } for tool in tools]
    }]


def _raise_for_status(status: int, body: str) -> None:
    if status in (401, 403):
        raise LLMAuthError(f"Gemini auth failed: {body}", status=status)
    if status == 429:
        raise LLMRateLimitError(f"Gemini rate limit: {body}", status=status)
    if status >= 400:
        raise LLMUnavailableError(f"Gemini {status}: {body}", status=status)


class GeminiProvider(LLMProvider):
    """Google Gemini over the Generative Language API."""

    name = "gemini"
    supports_embeddings = True

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        model: Optional[str] = None,
    ):
        super().__init__(api_key=api_key, base_url=base_url, model=model)

    def _headers(self) -> dict[str, str]:
        # The key goes in a header, not the `?key=` query parameter the docs
        # show first: a URL carrying it lands in proxy and access logs.
        return {"x-goog-api-key": self.api_key, "content-type": "application/json"}

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        tools: Optional[list[ToolDefinition]] = None,
        temperature: float = 0.4,
        max_tokens: Optional[int] = None,
        # How much the model may spend thinking before it answers, for
        # models that reason. Providers without the notion ignore it.
        thinking_budget: Optional[int] = None,
    ) -> AsyncIterator[ChatChunk]:
        url = f"{self.base_url.rstrip('/')}/models/{model}:streamGenerateContent?alt=sse"
        system, rest = _split_system(messages)
        payload: dict = {
            "contents": _serialize_messages(rest),
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens or 8192,
            },
        }
        if thinking_budget is not None:
            # A ceiling, not a target: asked a trivial question the model
            # spends a couple of hundred tokens whatever this is set to. 0
            # switches thinking off; -1 lets the model decide.
            payload["generationConfig"]["thinkingConfig"] = {
                "thinkingBudget": thinking_budget
            }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        serialized_tools = _serialize_tools(tools)
        if serialized_tools:
            payload["tools"] = serialized_tools

        usage = Usage()
        finish_reason: Optional[str] = None
        call_index = 0
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
                async with client.stream("POST", url, json=payload, headers=self._headers()) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", errors="replace")
                        _raise_for_status(resp.status_code, body)
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if not raw:
                            continue
                        try:
                            event = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        meta = event.get("usageMetadata") or {}
                        if meta:
                            usage = Usage(
                                input_tokens=int(meta.get("promptTokenCount") or 0),
                                output_tokens=int(meta.get("candidatesTokenCount") or 0),
                            )

                        for candidate in event.get("candidates") or []:
                            for part in ((candidate.get("content") or {}).get("parts") or []):
                                if part.get("text"):
                                    yield ChatChunk(type="text_delta", text=part["text"])
                                call = part.get("functionCall")
                                if call:
                                    # Gemini sends the whole call at once and
                                    # names no id, so one is synthesized and the
                                    # arguments go out as a single delta.
                                    call_index += 1
                                    call_id = f"{call.get('name', 'tool')}-{call_index}"
                                    yield ChatChunk(
                                        type="tool_call_start",
                                        tool_call_id=call_id,
                                        tool_name=call.get("name") or "",
                                        signature=part.get("thoughtSignature"),
                                    )
                                    yield ChatChunk(
                                        type="tool_call_args_delta",
                                        tool_call_id=call_id,
                                        args_delta=json.dumps(call.get("args") or {}),
                                    )
                                    yield ChatChunk(type="tool_call_end", tool_call_id=call_id)
                            if candidate.get("finishReason"):
                                finish_reason = candidate["finishReason"]
        except httpx.HTTPError as exc:
            raise LLMUnavailableError(f"Gemini unreachable: {exc}") from exc

        if usage.input_tokens or usage.output_tokens:
            yield ChatChunk(type="usage", usage=usage)
        yield ChatChunk(type="finish", finish_reason=(finish_reason or "stop").lower())

    async def embed(self, texts: list[str], *, model: str) -> list[list[float]]:
        if not texts:
            return []
        url = f"{self.base_url.rstrip('/')}/models/{model}:batchEmbedContents"
        payload = {
            "requests": [
                {"model": f"models/{model}", "content": {"parts": [{"text": text}]}}
                for text in texts
            ]
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
                resp = await client.post(url, json=payload, headers=self._headers())
                if resp.status_code >= 400:
                    _raise_for_status(resp.status_code, resp.text)
                data = resp.json()
        except httpx.HTTPError as exc:
            raise LLMUnavailableError(f"Gemini unreachable: {exc}") from exc
        return [item.get("values") or [] for item in (data.get("embeddings") or [])]
