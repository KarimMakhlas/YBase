"""LLM client wrappers — Anthropic (Claude) or a local Ollama server.

Provider selection (config.LLM_PROVIDER):
  - "anthropic": Claude claude-fable-5 with streaming, adaptive thinking, and
    output_config effort "high". output_config is passed via extra_body so the
    code works across SDK versions. Note: on Fable 5 the `thinking` param must
    be `{"type": "adaptive"}` or omitted entirely — `{"type": "disabled"}` is
    a 400.
  - "ollama": local models via the Ollama HTTP API (default qwen3.5).
    Structured output uses Ollama's grammar-constrained `format` parameter.
  - "auto" (default): Anthropic when credentials are present, else Ollama.

Both providers expose the same two entry points used by formation and the
query engine: `structured_call()` and `stream_text()`.
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Dict

import httpx
from anthropic import AsyncAnthropic

from ..core import config

client = AsyncAnthropic()


def credentials_available() -> bool:
    if os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"):
        return True
    cred_dir = Path.home() / ".config" / "anthropic" / "credentials"
    return cred_dir.exists() and any(cred_dir.glob("*.json"))


def active_provider() -> str:
    if config.LLM_PROVIDER in ("anthropic", "ollama"):
        return config.LLM_PROVIDER
    return "anthropic" if credentials_available() else "ollama"


def active_model() -> str:
    return config.ANTHROPIC_MODEL if active_provider() == "anthropic" else config.OLLAMA_MODEL


async def structured_call(
    system: str, user_text: str, schema: Dict[str, Any], max_tokens: int = 16000
) -> Dict[str, Any]:
    """Call constrained to a JSON schema; returns the parsed object."""
    if active_provider() == "ollama":
        return await _ollama_structured(system, user_text, schema)
    async with client.messages.stream(
        model=config.ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_text}],
        thinking={"type": "adaptive"},
        extra_body={
            "output_config": {
                "effort": "high",
                "format": {"type": "json_schema", "schema": schema},
            }
        },
    ) as stream:
        async for _ in stream.text_stream:
            pass  # drain; structured output is consumed from the final message
        msg = await stream.get_final_message()
    text = "".join(b.text for b in msg.content if b.type == "text")
    return json.loads(text)


def stream_text(system: str, user_text: str, max_tokens: int = 16000):
    """Async context manager with a `.text_stream` iterator of answer tokens."""
    if active_provider() == "ollama":
        return _OllamaStream(system, user_text)
    return client.messages.stream(
        model=config.ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_text}],
        thinking={"type": "adaptive"},
        extra_body={"output_config": {"effort": "high"}},
    )


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
