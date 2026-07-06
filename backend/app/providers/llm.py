"""LLM client wrappers — Anthropic, NVIDIA NIM, or a local Ollama server.

Provider selection (config.LLM_PROVIDER):
  - "anthropic": Claude claude-fable-5 with streaming, adaptive thinking, and
    output_config effort "high". output_config is passed via extra_body so the
    code works across SDK versions. Note: on Fable 5 the `thinking` param must
    be `{"type": "adaptive"}` or omitted entirely — `{"type": "disabled"}` is
    a 400.
  - "nvidia": NVIDIA's OpenAI-compatible chat completions endpoint (default
    openai/gpt-oss-120b). Structured output embeds the JSON schema in the
    prompt and leniently parses the returned object.
  - "ollama": local models via the Ollama HTTP API (default qwen3.5).
    Structured output uses Ollama's grammar-constrained `format` parameter.
  - "auto" (default): Anthropic when credentials are present, then NVIDIA when
    configured, else Ollama.

Both providers expose the same two entry points used by formation and the
query engine: `structured_call()` and `stream_text()`.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict

import httpx
from anthropic import AsyncAnthropic

from ..core import config, usage

client = AsyncAnthropic()


def anthropic_credentials_available() -> bool:
    if os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"):
        return True
    cred_dir = Path.home() / ".config" / "anthropic" / "credentials"
    return cred_dir.exists() and any(cred_dir.glob("*.json"))


def nvidia_credentials_available() -> bool:
    return bool(config.NVIDIA_API_KEY)


def credentials_available(provider: str | None = None) -> bool:
    provider = provider or active_provider()
    if provider == "anthropic":
        return anthropic_credentials_available()
    if provider == "nvidia":
        return nvidia_credentials_available()
    if provider == "ollama":
        return True
    return anthropic_credentials_available() or nvidia_credentials_available()


def active_provider() -> str:
    if config.LLM_PROVIDER in ("anthropic", "nvidia", "ollama"):
        return config.LLM_PROVIDER
    if anthropic_credentials_available():
        return "anthropic"
    if nvidia_credentials_available():
        return "nvidia"
    return "ollama"


def active_model() -> str:
    provider = active_provider()
    if provider == "anthropic":
        return config.ANTHROPIC_MODEL
    if provider == "nvidia":
        return config.NVIDIA_MODEL
    return config.OLLAMA_MODEL


async def structured_call(
    system: str, user_text: str, schema: Dict[str, Any], max_tokens: int = 16000,
    effort: str = "high",
) -> Dict[str, Any]:
    """Call constrained to a JSON schema; returns the parsed object.

    `effort` maps to Anthropic's output_config effort. Formation keeps the
    default "high"; light utility calls (the follow-up rewrite) pass "low" so
    they don't pay reasoning latency for a one-line transformation. NVIDIA and
    Ollama have no equivalent knob and ignore it."""
    provider = active_provider()
    if provider == "ollama":
        return await _ollama_structured(system, user_text, schema)
    if provider == "nvidia":
        return await _nvidia_structured(system, user_text, schema, max_tokens=max_tokens)
    async with client.messages.stream(
        model=config.ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_text}],
        thinking={"type": "adaptive"},
        extra_body={
            "output_config": {
                "effort": effort,
                "format": {"type": "json_schema", "schema": schema},
            }
        },
    ) as stream:
        async for _ in stream.text_stream:
            pass  # drain; structured output is consumed from the final message
        msg = await stream.get_final_message()
    await usage.record("llm", "anthropic", config.ANTHROPIC_MODEL,
                       **usage.usage_from_anthropic(msg))
    text = "".join(b.text for b in msg.content if b.type == "text")
    return json.loads(text)


def stream_text(system: str, user_text: str, max_tokens: int = 16000):
    """Async context manager with a `.text_stream` iterator of answer tokens."""
    provider = active_provider()
    if provider == "ollama":
        return _OllamaStream(system, user_text)
    if provider == "nvidia":
        return _NvidiaStream(system, user_text, max_tokens=max_tokens)
    return client.messages.stream(
        model=config.ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_text}],
        thinking={"type": "adaptive"},
        extra_body={"output_config": {"effort": "high"}},
    )


# ---- NVIDIA/OpenAI-compatible chat completions ----

_NVIDIA_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0)
_NVIDIA_SLOW_TIMEOUT = httpx.Timeout(
    connect=10.0, read=config.FORMATION_READ_TIMEOUT_S, write=60.0, pool=10.0
)


def _nvidia_url(path: str) -> str:
    return f"{config.NVIDIA_BASE_URL.rstrip('/')}{path}"


def _nvidia_headers() -> Dict[str, str]:
    if not config.NVIDIA_API_KEY:
        raise RuntimeError("NVIDIA_API_KEY is required when LLM_PROVIDER=nvidia")
    return {
        "Authorization": f"Bearer {config.NVIDIA_API_KEY}",
        "Content-Type": "application/json",
    }


def _nvidia_max_tokens(requested: int) -> int:
    if config.NVIDIA_MAX_TOKENS > 0:
        return min(requested, config.NVIDIA_MAX_TOKENS)
    return requested


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return "" if content is None else str(content)


async def _nvidia_chat_once(
    messages: list[Dict[str, str]],
    *,
    max_tokens: int,
    temperature: float,
    timeout: httpx.Timeout,
) -> Dict[str, Any]:
    payload = {
        "model": config.NVIDIA_MODEL,
        "messages": messages,
        "temperature": temperature,
        "top_p": config.NVIDIA_TOP_P,
        "max_tokens": _nvidia_max_tokens(max_tokens),
        "stream": False,
    }
    async with httpx.AsyncClient(timeout=timeout) as cx:
        res = await cx.post(
            _nvidia_url("/chat/completions"),
            headers=_nvidia_headers(),
            json=payload,
        )
        res.raise_for_status()
        return res.json()


def _nvidia_message_content(data: Dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return _content_text(message.get("content"))


async def _nvidia_structured(
    system: str, user_text: str, schema: Dict[str, Any], *, max_tokens: int
) -> Dict[str, Any]:
    schema_note = (
        "\n\nRespond with ONLY a single JSON object — no markdown fences, no prose "
        "before or after — that validates against this JSON schema:\n"
        + json.dumps(schema)
    )
    data = await _nvidia_chat_once(
        [
            {"role": "system", "content": system + schema_note},
            {"role": "user", "content": user_text},
        ],
        max_tokens=max_tokens,
        temperature=0.2,
        timeout=_NVIDIA_SLOW_TIMEOUT,
    )
    await usage.record("llm", "nvidia", config.NVIDIA_MODEL,
                       **usage.usage_from_openai_payload(data))
    content = _nvidia_message_content(data)
    try:
        obj = json.loads(content)
    except json.JSONDecodeError:
        obj = parse_loose_json(content)
    if not isinstance(obj, dict) or not obj:
        raise ValueError(f"model did not return a JSON object: {content[:200]!r}")
    return obj


class _NvidiaStream:
    """Mimics the Anthropic SDK stream context manager for query.py."""

    def __init__(self, system: str, user_text: str, max_tokens: int) -> None:
        self._system = system
        self._user_text = user_text
        self._max_tokens = max_tokens
        self._cx: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "_NvidiaStream":
        self._cx = httpx.AsyncClient(timeout=_NVIDIA_TIMEOUT)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._cx:
            await self._cx.aclose()

    @property
    async def text_stream(self):
        payload = {
            "model": config.NVIDIA_MODEL,
            "messages": [
                {"role": "system", "content": self._system},
                {"role": "user", "content": self._user_text},
            ],
            "temperature": config.NVIDIA_TEMPERATURE,
            "top_p": config.NVIDIA_TOP_P,
            "max_tokens": _nvidia_max_tokens(self._max_tokens),
            "stream": True,
        }
        async with self._cx.stream(
            "POST",
            _nvidia_url("/chat/completions"),
            headers=_nvidia_headers(),
            json=payload,
        ) as res:
            res.raise_for_status()
            async for line in res.aiter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line.removeprefix("data:").strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                for choice in chunk.get("choices") or []:
                    piece = _content_text((choice.get("delta") or {}).get("content"))
                    if piece:
                        yield piece


# ---- Ollama ----

_OLLAMA_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0)
_OLLAMA_SLOW_TIMEOUT = httpx.Timeout(
    connect=10.0, read=config.FORMATION_READ_TIMEOUT_S, write=60.0, pool=10.0
)


async def _ollama_chat_once(payload: Dict[str, Any], timeout: httpx.Timeout) -> Dict[str, Any]:
    """One non-streaming /api/chat call. `think: false` keeps thinking models
    (qwen3.5) from fighting the JSON grammar; models that reject the parameter
    get one retry without it. No timeout retry here — abandoned generations
    keep grinding Ollama's GPU queue, so retries are the job queue's call."""
    async with httpx.AsyncClient(timeout=timeout) as cx:
        res = await cx.post(f"{config.OLLAMA_BASE_URL}/api/chat", json=payload)
        if res.status_code == 400 and "think" in payload:
            retry = {k: v for k, v in payload.items() if k != "think"}
            res = await cx.post(f"{config.OLLAMA_BASE_URL}/api/chat", json=retry)
        res.raise_for_status()
        return res.json()


async def _ollama_structured(
    system: str, user_text: str, schema: Dict[str, Any]
) -> Dict[str, Any]:
    # On thinking models, Ollama's grammar-constrained `format` runs only
    # after a full (slow, sometimes unbounded) thinking pass — and combining
    # `format` with `think: false` makes Ollama silently drop the grammar
    # (observed on 0.30.7). So: thinking off, schema embedded in the prompt,
    # lenient parse. The formation job queue retries the occasional bad JSON.
    schema_note = (
        "\n\nRespond with ONLY a single JSON object — no markdown fences, no prose "
        "before or after — that validates against this JSON schema:\n"
        + json.dumps(schema)
    )
    data = await _ollama_chat_once(
        {
            "model": config.OLLAMA_MODEL,
            "messages": [
                {"role": "system", "content": system + schema_note},
                {"role": "user", "content": user_text},
            ],
            "think": False,
            "stream": False,
            "options": {"num_ctx": config.OLLAMA_NUM_CTX, "temperature": 0.2},
        },
        _OLLAMA_SLOW_TIMEOUT,
    )
    await usage.record("llm", "ollama", config.OLLAMA_MODEL,
                       **usage.usage_from_ollama_payload(data))
    content = data["message"]["content"]
    try:
        obj = json.loads(content)
    except json.JSONDecodeError:
        obj = parse_loose_json(content)
    if not isinstance(obj, dict) or not obj:
        raise ValueError(f"model did not return a JSON object: {content[:200]!r}")
    return obj


class _OllamaStream:
    """Mimics the Anthropic SDK stream context manager for query.py."""

    def __init__(self, system: str, user_text: str) -> None:
        self._system = system
        self._user_text = user_text
        self._cx: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "_OllamaStream":
        self._cx = httpx.AsyncClient(timeout=_OLLAMA_TIMEOUT)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._cx:
            await self._cx.aclose()

    @property
    async def text_stream(self):
        payload = {
            "model": config.OLLAMA_MODEL,
            "messages": [
                {"role": "system", "content": self._system},
                {"role": "user", "content": self._user_text},
            ],
            "think": False,
            "stream": True,
            "options": {"num_ctx": config.OLLAMA_NUM_CTX},
        }
        for attempt in (0, 1):
            async with self._cx.stream(
                "POST", f"{config.OLLAMA_BASE_URL}/api/chat", json=payload
            ) as res:
                # models without a thinking mode reject `think` — drop it once
                if res.status_code == 400 and attempt == 0 and "think" in payload:
                    payload.pop("think")
                    continue
                res.raise_for_status()
                async for line in res.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    piece = chunk.get("message", {}).get("content", "")
                    if piece:
                        yield piece
                    if chunk.get("done"):
                        # the final chunk carries prompt_eval_count/eval_count
                        await usage.record(
                            "llm", "ollama", config.OLLAMA_MODEL,
                            **usage.usage_from_ollama_payload(chunk))
                        break
            return


def parse_loose_json(raw: str) -> Dict[str, Any]:
    """Best-effort parse of a JSON object out of free text (metadata blocks)."""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?", "", raw).strip()
    raw = re.sub(r"```$", "", raw).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        obj = json.loads(raw[start : end + 1])
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        return {}
