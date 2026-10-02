"""Trust tiers for outlets by domain (config/outlets.yaml)."""
from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlparse

from .config import load_yaml

TIER_RANK = {"A": 0, "B": 1, "U": 2, "C": 3, "S": 4}


@lru_cache
def _table() -> tuple[dict[str, str], list[tuple[str, str]]]:
    cfg = load_yaml("outlets.yaml")
    domains = {d.lower(): tier for tier, ds in (cfg.get("domains") or {}).items() for d in ds}
    suffixes = sorted((cfg.get("suffix_tiers") or {}).items(), key=lambda kv: -len(kv[0]))
    return domains, suffixes


def domain_of(url_or_domain: str) -> str:
    d = urlparse(url_or_domain).netloc if "//" in url_or_domain else url_or_domain
    d = d.lower().split(":")[0]
    return d[4:] if d.startswith("www.") else d


def tier_for(url_or_domain: str) -> tuple[str, bool]:
    """(tier, is_state_media) for a URL or domain. Unknown outlets are tier U."""
    domains, suffixes = _table()
    d = domain_of(url_or_domain)
    parts = d.split(".")
    for i in range(len(parts) - 1):
        tier = domains.get(".".join(parts[i:]))
        if tier:
            return tier, tier == "S"
    for suffix, tier in suffixes:
        if d.endswith(suffix) or d == suffix.lstrip("."):
            return tier, tier == "S"
    return "U", False
