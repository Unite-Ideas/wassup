"""Triage with a local LLM through Ollama, using a JSON schema so the answer is always well formed."""
from __future__ import annotations

import json
import logging

import httpx

from ..config import load_yaml, settings
from .decider import Decider, Decision, StoryContext

log = logging.getLogger(__name__)


class OllamaDecider(Decider):
    name = "ollama"

    def __init__(self, url: str | None = None, model: str | None = None):
        s = settings()
        self.url = (url or s.ollama_url).rstrip("/")
        self.model = model or s.triage_model
        self.client = httpx.Client(timeout=120)
        desks = load_yaml("desks.yaml").get("desks", [])
        self.desk_keys = [d["key"] for d in desks]
        self.desk_lines = "\n".join(f"- {d['key']}: {d['name']}. {d.get('description', '')}".rstrip() for d in desks)
        self.excluded = list((load_yaml("interests.yaml").get("exclude") or {}).keys())
        self._available: bool | None = None

    def available(self) -> bool:
        if self._available is None:
            try:
                tags = self.client.get(f"{self.url}/api/tags", timeout=3).json().get("models", [])
                names = {m.get("name") for m in tags} | {m.get("model") for m in tags}
                self._available = self.model in names or f"{self.model}:latest" in names
                if not self._available:
                    log.warning("ollama is running but model %s is not pulled; triage uses rules only", self.model)
            except Exception:
                self._available = False
                log.warning("ollama not reachable at %s; triage uses rules only", self.url)
        return self._available

    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "desk": {"type": "string", "enum": self.desk_keys + ["none"]},
                "excluded_category": {"type": "string", "enum": self.excluded + ["none"]},
                "part_of_larger_story": {"type": "boolean"},
                "significance": {"type": "integer", "minimum": 0, "maximum": 5},
                "primary_source_release": {"type": "boolean"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["desk", "excluded_category", "part_of_larger_story", "significance", "primary_source_release", "confidence"],
        }

    def prompt(self, ctx: StoryContext) -> str:
        headlines = "\n".join(f"- {t}" for t in ctx.item_titles[:10])
        return f"""You triage news for an analyst who tracks geopolitics, wars, migration, US politics and Congress,
the UN, and government document releases. They do NOT want sports, celebrity, entertainment or lifestyle
news unless it is part of a larger political or international story.

Desks:
{self.desk_lines}

Story headline: {ctx.title}
Other headlines in this story ({ctx.item_count} articles, {ctx.source_count} outlets, {ctx.country_count} countries):
{headlines}

Answer:
- desk: which desk owns this story, or "none".
- excluded_category: if this is mainly {", ".join(self.excluded)} news, which one, else "none".
- part_of_larger_story: true if an excluded topic is tied to a war, scandal, government action, or international event.
- significance: 0 (trivial) to 5 (major world event). Judge the event itself, not how widely it is covered.
- primary_source_release: true if this is a government or court document release.
- confidence: 0 to 1."""

    def decide(self, ctx: StoryContext) -> Decision:
        r = self.client.post(f"{self.url}/api/chat", json={
            "model": self.model, "stream": False, "think": False, "format": self.schema(),
            "options": {"temperature": 0},
            "messages": [{"role": "user", "content": self.prompt(ctx)}],
        })
        r.raise_for_status()
        a = json.loads(r.json()["message"]["content"])
        desk = None if a["desk"] == "none" else a["desk"]
        excluded = None if a["excluded_category"] == "none" else a["excluded_category"]
        routed = desk is not None and (excluded is None or a["part_of_larger_story"])
        return Decision(
            desk=desk, routed=routed, excluded_reason=None if routed else (excluded or "no_desk_match"),
            significance=float(a["significance"]), relevance=0.8 if routed else 0.1,
            confidence=float(a["confidence"]), backend=self.name, notes={"llm": a},
        )
