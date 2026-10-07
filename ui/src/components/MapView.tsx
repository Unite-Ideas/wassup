import { useEffect, useRef, useState } from "react";
import maplibregl, { type GeoJSONSource, type LayerSpecification, type StyleSpecification } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { DARK, layers as basemapLayers, type Flavor } from "@protomaps/basemaps";
import { feature } from "topojson-client";
import type { GeometryCollection, Topology } from "topojson-specification";
import countriesTopo from "world-atlas/countries-110m.json";
import type { Desk, GlobeData, Selection, StoryDetail } from "../lib/types";
import { deskColor } from "../lib/format";

export interface MapInfo {
  world: { maxzoom: number; size_mb: number } | null;
  regions: { name: string; bounds: [number, number, number, number]; maxzoom: number; size_mb: number }[];
  detail_minzoom: number;
}

/** A map layer without its source; the overlay supplies it. */
export type OverlayLayer = LayerSpecification extends infer T ? (T extends unknown ? Omit<T, "source"> : never) : never;

/** Extra layers drawn over the base map, such as tracks. Each owns one GeoJSON source. */
export interface Overlay {
  id: string;
  data: GeoJSON.FeatureCollection;
  layers: OverlayLayer[];
  /** Layer id under which the overlay is inserted, so stories stay on top. */
  before?: string;
}

interface Props {
  data: GlobeData | null;
  desks: Map<string, Desk>;
  selection: Selection;
  storyDetail: StoryDetail | null;
  visible: boolean;
  overlays: Overlay[];
  onSelectStory: (id: number) => void;
  onMapClick?: (lngLat: [number, number], features: maplibregl.MapGeoJSONFeature[]) => void;
  /** Fly here when it changes (n makes repeated requests for the same spot count). */
  focus?: { lng: number; lat: number; zoom: number; n: number } | null;
}

// The Wassup palette on Protomaps' dark flavor: near black land, deep blue water.
const FLAVOR: Flavor = { ...DARK, background: "#05080d", earth: "#0b121b", water: "#071a2b" };

function tileUrl(layer: "world" | "detail") {
  return `${location.origin}/api/maps/tiles/${layer}/{z}/{x}/{y}.mvt`;
}

function buildStyle(info: MapInfo | null): StyleSpecification {
  const glyphs = `${location.origin}/api/maps/assets/fonts/{fontstack}/{range}.pbf`;
  if (!info?.world) {
    // No map files downloaded yet: country outlines only.
    const topo = countriesTopo as unknown as Topology<{ countries: GeometryCollection }>;
    return {
      version: 8, glyphs, projection: { type: "globe" },
      sources: { countries: { type: "geojson", data: feature(topo, topo.objects.countries) as unknown as GeoJSON.FeatureCollection } },
      layers: [
        { id: "background", type: "background", paint: { "background-color": FLAVOR.water } },
        { id: "countries", type: "fill", source: "countries", paint: { "fill-color": FLAVOR.earth } },
        { id: "borders", type: "line", source: "countries", paint: { "line-color": "#26384d", "line-width": 0.6 } },
      ],
    };
  }
  const world = basemapLayers("world", FLAVOR, { lang: "en" });
  const detailMax = Math.max(info.detail_minzoom, ...info.regions.map((r) => r.maxzoom));
  // Street level regions are drawn over the world map from the zoom where they start. Where no
  // region covers the view, the world map keeps showing, stretched beyond its own detail.
  const detail = info.regions.length
    ? basemapLayers("detail", FLAVOR, { lang: "en" })
        .filter((l) => l.type !== "background")
        .map((l) => ({ ...l, id: `detail-${l.id}`, minzoom: Math.max(l.minzoom ?? 0, info.detail_minzoom) }) as LayerSpecification)
    : [];
  return {
    version: 8, glyphs, projection: { type: "globe" },
    sprite: `${location.origin}/api/maps/assets/sprites/v4/dark`,
    sources: {
      world: { type: "vector", tiles: [tileUrl("world")], maxzoom: info.world.maxzoom, attribution: "© OpenStreetMap, Protomaps" },
      ...(info.regions.length ? { detail: { type: "vector" as const, tiles: [tileUrl("detail")], minzoom: info.detail_minzoom, maxzoom: detailMax } } : {}),
    },
    layers: [...world, ...detail],
  };
}

function storiesGeoJSON(data: GlobeData | null, desks: Map<string, Desk>, selectedId: number | null): GeoJSON.FeatureCollection {
  return {
    type: "FeatureCollection",
    features: (data?.stories ?? []).filter((s) => s.lat != null && s.lon != null).map((s) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: [s.lon as number, s.lat as number] },
      properties: { id: s.id, title: s.title, sig: s.significance, breaking: s.breaking, color: deskColor(desks, s.desk), selected: s.id === selectedId },
    })),
  };
}

const STORY_LAYERS: LayerSpecification[] = [
  {
    id: "story-halo", type: "circle", source: "stories", filter: ["any", ["get", "breaking"], ["get", "selected"]],
    paint: {
      "circle-radius": ["+", ["interpolate", ["linear"], ["get", "sig"], 0, 3, 5, 11], 6],
      "circle-color": "rgba(0,0,0,0)",
      "circle-stroke-width": 1.5,
      "circle-stroke-color": ["case", ["get", "selected"], "#ffffff", "#ff3b5c"],
    },
  },
  {
    id: "story-dot", type: "circle", source: "stories",
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["get", "sig"], 0, 3, 5, 11],
      "circle-color": ["get", "color"],
      "circle-opacity": 0.85,
      "circle-stroke-width": 0.8,
      "circle-stroke-color": "#05080d",
    },
  },
  {
    id: "story-label", type: "symbol", source: "stories", minzoom: 3.5, filter: [">=", ["get", "sig"], 3.5],
    layout: {
      "text-field": ["get", "title"], "text-font": ["Noto Sans Regular"], "text-size": 11, "text-max-width": 16,
      "text-offset": [0, 1.1], "text-anchor": "top", "text-optional": true,
    },
    paint: { "text-color": "#d7e3ef", "text-halo-color": "#05080d", "text-halo-width": 1.4 },
  },
];

/** An arrow pointing north, for directions of attack. Rotated per feature on the map. */
function arrowIcon(): ImageData {
  const size = 56;
  const c = document.createElement("canvas");
  c.width = c.height = size;
  const g = c.getContext("2d")!;
  g.translate(size / 2, size / 2);
  g.beginPath();
  g.moveTo(0, -24); g.lineTo(13, -4); g.lineTo(5, -4); g.lineTo(5, 22); g.lineTo(-5, 22); g.lineTo(-5, -4); g.lineTo(-13, -4);
  g.closePath();
  g.fillStyle = "#ffb020";
  g.strokeStyle = "#05080d";
  g.lineWidth = 3;
  g.stroke();
  g.fill();
  return g.getImageData(0, 0, size, size);
}

export default function MapView({ data, desks, selection, storyDetail, visible, overlays, onSelectStory, onMapClick, focus }: Props) {
  const box = useRef<HTMLDivElement>(null);
  const map = useRef<maplibregl.Map | null>(null);
  const [info, setInfo] = useState<MapInfo | null | undefined>(undefined);
  const [ready, setReady] = useState(false);
  const selectedId = selection?.type === "story" ? selection.id : null;
  const latest = useRef({ onSelectStory, onMapClick });
  latest.current = { onSelectStory, onMapClick };

  useEffect(() => {
    fetch("/api/maps/info").then((r) => r.json()).then(setInfo).catch(() => setInfo(null));
  }, []);

  // Create the map once the map file list is known.
  useEffect(() => {
    if (info === undefined || !box.current || map.current) return;
    const m = new maplibregl.Map({
      container: box.current, style: buildStyle(info), center: [30, 30], zoom: 1.6,
      attributionControl: { compact: true }, maxPitch: 70,
    });
    m.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");
    m.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");
    m.on("load", () => {
      m.addImage("track-arrow", arrowIcon(), { pixelRatio: 2 });
      m.addSource("stories", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      STORY_LAYERS.forEach((l) => m.addLayer(l));
      setReady(true);
    });
    const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, className: "map-popup", offset: 10 });
    m.on("mousemove", "story-dot", (e) => {
      m.getCanvas().style.cursor = "pointer";
      const f = e.features?.[0];
      if (f) popup.setLngLat(e.lngLat).setText(String(f.properties.title)).addTo(m);
    });
    m.on("mouseleave", "story-dot", () => { m.getCanvas().style.cursor = ""; popup.remove(); });
    m.on("click", (e) => {
      const hit = m.queryRenderedFeatures(e.point, { layers: ["story-dot"] })[0];
      if (hit) {
        latest.current.onSelectStory(Number(hit.properties.id));
        return;
      }
      latest.current.onMapClick?.([e.lngLat.lng, e.lngLat.lat], m.queryRenderedFeatures(e.point));
    });
    map.current = m;
    if (new URLSearchParams(location.search).has("debug")) (window as unknown as { wassupMap: maplibregl.Map }).wassupMap = m;
    return () => { m.remove(); map.current = null; setReady(false); };
  }, [info]);

  useEffect(() => {
    if (visible) map.current?.resize();
  }, [visible]);

  useEffect(() => {
    if (!ready) return;
    (map.current?.getSource("stories") as GeoJSONSource | undefined)?.setData(storiesGeoJSON(data, desks, selectedId));
  }, [ready, data, desks, selectedId]);

  // Fly to a story when it is picked somewhere else (search, panels, the board).
  useEffect(() => {
    const m = map.current;
    if (!ready || !m || !storyDetail || storyDetail.id !== selectedId || storyDetail.lat == null || storyDetail.lon == null) return;
    const target = new maplibregl.LngLat(storyDetail.lon, storyDetail.lat);
    if (!m.getBounds().contains(target) || m.getZoom() < 3) {
      m.flyTo({ center: target, zoom: Math.max(m.getZoom(), 4.5), speed: 1.4 });
    }
  }, [ready, storyDetail, selectedId]);

  useEffect(() => {
    if (ready && focus) map.current?.flyTo({ center: [focus.lng, focus.lat], zoom: focus.zoom, speed: 1.4 });
  }, [ready, focus]);

  // Overlays: add, update and remove their sources and layers as the list changes.
  const shown = useRef<Map<string, string[]>>(new Map());
  useEffect(() => {
    const m = map.current;
    if (!ready || !m) return;
    const wanted = new Set(overlays.map((o) => o.id));
    for (const [id, layerIds] of shown.current) {
      if (wanted.has(id)) continue;
      layerIds.forEach((l) => m.getLayer(l) && m.removeLayer(l));
      if (m.getSource(id)) m.removeSource(id);
      shown.current.delete(id);
    }
    for (const o of overlays) {
      const src = m.getSource(o.id) as GeoJSONSource | undefined;
      if (src) {
        src.setData(o.data);
        continue;
      }
      m.addSource(o.id, { type: "geojson", data: o.data });
      const ids = o.layers.map((l) => {
        m.addLayer({ ...l, source: o.id } as LayerSpecification, o.before ?? "story-halo");
        return l.id;
      });
      shown.current.set(o.id, ids);
    }
  }, [ready, overlays]);

  return (
    <div style={{ position: "absolute", inset: 0 }}>
      <div ref={box} style={{ position: "absolute", inset: 0 }} />
      {info && !info.world && (
        <div className="map-note">
          No map files yet, so only country outlines are shown. In Ubuntu, run
          <code>bash scripts/download_maps.sh</code> in the wassup folder, then restart Wassup.
        </div>
      )}
    </div>
  );
}
