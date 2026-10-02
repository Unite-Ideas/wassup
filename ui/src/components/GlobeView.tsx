import { useEffect, useMemo, useRef } from "react";
import Globe, { type GlobeInstance } from "globe.gl";
import { MeshPhongMaterial, Color } from "three";
import { feature } from "topojson-client";
import type { Topology, GeometryCollection } from "topojson-specification";
import countriesTopo from "world-atlas/countries-110m.json";
import type { Desk, GlobeData, Place, Selection, Story, StoryDetail } from "../lib/types";
import { deskColor, escapeHtml, hexToRgba } from "../lib/format";

interface Props {
  data: GlobeData | null;
  desks: Map<string, Desk>;
  selection: Selection;
  storyDetail: StoryDetail | null;
  showLinks: boolean;
  showLabels: boolean;
  autoRotate: boolean;
  onSelectPlace: (id: number) => void;
  onSelectStory: (id: number) => void;
}

interface Arc {
  a: number;
  b: number;
  kind: string;
  weight: number;
  startLat: number;
  startLng: number;
  endLat: number;
  endLng: number;
  colors: [string, string];
  focus: boolean;
  titles: [string, string];
  evidence: string;
}

const topo = countriesTopo as unknown as Topology<{ countries: GeometryCollection }>;
const COUNTRIES = (feature(topo, topo.objects.countries) as unknown as GeoJSON.FeatureCollection).features;

function pointSize(p: Place) {
  return 0.14 + Math.min(0.55, Math.sqrt(p.story_count) * 0.07);
}

function pointHeight(p: Place) {
  return 0.004 + Math.min(0.11, Math.log2(1 + p.story_count) * 0.014);
}

export default function GlobeView(props: Props) {
  const el = useRef<HTMLDivElement>(null);
  const globe = useRef<GlobeInstance | null>(null);
  const handlers = useRef(props);
  handlers.current = props;

  // Create the globe once.
  useEffect(() => {
    if (!el.current) return;
    const g = new Globe(el.current, { rendererConfig: { antialias: true, alpha: true } })
      .backgroundColor("rgba(0,0,0,0)")
      .showAtmosphere(true)
      .atmosphereColor("#00e5ff")
      .atmosphereAltitude(0.13)
      .globeMaterial(new MeshPhongMaterial({ color: new Color("#06101a"), emissive: new Color("#03101a"), shininess: 4, transparent: false }))
      .hexPolygonsData(COUNTRIES)
      .hexPolygonResolution(3)
      .hexPolygonMargin(0.42)
      .hexPolygonUseDots(false)
      .hexPolygonColor(() => "rgba(0, 229, 255, 0.17)")
      .hexPolygonAltitude(0.002)
      // Places
      .pointLat("lat")
      .pointLng("lon")
      .pointAltitude((d: object) => pointHeight(d as Place))
      .pointRadius((d: object) => pointSize(d as Place))
      .pointResolution(10)
      .pointsMerge(false)
      .pointsTransitionDuration(400)
      .pointLabel((d: object) => {
        const p = d as Place;
        return `<div class="globe-tip"><div class="k">${escapeHtml(p.kind)}${p.country ? ` · ${escapeHtml(p.country)}` : ""}</div>
          <b>${escapeHtml(p.name)}</b><br/>${p.story_count} stories · ${p.item_count} mentions${p.breaking ? ' · <span style="color:#ff3b5c">BREAKING</span>' : ""}</div>`;
      })
      .onPointClick((d: object) => handlers.current.onSelectPlace((d as Place).id))
      // Breaking pulses
      .ringLat("lat")
      .ringLng("lon")
      .ringColor((d: object) => ((d as { accent?: boolean }).accent
        ? (t: number) => `rgba(0, 229, 255, ${1 - t})`
        : (t: number) => `rgba(255, 59, 92, ${1 - t})`))
      .ringMaxRadius((d: object) => ((d as { accent?: boolean }).accent ? 2.2 : 4.5))
      .ringPropagationSpeed(2.2)
      .ringRepeatPeriod((d: object) => ((d as { accent?: boolean }).accent ? 900 : 1300))
      // Strings between stories
      .arcColor((d: object) => (d as Arc).colors)
      .arcStroke((d: object) => {
        const a = d as Arc;
        return a.focus ? 0.55 + a.weight * 0.5 : 0.12 + a.weight * 0.3;
      })
      .arcAltitudeAutoScale(0.42)
      .arcDashLength((d: object) => ((d as Arc).focus ? 0.45 : 1))
      .arcDashGap((d: object) => ((d as Arc).focus ? 0.18 : 0))
      .arcDashAnimateTime((d: object) => ((d as Arc).focus ? 1800 : 0))
      .arcsTransitionDuration(0)
      .arcLabel((d: object) => {
        const a = d as Arc;
        return `<div class="globe-tip"><div class="k">${a.kind === "same_actor" ? "Shared actors" : "Related coverage"}</div>
          ${escapeHtml(a.titles[0])}<br/><span style="color:#5b6f84">to</span><br/>${escapeHtml(a.titles[1])}
          ${a.evidence ? `<div class="k" style="margin-top:6px">${escapeHtml(a.evidence)}</div>` : ""}</div>`;
      })
      .onArcClick((d: object) => {
        const a = d as Arc;
        const sel = handlers.current.selection;
        handlers.current.onSelectStory(sel?.type === "story" && sel.id === a.a ? a.b : a.a);
      })
      // Labels for the busiest places
      .labelLat("lat")
      .labelLng("lon")
      .labelText("name")
      .labelSize((d: object) => 0.42 + Math.min(0.55, Math.sqrt((d as Place).story_count) * 0.06))
      .labelColor(() => "rgba(215, 227, 239, 0.78)")
      .labelDotRadius(0)
      .labelAltitude((d: object) => pointHeight(d as Place) + 0.01)
      .labelResolution(2)
      .labelsTransitionDuration(0);

    g.pointOfView({ lat: 30, lng: 20, altitude: 2.3 });
    const controls = g.controls();
    controls.autoRotate = true;
    controls.autoRotateSpeed = 0.22;
    controls.enableDamping = true;
    const stop = () => (controls.autoRotate = false);
    el.current.addEventListener("pointerdown", stop);

    const ro = new ResizeObserver(([e]) => g.width(e.contentRect.width).height(e.contentRect.height));
    ro.observe(el.current);
    globe.current = g;
    const node = el.current;
    return () => {
      ro.disconnect();
      node.removeEventListener("pointerdown", stop);
      g._destructor();
      node.innerHTML = "";
      globe.current = null;
    };
  }, []);

  useEffect(() => {
    const g = globe.current;
    if (g) g.controls().autoRotate = props.autoRotate;
  }, [props.autoRotate]);

  const storyById = useMemo(() => {
    const m = new Map<number, Story>();
    props.data?.stories.forEach((s) => m.set(s.id, s));
    props.storyDetail?.links.forEach((s) => m.set(s.id, s));
    if (props.storyDetail) m.set(props.storyDetail.id, props.storyDetail);
    return m;
  }, [props.data, props.storyDetail]);

  const focusId = props.selection?.type === "story" ? props.selection.id : null;

  const arcs = useMemo<Arc[]>(() => {
    if (!props.data) return [];
    const links = [...props.data.links];
    // The selected story's own links, even when the far end is outside the current filters.
    if (props.storyDetail) {
      const have = new Set(links.map((l) => `${l.a}-${l.b}-${l.kind}`));
      for (const n of props.storyDetail.links) {
        const [a, b] = props.storyDetail.id < n.id ? [props.storyDetail.id, n.id] : [n.id, props.storyDetail.id];
        if (!have.has(`${a}-${b}-${n.kind}`)) links.push({ a, b, kind: n.kind, weight: n.weight, evidence: n.evidence });
      }
    }
    const out: Arc[] = [];
    for (const l of links) {
      const sa = storyById.get(l.a);
      const sb = storyById.get(l.b);
      if (!sa || !sb || sa.lat == null || sb.lat == null || sa.lon == null || sb.lon == null) continue;
      if (Math.abs(sa.lat - sb.lat) < 0.05 && Math.abs(sa.lon - sb.lon) < 0.05) continue; // same spot, nothing to draw
      const focus = focusId != null && (l.a === focusId || l.b === focusId);
      if (!props.showLinks && !focus) continue;
      const alpha = focusId == null ? 0.22 + l.weight * 0.35 : focus ? 0.95 : 0.05;
      const colors: [string, string] = l.kind === "same_actor"
        ? [`rgba(255, 59, 92, ${alpha})`, `rgba(255, 120, 140, ${alpha})`]
        : [hexToRgba(deskColor(props.desks, sa.desk), alpha), hexToRgba(deskColor(props.desks, sb.desk), alpha)];
      out.push({
        a: l.a, b: l.b, kind: l.kind, weight: l.weight, focus, colors,
        startLat: sa.lat, startLng: sa.lon, endLat: sb.lat, endLng: sb.lon,
        titles: [sa.title, sb.title],
        evidence: l.evidence?.entities?.length ? l.evidence.entities.slice(0, 4).join(", ") : l.evidence?.similarity ? `similarity ${l.evidence.similarity}` : "",
      });
    }
    return out;
  }, [props.data, props.storyDetail, props.showLinks, props.desks, storyById, focusId]);

  // Push data into the globe.
  useEffect(() => {
    const g = globe.current;
    if (!g) return;
    const places = props.data?.places ?? [];
    const selPlace = props.selection?.type === "place" ? props.selection.id : null;
    g.pointsData(places).pointColor((d: object) => {
      const p = d as Place;
      if (p.id === selPlace) return "#ffffff";
      return deskColor(props.desks, p.top_desk);
    });
    const rings: object[] = places.filter((p) => p.breaking).map((p) => ({ lat: p.lat, lon: p.lon }));
    const sel = selPlace != null ? places.find((p) => p.id === selPlace) : null;
    if (sel) rings.push({ lat: sel.lat, lon: sel.lon, accent: true });
    const fs = focusId != null ? storyById.get(focusId) : null;
    if (fs?.lat != null) rings.push({ lat: fs.lat, lon: fs.lon, accent: true });
    g.ringsData(rings);
    g.labelsData(props.showLabels ? [...places].sort((a, b) => b.story_count - a.story_count).slice(0, 28) : []);
  }, [props.data, props.desks, props.selection, props.showLabels, focusId, storyById]);

  useEffect(() => {
    globe.current?.arcsData(arcs);
  }, [arcs]);

  // Fly to whatever was selected.
  useEffect(() => {
    const g = globe.current;
    if (!g || !props.selection) return;
    let target: { lat: number; lng: number } | null = null;
    if (props.selection.type === "place") {
      const p = props.data?.places.find((x) => x.id === props.selection!.id);
      if (p) target = { lat: p.lat, lng: p.lon };
    } else {
      const s = storyById.get(props.selection.id);
      if (s?.lat != null && s.lon != null) target = { lat: s.lat, lng: s.lon };
    }
    if (target) {
      g.controls().autoRotate = false;
      g.pointOfView({ ...target, altitude: Math.min(g.pointOfView().altitude, 1.8) }, 1100);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.selection?.type, props.selection?.id, props.storyDetail?.id]);

  return <div ref={el} style={{ position: "absolute", inset: 0 }} />;
}
