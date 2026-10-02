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
