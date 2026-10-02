import { useEffect, useMemo, useRef, useState } from "react";
import type { Desk, TimelineData } from "../lib/types";
import { deskColor, stamp } from "../lib/format";

interface Props {
  extentHours: number;
  onExtent: (h: number) => void;
  data: TimelineData | null;
  desks: Desk[];
  deskMap: Map<string, Desk>;
  start: Date;
  end: Date;
  live: boolean;
  playing: boolean;
  onWindow: (start: Date, end: Date) => void;
  onLive: () => void;
  onPlay: () => void;
}

const EXTENTS = [
  { h: 24, label: "24H" },
  { h: 72, label: "3D" },
  { h: 168, label: "7D" },
  { h: 720, label: "30D" },
];
const M = { l: 8, r: 14, t: 10, b: 20 };
const MIN_WINDOW_MS = 15 * 60 * 1000;

type Drag = { mode: "move" | "l" | "r" | "new"; x0: number; s0: number; e0: number } | null;

export default function Timeline(p: Props) {
  const wrap = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 800, h: 110 });
  const [drag, setDrag] = useState<Drag>(null);

  useEffect(() => {
    if (!wrap.current) return;
    const ro = new ResizeObserver(([e]) => setSize({ w: e.contentRect.width, h: e.contentRect.height }));
    ro.observe(wrap.current);
    return () => ro.disconnect();
  }, []);

  const t0 = p.data ? new Date(p.data.start).getTime() : Date.now() - p.extentHours * 3600e3;
  const t1 = p.data ? new Date(p.data.end).getTime() : Date.now();
  const iw = Math.max(10, size.w - M.l - M.r);
  const ih = Math.max(10, size.h - M.t - M.b);
  const x = (t: number) => M.l + ((t - t0) / (t1 - t0)) * iw;
  const tx = (px: number) => t0 + ((px - M.l) / iw) * (t1 - t0);

  const bars = useMemo(() => {
    if (!p.data) return { stacks: [] as { b: number; segs: { desk: string; y: number; h: number }[]; brk: number }[], max: 1 };
    const order = [...p.desks.map((d) => d.key), "none"];
    const byBucket = new Map<number, Map<string, number>>();
    const brk = new Map<number, number>();
    for (const c of p.data.counts) {
      if (!byBucket.has(c.bucket)) byBucket.set(c.bucket, new Map());
      byBucket.get(c.bucket)!.set(c.desk, (byBucket.get(c.bucket)!.get(c.desk) ?? 0) + c.n);
      brk.set(c.bucket, (brk.get(c.bucket) ?? 0) + c.breaking);
    }
    let max = 1;
    byBucket.forEach((m) => (max = Math.max(max, [...m.values()].reduce((a, b) => a + b, 0))));
    const stacks = [...byBucket.entries()].map(([b, m]) => {
      let acc = 0;
      const segs = order.filter((k) => m.has(k)).map((k) => {
        const v = m.get(k)!;
        const seg = { desk: k, y: acc, h: v };
        acc += v;
        return seg;
      });
      return { b, segs, brk: brk.get(b) ?? 0 };
    });
    return { stacks, max };
  }, [p.data, p.desks]);

  const bw = p.data ? iw / p.data.buckets : 1;
  const yScale = (v: number) => (Math.sqrt(v) / Math.sqrt(bars.max)) * ih;

  const ticks = useMemo(() => {
    const span = t1 - t0;
    const step = span <= 26 * 3600e3 ? 3 * 3600e3 : span <= 80 * 3600e3 ? 12 * 3600e3 : span <= 170 * 3600e3 ? 24 * 3600e3 : 4 * 24 * 3600e3;
    const out: number[] = [];
    for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) out.push(t);
    return out;
  }, [t0, t1]);

  const bs = Math.max(M.l, x(p.start.getTime()));
  const be = Math.min(M.l + iw, x(p.end.getTime()));

  function down(e: React.PointerEvent<SVGSVGElement>) {
    const r = (e.currentTarget as SVGSVGElement).getBoundingClientRect();
    const px = e.clientX - r.left;
    const s0 = p.start.getTime();
    const e0 = p.end.getTime();
    let mode: NonNullable<Drag>["mode"] = "new";
    if (Math.abs(px - bs) < 7) mode = "l";
    else if (Math.abs(px - be) < 7) mode = "r";
    else if (px > bs && px < be) mode = "move";
    (e.currentTarget as SVGSVGElement).setPointerCapture(e.pointerId);
    setDrag({ mode, x0: px, s0, e0 });
    if (mode === "new") {
      const t = tx(px);
      p.onWindow(new Date(t), new Date(t + MIN_WINDOW_MS));
    }
  }

  function move(e: React.PointerEvent<SVGSVGElement>) {
    if (!drag) return;
    const r = (e.currentTarget as SVGSVGElement).getBoundingClientRect();
    const px = e.clientX - r.left;
    const dt = tx(px) - tx(drag.x0);
    const clamp = (t: number) => Math.min(t1, Math.max(t0, t));
    if (drag.mode === "move") {
      const len = drag.e0 - drag.s0;
      let s = drag.s0 + dt;
      s = Math.min(t1 - len, Math.max(t0, s));
      p.onWindow(new Date(s), new Date(s + len));
    } else if (drag.mode === "l") {
      p.onWindow(new Date(Math.min(clamp(drag.s0 + dt), drag.e0 - MIN_WINDOW_MS)), new Date(drag.e0));
    } else if (drag.mode === "r") {
      p.onWindow(new Date(drag.s0), new Date(Math.max(clamp(drag.e0 + dt), drag.s0 + MIN_WINDOW_MS)));
    } else {
      const a = clamp(tx(drag.x0));
      const b = clamp(tx(px));
      p.onWindow(new Date(Math.min(a, b)), new Date(Math.max(Math.max(a, b), Math.min(a, b) + MIN_WINDOW_MS)));
    }
  }

  const hours = (p.end.getTime() - p.start.getTime()) / 3600e3;

  return (
    <div className="time">
      <div className="time-controls">
        <div className="seg">
          {EXTENTS.map((e) => (
            <button key={e.h} className={p.extentHours === e.h ? "on" : ""} onClick={() => p.onExtent(e.h)}>{e.label}</button>
          ))}
        </div>
        <div className="range">
          {stamp(p.start)}
          <br />
          {p.live ? <span><span className="live-dot" />NOW</span> : stamp(p.end)}
          <br />
          <span className="dimmer">{hours < 48 ? `${hours.toFixed(hours < 3 ? 1 : 0)} hours` : `${(hours / 24).toFixed(1)} days`}</span>
        </div>
        <div style={{ display: "flex", gap: 6 }}>
          <button className={`btn ${p.playing ? "primary" : ""}`} onClick={p.onPlay} title="Replay: slide the window forward through time">
            {p.playing ? "❚❚" : "▶"} {p.playing ? "PAUSE" : "REPLAY"}
          </button>
          {!p.live && <button className="btn" onClick={p.onLive}>LIVE</button>}
        </div>
      </div>
      <div ref={wrap} style={{ position: "relative" }}>
        <svg className="time-svg" width={size.w} height={size.h} onPointerDown={down} onPointerMove={move} onPointerUp={() => setDrag(null)}>
          <g className="axis">
            {ticks.map((t) => (
              <g key={t}>
                <line x1={x(t)} x2={x(t)} y1={M.t} y2={M.t + ih} />
                <text x={x(t) + 3} y={size.h - 6}>{new Date(t).toLocaleString(undefined, t1 - t0 > 80 * 3600e3 ? { month: "short", day: "numeric" } : { weekday: "short", hour: "2-digit" })}</text>
              </g>
            ))}
          </g>
          {bars.stacks.map((s) => {
            const bx = M.l + s.b * bw;
            const inWin = bx + bw >= bs && bx <= be;
            return (
              <g key={s.b} opacity={inWin ? 1 : 0.35}>
                {s.segs.map((seg) => {
                  const y0 = yScale(seg.y);
                  const y1 = yScale(seg.y + seg.h);
                  return <rect key={seg.desk} x={bx + 0.5} width={Math.max(1, bw - 1)} y={M.t + ih - y1} height={Math.max(0.5, y1 - y0)} fill={deskColor(p.deskMap, seg.desk === "none" ? null : seg.desk)} />;
                })}
                {s.brk > 0 && <rect x={bx + 0.5} width={Math.max(1, bw - 1)} y={M.t - 4} height={2} fill="#ff3b5c" />}
              </g>
            );
          })}
          <rect className="brush" x={bs} y={M.t - 6} width={Math.max(2, be - bs)} height={ih + 8} />
          <rect className="handle" x={bs - 1.5} y={M.t + ih / 2 - 10} width={3} height={20} rx={1} />
          <rect className="handle" x={be - 1.5} y={M.t + ih / 2 - 10} width={3} height={20} rx={1} />
          <line className="now" x1={x(Date.now())} x2={x(Date.now())} y1={M.t - 6} y2={M.t + ih} />
        </svg>
      </div>
    </div>
  );
}
