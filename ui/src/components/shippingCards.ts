/** Click cards for the SHIPPING layers. Built from DOM nodes only, never HTML, because names
 *  and summaries come from outside sources (AIS messages, news). */

type Rec = Record<string, any>;

const el = (tag: string, cls: string, text?: string) => { const e = document.createElement(tag); e.className = cls; if (text) e.textContent = text; return e; };
const link = (href: string, text: string) => { const a = document.createElement("a"); a.href = href; a.target = "_blank"; a.rel = "noreferrer noopener"; a.textContent = text; return a; };
const num = (n: number | null | undefined) => (n == null ? "?" : Math.round(n).toLocaleString());
const pct = (n: number | null | undefined) => (n == null ? "" : `${n > 0 ? "+" : ""}${n}%`);
const when = (s: string | null | undefined) => (s ? new Date(s).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "?");
const day = (s: string) => new Date(s).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });

/** The map flattens lists and objects in feature properties to JSON strings. */
export function unflatten(raw: Rec): Rec {
  return Object.fromEntries(Object.entries(raw).map(([k, v]) => {
    if (typeof v === "string" && (v.startsWith("[") || v.startsWith("{"))) { try { return [k, JSON.parse(v)]; } catch { /* plain text */ } }
    return [k, v];
  }));
}

function nearby(root: HTMLElement, d: Rec, openStory: (id: number) => void) {
  for (const e of (d.events ?? []) as Rec[]) {
    const row = el("div", e.status === "ended" ? "sc-meta" : "sc-warn", `${e.status === "ended" ? "Was" : "Now"}: ${e.summary || e.title}`);
    root.append(row);
    if (e.story_id) { const b = el("button", "sc-story", "Read the story") as HTMLButtonElement; b.onclick = () => openStory(e.story_id); root.append(b); }
  }
  if ((d.stories ?? []).length) {
    root.append(el("div", "sc-meta", "News placed nearby, last 7 days:"));
    for (const s of d.stories as Rec[]) {
      const b = el("button", "sc-story", s.title) as HTMLButtonElement;
      b.title = `${s.item_count} articles; open the story`;
      b.onclick = () => openStory(s.id);
      root.append(b);
    }
  }
}

/** Daily ships through a chokepoint over the last year, with this week marked. */
function spark(series: Rec[]): SVGSVGElement | null {
  if (series.length < 14) return null;
  const NS = "http://www.w3.org/2000/svg";
  const w = 300, h = 46;
  // A 7 day average, so weekday swings do not hide the trend.
  const avg = series.map((_, i) => { const s = series.slice(Math.max(0, i - 6), i + 1); return s.reduce((a, d) => a + (d.total ?? 0), 0) / s.length; });
  const hi = Math.max(...avg), lo = Math.min(...avg);
  const x = (i: number) => (i / (avg.length - 1)) * w;
  const y = (v: number) => h - 3 - ((v - lo) / (hi - lo || 1)) * (h - 8);
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("width", String(w)); svg.setAttribute("height", String(h)); svg.setAttribute("class", "sc-spark");
  const path = document.createElementNS(NS, "path");
  path.setAttribute("d", avg.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(""));
  path.setAttribute("fill", "none"); path.setAttribute("stroke", "#FF8C42"); path.setAttribute("stroke-width", "1.4");
  svg.append(path);
  return svg;
}

export function siteCard(d: Rec, openStory: (id: number) => void): HTMLElement {
  const root = el("div", "strike-card");
  const s = d.stats ?? {}, info = d.info ?? {};
  root.append(el("div", "sc-title", d.name));
  if (d.kind === "chokepoint") {
    root.append(el("div", "sc-meta", `Strait or canal · PortWatch, week to ${s.as_of ? day(s.as_of) : "?"}`));
    root.append(el("div", s.unusual ? "sc-warn" : "sc-meta",
      `${num(s.transits_week)} ships this week; normal ${num(s.transits_normal)} (${pct(s.change_pct) || "no comparison"})`));
    if (s.transits_last_year != null) root.append(el("div", "sc-meta", `Same week last year: ${num(s.transits_last_year)} (${pct(s.vs_last_year_pct)})`));
    if (s.tankers_week != null) root.append(el("div", "sc-meta", `${num(s.tankers_week)} of them tankers`));
    const sv = spark(d.series ?? []);
    if (sv) { root.append(sv); root.append(el("div", "sc-meta", "Ships a day over the last year, 7 day average")); }
  } else if (d.kind === "port") {
    root.append(el("div", "sc-meta", [d.country, info.locode, (info.industries ?? []).slice(0, 2).join(", ")].filter(Boolean).join(" · ")));
    if (s.as_of) {
      root.append(el("div", s.unusual ? "sc-warn" : "sc-meta",
        `${num(s.calls_week)} port calls in the week to ${day(s.as_of)}; normal ${num(s.calls_normal)}${s.change_pct != null ? ` (${pct(s.change_pct)})` : ""}`));
      root.append(el("div", "sc-meta", `Imports ${num(s.import_t)} t (normal ${num(s.import_normal_t)}) · exports ${num(s.export_t)} t (normal ${num(s.export_normal_t)})`));
    }
    // PortWatch gives these as percents already (100 for a country's only port).
    const share100 = (v: number) => `${v >= 10 ? Math.round(v) : v.toFixed(1)}%`;
    const share = [info.share_of_imports ? `${share100(info.share_of_imports)} of the country's sea imports` : "",
      info.share_of_exports ? `${share100(info.share_of_exports)} of its sea exports` : ""].filter(Boolean).join(", ");
    if (share) root.append(el("div", "sc-meta", share));
  } else if (d.kind === "airport") {
    root.append(el("div", "sc-meta", [info.city, d.country, info.iata, info.icao, info.size === "large" ? "large airport" : "airport"].filter(Boolean).join(" · ")));
    if (info.wikipedia) root.append(link(info.wikipedia, "About this airport"));
  } else if (d.kind === "border") {
    root.append(el("div", "sc-meta", `${info.border ?? "US border"} · ${s.status ?? "?"} · ${s.hours ?? ""}`));
    const t = s.truck_delay_min;
    root.append(el("div", t == null ? "sc-meta" : t >= 60 ? "sc-warn" : "sc-ok",
      t == null ? "No truck wait reported" : `Trucks: ${t} minute wait${s.truck_lanes_open ? `, ${s.truck_lanes_open} lanes open` : ""}${s.truck_fast_delay_min != null ? `; FAST lanes ${s.truck_fast_delay_min} min` : ""}`));
    if (s.car_delay_min != null) root.append(el("div", "sc-meta", `Cars: ${s.car_delay_min} minute wait`));
    root.append(el("div", "sc-meta", `US Customs and Border Protection, ${s.truck_update ?? s.updated ?? ""}`));
  }
  if (info.url) root.append(link(info.url, d.kind === "border" ? "CBP wait times" : "PortWatch page"));
  nearby(root, d, openStory);
  return root;
}

export function vesselCard(v: Rec): HTMLElement {
  const root = el("div", "strike-card");
  root.append(el("div", "sc-title", v.name || `Ship ${v.mmsi}`));
  root.append(el("div", "sc-meta", [v.type_label, v.length_m ? `${v.length_m} m long` : "", v.draught ? `draught ${v.draught} m` : ""].filter(Boolean).join(" · ")));
  if (v.destination) root.append(el("div", "sc-meta", `Bound for ${v.destination}${v.eta ? `, arriving ${v.eta} UTC (as the crew entered it)` : ""}`));
  root.append(el("div", "sc-meta", [v.nav, v.sog != null ? `${v.sog.toFixed(1)} knots` : "", v.cog != null ? `course ${Math.round(v.cog)}°` : ""].filter(Boolean).join(" · ")));
  root.append(el("div", "sc-meta", `Last heard ${when(v.pos_at)} · MMSI ${v.mmsi}${v.imo ? ` · IMO ${v.imo}` : ""}${v.callsign ? ` · ${v.callsign}` : ""}`));
  if ((v.track ?? []).length > 2) root.append(el("div", "sc-meta", "Its trail over the last 48 hours is drawn on the map"));
  const row = el("div", "sc-meta");
  row.append(link(v.links.marinetraffic, "MarineTraffic"), document.createTextNode(" · "), link(v.links.vesselfinder, "VesselFinder"));
  root.append(row);
  return root;
}

export function planeCard(raw: Rec): HTMLElement {
  const p = unflatten(raw);
  const root = el("div", "strike-card");
  root.append(el("div", "sc-title", `${p.callsign} · ${p.operator}`));
  root.append(el("div", "sc-meta", `${num(p.alt_m * 3.28084)} ft · ${num(p.speed_kt)} knots · heading ${p.dir}°`));
  root.append(el("div", "sc-meta", `Registered in ${p.country} · seen ${when(p.seen_at)} (OpenSky)`));
  root.append(link(p.link, "Follow it on ADS-B Exchange"));
  return root;
}

export function disruptionCard(raw: Rec, openStory: (id: number) => void): HTMLElement {
  const p = unflatten(raw);
  const root = el("div", "strike-card");
  const sev = ["", "minor", "serious", "major"][p.severity] ?? "";
  root.append(el("div", "sc-title", p.kind_label));
  root.append(el("div", p.status === "ongoing" ? "sc-warn" : "sc-meta",
    [p.place, sev, p.status === "threatened" ? "threatened, not yet happened" : p.status, p.started_at ? `since ${day(p.started_at)}` : ""].filter(Boolean).join(" · ")));
  root.append(el("div", "sc-meta", p.summary || p.title));
  const info = p.info ?? {};
  if (p.source === "portwatch") {
    if ((info.ports ?? []).length) root.append(el("div", "sc-meta", `Ports in its path: ${(info.ports as string[]).slice(0, 8).join(", ")}`));
    if (info.people) root.append(el("div", "sc-meta", `People affected: ${num(info.people)}`));
    root.append(link(p.url, "IMF PortWatch (from GDACS alerts)"));
  } else if (p.story_id) {
    const b = el("button", "sc-story", `${p.title} (${p.articles} articles)`) as HTMLButtonElement;
    b.onclick = () => openStory(p.story_id);
    root.append(b);
  }
  return root;
}
