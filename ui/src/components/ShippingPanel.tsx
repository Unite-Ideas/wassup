import { useEffect, useMemo, useRef, useState } from "react";
import type { Overlay, OverlayLayer } from "./MapView";

interface Props {
  view: { bbox: [number, number, number, number]; zoom: number } | null;
  onOverlays: (o: Overlay[]) => void;
  onFocus: (lng: number, lat: number, zoom?: number) => void;
}

type FC = GeoJSON.FeatureCollection;
type L = OverlayLayer;
type Filter = Extract<L, { type: "circle" }>["filter"];
const f = (x: unknown) => x as Filter;
const EMPTY: FC = { type: "FeatureCollection", features: [] };

interface Status {
  ais: { key: boolean; ships: number; connected?: boolean; error?: string | null; at?: string };
  planes: { count: number; account: boolean; ok?: boolean; error?: string; at?: string };
  sites: Record<string, number>;
  rail: boolean;
  events: number;
  kinds: Record<string, string>;
}

const LAYERS = [
  { key: "ships", name: "Live ships", what: "Cargo ships and tankers, from AISStream" },
  { key: "planes", name: "Cargo planes", what: "In the air now, from OpenSky" },
  { key: "events", name: "Disruptions", what: "From the news and PortWatch alerts, last 14 days" },
  { key: "chokepoints", name: "Straits and canals", what: "Ships through each per week, from PortWatch" },
  { key: "ports", name: "Ports", what: "Port calls this week against normal, from PortWatch" },
  { key: "borders", name: "US border crossings", what: "Truck wait times, from CBP" },
  { key: "airports", name: "Airports", what: "With scheduled service, from OurAirports" },
  { key: "rail", name: "Railways", what: "Natural Earth" },
] as const;
type Key = (typeof LAYERS)[number]["key"];
const DEFAULT_ON: Key[] = ["ships", "planes", "events", "chokepoints", "ports"];

export const SHIP_COLORS = { cargo: "#7fd1ff", tanker: "#ff9f43", plane: "#ffe08a", port: "#2ec4d6", chokepoint: "#FF8C42",
  airport: "#c77dff", rail: "#7d8ea3", down: "#ff5a5f", up: "#3ddc97" };
const EVENT_COLOR = ["match", ["get", "kind"],
  ["attack_on_ship", "seizure"], "#ff3b5c",
  ["port_closure", "chokepoint", "border"], "#ff8c1a",
  ["labour_strike"], "#ffb020",
  ["tariff", "sanctions"], "#c77dff",
  ["weather", "hazard"], "#22d3ee",
  "#f5d90a"];

function siteLayers(id: string): L[] {
  const kind = (k: string) => ["==", ["get", "kind"], k];
  const change = ["case", ["all", ["get", "unusual"], ["<", ["coalesce", ["get", "change_pct"], 0], 0]], SHIP_COLORS.down,
                  ["all", ["get", "unusual"], [">", ["coalesce", ["get", "change_pct"], 0], 0]], SHIP_COLORS.up];
  const label = { "text-font": ["Noto Sans Regular"], "text-size": 10.5, "text-offset": [0, 1.1], "text-anchor": "top", "text-optional": true } as const;
  const paintLabel = { "text-color": "#d7e3ef", "text-halo-color": "#05080d", "text-halo-width": 1.3 };
  return [
    { id: `${id}-airport`, type: "circle", filter: f(["all", kind("airport"), [">=", ["get", "rank"], 2]]), minzoom: 2.5,
      paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 3, 2, 8, 4.5], "circle-color": SHIP_COLORS.airport, "circle-opacity": 0.8 } as never },
    { id: `${id}-airport-small`, type: "circle", filter: f(["all", kind("airport"), ["<", ["get", "rank"], 2]]), minzoom: 5,
      paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 5, 1.8, 9, 3.5], "circle-color": SHIP_COLORS.airport, "circle-opacity": 0.65 } as never },
    { id: `${id}-port-small`, type: "circle", filter: f(["all", kind("port"), ["<", ["get", "rank"], 3.3]]), minzoom: 4,
      paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 4, 2, 9, 5], "circle-color": [...change, SHIP_COLORS.port],
               "circle-stroke-color": "#05080d", "circle-stroke-width": 0.6 } as never },
    { id: `${id}-port`, type: "circle", filter: f(["all", kind("port"), [">=", ["get", "rank"], 3.3]]),
      paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 1, ["-", ["get", "rank"], 1.6], 8, ["*", 2, ["get", "rank"]]],
               "circle-color": [...change, SHIP_COLORS.port], "circle-stroke-color": "#05080d", "circle-stroke-width": 0.8 } as never },
    { id: `${id}-border`, type: "circle", filter: f(kind("border")), minzoom: 2.5,
      paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 3, 3, 9, 7],
               "circle-color": ["case", ["get", "closed"], "#6b6b6b", ["<", ["get", "delay"], 0], "#8fa3b8", [">=", ["get", "delay"], 60], SHIP_COLORS.down,
                                [">=", ["get", "delay"], 30], "#ffb020", SHIP_COLORS.up],
               "circle-stroke-color": "#ffffff", "circle-stroke-width": 1 } as never },
    { id: `${id}-chokepoint`, type: "circle", filter: f(kind("chokepoint")),
      paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 1, 5, 8, 11], "circle-color": [...change, SHIP_COLORS.chokepoint],
               "circle-opacity": 0.9, "circle-stroke-color": "#ffffff", "circle-stroke-width": 1.5 } as never },
    { id: `${id}-chokepoint-label`, type: "symbol", filter: f(kind("chokepoint")), minzoom: 2.5,
      layout: { ...label, "text-field": ["get", "name"] } as never, paint: paintLabel },
    { id: `${id}-port-label`, type: "symbol", filter: f(["all", kind("port"), [">=", ["get", "rank"], 3.6]]), minzoom: 5,
      layout: { ...label, "text-field": ["get", "name"] } as never, paint: paintLabel },
    { id: `${id}-border-label`, type: "symbol", filter: f(kind("border")), minzoom: 7,
      layout: { ...label, "text-field": ["get", "name"] } as never, paint: paintLabel },
  ];
}

function shipLayers(id: string): L[] {
  const color = ["match", ["get", "kind"], "tanker", SHIP_COLORS.tanker, SHIP_COLORS.cargo];
  return [
    { id: `${id}-dot`, type: "circle", filter: f(["!", ["get", "moving"]]),
      paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 1, 1, 8, 3], "circle-color": color, "circle-opacity": 0.75 } as never },
    { id: `${id}-arrow`, type: "symbol", filter: f(["get", "moving"]),
      layout: { "icon-image": "ship", "icon-rotate": ["get", "dir"], "icon-rotation-alignment": "map", "icon-allow-overlap": true,
                "icon-ignore-placement": true, "icon-size": ["interpolate", ["linear"], ["zoom"], 1, 0.35, 6, 0.6, 10, 0.9] } as never,
      paint: { "icon-color": color, "icon-opacity": 0.95 } as never },
    { id: `${id}-label`, type: "symbol", minzoom: 8.5,
      layout: { "text-field": ["get", "name"], "text-font": ["Noto Sans Regular"], "text-size": 10, "text-offset": [0, 1.1],
                "text-anchor": "top", "text-optional": true } as never,
      paint: { "text-color": "#bfe6ff", "text-halo-color": "#05080d", "text-halo-width": 1.2 } },
  ];
}

function planeLayers(id: string): L[] {
  return [
    { id: `${id}-icon`, type: "symbol",
      layout: { "icon-image": "plane", "icon-rotate": ["get", "dir"], "icon-rotation-alignment": "map", "icon-allow-overlap": true,
                "icon-ignore-placement": true, "icon-size": ["interpolate", ["linear"], ["zoom"], 1, 0.45, 6, 0.75, 10, 1] } as never,
      paint: { "icon-color": SHIP_COLORS.plane } as never },
    { id: `${id}-label`, type: "symbol", minzoom: 5,
      layout: { "text-field": ["get", "callsign"], "text-font": ["Noto Sans Regular"], "text-size": 10, "text-offset": [0, 1.3],
                "text-anchor": "top", "text-optional": true } as never,
      paint: { "text-color": SHIP_COLORS.plane, "text-halo-color": "#05080d", "text-halo-width": 1.2 } },
  ];
}

function eventLayers(id: string): L[] {
  return [
    { id: `${id}-halo`, type: "circle", filter: f(["==", ["get", "status"], "ongoing"]),
      paint: { "circle-radius": ["+", 7, ["*", 4, ["get", "severity"]]], "circle-color": "rgba(0,0,0,0)",
               "circle-stroke-color": EVENT_COLOR, "circle-stroke-width": 1.2, "circle-stroke-opacity": 0.7 } as never },
    { id: `${id}-dot`, type: "circle",
      paint: { "circle-radius": ["+", 3, ["*", 2.5, ["get", "severity"]]], "circle-color": EVENT_COLOR,
               "circle-opacity": ["match", ["get", "status"], "ended", 0.35, "threatened", 0.55, 0.95],
               "circle-stroke-color": "#05080d", "circle-stroke-width": 1 } as never },
    { id: `${id}-label`, type: "symbol", minzoom: 4,
      layout: { "text-field": ["get", "kind_label"], "text-font": ["Noto Sans Regular"], "text-size": 10.5, "text-offset": [0, 1.3],
                "text-anchor": "top", "text-optional": true } as never,
      paint: { "text-color": "#ffe3c4", "text-halo-color": "#05080d", "text-halo-width": 1.3 } },
  ];
}

const RAIL_LAYERS: L[] = [
  { id: "ship-rail-line", type: "line", "source-layer": "rail", minzoom: 2,
    paint: { "line-color": SHIP_COLORS.rail, "line-width": ["interpolate", ["linear"], ["zoom"], 2, 0.4, 8, 1.4], "line-opacity": 0.7 } as never } as L,
];

const ago = (s?: string) => {
  if (!s) return "";
  const m = Math.round((Date.now() - Date.parse(s)) / 60000);
  return m < 1 ? "just now" : m < 60 ? `${m} min ago` : `${Math.round(m / 60)} h ago`;
};

async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status}`);
  return r.json() as Promise<T>;
}

export default function ShippingPanel({ view, onOverlays, onFocus }: Props) {
  const [open, setOpen] = useState(true);
  const [on, setOn] = useState<Set<Key>>(() => {
    try { const v = JSON.parse(localStorage.getItem("wassup.shipping.on") ?? "null"); if (Array.isArray(v)) return new Set(v as Key[]); } catch { /* private window */ }
    return new Set(DEFAULT_ON);
  });
  const [status, setStatus] = useState<Status | null>(null);
  const [sites, setSites] = useState<Partial<Record<string, FC>>>({});
  const [ships, setShips] = useState<FC>(EMPTY);
  const [planes, setPlanes] = useState<FC>(EMPTY);
  const [events, setEvents] = useState<FC>(EMPTY);
  const [tick, setTick] = useState(0);

  const toggle = (k: Key) => setOn((o) => {
    const n = new Set(o);
    if (n.has(k)) n.delete(k); else n.add(k);
    try { localStorage.setItem("wassup.shipping.on", JSON.stringify([...n])); } catch { /* private window */ }
    return n;
  });

  // Clock: live layers every 30 seconds, the rest as they change.
  useEffect(() => { const t = setInterval(() => setTick((x) => x + 1), 30_000); return () => clearInterval(t); }, []);
  useEffect(() => { getJSON<Status>("/api/shipping/status").then(setStatus).catch(() => undefined); }, [tick]);

  const siteKinds: Record<string, string> = { chokepoints: "chokepoint", ports: "port", borders: "border", airports: "airport" };
  useEffect(() => {
    for (const [k, kind] of Object.entries(siteKinds)) {
      // Fixed places load once; border waits every few minutes.
      if (!on.has(k as Key) || (sites[kind] && !(kind === "border" && tick % 10 === 0))) continue;
      getJSON<FC>(`/api/shipping/sites?kinds=${kind}`).then((fc) => setSites((s) => ({ ...s, [kind]: fc }))).catch(() => undefined);
    }
  }, [on, tick]); // eslint-disable-line react-hooks/exhaustive-deps

  // Ships in view (with a margin), when the map moves and every 30 seconds.
  const seq = useRef(0);
  useEffect(() => {
    if (!on.has("ships") || !view || !status?.ais.key) return;
    const n = ++seq.current;
    const [w, s, e, nn] = view.bbox;
    const padX = (e - w) * 0.2, padY = (nn - s) * 0.2;
    const bbox = e - w >= 300 ? "-180,-90,180,90" : [w - padX, Math.max(-90, s - padY), e + padX, Math.min(90, nn + padY)].map((x) => x.toFixed(3)).join(",");
    const limit = view.zoom < 3 ? 8000 : view.zoom < 5 ? 12000 : 20000;
    const t = setTimeout(() => getJSON<FC>(`/api/shipping/vessels?bbox=${bbox}&limit=${limit}`)
      .then((fc) => { if (n === seq.current) setShips(fc); }).catch(() => undefined), 250);
    return () => clearTimeout(t);
  }, [on, view, tick, status?.ais.key]);

  useEffect(() => { if (on.has("planes")) getJSON<FC>("/api/shipping/planes").then(setPlanes).catch(() => undefined); }, [on, tick]);
  useEffect(() => { if (on.has("events") && tick % 4 === 0) getJSON<FC>("/api/shipping/events").then(setEvents).catch(() => undefined); }, [on, tick]);
  useEffect(() => { if (on.has("events")) getJSON<FC>("/api/shipping/events").then(setEvents).catch(() => undefined); }, [on]);

  const siteData = useMemo<FC>(() => ({
    type: "FeatureCollection",
    features: Object.entries(siteKinds).filter(([k]) => on.has(k as Key)).flatMap(([, kind]) => sites[kind]?.features ?? []),
  }), [on, sites]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const o: Overlay[] = [];
    if (on.has("rail")) o.push({ id: "ship-rail", data: EMPTY, tiles: [`${location.origin}/api/shipping/tiles/rail/{z}/{x}/{y}.mvt`], layers: RAIL_LAYERS });
    if (siteData.features.length) o.push({ id: "ship-sites", data: siteData, layers: siteLayers("ship-sites") });
    if (on.has("ships")) o.push({ id: "ship-vessels", data: ships, layers: shipLayers("ship-vessels") });
    if (on.has("events")) o.push({ id: "ship-events", data: events, layers: eventLayers("ship-events") });
    if (on.has("planes")) o.push({ id: "ship-planes", data: planes, layers: planeLayers("ship-planes") });
    onOverlays(o);
  }, [on, siteData, ships, planes, events, onOverlays]);

  const chokepoints = (sites.chokepoint?.features ?? [])
    .filter((x) => x.properties?.change_pct != null)
    .sort((a, b) => Math.abs(b.properties!.change_pct) - Math.abs(a.properties!.change_pct)).slice(0, 8);
  const odd = (sites.port?.features ?? []).filter((x) => x.properties?.unusual).sort((a, b) => b.properties!.rank - a.properties!.rank).slice(0, 8);
  const waits = (sites.border?.features ?? []).filter((x) => (x.properties?.delay ?? -1) >= 0).sort((a, b) => b.properties!.delay - a.properties!.delay).slice(0, 5);
  const ev = events.features.slice(0, 10);
  const at = (x: GeoJSON.Feature) => (x.geometry as GeoJSON.Point).coordinates as [number, number];

  return (
    <div className={`tracks-panel shipping-panel ${open ? "" : "closed"}`}>
      <div className="tp-head" onClick={() => setOpen(!open)}>
        <span>SHIPPING AND TRADE</span><span className="dimmer">{open ? "▾" : "▸"}</span>
      </div>
      {open && LAYERS.map((l) => (
        <div key={l.key} className="tp-track">
          <label className="toggle-row">
            <span><b>{l.name}</b><br /><span className="dimmer mono" style={{ fontSize: 10 }}>{l.what}</span></span>
            <input type="checkbox" checked={on.has(l.key)} onChange={() => toggle(l.key)} />
          </label>
          {on.has(l.key) && l.key === "ships" && status && (
            <div className="tp-stats mono">
              {!status.ais.key ? (
                <div className="dimmer">No AISStream key yet. Add AISSTREAM_API_KEY to the .env file and restart (docs/SHIPPING.md).</div>
              ) : status.ais.error && status.ais.connected === false ? (
                <div className="sc-warn">Not connected: {status.ais.error}</div>
              ) : (
                <>
                  <div><i style={{ background: SHIP_COLORS.cargo }} />cargo <i style={{ background: SHIP_COLORS.tanker, marginLeft: 8 }} />tanker</div>
                  <div>{status.ais.ships.toLocaleString()} heard from in 6 hours; {ships.features.length.toLocaleString()} shown</div>
                  {view && view.zoom < 3 && ships.features.length >= 8000 && <div className="dimmer">Zoom in to see every ship</div>}
                </>
              )}
            </div>
          )}
          {on.has(l.key) && l.key === "planes" && status && (
            <div className="tp-stats mono">
              {status.planes.ok === false ? <div className="sc-warn">{status.planes.error}</div>
                : <div><i style={{ background: SHIP_COLORS.plane }} />{planes.features.length} in the air · {ago(status.planes.at)}</div>}
              {!status.planes.account && <div className="dimmer">Every 15 minutes; an OpenSky account makes it every 2.</div>}
            </div>
          )}
          {on.has(l.key) && l.key === "events" && (
            <div className="tp-stats mono">
              <div>
                <i style={{ background: "#ff3b5c" }} />attack <i style={{ background: "#ff8c1a", marginLeft: 6 }} />closure
                <i style={{ background: "#ffb020", marginLeft: 6 }} />strike <i style={{ background: "#c77dff", marginLeft: 6 }} />trade rule
                <i style={{ background: "#22d3ee", marginLeft: 6 }} />hazard
              </div>
              {ev.length ? (
                <div className="tp-reports">
                  {ev.map((x) => {
                    const p = x.properties as Record<string, any>;
                    return (
                      <div key={p.id} className="tp-report">
                        <div className="tp-report-head">
                          <button className="linkish" onClick={() => onFocus(...at(x), 5)} title="Show on the map"><b>{p.kind_label}</b></button>
                          <span className="dimmer"> · {p.place}{p.status !== "ongoing" ? ` · ${p.status}` : ""}</span>
                        </div>
                        <div className="tp-quote" style={{ fontStyle: "normal" }}>{p.summary || p.title}</div>
                      </div>
                    );
                  })}
                </div>
              ) : <div className="dimmer">None in the last 14 days yet. Shipping stories are read as they come in.</div>}
            </div>
          )}
          {on.has(l.key) && l.key === "chokepoints" && chokepoints.length > 0 && (
            <div className="tp-stats mono">
              <div className="dimmer">This week against normal:</div>
              {chokepoints.map((x) => {
                const p = x.properties as Record<string, any>;
                return (
                  <div key={p.id} className="tp-side">
                    <button className="linkish" onClick={() => onFocus(...at(x), 6)}>{p.name}</button>
                    <span style={{ color: p.unusual ? (p.change_pct < 0 ? SHIP_COLORS.down : SHIP_COLORS.up) : undefined }}>
                      {" "}{p.change_pct > 0 ? "+" : ""}{p.change_pct}%</span>
                  </div>
                );
              })}
            </div>
          )}
          {on.has(l.key) && l.key === "ports" && (
            <div className="tp-stats mono">
              <div><i style={{ background: SHIP_COLORS.down }} />far fewer calls <i style={{ background: SHIP_COLORS.up, marginLeft: 6 }} />far more</div>
              {odd.map((x) => {
                const p = x.properties as Record<string, any>;
                return (
                  <div key={p.id} className="tp-side">
                    <button className="linkish" onClick={() => onFocus(...at(x), 7)}>{p.name}</button>
                    <span style={{ color: p.change_pct < 0 ? SHIP_COLORS.down : SHIP_COLORS.up }}> {p.change_pct > 0 ? "+" : ""}{p.change_pct}%</span>
                  </div>
                );
              })}
            </div>
          )}
          {on.has(l.key) && l.key === "borders" && (
            <div className="tp-stats mono">
              <div><i style={{ background: SHIP_COLORS.up }} />under 30 min <i style={{ background: "#ffb020", marginLeft: 6 }} />30+ <i style={{ background: SHIP_COLORS.down, marginLeft: 6 }} />60+</div>
              {waits.map((x) => {
                const p = x.properties as Record<string, any>;
                return (
                  <div key={p.id} className="tp-side">
                    <button className="linkish" onClick={() => onFocus(...at(x), 8)}>{p.name}</button> <span>{p.delay} min</span>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
