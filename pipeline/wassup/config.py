"""Settings from environment variables plus the YAML files in config/."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "postgresql://wassup:wassup@localhost:5432/wassup"))
    config_dir: Path = field(default_factory=lambda: Path(_env("WASSUP_CONFIG_DIR", str(REPO_ROOT / "config"))))
    ui_dist: Path = field(default_factory=lambda: Path(_env("WASSUP_UI_DIST", str(REPO_ROOT / "ui" / "dist"))))
    maps_dir: Path = field(default_factory=lambda: Path(_env("MAPS_DIR", str(REPO_ROOT / "data" / "maps"))))

    # Embeddings: "ollama" uses a local model (bge-m3 by default). "hash" is a dependency free
    # fallback that only matches shared words. Use it for tests or machines without Ollama.
    embed_backend: str = field(default_factory=lambda: _env("EMBED_BACKEND", "ollama"))
    ollama_url: str = field(default_factory=lambda: _env("OLLAMA_URL", "http://localhost:11434"))
    embed_model: str = field(default_factory=lambda: _env("EMBED_MODEL", "bge-m3"))

    # Triage: "rules" (keywords and GDELT themes only), "ollama" (local LLM for every story it is
    # worth asking about), or "hybrid" (rules first, local LLM only when rules are unsure).
    triage_backend: str = field(default_factory=lambda: _env("TRIAGE_BACKEND", "hybrid"))
    triage_model: str = field(default_factory=lambda: _env("TRIAGE_MODEL", "qwen3:8b"))

    # Jev (TypeSafe AI): fast typed decisions for triage. Used when TYPESAFE_API_KEY is set.
    typesafe_api_key: str = field(default_factory=lambda: _env("TYPESAFE_API_KEY", ""))
    jev_model: str = field(default_factory=lambda: _env("JEV_MODEL", "jev-latest"))
    jev_daily_budget_usd: float = field(default_factory=lambda: _env_float("JEV_DAILY_BUDGET_USD", 0.5))
    jev_price_per_mtok: float = field(default_factory=lambda: _env_float("JEV_PRICE_PER_MTOK", 0.042))

    # Translation of non English headlines: "ollama" or "off". Uses the triage model unless set.
    translate_backend: str = field(default_factory=lambda: _env("TRANSLATE_BACKEND", "ollama"))
    translate_model: str = field(default_factory=lambda: _env("TRANSLATE_MODEL", "") or _env("TRIAGE_MODEL", "qwen3:8b"))

    # Clustering and linking thresholds (cosine similarity). Defaults depend on the embed backend.
    cluster_threshold: float | None = field(default_factory=lambda: float(os.environ["CLUSTER_THRESHOLD"]) if "CLUSTER_THRESHOLD" in os.environ else None)
    link_threshold: float | None = field(default_factory=lambda: float(os.environ["LINK_THRESHOLD"]) if "LINK_THRESHOLD" in os.environ else None)
    merge_threshold: float | None = field(default_factory=lambda: float(os.environ["MERGE_THRESHOLD"]) if "MERGE_THRESHOLD" in os.environ else None)
    cluster_window_hours: float = field(default_factory=lambda: _env_float("CLUSTER_WINDOW_HOURS", 72))

    gdelt_backfill_files: int = field(default_factory=lambda: int(_env("GDELT_BACKFILL_FILES", "8")))
    http_user_agent: str = field(default_factory=lambda: _env("HTTP_USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Wassup/0.1"))

    def thresholds(self) -> tuple[float, float]:
        """Return (cluster, link) cosine thresholds for the active embed backend."""
        if self.embed_backend == "hash":
            cluster, link = 0.55, 0.30
        else:
            # Measured on real GDELT and RSS headlines with bge-m3: pairs above 0.75 are nearly
            # always the same event (often in two languages); below 0.70 mostly related events.
            cluster, link = 0.75, 0.62
        return (self.cluster_threshold or cluster, self.link_threshold or link)

    def merge_at(self) -> float:
        """Cosine similarity at which two whole stories are merged into one."""
        return self.merge_threshold or (0.62 if self.embed_backend == "hash" else 0.78)


@lru_cache
def settings() -> Settings:
    return Settings()


def load_yaml(name: str) -> dict:
    path = settings().config_dir / name
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}
