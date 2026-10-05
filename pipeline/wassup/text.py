"""Small text helpers: HTML stripping, whole word phrase matching, language detection."""
from __future__ import annotations

import html
import re
from typing import Iterable

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def clean(text: str | None, limit: int | None = None) -> str:
    if not text:
        return ""
    text = _WS.sub(" ", html.unescape(_TAG.sub(" ", text))).strip()
    if limit and len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "..."
    return text


_PIPE_SUFFIX = re.compile(r"\s+[|\u2013\u2014]\s+[^|\u2013\u2014]{2,60}$")
_DASH_SUFFIX = re.compile(r"\s+-\s+([^-]{2,50})$")


def clean_title(title: str | None) -> str:
    """Unescape HTML entities and drop trailing site names ("Headline | Site", "Headline - Site")."""
    t = clean(title)
    for _ in range(2):
        m = _PIPE_SUFFIX.search(t)
        if m and m.start() >= 25:
            t = t[: m.start()]
    m = _DASH_SUFFIX.search(t)
    if m and m.start() >= 25:
        tail = m.group(1).split()
        if len(tail) <= 5 and all(w[:1].isupper() or not w[:1].isalpha() for w in tail):
            t = t[: m.start()]
    return t.strip()



# Labels wire services and sites put in front of headlines: "(LEAD)", "(2nd LD)", "(Asiad)",
# "【禁聞】", "[VIDEO]", "UPDATE 2-", "BREAKING:", "WATCH:". They say nothing about the event,
# and when every Yonhap sports headline starts with "(LEAD) (Asiad)" they make unrelated
# articles look alike. Stripped for comparing articles only; the headline you see keeps them.
_LEAD_LABEL = re.compile(
    r"^\s*(?:\([^()]{1,24}\)|\[[^\[\]]{1,24}\]|【[^【】]{1,16}】|(?:UPDATE|REFILE|CORRECTED|WRAPUP|RPT)\s?\d*-(?=\S)"
    r"|(?:breaking|exclusive|update|updated|urgent|watch|video|live|photos?|analysis|opinion|explainer|factbox|"
    r"just in|news focus|lead|live updates?|breaking news)(?:\s\d+)?\s?[:|-])\s*", re.IGNORECASE)


def comparable_title(title: str) -> str:
    t = title
    for _ in range(4):
        m = _LEAD_LABEL.match(t)
        if not m or m.end() >= len(t):
            break
        t = t[m.end():]
    return t.strip() or title


# Pages that are not news at all: site sections, legal pages, error pages, job ads.
_JUNK = re.compile(
    r"^(?:terms of (?:service|use)|privacy (?:policy|notice)|cookie (?:policy|settings)|page not found|404\b|access denied"
    r"|subscribe\b|sign in\b|log ?in\b|contact us|about us|advertise with us)"
    r"|^(?:[\w&]+ ){0,2}(?:news|headlines|stories|articles|videos?|podcasts?|archives?)(?: - .{2,60})?$"
    r"|^(?:senior |junior |lead |principal |staff )?[\w-]+ (?:engineer|developer|designer|manager|analyst|technician|nurse)$",
    re.IGNORECASE)


def is_junk_title(title: str) -> bool:
    return bool(_JUNK.search(title.strip()))


class PhraseMatcher:
    """Finds which of a set of phrases appear in text as whole words.

    Case insensitive by default. Works for any script because it relies on Unicode word
    characters rather than ASCII boundaries.
    """

    def __init__(self, phrases: Iterable[str], case_sensitive: bool = False):
        uniq = sorted({p.strip() for p in phrases if p and p.strip()}, key=len, reverse=True)
        self._lookup = {(p if case_sensitive else p.lower()): p for p in uniq}
        self.empty = not uniq
        if not self.empty:
            body = "|".join(re.escape(p) for p in uniq)
            flags = 0 if case_sensitive else re.IGNORECASE
            self._re = re.compile(rf"(?<!\w)(?:{body})(?!\w)", flags)
        self._cs = case_sensitive

    def find(self, text: str) -> list[str]:
        """Distinct phrases found, in order of first appearance."""
        if self.empty or not text:
            return []
        seen: dict[str, None] = {}
        for m in self._re.finditer(text):
            key = m.group(0) if self._cs else m.group(0).lower()
            seen.setdefault(self._lookup.get(key, m.group(0)), None)
        return list(seen)


def detect_language(text: str, fallback: str | None = None) -> str | None:
    if not text or len(text) < 12:
        return fallback
    try:
        from langdetect import DetectorFactory, detect

        DetectorFactory.seed = 0
        return detect(text)
    except Exception:
        return fallback
