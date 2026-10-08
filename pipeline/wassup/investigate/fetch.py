"""Reading a link for an investigation: a news article, a YouTube video, a Telegram post or an
X post, whatever it is, into the same shape:

    {"kind", "url", "title", "text", "published_at", "author", "outlet", "thumbnail", "links"}

`text` is the article, the post, or the video's transcript (with its description); `links` are
the links the source itself gives (in an article's text, a post, or a video description), which
is how investigations follow a report back to what it cites.

Reading only: nothing is posted, no account is used except the Telegram one for posts that have
no public page. X posts are read through fxtwitter's public mirror, which needs no account.
"""
from __future__ import annotations

import html as htmllib
import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

from ..config import settings

log = logging.getLogger(__name__)

MAX_TEXT = 30_000
YOUTUBE = re.compile(r"^https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?|shorts/|live/)|youtu\.be/)", re.I)
TELEGRAM = re.compile(r"^https?://t\.me/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,31})/(\d+)", re.I)
FACEBOOK = re.compile(r"^https?://(?:www\.|m\.|web\.)?(?:facebook\.com|fb\.watch)/", re.I)
XPOST = re.compile(r"^https?://(?:www\.|mobile\.)?(?:twitter\.com|x\.com)/([A-Za-z0-9_]{1,15})/status/(\d+)", re.I)
_URL = re.compile(r"https?://[^\s<>\"')\]]+")


class FetchError(Exception):
    """The link could not be read; the message says why, in plain words."""


def kind_of(url: str) -> str:
    if YOUTUBE.match(url):
        return "youtube"
    if TELEGRAM.match(url):
        return "telegram"
    if XPOST.match(url):
        return "x"
    if FACEBOOK.match(url):
        return "facebook"
    return "article"


def canonical(url: str) -> str:
    """One spelling per source, so the same video or post pasted twice is one source."""
    url = url.strip()
    m = XPOST.match(url)
    if m:
        return f"https://x.com/{m.group(1)}/status/{m.group(2)}"
    m = TELEGRAM.match(url)
    if m:
        return f"https://t.me/{m.group(1)}/{m.group(2)}"
    if YOUTUBE.match(url):
        vid = _youtube_id(url)
        if vid:
            return f"https://www.youtube.com/watch?v={vid}"
    parts = urlsplit(url)
    query = "&".join(p for p in parts.query.split("&") if p and not p.lower().startswith(("utm_", "fbclid", "gclid")))
    return parts._replace(query=query, fragment="").geturl()


def _youtube_id(url: str) -> str | None:
    p = urlsplit(url)
    if p.netloc.endswith("youtu.be"):
        return p.path.strip("/").split("/")[0] or None
    if "/shorts/" in p.path or "/live/" in p.path:
        return p.path.rstrip("/").split("/")[-1] or None
    return (parse_qs(p.query).get("v") or [None])[0]


def _client() -> httpx.Client:
    return httpx.Client(timeout=30, follow_redirects=True,
                        headers={"User-Agent": settings().http_user_agent, "Accept-Language": "en,*;q=0.5"})


def fetch(url: str, client: httpx.Client | None = None) -> dict:
    kind = kind_of(url)
    client = client or _client()
    if kind == "youtube":
        return fetch_youtube(url)
    if kind == "telegram":
        return fetch_telegram(url, client)
    if kind == "x":
        return fetch_x(url, client)
    if kind == "facebook":
        return fetch_facebook(url, client)
    return fetch_article(url, client)


# --- articles ------------------------------------------------------------------------------

def fetch_article(url: str, client: httpx.Client) -> dict:
    try:
        r = client.get(url)
    except httpx.HTTPError as e:
        raise FetchError(f"could not reach the page ({type(e).__name__})") from e
    if r.status_code in (401, 402, 403, 451):
        raise FetchError(f"the site refused (HTTP {r.status_code}); it may be paywalled or block robots")
    if r.status_code >= 400:
        raise FetchError(f"the page answered HTTP {r.status_code}")
    if "html" not in r.headers.get("content-type", "html"):
        raise FetchError(f"not a web page ({r.headers.get('content-type')})")
    return parse_article(str(r.url), r.text)


def parse_article(url: str, page: str) -> dict:
    import trafilatura
    from lxml import etree

    meta = trafilatura.extract_metadata(page, default_url=url)
    xml = trafilatura.extract(page, url=url, output_format="xml", include_links=True, include_comments=False,
                              include_tables=False, favor_precision=True)
    text, links = "", []
    if xml:
        root = etree.fromstring(xml.encode())
        text = "\n".join(" ".join(p.itertext()).strip() for p in root.iter("p", "head", "item", "quote") if " ".join(p.itertext()).strip())
        for ref in root.iter("ref"):
            target = (ref.get("target") or "").strip()
            if target.startswith(("http://", "https://", "/")):
                links.append({"url": urljoin(url, target), "text": " ".join(ref.itertext()).strip()[:120]})
    # Embedded posts and videos are often outside the article text: keep their links too.
    for m in re.finditer(r'(?:href|src)="(https?://(?:twitter\.com|x\.com|t\.me|www\.youtube\.com/(?:watch|embed)|youtu\.be)[^"]+)"', page):
        u = htmllib.unescape(m.group(1)).replace("/embed/", "/watch?v=")
        links.append({"url": u, "text": "embedded"})
    if len(text) < 200:
        raise FetchError("no article text found on the page (a paywall, a video page or a cookie wall)")
    published = _date(meta.date if meta else None)
    from_url = date_in_url(url)
    if from_url and (published is None or abs((published - from_url).days) > 31):
        published = from_url  # sites often give the date the page template was made
    return {"kind": "article", "url": url, "title": (meta.title if meta and meta.title else text[:120]),
            "text": text[:MAX_TEXT], "published_at": published, "author": meta.author if meta else None,
            "outlet": meta.sitename if meta and meta.sitename else urlsplit(url).netloc.removeprefix("www."),
            "thumbnail": meta.image if meta else None, "links": _dedupe_links(links, url)}


def _date(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            d = datetime.strptime(s[:25], fmt)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def date_in_url(url: str) -> datetime | None:
    """A date written in the address: /2026/09/30/ or 9-28-26 or 2026-09-30."""
    path = urlsplit(url).path
    for pat, order in ((r"/(20\d\d)/(\d{1,2})/(\d{1,2})(?:/|$)", "ymd"), (r"(20\d\d)-(\d{2})-(\d{2})", "ymd"),
                       (r"(?<!\d)(\d{1,2})-(\d{1,2})-(\d{2})(?!\d)", "mdy")):
        m = re.search(pat, path)
        if not m:
            continue
        a, b, c = (int(x) for x in m.groups())
        y, mo, d = (a, b, c) if order == "ymd" else (2000 + c, a, b)
        try:
            return datetime(y, mo, d, 12, tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _dedupe_links(links: list[dict], base: str) -> list[dict]:
    """Links worth following: not to the same site's sections, tags, share buttons or ads."""
    host = urlsplit(base).netloc.removeprefix("www.")
    out, seen = [], set()
    for link in links:
        u = canonical(link["url"])
        p = urlsplit(u)
        h = p.netloc.removeprefix("www.")
        if not h or u in seen or u == canonical(base):
            continue
        if any(s in h for s in ("facebook.com", "linkedin.com", "pinterest.", "whatsapp.", "doubleclick", "google.com", "apple.com",
                                "instagram.com/accounts", "reddit.com/submit")):
            continue
        if "share" in p.path.lower() or "intent/tweet" in u or p.path in ("", "/") or "cdn-cgi" in p.path \
                or "videoseries" in u or u.endswith(("/donate", "/donate/", "/subscribe", "/subscribe/")):
            continue
        if h in ("twitter.com", "x.com") and "/status/" not in p.path:
            continue  # a profile, not a post
        same_site = h == host or h.endswith("." + host)
        if same_site and (p.path.count("/") < 2 or re.search(r"/(tag|tags|topic|topics|author|section|category|live)/", p.path)):
            continue
        seen.add(u)
        out.append({"url": u, "text": link.get("text") or ""})
    return out[:60]


# --- YouTube -------------------------------------------------------------------------------

def fetch_youtube(url: str) -> dict:
    from yt_dlp import YoutubeDL

    opts = {"skip_download": True, "quiet": True, "no_warnings": True, "noplaylist": True}
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        raise FetchError(f"YouTube would not give the video's details ({str(e).splitlines()[0][:160]})") from e
    transcript = youtube_transcript(info)
    desc = info.get("description") or ""
    text = (f"Video description: {desc}\n\nTranscript:\n{transcript}" if transcript else f"Video description: {desc}\n\n(No transcript.)")
    published = None
    if info.get("timestamp"):
        published = datetime.fromtimestamp(info["timestamp"], timezone.utc)
    elif info.get("upload_date"):
        published = datetime.strptime(info["upload_date"], "%Y%m%d").replace(tzinfo=timezone.utc)
    return {"kind": "youtube", "url": canonical(url), "title": info.get("title") or url, "text": text[:MAX_TEXT],
            "published_at": published, "author": info.get("uploader") or info.get("channel"),
            "outlet": f"YouTube: {info.get('channel') or info.get('uploader') or 'unknown channel'}",
            "thumbnail": info.get("thumbnail"), "duration": info.get("duration"), "views": info.get("view_count"),
            "links": _dedupe_links([{"url": u, "text": "in the description"} for u in _URL.findall(desc)], url)}


def youtube_transcript(info: dict) -> str:
    """The video's own subtitles, or YouTube's automatic ones, in the language spoken."""
    lang = (info.get("language") or "").split("-")[0]
    subs, auto = info.get("subtitles") or {}, info.get("automatic_captions") or {}
    order = [(subs, lang), (auto, f"{lang}-orig"), (auto, lang), (subs, "en"), (auto, "en")]
    order += [(auto, k) for k in auto if k.endswith("-orig")]
    for table, key in order:
        fmts = table.get(key) if key else None
        if not fmts:
            continue
        fmt = next((f for f in fmts if f.get("ext") == "json3"), None)
        if not fmt:
            continue
        try:
            data = httpx.get(fmt["url"], timeout=30).json()
        except Exception:
            continue
        words = []
        for ev in data.get("events") or []:
            line = "".join(s.get("utf8", "") for s in ev.get("segs") or []).strip()
            if line:
                words.append(line)
        text = " ".join(" ".join(words).split())
        if text:
            return text
    return ""


def search_youtube(query: str, n: int = 10) -> list[dict]:
    """Videos YouTube finds for a query: url, title, channel (no download, no account)."""
    from yt_dlp import YoutubeDL

    with YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}) as ydl:
        info = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
    out = []
    for e in info.get("entries") or []:
        vid = e.get("id")
        if vid:
            out.append({"url": f"https://www.youtube.com/watch?v={vid}", "title": e.get("title") or "",
                        "outlet": f"YouTube: {e.get('channel') or e.get('uploader') or ''}"})
    return out


# --- Telegram and X ------------------------------------------------------------------------

def fetch_telegram(url: str, client: httpx.Client) -> dict:
    from ..social.telegram import parse_page

    m = TELEGRAM.match(url)
    handle, post = m.group(1), int(m.group(2))
    try:
        r = client.get(f"https://t.me/{handle}/{post}?embed=1&mode=tme")
        r.raise_for_status()
        page = parse_page(r.text)
    except Exception as e:
        raise FetchError(f"could not read the post ({type(e).__name__})") from e
    p = next((x for x in page["posts"] if x["id"] == post), None)
    if p is None:
        from ..social.telegram_live import read_post
        p = read_post(handle, post)  # private or hidden from the web: ask the logged in account
        if p is None:
            raise FetchError("the post is not public and the Telegram account cannot see it")
    from ..social.telegram import _title
    return {"kind": "telegram", "url": f"https://t.me/{handle}/{post}", "title": _title(p["text"]) if p["text"] else f"@{handle} post {post}",
            "text": p["text"], "published_at": p["at"], "author": f"@{handle}", "outlet": f"Telegram: @{handle}",
            "thumbnail": next((u for u in p.get("media") or [] if u.startswith("https://")), None),
            "forwarded_from": p.get("forwarded_from"),
            "links": [{"url": f"https://t.me/{h}", "text": "linked channel"} for h in p.get("links") or []]
                     + [{"url": u, "text": "in the post"} for u in _URL.findall(p["text"] or "")]}


def fetch_facebook(url: str, client: httpx.Client) -> dict:
    """Facebook shows posts only to logged in people, except the preview it gives link previews:
    the start of the post and its picture. Marked partial; paste the full text by hand."""
    try:
        r = client.get(url, headers={"User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"})
    except httpx.HTTPError as e:
        raise FetchError(f"could not reach Facebook ({type(e).__name__})") from e
    og = {m.group(1): htmllib.unescape(m.group(2)) for m in
          re.finditer(r'<meta (?:property|name)="(og:title|og:description|og:image|og:url)" content="([^"]*)"', r.text)}
    title, desc = og.get("og:title", ""), og.get("og:description", "")
    if not (title or desc) or "log in" in title.lower():
        raise FetchError("Facebook shows this only to logged in people (a private group or profile); paste its text instead")
    body = desc if len(desc) > len(title) else title
    page = title.split(" | ")[0] if " | " in title and not title.split(" | ")[0][:1].isdigit() else ""
    return {"kind": "facebook", "url": canonical(url), "title": body.split("\n")[0][:200], "text": f"{title}\n\n{desc}".strip(),
            "published_at": None, "author": page or None, "outlet": f"Facebook{': ' + page if page else ''}",
            "thumbnail": og.get("og:image"), "partial": True,
            "links": [{"url": u, "text": "in the post"} for u in _URL.findall(desc)]}


def fetch_x(url: str, client: httpx.Client) -> dict:
    m = XPOST.match(url)
    user, status = m.group(1), m.group(2)
    try:
        r = client.get(f"https://api.fxtwitter.com/{user}/status/{status}")
        data = r.json()
    except Exception as e:
        raise FetchError(f"could not read the X post ({type(e).__name__})") from e
    t = data.get("tweet")
    if not t:
        raise FetchError(f"X post unavailable ({data.get('message') or r.status_code}); it may be deleted or protected")
    text = t.get("text") or ""
    if t.get("quote"):
        q = t["quote"]
        text += f"\n\nQuoting @{(q.get('author') or {}).get('screen_name')}: {q.get('text') or ''}"
    media = t.get("media") or {}
    thumb = next((p.get("url") for p in media.get("photos") or []), None) or next((v.get("thumbnail_url") for v in media.get("videos") or []), None)
    author = t.get("author") or {}
    links = [{"url": u, "text": "in the post"} for u in _URL.findall(text)]
    if t.get("quote"):
        links.append({"url": t["quote"].get("url") or "", "text": "quoted post"})
    return {"kind": "x", "url": canonical(url), "title": text.split("\n")[0][:200] or f"@{user} on X", "text": text[:MAX_TEXT],
            "published_at": datetime.fromtimestamp(t["created_timestamp"], timezone.utc) if t.get("created_timestamp") else None,
            "author": f"@{author.get('screen_name') or user}", "outlet": f"X: @{author.get('screen_name') or user}",
            "thumbnail": thumb, "has_video": bool(media.get("videos")),
            "links": [l for l in links if l["url"]]}


def as_json(d: dict) -> str:
    return json.dumps(d, default=str)
