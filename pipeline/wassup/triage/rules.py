"""Keyword, URL and GDELT theme rules. Free, instant, and good enough for most stories."""
from __future__ import annotations

import re

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
            # A desk needs one of its own words. Themes are broad (LEGISLATION fits any parliament)
            # and a location says little on its own (a car crash in Ohio is not US politics), so
            # both only strengthen a keyword match. Headlines in other languages are matched again
            # once translated.
            hint = 0.5 * min(len(themes), 3) + (0.75 if geo else 0)
            score = min(len(kws), 5) + hint if kws else 0
            scores[d.key] = {"score": score, "keywords": kws[:8], "themes": themes[:5], "geo": geo, "priority": d.priority,
                             "hint": hint if geo else 0}
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
        if not_news(ctx):
            return Decision(desk=None, routed=False, excluded_reason="not_news", significance=0.5, relevance=0.0, confidence=0.95,
                            backend=self.name, notes={"desk_score": 0, "matched": {}, "excluded": "not_news", "include": []})
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
        # Importance only: how widely the story is covered is added when the score is stored.
        significance = (1.0 + min(2.0, best / 2.5) + (0.75 if heavy else 0) + (0.5 if tier_a else 0)
                        + (0.5 if include_hits else 0))
        relevance = min(1.0, best / 5 + 0.1 * len(include_hits)) if routed else min(0.2, best / 10)

        # Confident when the evidence is lopsided either way; unsure in the middle.
        if routed:
            confidence = 0.9 if best >= 3 and not excluded else 0.6 if best >= 2 else 0.45
        else:
            # No desk word, but the place and topics point at a desk: not routed on that alone,
            # but unsure, so the local model is asked.
            hinted = max((v["hint"] for v in scores.values()), default=0) >= 0.75
            confidence = 0.45 if hinted and not excluded else 0.9 if best < 0.75 else 0.7 if excluded and best < 2 else 0.45
        return Decision(
            desk=desk if routed else (desk if best >= 1 else None), routed=routed, excluded_reason=reason,
            significance=round(min(5.0, significance), 2), relevance=round(relevance, 3),
            confidence=confidence, backend=self.name,
            notes={"desk_score": best, "matched": {k: v for k, v in info.items() if k in ("keywords", "themes", "geo")},
                   "excluded": excluded, "include": include_hits[:5]},
        )


# Pages that are not a news story: site pages, author pages, automated stock notes, and
# digests of many unrelated stories (briefs, letters, morning shows; live blogs on one topic are news). They match every desk a
# little and tie unrelated stories together, so they go to cold storage.
NOT_NEWS = re.compile(
    r"\bterms of (service|use)\b|\bprivacy policy\b|\bcookie policy\b|\bwriter page\b|\|\s*author\b|^subscribe\b|"
    r"\bprice target\b|\bshares (bought|sold|acquired) by\b|\$[a-z]{1,5} shares\b|"
    r"\((nyse|nasdaq|nysearca|otcmkts|tsx|lon)\s*:|\bstock (rating|position)\b|\bweek in review\b|\btest your knowledge\b", re.I)


def not_news(ctx: StoryContext) -> bool:
    """Most of the story's articles look like site pages, stock notes or roundups."""
    from ..signals import DIGEST

    titles = [t for t in [ctx.title, *ctx.item_titles] if t]
    hits = sum(bool(NOT_NEWS.search(t) or DIGEST.search(t)) for t in titles)
    return bool(titles) and hits * 2 > len(titles)
