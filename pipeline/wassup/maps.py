"""Map tiles served from files on this PC.

Maps come from Protomaps (OpenStreetMap data) as PMTiles files in data/maps:

  world.pmtiles      the whole planet down to city level (about zoom 9)
  <region>.pmtiles   street level detail for one region (ukraine, mideast, ...)

scripts/download_maps.sh fetches them. The map asks for two layers: "world", and "detail",
which answers from whichever region file covers the tile. Fonts and icons for labels are
fetched from the Protomaps site the first time they are needed and kept here after that, so
once a place has been viewed the map works offline.
"""
from __future__ import annotations

import logging
import math
import mmap
import re
import threading
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path

import httpx
from pmtiles.tile import Compression, deserialize_directory, deserialize_header, find_tile, zxy_to_tileid

from .config import settings

log = logging.getLogger(__name__)

ASSETS_URL = "https://protomaps.github.io/basemaps-assets"
DETAIL_MINZOOM = 8


class TileFile:
    """One PMTiles file, memory mapped, with its directories cached."""

    def __init__(self, path: Path):
        self.path, self.name = path, path.stem
        self._f = open(path, "rb")
        self._m = mmap.mmap(self._f.fileno(), 0, access=mmap.ACCESS_READ)
        self.h = deserialize_header(self._m[:127])
        self.minzoom, self.maxzoom = self.h["min_zoom"], self.h["max_zoom"]
        self.bounds = (self.h["min_lon_e7"] / 1e7, self.h["min_lat_e7"] / 1e7, self.h["max_lon_e7"] / 1e7, self.h["max_lat_e7"] / 1e7)
        self.gzipped = self.h["tile_compression"] == Compression.GZIP
        self._dirs: OrderedDict[tuple[int, int], list] = OrderedDict()
        self._lock = threading.Lock()

    def _directory(self, offset: int, length: int) -> list:
        key = (offset, length)
        with self._lock:
            d = self._dirs.get(key)
            if d is not None:
                self._dirs.move_to_end(key)
                return d
        d = deserialize_directory(self._m[offset:offset + length])  # decompresses (gzip) itself
        with self._lock:
            self._dirs[key] = d
            if len(self._dirs) > 256:
                self._dirs.popitem(last=False)
        return d

    def get(self, z: int, x: int, y: int) -> bytes | None:
        if not (self.minzoom <= z <= self.maxzoom):
            return None
        tid = zxy_to_tileid(z, x, y)
        off, length = self.h["root_offset"], self.h["root_length"]
        for _ in range(4):
            e = find_tile(self._directory(off, length), tid)
            if e is None:
                return None
            if e.run_length == 0:  # a leaf directory
                off, length = self.h["leaf_directory_offset"] + e.offset, e.length
                continue
            start = self.h["tile_data_offset"] + e.offset
            return self._m[start:start + e.length]
        return None

    def covers(self, z: int, x: int, y: int) -> bool:
        w, s, e, n = tile_bounds(z, x, y)
        bw, bs, be, bn = self.bounds
        return w < be and e > bw and s < bn and n > bs

    def info(self) -> dict:
        return {"name": self.name, "bounds": self.bounds, "minzoom": self.minzoom, "maxzoom": self.maxzoom,
                "size_mb": round(self.path.stat().st_size / 1e6)}


def tile_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n = 2 ** z
    def lat(yy):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))
    return x / n * 360 - 180, lat(y + 1), (x + 1) / n * 360 - 180, lat(y)


class MapStore:
    def __init__(self, folder: Path):
        self.folder = folder
        self.world: TileFile | None = None
        self.regions: list[TileFile] = []
        if folder.is_dir():
            for p in sorted(folder.glob("*.pmtiles")):
                try:
                    f = TileFile(p)
                except Exception as e:  # a half downloaded file, for example
                    log.warning("skipping map file %s: %s", p.name, e)
                    continue
                if p.stem == "world":
                    self.world = f
                else:
                    self.regions.append(f)

    def tile(self, layer: str, z: int, x: int, y: int) -> tuple[bytes | None, bool]:
        """The tile and whether it is gzip compressed."""
        if layer == "world":
            f = self.world
            return (f.get(z, x, y), f.gzipped) if f else (None, False)
        if z < DETAIL_MINZOOM:
            return None, False
        for f in self.regions:
            if f.covers(z, x, y):
                t = f.get(z, x, y)
                if t:
                    return t, f.gzipped
        return None, False

    def info(self) -> dict:
        return {"world": self.world.info() if self.world else None, "regions": [r.info() for r in self.regions],
                "detail_minzoom": DETAIL_MINZOOM}


@lru_cache
def store() -> MapStore:
    return MapStore(settings().maps_dir)


_SAFE = re.compile(r"^[\w @.,+-]+(/[\w @.,+-]+)*$")


def asset(path: str) -> bytes | None:
    """A font or sprite file, from the local cache or fetched once from Protomaps."""
    if not _SAFE.match(path) or ".." in path:
        return None
    local = settings().maps_dir / "assets" / path
    if local.is_file():
        return local.read_bytes()
    try:
        r = httpx.get(f"{ASSETS_URL}/{path}", timeout=20, follow_redirects=True)
    except httpx.HTTPError as e:
        log.warning("map asset %s unavailable: %s", path, e)
        return None
    if r.status_code != 200:
        return None
    try:
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(r.content)
    except OSError:
        pass
    return r.content
