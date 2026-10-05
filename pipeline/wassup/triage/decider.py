"""The Decider interface. Any backend that can answer the triage questions plugs in here:
rules, a local LLM through Ollama, or later a typed decision model such as Jev."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StoryContext:
    id: int
    title: str
    item_titles: list[str]
    summaries: list[str]
    urls: list[str]
    themes: list[str]
    countries: list[str]
    item_count: int
    source_count: int
    country_count: int
    tiers: list[str]

    @property
    def text(self) -> str:
        return " \n".join([self.title, *self.item_titles, *self.summaries])


@dataclass
class Decision:
    desk: str | None              # None means no desk owns it
    routed: bool                  # False means cold storage
    excluded_reason: str | None   # category from interests.yaml, or "no_desk_match"
    significance: float           # 0..5, how much the story matters on its own; coverage is added later
    relevance: float              # 0..1
    confidence: float             # 0..1, how sure the backend is
    backend: str
    notes: dict = field(default_factory=dict)


class Decider:
    name = "base"

    def decide(self, ctx: StoryContext) -> Decision:
        raise NotImplementedError
