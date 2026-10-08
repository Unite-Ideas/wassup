import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../lib/api";
import type { MovementSummary, StrikeSummary, Track, TrackState } from "../lib/types";
import type { Overlay, OverlayLayer } from "./MapView";

interface Props {
  /** The end of the main timeline window. Tracks follow it unless you pick your own date. */
  timelineEnd: Date;
  onOverlays: (o: Overlay[]) => void;
  onFocus: (lng: number, lat: number) => void;
}

const DAY = 86400e3;
const COMPARE = [1, 7, 30];
const STRIKE = { hit: "#ff5a1f", claimed: "#ffb020", explosions_heard: "#ffe08a", intercepted: "#8fa3b8" };
const WEAPON: Record<string, string> = { missile: "missile", drone: "drone", glide_bomb: "glide bomb", airstrike: "air strike",
  artillery: "artillery", rocket: "rocket", unknown: "other" };
const C = { occupied: "#ff3b5c", contested: "#a3adb8", liberated: "#4fa3ff", gained: "#ff1f3d", lost: "#3d8bff", attack: "#ffb020", path: "#00e5ff" };

type L = OverlayLayer;
type Filter = Extract<L, { type: "fill" }>["filter"];
const cat = (c: string) => ["==", ["get", "category"], c] as unknown as Filter;

function frontLayers(id: string): L[] {
  return [
    { id: `${id}-liberated`, type: "fill", filter: cat("liberated"), paint: { "fill-color": C.liberated, "fill-opacity": 0.06 } },
    { id: `${id}-occupied`, type: "fill", filter: cat("occupied"), paint: { "fill-color": C.occupied, "fill-opacity": 0.2 } },
    { id: `${id}-occupied-line`, type: "line", filter: cat("occupied"), paint: { "line-color": C.occupied, "line-width": ["interpolate", ["linear"], ["zoom"], 4, 0.8, 10, 2] } },
    { id: `${id}-contested`, type: "fill", filter: cat("contested"), paint: { "fill-color": C.contested, "fill-opacity": 0.3 } },
    { id: `${id}-contested-line`, type: "line", filter: cat("contested"), paint: { "line-color": C.contested, "line-width": 0.8, "line-dasharray": [2, 2] } },
    { id: `${id}-gained`, type: "fill", filter: cat("gained"), paint: { "fill-color": C.gained, "fill-opacity": 0.85 } },
    { id: `${id}-lost`, type: "fill", filter: cat("lost"), paint: { "fill-color": C.lost, "fill-opacity": 0.85 } },
    { id: `${id}-change-line`, type: "line", filter: ["in", ["get", "category"], ["literal", ["gained", "lost"]]] as unknown as Filter,
      paint: { "line-color": "#ffffff", "line-width": 1.2 } },
    {
      id: `${id}-attack`, type: "symbol", filter: cat("attack"), minzoom: 4.5,
      layout: { "icon-image": "track-arrow", "icon-rotate": ["coalesce", ["get", "bearing"], 0], "icon-rotation-alignment": "map",
                "icon-allow-overlap": true, "icon-size": ["interpolate", ["linear"], ["zoom"], 5, 0.55, 10, 1] },
    },
  ];
}

function movementLayers(id: string): L[] {
  const pos = ["all", ["==", ["get", "category"], "position"]];
  return [
    { id: `${id}-path`, type: "line", filter: cat("path"), layout: { "line-join": "round", "line-cap": "round" },
      paint: { "line-color": C.path, "line-width": ["interpolate", ["linear"], ["zoom"], 4, 2, 10, 4], "line-opacity": 0.85 } },
    {
      id: `${id}-report`, type: "circle", filter: [...pos, ["!", ["get", "on_path"]]] as unknown as Filter,
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 4, 2.5, 10, 5],
        "circle-color": ["case", ["get", "outlier"], "rgba(0,0,0,0)", C.path],
        "circle-opacity": ["interpolate", ["linear"], ["coalesce", ["get", "age_days"], 0], 0, 0.7, 21, 0.25],
        "circle-stroke-color": ["case", ["get", "outlier"], "#8fa3b8", "#05080d"], "circle-stroke-width": 1,
      },
    },
    {
      id: `${id}-day`, type: "circle", filter: [...pos, ["get", "on_path"]] as unknown as Filter,
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 4, 4, 10, 8],
        "circle-color": C.path,
        "circle-stroke-color": ["case", ["==", ["get", "status"], "confirmed"], "#ffffff", "#05080d"],
        "circle-stroke-width": ["case", ["==", ["get", "status"], "confirmed"], 2, 1],
      },
    },
    {
      id: `${id}-day-label`, type: "symbol", filter: [...pos, ["get", "on_path"]] as unknown as Filter, minzoom: 5.5,
      layout: { "text-field": ["concat", ["get", "label"], "  ", ["slice", ["get", "observed_at"], 5, 10]],
                "text-font": ["Noto Sans Regular"], "text-size": 11, "text-offset": [0, 1.2], "text-anchor": "top", "text-optional": true },
      paint: { "text-color": "#d7e3ef", "text-halo-color": "#05080d", "text-halo-width": 1.4 },
    },
  ];
}

function strikeLayers(id: string): L[] {
  const reports = ["sqrt", ["get", "reports"]];
  return [
    {
      id: `${id}-strike`, type: "circle", filter: cat("strike"),
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 3, ["+", 2, reports], 10, ["+", 5, ["*", 2.5, reports]]] as never,
        "circle-color": ["match", ["get", "outcome"], "hit", STRIKE.hit, "claimed", STRIKE.claimed, "intercepted", STRIKE.intercepted,
                         STRIKE.explosions_heard] as never,
        "circle-opacity": ["interpolate", ["linear"], ["get", "age_hours"], 0, 0.95, 168, 0.35] as never,
        "circle-stroke-color": ["case", ["get", "corroborated"], "#ffffff", "#05080d"] as never,
        "circle-stroke-width": ["case", ["get", "corroborated"], 1.4, 0.8] as never,
      },
    },
    {
      id: `${id}-strike-label`, type: "symbol", filter: cat("strike"), minzoom: 7,
      layout: { "text-field": ["get", "label"], "text-font": ["Noto Sans Regular"], "text-size": 10.5, "text-offset": [0, 1.1],
                "text-anchor": "top", "text-optional": true },
      paint: { "text-color": "#ffd0b8", "text-halo-color": "#05080d", "text-halo-width": 1.4 },
    },
  ];
}

function layersFor(t: Track): L[] {
  const id = `track-${t.id}`;
  return t.kind === "front" ? frontLayers(id) : t.kind === "strikes" ? strikeLayers(id) : movementLayers(id);
}

const hhmm = (s: string) => new Date(s).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });

function Strikes({ state, onFocus, onChange }: { state: TrackState; onFocus: (lng: number, lat: number) => void; onChange: () => void }) {
  const sum = state.summary as StrikeSummary;
  const events = state.features.features
    .filter((f) => f.properties?.category === "strike")
    .sort((a, b) => String(b.properties?.last).localeCompare(String(a.properties?.last)))
    .slice(0, 12);
  const reject = (ids: number[]) => Promise.all(ids.map((i) => api.setObservation(i, "rejected"))).then(onChange).catch(() => undefined);
  const span = sum.days === 1 ? "day" : `${sum.days} days`;
  const weapons = Object.entries(sum.weapons).sort((a, b) => b[1] - a[1]).map(([w, n]) => `${WEAPON[w] ?? w} ${n}`).join(" · ");
  return (
    <div className="tp-stats mono">
      <div><b>{sum.strikes}</b> strikes in the last {span}</div>
      <div><i style={{ background: STRIKE.hit }} />{sum.hits} hits <i style={{ background: STRIKE.intercepted, marginLeft: 8 }} />{sum.intercepted} intercepted</div>
      <div>{sum.corroborated} reported by 2+ channels or outlets{sum.killed ? `, ${sum.killed} killed` : ""}</div>
      {weapons && <div className="dimmer">{weapons}</div>}
      {events.length ? (
        <div className="tp-reports">
          {events.map((f) => {
            const p = f.properties as Record<string, any>;
            const [lng, lat] = (f.geometry as GeoJSON.Point).coordinates;
            return (
              <div key={p.ids[0]} className="tp-report">
                <div className="tp-report-head">
                  <button className="linkish" onClick={() => onFocus(lng, lat)} title="Show on the map">
                    {hhmm(p.first)} · <b>{p.label}</b>
                  </button>
                  <span className="dimmer"> · {WEAPON[p.weapon] ?? p.weapon}, {String(p.outcome).replace("_", " ")}</span>
                  {!p.corroborated && <span className="tag" title="Only one channel has reported it so far">1 source</span>}
                  {p.rough && <span className="tag" title="Only the province is given; shown on its main city">province</span>}
                </div>
                <div className="dimmer">
                  {[p.target, p.killed ? `${p.killed} killed` : "", p.injured ? `${p.injured} injured` : ""].filter(Boolean).join(" · ")}
                </div>
                {p.quote && <div className="tp-quote">“{p.quote}”</div>}
                {(p.photos as string[] | undefined)?.length ? (
                  <div className="tp-photos">
                    {(p.photos as string[]).slice(0, 4).map((src) => (
                      <a key={src} href={src} target="_blank" rel="noreferrer noopener" title="Open the photo"><img src={src} loading="lazy" alt="Photo from the post" /></a>
                    ))}
                  </div>
                ) : null}
                <div className="tp-report-actions">
                  {(p.sources as { handle: string; url: string }[]).slice(0, 3).map((s) => (
                    <a key={s.url} href={s.url} target="_blank" rel="noreferrer noopener">@{s.handle}</a>
                  ))}
                  {p.channels > 3 && <span className="dimmer">+{p.channels - 3}</span>}
                  <button className="linkish" onClick={() => reject(p.ids)} title="Not a strike, or in the wrong place: hide it for good">✕ reject</button>
                </div>
              </div>
            );
          })}
        </div>
      ) : <div className="dimmer" style={{ marginTop: 6 }}>None reported in this span.</div>}
    </div>
  );
}

function Reports({ state, onFocus, onChange }: { state: TrackState; onFocus: (lng: number, lat: number) => void; onChange: () => void }) {
  const reports = state.features.features
    .filter((f) => f.properties?.category === "position")
    .sort((a, b) => String(b.properties?.observed_at).localeCompare(String(a.properties?.observed_at)))
    .slice(0, 12);
  if (!reports.length) return <div className="dimmer" style={{ marginTop: 6 }}>No positions reported yet.</div>;
  const set = (id: number, status: "confirmed" | "rejected" | "auto") => api.setObservation(id, status).then(onChange).catch(() => undefined);
  return (
    <div className="tp-reports">
      {reports.map((f) => {
        const p = f.properties as Record<string, any>;
        const [lng, lat] = (f.geometry as GeoJSON.Point).coordinates;
        return (
          <div key={p.id} className={`tp-report ${p.outlier ? "outlier" : ""} ${p.status === "confirmed" ? "confirmed" : ""}`}>
            <div className="tp-report-head">
              <button className="linkish" onClick={() => onFocus(lng, lat)} title="Show on the map">
                {day(p.observed_at)} · <b>{p.label}</b>
              </button>
              {p.people ? <span className="dimmer"> · ~{Number(p.people).toLocaleString()} people</span> : null}
              {p.placed_by === "approximate" && <span className="tag" title="Place not found in the town lists; the model's estimate">approx</span>}
              {p.outlier && <span className="tag" title="Too far from the rest of the route for that date">off route</span>}
              {!p.dated && <span className="tag" title="The article gave no date; the publication date is used">undated</span>}
            </div>
            {p.quote && <div className="tp-quote">“{p.quote}”</div>}
            <div className="tp-report-actions">
              {p.source_url && <a href={p.source_url} target="_blank" rel="noreferrer noopener">source</a>}
              {p.status !== "confirmed"
                ? <button className="linkish" onClick={() => set(p.id, "confirmed")}>✓ confirm</button>
                : <button className="linkish" onClick={() => set(p.id, "auto")}>undo confirm</button>}
              <button className="linkish" onClick={() => set(p.id, "rejected")}>✕ reject</button>
            </div>
          </div>
        );
      })}
    </div>
  );
}

const km2 = (n?: number) => (n == null ? "?" : n >= 1000 ? `${Math.round(n).toLocaleString()} km²` : `${n.toFixed(n < 10 ? 2 : 1)} km²`);
const day = (s: string | Date) => new Date(s).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });

function Sparkline({ points, cursor }: { points: { t: number; v: number }[]; cursor: number }) {
  if (points.length < 2) return null;
  const w = 248, h = 38;
  const t0 = points[0].t, t1 = points[points.length - 1].t;
  const vs = points.map((p) => p.v), lo = Math.min(...vs), hi = Math.max(...vs);
  const x = (t: number) => ((t - t0) / (t1 - t0 || 1)) * w;
  const y = (v: number) => h - 3 - ((v - lo) / (hi - lo || 1)) * (h - 6);
  const d = points.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join("");
  const cx = x(Math.min(Math.max(cursor, t0), t1));
  return (
    <svg width={w} height={h} className="spark" aria-label="Area held over time">
      <path d={d} fill="none" stroke={C.occupied} strokeWidth="1.3" />
      <line x1={cx} x2={cx} y1={0} y2={h} stroke="#d7e3ef" strokeWidth="1" strokeDasharray="2 2" />
    </svg>
  );
}

export default function TracksPanel({ timelineEnd, onOverlays, onFocus }: Props) {
  const [tracks, setTracks] = useState<Track[]>([]);
  const [on, setOn] = useState<Set<number>>(new Set());
  const [states, setStates] = useState<Map<number, TrackState>>(new Map());
  const [series, setSeries] = useState<Map<number, { t: number; v: number }[]>>(new Map());
  const [pinned, setPinned] = useState<Date | null>(null); // a date picked on the history slider
  const [compare, setCompare] = useState(1);
  const [playing, setPlaying] = useState(false);
  const [open, setOpen] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const at = pinned ?? timelineEnd;

  useEffect(() => {
    api.tracks().then((t) => {
      setTracks(t);
      setOn(new Set(t.map((x) => x.id)));
    }).catch(() => undefined);
  }, []);

  useEffect(() => {
    for (const t of tracks) {
      if (t.kind !== "front" || series.has(t.id)) continue;
      api.trackSeries(t.id).then((s) => setSeries((m) => new Map(m).set(t.id, s.map((p) => ({ t: Date.parse(p.t), v: p.stats.occupied_km2 ?? 0 })))))
        .catch(() => undefined);
    }
  }, [tracks, series]);

  // Fetch each visible track's state for the chosen moment (debounced while dragging).
  const seq = useRef(0);
  useEffect(() => {
    const n = ++seq.current;
    const timer = setTimeout(() => {
      Promise.all([...on].map((id) => api.trackState(id, at, compare).then((s) => [id, s] as const)))
        .then((pairs) => { if (n === seq.current) setStates(new Map(pairs)); })
        .catch(() => undefined);
    }, playing ? 0 : 150);
    return () => clearTimeout(timer);
  }, [on, at.getTime(), compare, playing, refresh]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    onOverlays(tracks.filter((t) => on.has(t.id) && states.has(t.id)).map((t) => ({
      id: `track-${t.id}`,
      data: states.get(t.id)!.features,
      layers: layersFor(t),
    })));
  }, [tracks, on, states, onOverlays]);

  // History range across all tracks shown.
  const range = useMemo(() => {
    const ts = tracks.filter((t) => on.has(t.id) && t.first && t.last);
    if (!ts.length) return null;
    return { lo: Math.min(...ts.map((t) => Date.parse(t.first!))), hi: Math.max(Date.now(), ...ts.map((t) => Date.parse(t.last!))) };
  }, [tracks, on]);

  // Play: step through history a day at a time, waiting for each day to load.
  useEffect(() => {
    if (!playing || !range) return;
    const timer = setTimeout(() => {
      // About 6 minutes for the whole war at most; a day per frame for shorter spans.
      const next = (pinned ? pinned.getTime() : range.lo) + Math.max(DAY, (range.hi - range.lo) / 800);
      if (next >= range.hi) { setPlaying(false); setPinned(null); return; }
      setPinned(new Date(next));
    }, 450);
    return () => clearTimeout(timer);
  }, [playing, states, range]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!tracks.length) return null;

  return (
    <div className={`tracks-panel ${open ? "" : "closed"}`}>
      <div className="tp-head" onClick={() => setOpen(!open)}>
        <span>TRACKS</span><span className="dimmer">{open ? "▾" : "▸"}</span>
      </div>
      {open && (
        <>
          {tracks.map((t) => {
            const s = states.get(t.id);
            const ms = t.kind === "movement" ? (s?.summary as MovementSummary | undefined) : undefined;
            const st = s?.snapshot?.stats;
            const changes = (s?.features.features ?? []).filter((f) => ["gained", "lost"].includes(f.properties?.category));
            const gained = changes.filter((f) => f.properties?.category === "gained").reduce((a, f) => a + (f.properties?.km2 ?? 0), 0);
            const lost = changes.filter((f) => f.properties?.category === "lost").reduce((a, f) => a + (f.properties?.km2 ?? 0), 0);
            return (
              <div key={t.id} className="tp-track">
                <label className="toggle-row">
                  <span><b>{t.name}</b><br /><span className="dimmer mono" style={{ fontSize: 10 }}>{t.source}</span></span>
                  <input type="checkbox" checked={on.has(t.id)} onChange={() => setOn((o) => { const n = new Set(o); if (n.has(t.id)) n.delete(t.id); else n.add(t.id); return n; })} />
                </label>
                {on.has(t.id) && s && ms && (
                  <div className="tp-stats mono">
                    {ms.latest
                      ? <div>Last seen <b>{ms.latest.place}</b>, {day(ms.latest.day)}</div>
                      : <div className="dimmer">No position yet</div>}
                    <div><i style={{ background: C.path }} />{ms.days} days on the map, about {ms.km.toLocaleString()} km</div>
                    {ms.people ? <div>Last reported size: ~{ms.people.toLocaleString()} people</div> : null}
                    <div className="dimmer">{ms.reports} reports from the news</div>
                    <Reports state={s} onFocus={onFocus} onChange={() => setRefresh((x) => x + 1)} />
                  </div>
                )}
                {on.has(t.id) && t.kind === "strikes" && s?.summary && (
                  <Strikes state={s} onFocus={onFocus} onChange={() => setRefresh((x) => x + 1)} />
                )}
                {on.has(t.id) && t.kind === "front" && s?.snapshot && (
                  <div className="tp-stats mono">
                    <div>Map of <b>{day(s.snapshot.observed_at)}</b></div>
                    <div><i style={{ background: C.occupied }} />held {km2(st?.occupied_km2)}</div>
                    <div><i style={{ background: C.contested }} />contested {km2(st?.contested_km2)}</div>
                    {s.previous ? (
                      <div className="tp-change">
                        since {day(s.previous.observed_at)}:
                        <span style={{ color: C.gained }}> +{km2(gained)} taken</span>,
                        <span style={{ color: C.lost }}> {km2(lost)} retaken</span>
                      </div>
                    ) : <div className="dimmer">no earlier map to compare</div>}
                    <Sparkline points={series.get(t.id) ?? []} cursor={at.getTime()} />
                  </div>
                )}
              </div>
            );
          })}
          {range && (
            <div className="tp-history">
              <div className="tp-row mono">
                <button className="btn" onClick={() => { if (!playing && (!pinned || pinned.getTime() >= range.hi - DAY)) setPinned(new Date(range.lo)); setPlaying(!playing); }}>
                  {playing ? "❚❚ PAUSE" : "▶ PLAY"}
                </button>
                <span>{day(at)}</span>
                {pinned && <button className="btn" onClick={() => { setPinned(null); setPlaying(false); }}>FOLLOW TIMELINE</button>}
              </div>
              <input type="range" min={range.lo} max={range.hi} step={DAY / 4} value={at.getTime()}
                onChange={(e) => { setPlaying(false); setPinned(new Date(Number(e.target.value))); }} />
              <div className="tp-row mono">
                <span className="dimmer" title="Fronts: what changed in this span. Strikes: those reported in it.">over</span>
                {COMPARE.map((c) => (
                  <button key={c} className={`btn ${compare === c ? "on" : ""}`} onClick={() => setCompare(c)}>{c === 1 ? "1 DAY" : `${c} DAYS`}</button>
                ))}
              </div>
              <div className="tp-legend mono">
                <span><i style={{ background: C.gained }} />taken</span>
                <span><i style={{ background: C.lost }} />retaken</span>
                <span><i style={{ background: C.attack }} />attack</span>
                {tracks.some((t) => t.kind === "strikes" && on.has(t.id)) && (
                  <>
                    <span><i style={{ background: STRIKE.hit }} />strike hit</span>
                    <span><i style={{ background: STRIKE.intercepted }} />intercepted</span>
                    <span><i style={{ background: STRIKE.explosions_heard }} />explosions</span>
                  </>
                )}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
