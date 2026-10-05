"""Map tiles, fonts and icons for the MAP view."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response

from . import maps

router = APIRouter(prefix="/api/maps")
WEEK = "public, max-age=604800"


@router.get("/info")
def info() -> dict:
    return maps.store().info()


@router.get("/tiles/{layer}/{z}/{x}/{y}.mvt")
def tile(layer: str, z: int, x: int, y: int) -> Response:
    if layer not in ("world", "detail") or not (0 <= z <= 22) or not (0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise HTTPException(404)
    data, gz = maps.store().tile(layer, z, x, y)
    if not data:
        return Response(status_code=204)
    headers = {"Cache-Control": WEEK}
    if gz:
        headers["Content-Encoding"] = "gzip"
    return Response(bytes(data), media_type="application/vnd.mapbox-vector-tile", headers=headers)


@router.get("/assets/{path:path}")
def asset(path: str) -> Response:
    data = maps.asset(path)
    if data is None:
        raise HTTPException(404)
    kind = "application/json" if path.endswith(".json") else "image/png" if path.endswith(".png") else "application/x-protobuf"
    return Response(data, media_type=kind, headers={"Cache-Control": WEEK})
