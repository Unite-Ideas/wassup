"""Shipping and trade: how goods move around the world (config/shipping.yaml, docs/SHIPPING.md).

- network.py: the fixed network: ports and chokepoints with their daily ship counts (IMF
  PortWatch), airports (OurAirports), freight railways (Natural Earth), US land border crossings
  with truck wait times (CBP).
- ais.py: live cargo ships and tankers from AISStream.
- planes.py: cargo planes in the air from OpenSky.
- events.py: disruptions to trade and transport read from the Shipping desk's stories, plus
  PortWatch's alerts for storms and quakes near ports.
- api.py: the map layers.
"""
from __future__ import annotations

from ..config import load_yaml


def cfg() -> dict:
    return load_yaml("shipping.yaml") or {}
