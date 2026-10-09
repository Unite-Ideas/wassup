import { useEffect, useRef, useState } from "react";
import maplibregl, { type GeoJSONSource, type LayerSpecification, type StyleSpecification } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { DARK, layers as basemapLayers, type Flavor } from "@protomaps/basemaps";
import { feature } from "topojson-client";
import type { GeometryCollection, Topology } from "topojson-specification";
import countriesTopo from "world-atlas/countries-110m.json";
import type { Desk, GlobeData, Selection, StoryDetail } from "../lib/types";
import { deskColor } from "../lib/format";
import { disruptionCard, planeCard, siteCard, vesselCard } from "./shippingCards";

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
  /** Vector tiles instead of data (layers then name their source-layer). */
  tiles?: string[];
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
  /** The area in view, after each move: [west, south, east, north] and the zoom. */
  onView?: (bbox: [number, number, number, number], zoom: number) => void;
}

/** Small icons drawn as signed distance fields, so each layer can colour them. */
function shipIcon(): ImageData {
  const size = 24;
  const c = document.createElement("canvas");
  c.width = c.height = size;
  const g = c.getContext("2d")!;
  g.translate(size / 2, size / 2);
  g.beginPath();
  g.moveTo(0, -10); g.lineTo(6, 8); g.lineTo(0, 4); g.lineTo(-6, 8);
  g.closePath();
  g.fillStyle = "#ffffff";
  g.fill();
  return g.getImageData(0, 0, size, size);
}

function planeIcon(): ImageData {
  const size = 28;
  const c = document.createElement("canvas");
  c.width = c.height = size;
  const g = c.getContext("2d")!;
  g.translate(size / 2, size / 2);
  g.fillStyle = "#ffffff";
  g.beginPath();
  g.moveTo(0, -12); g.lineTo(2, -4); g.lineTo(11, 2); g.lineTo(11, 4); g.lineTo(2, 1); g.lineTo(1.5, 8); g.lineTo(4, 10);
  g.lineTo(4, 12); g.lineTo(0, 11); g.lineTo(-4, 12); g.lineTo(-4, 10); g.lineTo(-1.5, 8); g.lineTo(-2, 1); g.lineTo(-11, 4);
  g.lineTo(-11, 2); g.lineTo(-2, -4);
  g.closePath();
  g.fill();
  return g.getImageData(0, 0, size, size);
}

const SHIPPING = ["vessel", "plane", "disruption", "site-port", "site-chokepoint", "site-airport", "site-border"];

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

const WEAPON: Record<string, string> = { missile: "missile", drone: "drone", glide_bomb: "glide bomb", airstrike: "air strike",
  artillery: "artillery", rocket: "rocket", unknown: "strike" };
const OUTCOME: Record<string, string> = { hit: "hit", intercepted: "intercepted", claimed: "claimed, not shown", explosions_heard: "explosions heard" };

/** What a strike on the map was, who reported it and where to read it. Built from DOM nodes,
 *  never HTML, because the text comes from Telegram posts. */
function strikeCard(raw: Record<string, unknown>): HTMLElement {
  // The map flattens lists and objects in feature properties to JSON strings.
  const p = Object.fromEntries(Object.entries(raw).map(([k, v]) => {
    if (typeof v === "string" && (v.startsWith("[") || v.startsWith("{"))) { try { return [k, JSON.parse(v)]; } catch { /* plain text */ } }
    return [k, v];
  })) as Record<string, any>;
  const el = (tag: string, cls: string, text?: string) => { const e = document.createElement(tag); e.className = cls; if (text) e.textContent = text; return e; };
  const root = el("div", "strike-card");
  const when = new Date(p.first).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  const province = p.admin1 && !String(p.admin1).startsWith(String(p.label).slice(0, 5)) ? `, ${p.admin1}` : "";
  root.append(el("div", "sc-title", `${p.label}${province}`));
  if (p.rough) root.append(el("div", "sc-warn", "Only the province is given; shown on its main city"));
  const photos = (p.photos ?? []) as string[];
  if (photos.length) {
    const row = el("div", "sc-photos");
    for (const src of photos) {
      const a = document.createElement("a");
      a.href = src; a.target = "_blank"; a.rel = "noreferrer noopener"; a.title = "Open the photo";
      const img = document.createElement("img");
      img.src = src; img.loading = "lazy"; img.alt = "Photo from the post";
      a.append(img);
      row.append(a);
    }
    root.append(row);
  }
  root.append(el("div", "sc-meta", [when, WEAPON[p.weapon] ?? p.weapon, OUTCOME[p.outcome] ?? p.outcome, p.attacker ? `by ${p.attacker}` : ""].filter(Boolean).join(" · ")));
  const toll = [p.target, p.killed ? `${p.killed} killed` : "", p.injured ? `${p.injured} injured` : ""].filter(Boolean).join(" · ");
  if (toll) root.append(el("div", "sc-meta", toll));
  const sides = (p.sides ?? []).length > 1 ? `, both sides (${p.sides.join(", ")})` : "";
  root.append(el("div", p.corroborated ? "sc-ok" : "sc-warn",
    `${p.channels} channel${p.channels === 1 ? "" : "s"}${sides}${p.news ? `, ${p.news} news outlets` : ""}${p.corroborated ? "" : ": unconfirmed"}`));
  for (const s of (p.sources ?? []) as { handle: string; url: string; quote: string }[]) {
    const row = el("div", "sc-source");
    const a = document.createElement("a");
    a.href = s.url; a.target = "_blank"; a.rel = "noreferrer noopener"; a.textContent = `@${s.handle}`;
    row.append(a, el("span", "sc-quote", ` ${s.quote ?? ""}`));
    root.append(row);
  }
  return root;
}

interface Town { name: string; admin1: string; km: number }
interface ArrowInfo {
  observed_at: string; heading: string | null;
  near: Town | null; towards: Town | null;
  persistence: { since: string | null; run_days: number; at_least: boolean; maps_last_year: number; marked_last_year: number };
  ground: Record<"week" | "month", { since: string; taken_km2: number; retaken_km2: number } | null>;
  strikes: { count: number; latest: { observed_at: string; label: string; weapon: string; outcome: string; source_url: string }[] };
  stories: { id: number; title: string; last_seen: string; item_count: number }[];
}

interface PlaceInfo {
  label: string; category: "place" | "held" | "zone"; side: string | null; sides: string[] | null; kind: string | null; link: string | null;
  map_date: string; source_url: string | null; since: string | null; at_least: boolean; before: string | null;
  km2?: number; name?: string | null;
  strikes: ArrowInfo["strikes"]; stories: ArrowInfo["stories"];
}

/** A place or an area on a control map (who holds it, since when) or an official zone. */
function placeCard(i: PlaceInfo, hex: string | undefined, openStory: (id: number) => void): HTMLElement {
  const el = (tag: string, cls: string, text?: string) => { const e = document.createElement(tag); e.className = cls; if (text) e.textContent = text; return e; };
  const date = (s: string) => new Date(s).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  const root = el("div", "strike-card");
  const kinds: Record<string, string> = { settlement: "", base: "military base", airport: "airport or air base", port: "port", hill: "strategic hill", rural: "rural presence", siege: "besieged", contested: "contested", mixed: "shared control" };
  if (i.category === "zone") {
    root.append(el("div", "sc-title", i.name ?? i.label));
    root.append(el("div", "sc-meta", `${i.km2?.toLocaleString() ?? "?"} km² · as of ${date(i.map_date)}`));
  } else if (i.category === "held") {
    root.append(el("div", "sc-title", i.side ?? i.label));
    root.append(el("div", "sc-meta", `Area around the places it holds, about ${i.km2?.toLocaleString() ?? "?"} km² · map of ${date(i.map_date)}`));
  } else {
    const t = el("div", "sc-title");
    if (hex) { const sw = el("span", "sc-swatch"); sw.style.background = hex; t.append(sw); }
    t.append(document.createTextNode(i.label || "Unnamed place"));
    root.append(t);
    if (i.kind && kinds[i.kind]) root.append(el("div", "sc-meta", kinds[i.kind]));
    root.append(el("div", "sc-meta", i.side ? `Held by ${i.side}` : `Contested: ${(i.sides ?? []).join(" and ")}`));
    if (i.since) root.append(el("div", "sc-meta", `Since ${i.at_least ? "at least " : ""}${date(i.since)}${i.before ? `; before that: ${i.before}` : ""}`));
  }
  if (i.source_url) {
    const a = document.createElement("a");
    a.href = i.source_url; a.target = "_blank"; a.rel = "noreferrer noopener";
    a.textContent = i.category === "zone" ? "Source data" : "Per Wikipedia, this version of the map";
    root.append(a);
  }
  if (i.link && i.category === "place") {
    const a = document.createElement("a");
    a.href = `https://en.wikipedia.org/wiki/${encodeURIComponent(i.link.replace(/ /g, "_"))}`; a.target = "_blank"; a.rel = "noreferrer noopener";
    a.textContent = ` · about ${i.label}`;
    root.append(a);
  }
  if (i.category === "place") {
    root.append(el("div", "sc-meta", i.strikes.count ? `${i.strikes.count} strike${i.strikes.count === 1 ? "" : "s"} reported within 25 km in the last 14 days` : "No strikes reported within 25 km in the last 14 days"));
    if (i.stories.length) {
      root.append(el("div", "sc-meta", "News placed nearby, last 7 days:"));
      for (const s of i.stories) {
        const b = el("button", "sc-story", s.title) as HTMLButtonElement;
        b.onclick = () => openStory(s.id);
        root.append(b);
      }
    }
  }
  return root;
}

/** A DeepStateMap attack arrow, explained (tracks/arrows.py). DOM nodes only, no HTML. */
function arrowCard(i: ArrowInfo, openStory: (id: number) => void): HTMLElement {
  const el = (tag: string, cls: string, text?: string) => { const e = document.createElement(tag); e.className = cls; if (text) e.textContent = text; return e; };
  const date = (s: string) => new Date(s).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  const place = (t: Town) => `${t.name}${t.admin1 ? `, ${t.admin1}` : ""}`;
  const root = el("div", "strike-card");
  root.append(el("div", "sc-title", i.towards ? `Push towards ${place(i.towards)}` : "Direction of attack"));
  root.append(el("div", "sc-meta", [`DeepStateMap, ${date(i.observed_at)}`, i.heading ? `heading ${i.heading}` : "",
    i.near ? `near ${i.near.name} (${i.near.km} km)` : "", i.towards ? `${i.towards.name} is ${i.towards.km} km ahead` : ""].filter(Boolean).join(" · ")));
  const p = i.persistence;
  if (p.since) {
    root.append(el("div", "sc-meta", `Marked here ${p.at_least ? "at least " : ""}since ${date(p.since)} (${p.run_days} days)` +
      (p.maps_last_year ? `; on ${p.marked_last_year} of ${p.maps_last_year} maps in the last year` : "")));
  }
  for (const [label, g] of [["7 days", i.ground.week], ["30 days", i.ground.month]] as const) {
    if (!g) continue;
    const moved = g.taken_km2 > 0.05 || g.retaken_km2 > 0.05;
    root.append(el("div", moved ? "sc-warn" : "sc-meta",
      `Within 15 km, last ${label}: ${moved ? [g.taken_km2 > 0.05 ? `${g.taken_km2.toFixed(1)} km² taken` : "", g.retaken_km2 > 0.05 ? `${g.retaken_km2.toFixed(1)} km² retaken` : ""].filter(Boolean).join(", ") : "no ground changed hands"}`));
  }
  root.append(el("div", "sc-meta", i.strikes.count ? `${i.strikes.count} strike${i.strikes.count === 1 ? "" : "s"} reported within 25 km in the last 14 days:` : "No strikes reported within 25 km in the last 14 days."));
  for (const s of i.strikes.latest) {
    const row = el("div", "sc-source");
    const a = document.createElement("a");
    a.href = s.source_url; a.target = "_blank"; a.rel = "noreferrer noopener"; a.textContent = s.label;
    row.append(a, el("span", "sc-quote", ` ${date(s.observed_at)}, ${s.weapon}, ${String(s.outcome).replace("_", " ")}`));
    root.append(row);
  }
  if (i.stories.length) {
    root.append(el("div", "sc-meta", "News placed nearby, last 7 days:"));
    for (const s of i.stories) {
      const b = el("button", "sc-story", s.title) as HTMLButtonElement;
      b.title = `${s.item_count} articles; open the story`;
      b.onclick = () => openStory(s.id);
      root.append(b);
    }
  }
  return root;
}

export default function MapView({ data, desks, selection, storyDetail, visible, overlays, onSelectStory, onMapClick, focus, onView }: Props) {
  const box = useRef<HTMLDivElement>(null);
  const map = useRef<maplibregl.Map | null>(null);
  const [info, setInfo] = useState<MapInfo | null | undefined>(undefined);
  const [ready, setReady] = useState(false);
  const selectedId = selection?.type === "story" ? selection.id : null;
  const latest = useRef({ onSelectStory, onMapClick, onView });
  latest.current = { onSelectStory, onMapClick, onView };

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
      m.addImage("ship", shipIcon(), { pixelRatio: 2, sdf: true });
      m.addImage("plane", planeIcon(), { pixelRatio: 2, sdf: true });
      // The trail of the ship last clicked.
      m.addSource("ship-path", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      m.addLayer({ id: "ship-path", type: "line", source: "ship-path", layout: { "line-join": "round", "line-cap": "round" },
                   paint: { "line-color": "#ffffff", "line-width": 1.6, "line-dasharray": [2, 1.5], "line-opacity": 0.8 } });
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
    // Arrows and strikes can be clicked too: show it.
    m.on("mousemove", (e) => {
      if (popup.isOpen()) return;
      const hit = m.queryRenderedFeatures(e.point).some((f) => ["strike", "place", ...SHIPPING].includes(f.properties?.category) || (f.properties?.category === "attack" && f.properties?.id));
      m.getCanvas().style.cursor = hit ? "pointer" : "";
    });
    m.on("click", (e) => {
      const hit = m.queryRenderedFeatures(e.point, { layers: ["story-dot"] })[0];
      if (hit) {
        latest.current.onSelectStory(Number(hit.properties.id));
        return;
      }
      const all = m.queryRenderedFeatures(e.point);
      const ship = all.find((f) => SHIPPING.includes(f.properties?.category));
      if (ship) {
        const p = ship.properties, cat = String(p.category);
        const at = (ship.geometry as GeoJSON.Point).coordinates as [number, number];
        const pop = new maplibregl.Popup({ className: "map-popup strike-popup", offset: 8, maxWidth: "340px" }).setLngLat(at);
        const open = (id: number) => latest.current.onSelectStory(id);
        const path = (coords: [number, number][]) => (m.getSource("ship-path") as GeoJSONSource | undefined)?.setData(
          { type: "FeatureCollection", features: coords.length > 1 ? [{ type: "Feature", geometry: { type: "LineString", coordinates: coords }, properties: {} }] : [] });
        if (cat === "plane") pop.setDOMContent(planeCard(p)).addTo(m);
        else if (cat === "disruption") pop.setDOMContent(disruptionCard(p, open)).addTo(m);
        else if (cat === "vessel") {
          pop.setText("Looking up this ship…").addTo(m);
          fetch(`/api/shipping/vessels/${p.mmsi}`).then((r) => r.json())
            .then((v) => { pop.setDOMContent(vesselCard(v)); path(v.track ?? []); })
            .catch(() => pop.setText("No recent details for this ship."));
          pop.on("close", () => path([]));
        } else {
          pop.setText("Looking it up…").addTo(m);
          fetch(`/api/shipping/sites/${p.id}`).then((r) => r.json())
            .then((d) => pop.setDOMContent(siteCard(d, open)))
            .catch(() => pop.setText("Could not load details."));
        }
        return;
      }
      const arrow = all.find((f) => f.properties?.category === "attack" && f.properties?.id);
      if (arrow) {
        const at = (arrow.geometry as GeoJSON.Point).coordinates as [number, number];
        const pop = new maplibregl.Popup({ className: "map-popup strike-popup", offset: 8, maxWidth: "340px" })
          .setLngLat(at).setText("Looking at this push…").addTo(m);
        fetch(`/api/tracks/${arrow.properties.track_id}/attacks/${arrow.properties.id}`).then((r) => r.json())
          .then((info) => pop.setDOMContent(arrowCard(info, (id) => latest.current.onSelectStory(id))))
          .catch(() => pop.setText("Could not load details for this arrow."));
        return;
      }
      const held = all.find((f) => f.properties?.category === "place")
        ?? (all.some((f) => f.properties?.category === "strike") ? undefined
          : all.find((f) => f.properties?.category === "held" || f.properties?.category === "zone"));
      if (held) {
        const pop = new maplibregl.Popup({ className: "map-popup strike-popup", offset: 8, maxWidth: "340px" })
          .setLngLat(held.properties.category === "place" ? (held.geometry as GeoJSON.Point).coordinates as [number, number] : e.lngLat)
          .setText("Looking it up…").addTo(m);
        fetch(`/api/tracks/${held.properties.track_id}/places/${held.properties.id}`).then((r) => r.json())
          .then((info) => pop.setDOMContent(placeCard(info, held.properties.hex, (id) => latest.current.onSelectStory(id))))
          .catch(() => pop.setText("Could not load details."));
        return;
      }
      const strike = all.find((f) => f.properties?.category === "strike");
      if (strike) {
        new maplibregl.Popup({ className: "map-popup strike-popup", offset: 8, maxWidth: "320px" })
          .setLngLat((strike.geometry as GeoJSON.Point).coordinates as [number, number])
          .setDOMContent(strikeCard(strike.properties)).addTo(m);
        return;
      }
      latest.current.onMapClick?.([e.lngLat.lng, e.lngLat.lat], all);
    });
    const report = () => {
      const b = m.getBounds();
      latest.current.onView?.([b.getWest(), b.getSouth(), b.getEast(), b.getNorth()], m.getZoom());
    };
    m.on("moveend", report);
    m.on("load", report);
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
      const src = m.getSource(o.id);
      if (src) {
        if (!o.tiles) (src as GeoJSONSource).setData(o.data);
        continue;
      }
      m.addSource(o.id, o.tiles ? { type: "vector", tiles: o.tiles, maxzoom: 14 } : { type: "geojson", data: o.data });
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
