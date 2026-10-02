from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import psycopg
from psycopg.types.json import Jsonb

from ..geo import Place, gazetteer

log = logging.getLogger(__name__)


@dataclass
class RawItem:
    url: str
    title: str
    published_at: datetime
    summary: str = ""
    language: str | None = None
    outlet: str | None = None
    outlet_tier: str | None = None
    outlet_state: bool = False
    places: list[Place] = field(default_factory=list)
    entities: list[tuple[str, str]] = field(default_factory=list)  # (kind, name)
    meta: dict = field(default_factory=dict)


class Collector:
    """A source of items. Subclasses set key and interval_s and implement run()."""

    key = "base"
    interval_s = 600

    def run(self, conn: psycopg.Connection) -> int:
        raise NotImplementedError


def ensure_source(conn: psycopg.Connection, key: str, name: str, kind: str, url: str | None = None,
                  country: str | None = None, language: str | None = None, tier: str = "B",
                  state_media: bool = False) -> int:
    row = conn.execute(
        """INSERT INTO sources (key, name, kind, url, country, language, trust_tier, state_media)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (key) DO UPDATE SET name = EXCLUDED.name, kind = EXCLUDED.kind, url = EXCLUDED.url,
             country = EXCLUDED.country, language = EXCLUDED.language, trust_tier = EXCLUDED.trust_tier,
             state_media = EXCLUDED.state_media
           RETURNING id""",
        (key, name, kind, url, country, language, tier, state_media),
    ).fetchone()
    return row["id"]


def mark_polled(conn: psycopg.Connection, source_id: int, error: str | None = None) -> None:
    conn.execute("UPDATE sources SET last_polled_at = now(), last_error = %s WHERE id = %s", (error, source_id))


def _place_id(conn: psycopg.Connection, p: Place, cache: dict[str, int]) -> int:
    if p.key in cache:
        return cache[p.key]
    row = conn.execute(
        """INSERT INTO places (key, name, country, kind, lat, lon) VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (key) DO UPDATE SET name = places.name RETURNING id""",
        (p.key, p.name, p.country, p.kind, p.lat, p.lon),
    ).fetchone()
    cache[p.key] = row["id"]
    return row["id"]


def _entity_id(conn: psycopg.Connection, kind: str, name: str, cache: dict[tuple[str, str], int]) -> int:
    norm = " ".join(name.lower().split())
    ck = (kind, norm)
    if ck in cache:
        return cache[ck]
    row = conn.execute(
        """INSERT INTO entities (kind, name, norm) VALUES (%s, %s, %s)
           ON CONFLICT (kind, norm) DO UPDATE SET name = entities.name RETURNING id""",
        (kind, name, norm),
    ).fetchone()
    cache[ck] = row["id"]
    return row["id"]


def places_with_title_flag(it: RawItem) -> list[tuple[Place, bool]]:
    """The item's places, flagged when the headline names them. Places named in the headline
    but missing from the list are added, since the headline is the strongest location signal."""
    in_title = gazetteer().find(it.title)
    keys = {p.key for p in in_title}
    countries = {p.country for p in in_title}
    out = [(p, p.key in keys or (p.kind == "country" and p.country in countries)) for p in it.places]
    have = {p.key for p in it.places}
    out += [(p, True) for p in in_title if p.key not in have]
    return out


def store_items(conn: psycopg.Connection, source_id: int, items: list[RawItem]) -> int:
    """Insert new items (skipping URLs already stored). Returns how many were new."""
    now = datetime.now(timezone.utc)
    place_cache: dict[str, int] = {}
    entity_cache: dict[tuple[str, str], int] = {}
    # Collectors run in parallel and share the places and entities tables. Create any new
    # rows first, in a fixed order, and commit at once, so two collectors never hold locks
    # on the same rows in opposite orders (which deadlocks).
    flagged = {id(it): places_with_title_flag(it) for it in items}
    places = {p.key: p for pl in flagged.values() for p, _ in pl}
    for key in sorted(places):
        _place_id(conn, places[key], place_cache)
    for kind, name in sorted({e for it in items for e in it.entities}):
        _entity_id(conn, kind, name, entity_cache)
    conn.commit()
    new = 0
    for it in items:
        if not it.url or not it.title:
            continue
        published = min(it.published_at, now)  # some feeds publish dates in the future
        row = conn.execute(
            """INSERT INTO items (source_id, outlet, outlet_tier, outlet_state, url, title, summary, language, published_at, meta)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (url) DO NOTHING RETURNING id""",
            (source_id, it.outlet, it.outlet_tier, it.outlet_state, it.url[:2000], it.title[:500], it.summary,
             it.language, published, Jsonb(it.meta)),
        ).fetchone()
        if not row:
            continue
        new += 1
        item_id = row["id"]
        for p, in_title in flagged[id(it)]:
            conn.execute("INSERT INTO item_places (item_id, place_id, in_title) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                         (item_id, _place_id(conn, p, place_cache), in_title))
        for kind, name in it.entities:
            conn.execute("INSERT INTO item_entities VALUES (%s, %s) ON CONFLICT DO NOTHING",
                         (item_id, _entity_id(conn, kind, name, entity_cache)))
    if new:
        conn.execute("UPDATE sources SET item_count = item_count + %s WHERE id = %s", (new, source_id))
    return new
