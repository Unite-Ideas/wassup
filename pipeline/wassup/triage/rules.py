"""Keyword, URL and GDELT theme rules. Free, instant, and good enough for most stories."""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..config import load_yaml
from ..text import PhraseMatcher
from .decider import Decider, Decision, StoryContext

HEAVY_THEMES = ("ARMEDCONFLICT", "KILL", "TERROR", "WMD", "COUP", "MILITARY")


@dataclass
class Desk:
    key: str
    name: str
    color: str
    priority: int
    keywords: PhraseMatcher
    themes: tuple[str, ...]
    countries: set[str]
    description: str = ""


def load_desks() -> list[Desk]:
    out = []
    for d in load_yaml("desks.yaml").get("desks", []):
        out.append(Desk(
            key=d["key"], name=d["name"], color=d.get("color", "#888"), priority=int(d.get("priority", 0)),
            keywords=PhraseMatcher(d.get("keywords") or []), themes=tuple(d.get("themes") or []),
            countries=set(d.get("countries") or []), description=d.get("description", ""),
        ))
    return out


class RulesDecider(Decider):
    name = "rules"

    def __init__(self):
        self.desks = load_desks()
        cfg = load_yaml("interests.yaml")
        self.exclude = {k: (PhraseMatcher(v.get("keywords") or []), [p.lower() for p in v.get("url_patterns") or []])
                        for k, v in (cfg.get("exclude") or {}).items()}
        self.include = PhraseMatcher([kw for v in (cfg.get("include") or {}).values() for kw in v.get("keywords") or []])

    def desk_scores(self, ctx: StoryContext) -> dict[str, dict]:
        text = ctx.text
        scores = {}
        for d in self.desks:
            kws = d.keywords.find(text)
            themes = sorted({t for t in ctx.themes if t.startswith(d.themes)}) if d.themes else []
            geo = bool(d.countries & set(ctx.countries))
            # Themes are broad (LEGISLATION fits any parliament), so they only count when a
            # keyword or a location already ties the story to this desk.
            score = min(len(kws), 5) + ((0.5 * min(len(themes), 3) + (0.75 if geo else 0)) if (kws or geo) else 0)
            scores[d.key] = {"score": score, "keywords": kws[:8], "themes": themes[:5], "geo": geo, "priority": d.priority}
        return scores

    def exclusion(self, ctx: StoryContext) -> tuple[str | None, float]:
        text = ctx.text
        best, best_strength = None, 0.0
        for cat, (matcher, patterns) in self.exclude.items():
            kw = len(matcher.find(text))
            url_share = sum(any(p in u.lower() for p in patterns) for u in ctx.urls) / max(len(ctx.urls), 1)
            strength = kw + 3 * url_share
            if strength > best_strength:
                best, best_strength = cat, strength
        return (best, best_strength) if best_strength >= 1 else (None, 0.0)

    def decide(self, ctx: StoryContext) -> Decision:
        scores = self.desk_scores(ctx)
        desk, info = max(scores.items(), key=lambda kv: (kv[1]["score"], kv[1]["priority"]))
        best = info["score"]
        excluded, ex_strength = self.exclusion(ctx)
        include_hits = self.include.find(ctx.text)

        # A strong desk match beats an exclusion (the celebrity in an international scandal case).
        routed = best >= 1.5 and (excluded is None or best >= max(2.5, ex_strength + 0.5))
        reason = None if routed else (excluded or "no_desk_match")

        tier_a = "A" in ctx.tiers
        heavy = any(t.startswith(HEAVY_THEMES) for t in ctx.themes)
        significance = (min(2.0, best / 3)
                        + min(1.5, math.log2(1 + ctx.source_count) / 2)
                        + min(0.75, max(ctx.country_count - 1, 0) * 0.25)
                        + (0.5 if tier_a else 0) + (0.25 if heavy else 0))
        relevance = min(1.0, best / 5 + 0.1 * len(include_hits)) if routed else min(0.2, best / 10)

        # Confident when the evidence is lopsided either way; unsure in the middle.
        if routed:
            confidence = 0.9 if best >= 3 and not excluded else 0.6 if best >= 2 else 0.45
        else:
            confidence = 0.9 if best < 0.75 else 0.7 if excluded and best < 2 else 0.45
        return Decision(
            desk=desk if routed else (desk if best >= 1 else None), routed=routed, excluded_reason=reason,
            significance=round(min(5.0, significance), 2), relevance=round(relevance, 3),
            confidence=confidence, backend=self.name,
            notes={"desk_score": best, "matched": {k: v for k, v in info.items() if k in ("keywords", "themes", "geo")},
                   "excluded": excluded, "include": include_hits[:5]},
        )
