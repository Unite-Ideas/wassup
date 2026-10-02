"""Translates non English headlines into English with a local model through Ollama.

Runs in the background, a batch of headlines per model call, most important stories first
(stories on a desk, then the biggest). When a story's headline gets translated it is triaged
again, since the keyword rules can now read it."""
from __future__ import annotations

import json
import logging
import time

import httpx
import psycopg

from .cluster import refresh_headlines
from .config import settings

log = logging.getLogger(__name__)

BATCH = 20

PENDING = """
    SELECT i.id, i.story_id, i.title, i.language FROM items i JOIN stories s ON s.id = i.story_id
    WHERE i.translated_at IS NULL AND i.language IS NOT NULL AND i.language <> 'en'
    ORDER BY s.routed DESC, s.item_count DESC, i.published_at DESC
    LIMIT %s"""


class Translator:
    def __init__(self, url: str | None = None, model: str | None = None):
        s = settings()
        self.url = (url or s.ollama_url).rstrip("/")
        self.model = model or s.translate_model
        self.client = httpx.Client(timeout=180)
        self._available: bool | None = None
        self._checked = 0.0

    def available(self) -> bool:
        if self._available is None or (not self._available and time.time() - self._checked > 300):
            self._checked = time.time()
            try:
                tags = self.client.get(f"{self.url}/api/tags", timeout=3).json().get("models", [])
                names = {m.get("name") for m in tags} | {m.get("model") for m in tags}
                self._available = self.model in names or f"{self.model}:latest" in names
                if not self._available:
                    log.warning("translation off: Ollama does not have %s (set TRANSLATE_MODEL)", self.model)
            except Exception:
                self._available = False
                log.warning("translation off: Ollama not reachable at %s", self.url)
        return self._available

    def translate(self, headlines: list[str]) -> list[str]:
        """English versions, in the same order. Raises ValueError when the model returns the
        wrong number of lines."""
        numbered = "\n".join(f"{i + 1}. {h}" for i, h in enumerate(headlines))
        schema = {"type": "object", "properties": {"translations": {
            "type": "array", "items": {"type": "string"}, "minItems": len(headlines), "maxItems": len(headlines)}},
            "required": ["translations"]}
        r = self.client.post(f"{self.url}/api/chat", json={
            "model": self.model, "stream": False, "think": False, "format": schema, "options": {"temperature": 0},
            "messages": [{"role": "user", "content":
                "Translate each news headline into natural English. Keep names of people, places and "
                "organizations in their usual English spelling. Do not add commentary. Return exactly "
                f"{len(headlines)} translations, in order.\n\n{numbered}"}],
        })
        r.raise_for_status()
        out = json.loads(r.json()["message"]["content"])["translations"]
        if len(out) != len(headlines):
            raise ValueError(f"expected {len(headlines)} translations, got {len(out)}")
        return [" ".join(t.split()) for t in out]


def run_translate(conn: psycopg.Connection, max_seconds: float = 60, translator: Translator | None = None) -> int:
    if settings().translate_backend == "off":
        return 0
    tr = translator or _translator()
    if not tr.available():
        return 0
    started, done = time.time(), 0
    while time.time() - started < max_seconds:
        rows = conn.execute(PENDING, (BATCH,)).fetchall()
        if not rows:
            break
        try:
            english = tr.translate([r["title"] for r in rows])
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            log.warning("translation batch failed (%s); retrying one at a time", e)
            english = []
            for r in rows:
                try:
                    english.append(tr.translate([r["title"]])[0])
                except Exception:
                    english.append(None)
        with conn.cursor() as cur:
            # A blank or unchanged answer still counts as done so the item is not retried forever.
            cur.executemany("UPDATE items SET title_en = %s, translated_at = now() WHERE id = %s",
                            [(e if e and e != r["title"] else None, r["id"]) for r, e in zip(rows, english)])
        story_ids = list({r["story_id"] for r in rows})
        refresh_headlines(conn, story_ids)
        # Let triage look again now that the keyword rules can read the headline.
        conn.execute("UPDATE stories SET triaged_item_count = 0 WHERE id = ANY(%s) AND title_en IS NOT NULL", (story_ids,))
        conn.commit()
        done += len(rows)
    if done:
        log.info("translated %d headlines", done)
    return done


_tr: Translator | None = None


def _translator() -> Translator:
    global _tr
    if _tr is None:
        _tr = Translator()
    return _tr

