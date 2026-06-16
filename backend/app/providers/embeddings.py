"""Embedding providers.

Anthropic does not offer an embeddings endpoint, so this module supports:
  - Voyage AI (`voyage-3-lite`, 512-dim) when VOYAGE_API_KEY is set — use this
    for real deployments.
  - Local Ollama (`nomic-embed-text`, Matryoshka-trained) when an Ollama
    server is reachable. Vectors are truncated to EMBED_DIM and re-normalized
    (valid for Matryoshka models), and texts get the model's task prefixes
    (`search_document:` / `search_query:`).
  - A deterministic local hashing embedder (hashed word/bigram bag with signed
    buckets, L2-normalized) as a zero-dependency fallback. Lexical-overlap
    quality only.

The provider is picked once (EMBED_PROVIDER: auto | voyage | ollama | local)
and then pinned: silently switching providers mid-corpus would mix embedding
spaces and quietly break retrieval.
"""

import hashlib
import math
import re
from typing import List, Optional

import httpx

from ..core import config

_WORD_RE = re.compile(r"[a-z0-9']+")
_NOMIC_PREFIX = {"document": "search_document: ", "query": "search_query: "}

_provider: Optional[str] = None  # pinned on first use


def _local_embed(text: str) -> List[float]:
    dim = config.EMBED_DIM
    vec = [0.0] * dim
    words = _WORD_RE.findall(text.lower())
    grams = words + [f"{a}_{b}" for a, b in zip(words, words[1:])]
    counts = {}
    for g in grams:
        counts[g] = counts.get(g, 0) + 1
    for g, c in counts.items():
        h = hashlib.md5(g.encode()).digest()
        idx = int.from_bytes(h[:4], "little") % dim
        sign = 1.0 if h[4] % 2 == 0 else -1.0
        vec[idx] += sign * (1.0 + math.log(c))
    return _normalize(vec)


def _normalize(vec: List[float]) -> List[float]:
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


async def _ollama_reachable() -> bool:
    try:
        async with httpx.AsyncClient(timeout=3) as cx:
            r = await cx.get(f"{config.OLLAMA_BASE_URL}/api/version")
            return r.status_code == 200
    except httpx.TransportError:
        return False


async def active_embedder() -> str:
    """Resolve and pin the embedding provider."""
    global _provider
    if _provider is None:
        if config.EMBED_PROVIDER != "auto":
            _provider = config.EMBED_PROVIDER
        elif config.VOYAGE_API_KEY:
            _provider = "voyage"
        elif await _ollama_reachable():
            _provider = "ollama"
        else:
            _provider = "local"
    return _provider


async def _voyage_embed(texts: List[str], kind: str) -> List[List[float]]:
    async with httpx.AsyncClient(timeout=60) as cx:
        r = await cx.post(
            "https://api.voyageai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {config.VOYAGE_API_KEY}"},
            json={
                "model": "voyage-3-lite",
                "input": texts,
                "input_type": "query" if kind == "query" else "document",
            },
        )
        r.raise_for_status()
        data = sorted(r.json()["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in data]


async def _ollama_embed(texts: List[str], kind: str) -> List[List[float]]:
    prefix = _NOMIC_PREFIX.get(kind, _NOMIC_PREFIX["document"])
    async with httpx.AsyncClient(timeout=120) as cx:
        r = await cx.post(
            f"{config.OLLAMA_BASE_URL}/api/embed",
            json={
                "model": config.OLLAMA_EMBED_MODEL,
                "input": [prefix + t for t in texts],
            },
        )
        r.raise_for_status()
        embs = r.json()["embeddings"]
    # Matryoshka truncation to the schema's vector dimension, then re-normalize
    return [_normalize(e[: config.EMBED_DIM]) for e in embs]


async def embed_texts(texts: List[str], kind: str = "document") -> List[List[float]]:
    provider = await active_embedder()
    if provider == "voyage":
        return await _voyage_embed(texts, kind)
    if provider == "ollama":
        return await _ollama_embed(texts, kind)
    return [_local_embed(t) for t in texts]


def to_pgvector(vec: List[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"
