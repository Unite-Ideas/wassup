"""US government primary sources: Congress (bills and Senate roll call votes) and the
Federal Register (presidential documents and notices from security agencies)."""
from __future__ import annotations

import logging
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import httpx
import psycopg

from ..config import load_yaml, settings
from ..geo import gazetteer
from ..text import clean
from .base import Collector, RawItem, ensure_source, mark_polled, store_items

log = logging.getLogger(__name__)

BILL_PATH = {"HR": "house-bill", "S": "senate-bill", "HJRES": "house-joint-resolution", "SJRES": "senate-joint-resolution",
             "HRES": "house-resolution", "SRES": "senate-resolution", "HCONRES": "house-concurrent-resolution",
             "SCONRES": "senate-concurrent-resolution"}


def current_congress(now: datetime | None = None) -> tuple[int, int]:
    now = now or datetime.now(timezone.utc)
    return (now.year - 1789) // 2 + 1, 1 if now.year % 2 else 2


def _places_or_dc(text: str):
    gaz = gazetteer()
    return gaz.find(text) or [p for p in [gaz.by_name("Washington")] if p]


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def parse_bills(payload: dict) -> list[RawItem]:
    items = []
    for b in payload.get("bills", []):
        btype, number, congress = b.get("type", ""), b.get("number", ""), b.get("congress")
        action = b.get("latestAction") or {}
        title = clean(b.get("title"), 400)
        if not (btype and number and title):
            continue
        url = f"https://www.congress.gov/bill/{_ordinal(congress)}-congress/{BILL_PATH.get(btype, 'bill')}/{number}"
        when = action.get("actionDate") or b.get("updateDate", "")[:10]
        try:
            published = datetime.fromisoformat(when).replace(tzinfo=timezone.utc)
        except ValueError:
            published = datetime.now(timezone.utc)
        summary = clean(f"Latest action ({when}): {action.get('text', '')}", 800)
        items.append(RawItem(
            url=f"{url}#{when}", title=f"{btype} {number}: {title}", summary=summary, language="en",
            published_at=published, outlet="Congress.gov", outlet_tier="A",
            places=_places_or_dc(f"{title}. {summary}"),
            meta={"kind": "bill", "bill": f"{btype}{number}", "congress": congress, "chamber": b.get("originChamber")},
        ))
    return items


def parse_senate_votes(xml: bytes, congress: int, session: int) -> list[RawItem]:
    root = ET.fromstring(xml)
    year = int(root.findtext("congress_year") or datetime.now(timezone.utc).year)
    items = []
    for v in root.iter("vote"):
        num = (v.findtext("vote_number") or "").strip()
        title = clean(v.findtext("title"), 400)
        if not num or not title:
            continue
        question = clean(v.findtext("question"))
        result = clean(v.findtext("result"))
        yeas, nays = v.findtext("vote_tally/yeas"), v.findtext("vote_tally/nays")
        try:
            published = datetime.strptime(f"{v.findtext('vote_date')}-{year} 12:00", "%d-%b-%Y %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            published = datetime.now(timezone.utc)
        url = f"https://www.senate.gov/legislative/LIS/roll_call_votes/vote{congress}{session}/vote_{congress}_{session}_{num}.htm"
        items.append(RawItem(
            url=url, title=f"Senate vote {int(num)}: {title}", language="en", published_at=published,
            summary=f"{question}: {result} ({yeas} yeas, {nays} nays). Issue {clean(v.findtext('issue'))}.",
            outlet="U.S. Senate", outlet_tier="A", places=_places_or_dc(title),
            meta={"kind": "senate_vote", "result": result, "yeas": yeas, "nays": nays},
        ))
    return items


class CongressCollector(Collector):
    key = "congress"
    interval_s = 1800

    def __init__(self):
        cfg = load_yaml("sources.yaml").get("congress") or {}
        self.enabled = cfg.get("enabled", True)
        self.api_key = os.environ.get(cfg.get("api_key_env", "CONGRESS_API_KEY")) or "DEMO_KEY"
        self.client = httpx.Client(timeout=40, follow_redirects=True, headers={"User-Agent": settings().http_user_agent})

    def run(self, conn: psycopg.Connection) -> int:
        if not self.enabled:
            return 0
        total = 0
        congress, session = current_congress()

        sid = ensure_source(conn, "gov:congress_bills", "Congress.gov bills", "congress", "https://api.congress.gov", "US", "en", "A")
        try:
            r = self.client.get("https://api.congress.gov/v3/bill", params={
                "api_key": self.api_key, "format": "json", "limit": 100, "sort": "updateDate+desc"})
            r.raise_for_status()
            total += store_items(conn, sid, parse_bills(r.json()))
            mark_polled(conn, sid)
        except Exception as e:
            mark_polled(conn, sid, f"{type(e).__name__}: {e}"[:300])
            log.warning("congress bills: %s", e)
        conn.commit()

        sid = ensure_source(conn, "gov:senate_votes", "U.S. Senate roll call votes", "congress", "https://www.senate.gov", "US", "en", "A")
        try:
            r = self.client.get(f"https://www.senate.gov/legislative/LIS/roll_call_lists/vote_menu_{congress}_{session}.xml")
            r.raise_for_status()
            total += store_items(conn, sid, parse_senate_votes(r.content, congress, session))
            mark_polled(conn, sid)
        except Exception as e:
            mark_polled(conn, sid, f"{type(e).__name__}: {e}"[:300])
            log.warning("senate votes: %s", e)
        conn.commit()
        return total


# Agencies whose notices tend to matter for the desks. Slugs from https://www.federalregister.gov/agencies
FR_AGENCIES = ["state-department", "defense-department", "homeland-security-department", "justice-department",
               "foreign-assets-control-office", "industry-and-security-bureau", "u-s-citizenship-and-immigration-services",
               "u-s-immigration-and-customs-enforcement", "national-archives-and-records-administration", "national-security-council"]
FR_FIELDS = ["title", "html_url", "publication_date", "abstract", "agencies", "type", "document_number", "subtype"]


def parse_federal_register(payload: dict) -> list[RawItem]:
    items = []
    for d in payload.get("results", []):
        title, url = clean(d.get("title"), 400), d.get("html_url")
        if not title or not url:
            continue
        agencies = [a.get("name") for a in d.get("agencies") or [] if a.get("name")]
        try:
            published = datetime.fromisoformat(d["publication_date"]).replace(hour=12, tzinfo=timezone.utc)
        except (KeyError, ValueError):
            published = datetime.now(timezone.utc)
        summary = clean(d.get("abstract"), 1000)
        label = d.get("subtype") or d.get("type") or "Document"
        items.append(RawItem(
            url=url, title=f"{label}: {title}", summary=summary, language="en", published_at=published,
            outlet="Federal Register", outlet_tier="A", places=_places_or_dc(f"{title}. {summary}"),
            meta={"kind": "federal_register", "type": d.get("type"), "agencies": agencies, "doc": d.get("document_number")},
        ))
    return items


class FederalRegisterCollector(Collector):
    key = "federal_register"
    interval_s = 3600

    def __init__(self):
        cfg = load_yaml("sources.yaml").get("federal_register") or {}
        self.enabled = cfg.get("enabled", True)
        self.client = httpx.Client(timeout=40, follow_redirects=True, headers={"User-Agent": settings().http_user_agent})

    def _query(self, params: list[tuple[str, str]]) -> list[RawItem]:
        base = [("per_page", "100"), ("order", "newest")] + [("fields[]", f) for f in FR_FIELDS]
        r = self.client.get("https://www.federalregister.gov/api/v1/documents.json", params=base + params)
        r.raise_for_status()
        return parse_federal_register(r.json())

    def run(self, conn: psycopg.Connection) -> int:
        if not self.enabled:
            return 0
        sid = ensure_source(conn, "gov:federal_register", "Federal Register", "federal_register",
                            "https://www.federalregister.gov", "US", "en", "A")
        try:
            items = self._query([("conditions[type][]", "PRESDOCU")])
            items += self._query([("conditions[agencies][]", a) for a in FR_AGENCIES]
                                 + [("conditions[type][]", t) for t in ("RULE", "PRORULE", "NOTICE")])
            n = store_items(conn, sid, items)
            mark_polled(conn, sid)
        except Exception as e:
            n = 0
            mark_polled(conn, sid, f"{type(e).__name__}: {e}"[:300])
            log.warning("federal register: %s", e)
        conn.commit()
        return n
