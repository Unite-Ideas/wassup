"""Text embeddings. Every vector is 1024 dimensions and L2 normalized."""
from __future__ import annotations

import logging
import re
import zlib

import httpx
import numpy as np

from .config import settings

DIM = 1024
log = logging.getLogger(__name__)


def as_array(v) -> np.ndarray:
    """pgvector values come back as Vector objects or numpy arrays depending on context."""
    if hasattr(v, "to_numpy"):
        v = v.to_numpy()
    return np.asarray(v, dtype=np.float32)


class Embedder:
    name = "base"

    @property
    def id(self) -> str:
        return self.name

    def embed(self, texts: list[str]) -> np.ndarray:
        raise NotImplementedError


def _normalize(m: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (m / norms).astype(np.float32)


class OllamaEmbedder(Embedder):
    """Local embeddings through Ollama. bge-m3 is multilingual, so a Farsi and an English
    headline about the same event land close together."""

    name = "ollama"

    @property
    def id(self) -> str:
        return f"ollama:{self.model}"

    def __init__(self, url: str, model: str):
        self.url = url.rstrip("/")
        self.model = model
        self.client = httpx.Client(timeout=120)

    def embed(self, texts: list[str]) -> np.ndarray:
        try:
            r = self.client.post(f"{self.url}/api/embed", json={"model": self.model, "input": texts, "truncate": True})
        except httpx.ConnectError as e:
            raise RuntimeError(f"Ollama is not reachable at {self.url}. Start Ollama, or set EMBED_BACKEND=hash to run without it.") from e
        if r.status_code == 404:
            raise RuntimeError(f"Ollama does not have the embedding model '{self.model}'. Run: ollama pull {self.model}")
        r.raise_for_status()
        vecs = np.array(r.json()["embeddings"], dtype=np.float32)
        if vecs.shape[1] != DIM:
            raise ValueError(f"{self.model} returns {vecs.shape[1]} dimensions, schema expects {DIM}. Use bge-m3 or change db/schema.sql.")
        return _normalize(vecs)


_TOKEN = re.compile(r"\w+", re.UNICODE)
_STOP = set("""a an the of to in on for and or but with at by from as is are was were be been it its this that
these those after before over under into about says said will would can could has have had not no new
than then their his her they them he she we you i our out up more most""".split())


class HashEmbedder(Embedder):
    """Feature hashing over words and word pairs. No model needed. Only finds shared vocabulary,
    so it will not match across languages. Good for tests and as a fallback."""

    name = "hash"

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), DIM), dtype=np.float32)
        for i, text in enumerate(texts):
            words = [w for w in _TOKEN.findall(text.lower()) if w not in _STOP and len(w) > 1]
            feats = words + [f"{a}_{b}" for a, b in zip(words, words[1:])]
            for f in feats:
                h = zlib.crc32(f.encode("utf-8"))
                out[i, h % DIM] += 1.0 if (h >> 16) & 1 else -1.0
        return _normalize(out)


_embedder: Embedder | None = None


def get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        s = settings()
        if s.embed_backend == "hash":
            _embedder = HashEmbedder()
        else:
            _embedder = OllamaEmbedder(s.ollama_url, s.embed_model)
        log.info("embedder: %s", _embedder.name)
    return _embedder
