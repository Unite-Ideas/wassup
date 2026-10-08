"""Investigations: everything about one story, traced back toward the originals.

You start one with a title, what you want to know, and links (articles, YouTube videos,
Telegram, X and Facebook posts), and can paste the text of anything Wassup cannot read. Then,
while the investigation is active (two weeks by default), the investigate lane:

1. Reads each link (fetch.py). The text becomes an item, so it is translated, grouped into
   stories and placed on the map like anything else Wassup collects.
2. Finds related reports Wassup already has: everything in the same stories, and the closest
   articles and posts by meaning (bge-m3), in any language.
3. Asks the local model about each source: is it about this story, how does it know what it
   says (an eyewitness, someone involved, an official statement, a primary document, a reporter's
   own reporting, or a rewrite of someone else's), what evidence it mentions and whether the
   author saw it, who the people are, what they are said to stand to gain or lose, what is
   claimed and denied, and which of its links are what it relies on.
4. Follows those links (two steps at most) toward the originals: the documents, the first
   report, the post or video it all came from.
5. Searches further every few hours: news worldwide (GDELT), YouTube, and the Telegram channels
   the logged in account can see, with queries the model writes from your question.
6. Keeps a summary that answers your questions from the sources, citing them, separating what
   was shown from what was only said, and saying "not reported" rather than guessing.
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit

import httpx
import psycopg
from psycopg.types.json import Jsonb

from ..collectors.base import RawItem, ensure_source, store_items
from ..text import detect_language
from .fetch import FetchError, canonical, fetch, kind_of, search_youtube

log = logging.getLogger(__name__)

WATCH_DAYS = 14
MAX_SOURCES = 300          # per investigation, all kinds
MAX_DEPTH = 2              # how far citations are followed
RELATED_MIN = 0.62         # bge-m3 similarity for a related report Wassup already has
RELATED_EVERY = timedelta(minutes=10)
SEARCH_EVERY = timedelta(hours=6)
SUMMARY_EVERY = timedelta(minutes=15)
TEXT_LIMIT = 24_000        # characters of a source given to the model (a long video transcript fits)
DOCUMENT = re.compile(r"\.pdf($|\?)|acrobat\.adobe\.com|drive\.google\.com|docs\.google\.com|documentcloud\.org|scribd\.com|"
                      r"courtlistener\.com|pacer|/uploads/.*\.(pdf|docx?)", re.I)
ACCOUNTS = ["eyewitness", "participant", "official", "document", "original_reporting", "secondhand", "commentary", "unrelated"]
FIRSTHAND = {"eyewitness", "participant", "official", "document"}


# --- starting and adding ---------------------------------------------------------------------

def split_links(text: str) -> list[str]:
    return list(dict.fromkeys(canonical(u) for u in re.findall(r"https?://[^\s<>\"]+", text or "")))


def create(conn: psycopg.Connection, title: str, brief: str, links: str = "", watch_days: int = WATCH_DAYS) -> int:
    row = conn.execute(
        "INSERT INTO investigations (title, brief, watch_until) VALUES (%s, %s, now() + %s * interval '1 day') RETURNING id",
        (title.strip()[:300], brief.strip(), watch_days)).fetchone()
    add_links(conn, row["id"], split_links(links), "you")
    conn.commit()
    return row["id"]


def add_links(conn, inv: int, urls: list[str], found_by: str, parent: int | None = None, depth: int = 0,
              extra: dict | None = None) -> int:
    n = 0
    total = conn.execute("SELECT count(*) AS n FROM investigation_sources WHERE investigation_id = %s", (inv,)).fetchone()["n"]
    for url in urls:
        if total + n >= MAX_SOURCES and found_by not in ("you", "investigator"):
            break
        url = canonical(url)
        kind = "document" if DOCUMENT.search(url) else kind_of(url)
        row = conn.execute(
            """INSERT INTO investigation_sources (investigation_id, url, kind, found_by, parent_id, depth, title, outlet, meta)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (investigation_id, url) DO NOTHING RETURNING id""",
            (inv, url, kind, found_by, parent, depth, (extra or {}).get(url, {}).get("title"),
             (extra or {}).get(url, {}).get("outlet"), Jsonb({}))).fetchone()
        n += bool(row)
    conn.execute("UPDATE investigations SET updated_at = now() WHERE id = %s", (inv,))
    return n


_JUNK = re.compile(r"[͏​-‏⁠﻿]")


def clean_pasted(text: str) -> str:
    """Pasted text minus the scrambled dates and invisible characters social sites add."""
    text = _JUNK.sub("", unicodedata.normalize("NFC", text or ""))
    lines = [ln.rstrip() for ln in text.splitlines()]
    kept = [ln for ln in lines if len(ln.strip()) > 2 or not ln.strip()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def add_text(conn, inv: int, text: str, url: str | None = None, title: str | None = None, author: str | None = None,
             published_at: datetime | None = None, found_by: str = "you") -> int:
    """Text you paste: the full version of a post Wassup could only partly read, or anything else."""
    text = clean_pasted(text)
    if url:
        url = canonical(url)
    else:
        n = conn.execute("SELECT count(*) AS n FROM investigation_sources WHERE investigation_id = %s", (inv,)).fetchone()["n"]
        url = f"wassup:pasted/{inv}/{n + 1}"
    conn.execute(
        """INSERT INTO investigation_sources (investigation_id, url, kind, found_by, status) VALUES (%s, %s, %s, %s, 'pending')
           ON CONFLICT (investigation_id, url) DO NOTHING""",
        (inv, url, "pasted" if url.startswith("wassup:") else kind_of(url), found_by))
    src = conn.execute("SELECT * FROM investigation_sources WHERE investigation_id = %s AND url = %s", (inv, url)).fetchone()
    first_line = next((ln for ln in text.splitlines() if len(ln) > 15), text[:120])
    d = {"kind": src["kind"], "url": url, "title": title or src["title"] or first_line[:200], "text": text,
         "published_at": published_at or src["published_at"], "author": author or src["author"],
         "outlet": src["outlet"] or (author or ("Pasted by you" if found_by == "you" else "Found by the Investigator")), "thumbnail": src["thumbnail"], "links": [], "pasted": True}
    _store_fetched(conn, inv, src, d)
    conn.commit()
    return src["id"]


# --- the lane --------------------------------------------------------------------------------

def run_investigations(conn: psycopg.Connection, llm=None, max_seconds: float = 180) -> int:
    conn.execute("UPDATE investigations SET status = 'done' WHERE status = 'active' AND watch_until < now()")
    invs = conn.execute("SELECT * FROM investigations WHERE status = 'active' ORDER BY updated_at DESC").fetchall()
    conn.commit()
    if not invs:
        return 0
    if llm is None:
        from ..newsroom.llm import default_llm
        llm = default_llm()
    started, work = time.time(), 0
    try:
        from .agent import hand_off
        work += hand_off(conn)
    except Exception:
        conn.rollback()
        log.exception("could not hand investigations to the Investigator")
    budget = max_seconds / len(invs)
    for inv in invs:
        until = min(started + max_seconds, time.time() + budget)
        work += step_fetch(conn, inv["id"], until)
        work += step_related(conn, inv)
        work += step_analyze(conn, inv, llm, until)
        work += step_search(conn, inv, llm)
        work += step_summary(conn, inv["id"], llm)
    return work


def step_fetch(conn, inv: int, until: float) -> int:
    rows = conn.execute(
        """SELECT * FROM investigation_sources WHERE investigation_id = %s AND status = 'pending'
           ORDER BY (found_by IN ('you', 'investigator')) DESC, depth, id LIMIT 12""", (inv,)).fetchall()
    conn.commit()
    if not rows:
        return 0
    client = httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": _ua()})

    def get(src):
        if time.time() > until:
            return src, None
        try:
            return src, fetch(src["url"], client)
        except FetchError as e:
            return src, e
        except Exception as e:
            log.warning("investigation %s: reading %s failed: %s", inv, src["url"], e)
            return src, FetchError(f"{type(e).__name__}: {e}"[:200])

    n = 0
    with ThreadPoolExecutor(6) as ex:
        for src, d in ex.map(get, rows):
            if d is None:
                continue
            if isinstance(d, Exception):
                conn.execute("UPDATE investigation_sources SET status = 'failed', error = %s WHERE id = %s", (str(d)[:300], src["id"]))
            else:
                _store_fetched(conn, inv, src, d)
            conn.commit()
            n += 1
    return n


def _ua() -> str:
    from ..config import settings
    return settings().http_user_agent


def _store_fetched(conn, inv: int, src: dict, d: dict) -> None:
    """Keep what was read as an item with its full text, and describe the source."""
    sid = ensure_source(conn, "investigations", "Investigations", "web", None, None, None, "U", False)
    url = d["url"] if not d["url"].startswith("wassup:") else d["url"]
    item = conn.execute("SELECT id FROM items WHERE url = %s", (url,)).fetchone()
    if item is None:
        store_items(conn, sid, [RawItem(url=url, title=(d["title"] or url)[:500], summary=d["text"][:1200],
                                        published_at=d["published_at"] or datetime.now(timezone.utc),
                                        language=detect_language(d["text"]), outlet=d["outlet"],
                                        meta={"investigation": inv, "kind": d["kind"]})])
        item = conn.execute("SELECT id FROM items WHERE url = %s", (url,)).fetchone()
    if item is not None:
        conn.execute(
            """INSERT INTO item_texts (item_id, status, body, chars) VALUES (%s, 'ok', %s, %s)
               ON CONFLICT (item_id) DO UPDATE SET body = EXCLUDED.body, chars = EXCLUDED.chars, status = 'ok'
               WHERE %s OR item_texts.chars IS NULL OR item_texts.chars < EXCLUDED.chars""",
            (item["id"], d["text"], len(d["text"]), bool(d.get("pasted"))))
    meta = {k: d[k] for k in ("links", "partial", "duration", "views", "has_video", "forwarded_from", "pasted") if d.get(k)}
    if d.get("pasted"):
        meta["partial"] = False
    conn.execute(
        """UPDATE investigation_sources SET item_id = %s, status = 'fetched', error = NULL, kind = %s,
                  title = %s, outlet = coalesce(%s, outlet), author = coalesce(%s, author),
                  published_at = coalesce(%s, published_at), thumbnail = coalesce(%s, thumbnail),
                  meta = meta || %s WHERE id = %s""",
        (item["id"] if item else None, d["kind"] if d["kind"] != "article" or src["kind"] != "document" else "document",
         (d["title"] or "")[:500], d["outlet"], d["author"], d["published_at"], d["thumbnail"], Jsonb(meta), src["id"]))


def step_related(conn, inv: dict) -> int:
    """Reports Wassup already has: the same stories, and the nearest by meaning."""
    if inv["related_at"] and datetime.now(timezone.utc) - inv["related_at"] < RELATED_EVERY:
        return 0
    seeds = conn.execute(
        """SELECT s.id, i.embedding, i.story_id, i.published_at FROM investigation_sources s JOIN items i ON i.id = s.item_id
           WHERE s.investigation_id = %s AND NOT s.hidden AND i.embedding IS NOT NULL
             AND (s.found_by IN ('you', 'investigator') OR (s.analysis->>'relevant')::boolean)
           ORDER BY (s.found_by IN ('you', 'investigator')) DESC, s.id LIMIT 12""", (inv["id"],)).fetchall()
    conn.execute("UPDATE investigations SET related_at = now() WHERE id = %s", (inv["id"],))
    if not seeds:
        conn.commit()
        return 0
    lo = min(s["published_at"] for s in seeds) - timedelta(days=7)
    found: dict[int, tuple[str, float]] = {}
    stories = [s["story_id"] for s in seeds if s["story_id"]]
    for r in conn.execute("SELECT id, url FROM items WHERE story_id = ANY(%s) LIMIT 400", (stories,)):
        found[r["id"]] = (r["url"], 1.0)
    for s in seeds:
        for r in conn.execute(
                """SELECT id, url, 1 - (embedding <=> %s) AS sim FROM items
                   WHERE embedding IS NOT NULL AND published_at >= %s ORDER BY embedding <=> %s LIMIT 40""",
                (s["embedding"], lo, s["embedding"])):
            if r["sim"] >= RELATED_MIN and r["sim"] > found.get(r["id"], ("", 0))[1]:
                found[r["id"]] = (r["url"], r["sim"])
    n = 0
    for item_id, (url, sim) in sorted(found.items(), key=lambda x: -x[1][1]):
        row = conn.execute(
            """INSERT INTO investigation_sources (investigation_id, url, item_id, kind, found_by, status, similarity, title, outlet, published_at)
               SELECT %s, %s, i.id, %s, 'wassup', 'fetched', %s, i.title, i.outlet, i.published_at FROM items i WHERE i.id = %s
               ON CONFLICT (investigation_id, url) DO NOTHING RETURNING id""",
            (inv["id"], canonical(url), kind_of(url), round(sim, 3), item_id)).fetchone()
        n += bool(row)
    conn.commit()
    if n:
        log.info("investigation %s: %d related reports already in Wassup", inv["id"], n)
    return n


# --- reading each source -----------------------------------------------------------------------

SCHEMA = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "account": {"type": "string", "enum": ACCOUNTS},
        "summary": {"type": "string"},
        "relies_on": {"type": "array", "maxItems": 8, "items": {"type": "string"}},
        "evidence": {"type": "array", "maxItems": 10, "items": {"type": "object", "properties": {
            "what": {"type": "string"},
            "kind": {"type": "string", "enum": ["document", "messages", "photo", "video", "recording", "financial_record",
                                                "calendar", "forensic_report", "testimony", "other"]},
            "held_by": {"type": "string"},
            "seen_by_source": {"type": "boolean"},
            "checked_by": {"type": "string"},
            "quote": {"type": "string"}},
            "required": ["what", "kind", "held_by", "seen_by_source", "checked_by", "quote"]}},
        "people": {"type": "array", "maxItems": 10, "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "role": {"type": "string"},
            "stance": {"type": "string", "enum": ["accuses", "denies", "supports_accused", "supports_accuser", "neutral", "unclear"]},
            "interest": {"type": "string"}, "quote": {"type": "string"}},
            "required": ["name", "role", "stance", "interest", "quote"]}},
        "claims": {"type": "array", "maxItems": 8, "items": {"type": "object", "properties": {
            "claim": {"type": "string"}, "who_says": {"type": "string"}, "quote": {"type": "string"}},
            "required": ["claim", "who_says", "quote"]}},
        "responses": {"type": "array", "maxItems": 6, "items": {"type": "object", "properties": {
            "who": {"type": "string"}, "response": {"type": "string"}, "quote": {"type": "string"}},
            "required": ["who", "response", "quote"]}},
        "cited_links": {"type": "array", "maxItems": 12, "items": {"type": "integer"}},
        "dates": {"type": "array", "maxItems": 12, "items": {"type": "object", "properties": {
            "date": {"type": "string"}, "what": {"type": "string"}}, "required": ["date", "what"]}},
    },
    "required": ["relevant", "account", "summary", "relies_on", "evidence", "people", "claims", "responses", "cited_links", "dates"],
}


def analyze_source(llm, inv: dict, src: dict, text: str) -> dict:
    links = (src["meta"] or {}).get("links") or []
    link_list = "\n".join(f"[{i + 1}] {l['url']}  ({l.get('text') or ''})" for i, l in enumerate(links[:40])) or "(none)"
    partial = "\nNote: only the start of this post could be read." if (src["meta"] or {}).get("partial") else ""
    prompt = f"""You help a researcher get to the bottom of a story. They are investigating: {inv['title']}
What they want to know: {inv['brief'] or '(not stated)'}

One source to read carefully:
Kind: {src['kind']}. Outlet: {src['outlet'] or 'unknown'}. Author: {src['author'] or 'unknown'}.
Published: {src['published_at'].strftime('%Y-%m-%d %H:%M UTC') if src['published_at'] else 'unknown'}. Address: {src['url']}
Title: {src['title'] or ''}{partial}

Text:
{text[:TEXT_LIMIT]}

Links in this source:
{link_list}

Answer about this source only, from its text. Do not add what you know from elsewhere.
- relevant: does it say anything about the story being investigated?
- account: how this source knows what it says. eyewitness (saw or heard it themselves), participant (one of the
  people involved speaking for themselves, the accused and the accuser included), official (an organisation's own
  statement), document (the source is itself a primary record), original_reporting (a journalist's own reporting:
  interviews, documents they reviewed, checks they made), secondhand (passes on another outlet's reporting),
  commentary (opinion or reaction), unrelated.
- summary: two sentences in English on what this source adds.
- relies_on: who or what it names as where its information comes from.
- evidence: each piece of evidence it mentions. held_by: who has it. seen_by_source: true only if the author says
  they saw or reviewed it themselves. checked_by: who independent of the people involved checked it, and how, or ""
  if nobody is said to have. quote: the sentence that says so.
- people: each person or organisation involved. role (accuser, accused, witness, board, investigator, reporter...),
  stance, and interest: what this source says they stand to gain or lose, their stated reasons, or their ties to
  others involved; "" if it says nothing. Never guess motives.
- claims: the main factual claims and who makes them. responses: denials or replies, with who gave them.
- cited_links: the numbers of the links above that this source relies on (documents, the original report, a post or
  video it is based on). Not navigation, profiles, ads or unrelated stories.
- dates: dates it gives for events in the story, as YYYY-MM-DD where possible.
Quotes are copied exactly from the text."""
    return llm(prompt, SCHEMA)


def _text_of(conn, src: dict) -> str:
    row = conn.execute(
        """SELECT coalesce(t.body, i.summary, i.title) AS text FROM items i LEFT JOIN item_texts t ON t.item_id = i.id AND t.status = 'ok'
           WHERE i.id = %s""", (src["item_id"],)).fetchone() if src["item_id"] else None
    return (row or {}).get("text") or src["title"] or ""


def step_analyze(conn, inv: dict, llm, until: float, parallel: int = 2) -> int:
    rows = conn.execute(
        """SELECT * FROM investigation_sources WHERE investigation_id = %s AND status = 'fetched' AND NOT hidden
           ORDER BY (found_by IN ('you', 'investigator')) DESC, (found_by = 'traced') DESC, similarity DESC NULLS LAST, id LIMIT %s""",
        (inv["id"], parallel * 3)).fetchall()
    jobs = []
    for src in rows:
        text = _text_of(conn, src)
        if len(text) < 400 and src["kind"] == "article" and src["found_by"] == "wassup":
            try:  # only the headline so far: read the article now
                d = fetch(src["url"])
                _store_fetched(conn, inv["id"], src, d)
                src = conn.execute("SELECT * FROM investigation_sources WHERE id = %s", (src["id"],)).fetchone()
                text = d["text"]
            except Exception:
                pass
        jobs.append((src, text))
    conn.commit()
    if not jobs:
        return 0

    def ask(job):
        src, text = job
        if time.time() > until:
            return src, None
        try:
            return src, analyze_source(llm, inv, src, text)
        except Exception as e:
            log.warning("investigation %s: reading source %s failed: %s", inv["id"], src["id"], e)
            return src, None

    n = 0
    with ThreadPoolExecutor(parallel) as ex:
        for src, out in ex.map(ask, jobs):
            if out is None:
                continue
            relevant = bool(out.get("relevant")) and out.get("account") != "unrelated"
            conn.execute("UPDATE investigation_sources SET analysis = %s, status = %s WHERE id = %s",
                         (Jsonb(out), "analyzed" if relevant else "unrelated", src["id"]))
            if relevant and src["depth"] < MAX_DEPTH:
                links = (src["meta"] or {}).get("links") or []
                cited = [links[i - 1]["url"] for i in out.get("cited_links") or [] if isinstance(i, int) and 0 < i <= min(len(links), 40)]
                if cited:
                    add_links(conn, inv["id"], cited, "traced", src["id"], src["depth"] + 1)
                fwd = (src["meta"] or {}).get("forwarded_from")
                if fwd:
                    add_links(conn, inv["id"], [f"https://t.me/{fwd}"], "traced", src["id"], src["depth"] + 1)
            conn.commit()
            n += 1
    if n:
        log.info("investigation %s: read %d sources", inv["id"], n)
    return n


# --- searching further -----------------------------------------------------------------------

QUERIES = {
    "type": "object",
    "properties": {"queries": {"type": "array", "maxItems": 6, "items": {"type": "object", "properties": {
        "query": {"type": "string"}, "language": {"type": "string"}}, "required": ["query", "language"]}}},
    "required": ["queries"],
}


def make_queries(llm, inv: dict, seeds: list[dict]) -> list[dict]:
    lines = "\n".join(f"- {s['title']} ({s['outlet'] or ''})" for s in seeds[:12])
    prompt = f"""Write web search queries to find every report on this story: news articles, videos, posts.
Story: {inv['title']}
What the researcher wants to know: {inv['brief']}
Known sources:
{lines}
Give 3 to 6 short queries (2 to 6 words each: names of the people, organisations and places involved, and the
key event), in English, plus queries in the local language if the story happened where another language is spoken."""
    return [q for q in (llm(prompt, QUERIES).get("queries") or []) if (q.get("query") or "").strip()][:6]


def search_gdelt(query: str, since: datetime, client: httpx.Client, n: int = 50) -> list[dict]:
    start = max(since, datetime.now(timezone.utc) - timedelta(days=89))
    url = ("https://api.gdeltproject.org/api/v2/doc/doc?mode=artlist&format=json&sort=datedesc"
           f"&maxrecords={n}&startdatetime={start:%Y%m%d%H%M%S}&query={quote(query)}")
    r = client.get(url)
    if r.status_code != 200 or not r.text.strip().startswith("{"):
        return []
    return [{"url": a["url"], "title": a.get("title"), "outlet": a.get("domain")} for a in r.json().get("articles") or []]


def step_search(conn, inv: dict, llm) -> int:
    if inv["searched_at"] and datetime.now(timezone.utc) - inv["searched_at"] < SEARCH_EVERY:
        return 0
    seeds = conn.execute(
        """SELECT title, outlet, published_at FROM investigation_sources WHERE investigation_id = %s AND title IS NOT NULL
             AND (found_by = 'you' OR status = 'analyzed') ORDER BY (found_by = 'you') DESC, id LIMIT 12""", (inv["id"],)).fetchall()
    if not seeds:
        return 0  # nothing read yet: search once the first sources are in
    conn.execute("UPDATE investigations SET searched_at = now() WHERE id = %s", (inv["id"],))
    conn.commit()
    queries = inv["queries"] or []
    if not queries:
        try:
            queries = make_queries(llm, inv, seeds)
        except Exception as e:
            log.warning("investigation %s: could not write search queries: %s", inv["id"], e)
            return 0
        conn.execute("UPDATE investigations SET queries = %s WHERE id = %s", (Jsonb(queries), inv["id"]))
        conn.commit()
    since = min((s["published_at"] for s in seeds if s["published_at"]), default=datetime.now(timezone.utc)) - timedelta(days=14)
    client = httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": _ua()})
    hits: list[dict] = []
    for i, q in enumerate(queries):
        try:
            hits += search_gdelt(q["query"], since, client)[:25]
            time.sleep(5)  # GDELT allows one query every five seconds
        except Exception as e:
            log.debug("gdelt %r: %s", q["query"], e)
        if i < 2:
            try:
                hits += search_youtube(q["query"], 8)
            except Exception as e:
                log.debug("youtube %r: %s", q["query"], e)
            try:
                from ..social.telegram_live import search_posts
                hits += search_posts(q["query"], since)
            except Exception as e:
                log.debug("telegram %r: %s", q["query"], e)
    extra = {canonical(h["url"]): h for h in hits}
    n = add_links(conn, inv["id"], list(extra), "search", extra=extra)
    conn.commit()
    if n:
        log.info("investigation %s: search found %d new sources", inv["id"], n)
    return n


# --- the summary -----------------------------------------------------------------------------

SUMMARY = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "answers": {"type": "array", "maxItems": 8, "items": {"type": "object", "properties": {
            "question": {"type": "string"}, "answer": {"type": "string"},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "sources": {"type": "array", "items": {"type": "integer"}}}, "required": ["question", "answer", "confidence", "sources"]}},
        "evidence": {"type": "array", "maxItems": 14, "items": {"type": "object", "properties": {
            "what": {"type": "string"}, "held_by": {"type": "string"},
            "status": {"type": "string", "enum": ["only described", "seen by a reporter", "checked independently", "disputed"]},
            "detail": {"type": "string"},
            "sources": {"type": "array", "items": {"type": "integer"}}}, "required": ["what", "held_by", "status", "detail", "sources"]}},
        "people": {"type": "array", "maxItems": 12, "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "role": {"type": "string"}, "position": {"type": "string"},
            "interest": {"type": "string"},
            "sources": {"type": "array", "items": {"type": "integer"}}}, "required": ["name", "role", "position", "interest", "sources"]}},
        "timeline": {"type": "array", "maxItems": 25, "items": {"type": "object", "properties": {
            "date": {"type": "string"}, "what": {"type": "string"},
            "sources": {"type": "array", "items": {"type": "integer"}}}, "required": ["date", "what", "sources"]}},
        "origin": {"type": "string"},
        "disagreements": {"type": "array", "maxItems": 8, "items": {"type": "object", "properties": {
            "about": {"type": "string"}, "sides": {"type": "string"},
            "sources": {"type": "array", "items": {"type": "integer"}}}, "required": ["about", "sides", "sources"]}},
        "open_questions": {"type": "array", "maxItems": 8, "items": {"type": "string"}},
    },
    "required": ["headline", "answers", "evidence", "people", "timeline", "origin", "disagreements", "open_questions"],
}


def _digest(n: int, s: dict) -> str:
    a = s["analysis"] or {}
    when = s["published_at"].strftime("%Y-%m-%d") if s["published_at"] else "date unknown"
    ev = "; ".join(f"{e['what']} (held by {e['held_by'] or '?'}, {'seen by the author' if e.get('seen_by_source') else 'described only'}"
                   f"{', checked by ' + e['checked_by'] if e.get('checked_by') else ''})" for e in (a.get("evidence") or [])[:6])
    ppl = "; ".join(f"{p['name']} ({p['role']}, {p['stance']}{': ' + p['interest'] if p.get('interest') else ''})" for p in (a.get("people") or [])[:6])
    resp = "; ".join(f"{r['who']}: {r['response']}" for r in (a.get("responses") or [])[:4])
    return (f"[{n}] {when} | {s['outlet'] or s['kind']} | {a.get('account')} | {s['title'] or ''}\n"
            f"  {a.get('summary') or ''}\n  relies on: {', '.join(a.get('relies_on') or []) or '-'}\n"
            f"  evidence: {ev or '-'}\n  people: {ppl or '-'}\n  responses: {resp or '-'}")


def step_summary(conn, inv_id: int, llm, force: bool = False) -> int:
    inv = conn.execute("SELECT * FROM investigations WHERE id = %s", (inv_id,)).fetchone()
    srcs = conn.execute(
        """SELECT * FROM investigation_sources WHERE investigation_id = %s AND status = 'analyzed' AND NOT hidden
           ORDER BY pinned DESC, (analysis->>'account' = ANY(%s)) DESC, (analysis->>'account' = 'original_reporting') DESC,
                    published_at NULLS LAST, id LIMIT 40""", (inv_id, list(FIRSTHAND))).fetchall()
    if not srcs:
        return 0
    if not force:
        if len(srcs) == inv["summary_sources"]:
            return 0
        if inv["summary_at"] and datetime.now(timezone.utc) - inv["summary_at"] < SUMMARY_EVERY:
            return 0
    numbered = {n + 1: s for n, s in enumerate(srcs)}
    digest = "\n".join(_digest(n, s) for n, s in numbered.items())
    prompt = f"""You are a careful investigative editor. Using ONLY the sources below, answer the researcher's questions
about this story and lay out the evidence. Today is {datetime.now(timezone.utc):%Y-%m-%d}.

Story: {inv['title']}
The researcher wants to know: {inv['brief'] or '(see title)'}

Sources (numbered; cite them by number):
{digest}

Rules:
- Allegations are allegations: say who alleges what, and who denies it. Nobody is guilty or innocent here.
- Keep apart what was shown (documents seen by a reporter, independent checks) from what was only said.
- Interests and motives: report only what sources say (stated reasons, relationships, what someone risks or gains,
  and the timing of events). Where sources say nothing, say "not reported". Do not speculate.
- answers: one per question the researcher asked (split their questions), each with a confidence and the sources.
- evidence: each piece, who holds it, and whether it is only described, seen by a reporter, checked independently,
  or disputed.
- people: everyone involved: role, their position on the allegations, and their interests as reported.
- timeline: dated events, oldest first. origin: how the story came out, who reported first and from where.
- disagreements: where sources or people contradict each other. open_questions: what is still unknown."""
    try:
        out = llm(prompt, SUMMARY)
    except Exception as e:
        log.warning("investigation %s: summary failed: %s", inv_id, e)
        return 0
    out["source_ids"] = {str(n): s["id"] for n, s in numbered.items()}
    conn.execute("UPDATE investigations SET summary = %s, summary_at = now(), summary_sources = %s WHERE id = %s",
                 (Jsonb(out), len(srcs), inv_id))
    conn.commit()
    log.info("investigation %s: summary updated from %d sources", inv_id, len(srcs))
    return 1


def domain(url: str) -> str:
    return urlsplit(url).netloc.removeprefix("www.")


def brief_text(conn, inv_id: int) -> str:
    """The whole investigation as one readable text, for the Investigator agent."""
    inv = conn.execute("SELECT * FROM investigations WHERE id = %s", (inv_id,)).fetchone()
    srcs = conn.execute(
        """SELECT * FROM investigation_sources WHERE investigation_id = %s AND NOT hidden
           ORDER BY (status = 'analyzed') DESC, published_at NULLS LAST, id""", (inv_id,)).fetchall()
    leads = conn.execute("SELECT * FROM investigation_leads WHERE investigation_id = %s ORDER BY id", (inv_id,)).fetchall()
    out = [f"# Investigation {inv_id}: {inv['title']}", "", f"The researcher wants to know: {inv['brief'] or '(see title)'}"]
    if inv["ask_note"]:
        out += ["", f"The researcher's note for you: {inv['ask_note']}"]
    s = inv["summary"] or {}
    if s:
        out += ["", "## Summary so far (written by the local model; check it)", s.get("headline", "")]
        for a in s.get("answers") or []:
            out.append(f"- Q: {a['question']}\n  A ({a['confidence']}): {a['answer']}")
        for e in s.get("evidence") or []:
            out.append(f"- Evidence: {e['what']} | held by {e['held_by']} | {e['status']} | {e.get('detail', '')}")
        if s.get("origin"):
            out.append(f"- How it came out: {s['origin']}")
        for q in s.get("open_questions") or []:
            out.append(f"- Unknown: {q}")
    out += ["", f"## Sources ({len(srcs)}; full text: GET {inv_id}/sources/<id>/text)"]
    for x in srcs:
        a = x["analysis"] or {}
        when = x["published_at"].strftime("%Y-%m-%d") if x["published_at"] else "date unknown"
        line = f"- [{x['id']}] {when} | {x['outlet'] or x['kind']} | {x['status']}"
        line += f" | {a.get('account')}" if a else ""
        line += f" | found by {x['found_by']}" + (f" (cited by [{x['parent_id']}])" if x["parent_id"] else "")
        line += f"\n  {x['title'] or ''}\n  {x['url']}"
        if a.get("summary"):
            line += f"\n  {a['summary']}"
        if x["error"]:
            line += f"\n  could not read: {x['error']}"
        out.append(line)
    out += ["", f"## Leads ({len(leads)})"]
    for l in leads:
        out.append(f"- [{l['id']}] {l['status']} | {l['title']} (added by {l['added_by']})"
                   + (f"\n  why: {l['why']}" if l["why"] else "") + (f"\n  finding: {l['finding']}" if l["finding"] else ""))
    if inv["memo"]:
        out += ["", "## Your notes from last time", inv["memo"]]
    return "\n".join(out)
