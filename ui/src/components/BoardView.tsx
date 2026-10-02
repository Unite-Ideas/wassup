import { useEffect, useRef, useState } from "react";
import ForceGraph from "force-graph";
import { api } from "../lib/api";
import type { Desk, GraphData, GraphEdge, GraphNode } from "../lib/types";
import { deskColor, hexToRgba } from "../lib/format";

interface Props {
  rootId: number | null;
  selectedId: number | null;
  desks: Map<string, Desk>;
  onSelectStory: (id: number) => void;
  onReRoot: (id: number) => void;
}

const CARD_W = 74;
// Relations the newsroom's agents draw; rule based links are "related" and "same_actor".
const AGENT_KINDS = new Set(["causes", "responds_to", "escalates", "part_of", "contradicts", "parallels"]);
const FONT = 3.6;
const LINE_H = 4.6;
const PAD = 4;

function wrap(ctx: CanvasRenderingContext2D, text: string, width: number, maxLines: number): string[] {
  const words = text.split(/\s+/);
  const lines: string[] = [];
  let line = "";
  for (const w of words) {
    const test = line ? `${line} ${w}` : w;
    if (ctx.measureText(test).width > width && line) {
      lines.push(line);
      line = w;
      if (lines.length === maxLines) break;
    } else {
      line = test;
    }
  }
  if (lines.length < maxLines && line) lines.push(line);
  if (lines.length === maxLines && words.join(" ").length > lines.join(" ").length) {
    lines[maxLines - 1] = lines[maxLines - 1].replace(/\s*\S*$/, "") + "...";
  }
  return lines;
}

function nodeId(x: string | GraphNode): string {
  return typeof x === "string" ? x : x.id;
}

export default function BoardView({ rootId, selectedId, desks, onSelectStory, onReRoot }: Props) {
  const el = useRef<HTMLDivElement>(null);
  const graph = useRef<ForceGraph<GraphNode, GraphEdge> | null>(null);
  const [data, setData] = useState<GraphData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const state = useRef({ selectedId, desks, onSelectStory, onReRoot, hover: null as string | null, lastClick: { id: "", t: 0 } });
  state.current = { ...state.current, selectedId, desks, onSelectStory, onReRoot };

  useEffect(() => {
    if (rootId == null) {
      setData(null);
      return;
    }
    let cancel = false;
    setError(null);
    api.graph(rootId, 2).then((d) => !cancel && setData(d)).catch((e) => !cancel && setError(String(e)));
    return () => {
      cancel = true;
    };
  }, [rootId]);

  useEffect(() => {
    if (!el.current) return;
    const g = new ForceGraph<GraphNode, GraphEdge>(el.current)
      .backgroundColor("rgba(0,0,0,0)")
      .nodeId("id")
      .linkSource("source")
      .linkTarget("target")
      .cooldownTicks(160)
      .linkCurvature((l) => (l.kind === "mentions" ? 0.08 : 0.18))
      .linkColor((l) => {
        const hover = state.current.hover;
        const touch = hover && (nodeId(l.source) === hover || nodeId(l.target) === hover);
        if (AGENT_KINDS.has(l.kind)) return touch ? "rgba(255,214,90,1)" : "rgba(255,196,64,0.8)";
        if (l.kind === "related") return touch ? "rgba(0,229,255,0.9)" : "rgba(0,229,255,0.35)";
        return touch ? "rgba(255,59,92,0.95)" : "rgba(220,40,60,0.55)";
      })
      .linkWidth((l) => (l.kind === "mentions" ? 0.8 : AGENT_KINDS.has(l.kind) ? 2.6 : 0.8 + l.weight * 1.6))
      .linkLabel((l) => (AGENT_KINDS.has(l.kind) ? `${l.kind.replace("_", " ")}: ${l.evidence?.reason ?? ""}` : ""))
      .linkLineDash((l) => (l.kind === "related" ? [3, 2] : null))
      .nodeCanvasObject((n, ctx, scale) => {
        const s = state.current;
        if (n.type === "entity") {
          const r = 4 + Math.min(4, (n.stories ?? 1) * 0.8);
          ctx.beginPath();
          ctx.arc(n.x!, n.y!, r, 0, 2 * Math.PI);
          ctx.fillStyle = n.kind === "person" ? "#2a1418" : "#14202a";
          ctx.fill();
          ctx.lineWidth = 0.8;
          ctx.strokeStyle = n.kind === "person" ? "#ff3b5c" : "#00e5ff";
          ctx.stroke();
          ctx.fillStyle = n.kind === "person" ? "#ff8095" : "#7aefff";
          ctx.font = `600 ${r}px IBM Plex Mono, monospace`;
          ctx.textAlign = "center";
          ctx.textBaseline = "middle";
          ctx.fillText(n.kind === "person" ? "P" : "O", n.x!, n.y! + 0.3);
          if (scale > 0.9 || s.hover === n.id) {
            ctx.font = `500 ${3.4}px Inter, sans-serif`;
            ctx.fillStyle = "rgba(215,227,239,0.9)";
            ctx.fillText(n.name ?? "", n.x!, n.y! + r + 4);
          }
          return;
        }
        const color = deskColor(s.desks, n.desk);
        const root = n.depth === 0;
        const selected = Number(n.id.slice(1)) === s.selectedId;
        ctx.font = `${root ? 600 : 500} ${FONT}px Inter, sans-serif`;
        const lines = wrap(ctx, n.title ?? "", CARD_W - PAD * 2, root ? 5 : 4);
        const h = PAD * 2 + 5 + lines.length * LINE_H + 5;
        const x = n.x! - CARD_W / 2;
        const y = n.y! - h / 2;
        ctx.save();
        ctx.shadowColor = n.breaking ? "rgba(255,59,92,0.55)" : "rgba(0,0,0,0.6)";
        ctx.shadowBlur = n.breaking ? 12 : 8;
        ctx.fillStyle = root ? "#132233" : "#0e1822";
        ctx.fillRect(x, y, CARD_W, h);
        ctx.restore();
        ctx.fillStyle = color;
        ctx.fillRect(x, y, CARD_W, 1.6);
        ctx.lineWidth = selected || root ? 0.9 : 0.4;
        ctx.strokeStyle = selected ? "#ffffff" : root ? "#00e5ff" : hexToRgba(color, 0.45);
        ctx.strokeRect(x, y, CARD_W, h);
        // The pin
        ctx.beginPath();
        ctx.arc(n.x!, y + 0.4, 1.7, 0, 2 * Math.PI);
        ctx.fillStyle = "#ff3b5c";
        ctx.fill();
        ctx.fillStyle = hexToRgba(color, 0.95);
        ctx.font = `600 2.6px IBM Plex Mono, monospace`;
        ctx.textAlign = "left";
        ctx.textBaseline = "top";
        const label = `${(s.desks.get(n.desk ?? "")?.name ?? "UNASSIGNED").toUpperCase()}${n.breaking ? "  ·  BREAKING" : ""}`;
        ctx.fillText(label, x + PAD, y + PAD);
        ctx.fillStyle = "#d7e3ef";
        ctx.font = `${root ? 600 : 500} ${FONT}px Inter, sans-serif`;
        lines.forEach((ln, i) => ctx.fillText(ln, x + PAD, y + PAD + 5 + i * LINE_H));
        ctx.fillStyle = "#5b6f84";
        ctx.font = `500 2.5px IBM Plex Mono, monospace`;
        ctx.fillText(`${n.item_count} ARTICLES · ${n.source_count} OUTLETS · SIG ${(n.significance ?? 0).toFixed(1)}`, x + PAD, y + h - PAD - 2.2);
        (n as GraphNode & { __h?: number }).__h = h;
      })
      .nodePointerAreaPaint((n, color, ctx) => {
        ctx.fillStyle = color;
        if (n.type === "entity") {
          ctx.beginPath();
          ctx.arc(n.x!, n.y!, 8, 0, 2 * Math.PI);
          ctx.fill();
        } else {
          const h = (n as GraphNode & { __h?: number }).__h ?? 30;
          ctx.fillRect(n.x! - CARD_W / 2, n.y! - h / 2, CARD_W, h);
        }
      })
      .onNodeHover((n) => {
        state.current.hover = n?.id ?? null;
        if (el.current) el.current.style.cursor = n ? "pointer" : "grab";
      })
      .onNodeClick((n) => {
        if (n.type !== "story") return;
        const id = Number(n.id.slice(1));
        const now = Date.now();
        const last = state.current.lastClick;
        if (last.id === n.id && now - last.t < 350) state.current.onReRoot(id);
        else state.current.onSelectStory(id);
        state.current.lastClick = { id: n.id, t: now };
      })
      .onNodeDragEnd((n) => {
        n.fx = n.x;
        n.fy = n.y;
      })
      .onRenderFramePre((ctx, scale) => {
        // Faint board grid
        const { width, height } = ctx.canvas;
        ctx.save();
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.strokeStyle = "rgba(0,229,255,0.035)";
        ctx.lineWidth = 1;
        const step = Math.max(24, 40 * scale);
        for (let x = 0; x < width; x += step) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, height); ctx.stroke(); }
        for (let y = 0; y < height; y += step) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(width, y); ctx.stroke(); }
        ctx.restore();
      });
    g.d3Force("charge")?.strength?.(-420);
    (g.d3Force("link") as unknown as { distance?: (fn: (l: GraphEdge) => number) => void })?.distance?.((l) => (l.kind === "mentions" ? 55 : 120));
    const ro = new ResizeObserver(([e]) => g.width(e.contentRect.width).height(e.contentRect.height));
    ro.observe(el.current);
    graph.current = g;
    const node = el.current;
    return () => {
      ro.disconnect();
      g._destructor();
      node.innerHTML = "";
      graph.current = null;
    };
  }, []);

  useEffect(() => {
    const g = graph.current;
    if (!g) return;
    if (!data) {
      g.graphData({ nodes: [], links: [] });
      return;
    }
    const nodes = data.nodes.map((n) => ({ ...n }));
    const root = nodes.find((n) => n.id === data.root);
    if (root) {
      root.fx = 0;
      root.fy = 0;
    }
    g.graphData({ nodes, links: data.edges.map((e) => ({ ...e })) });
    setTimeout(() => g.zoomToFit(600, 60), 900);
  }, [data]);

  return (
    <>
      <div ref={el} style={{ position: "absolute", inset: 0 }} />
      {rootId == null && (
        <div className="board-empty">
          <div>
            <div className="mono" style={{ letterSpacing: ".2em", fontSize: 11 }}>EVIDENCE BOARD</div>
            <p>Select a story, then choose "Evidence board" to pin it here with everything connected to it.</p>
          </div>
        </div>
      )}
      {error && <div className="error-banner">{error}</div>}
      {data && (
        <div className="board-hint">
          {data.nodes.filter((n) => n.type === "story").length} stories · {data.nodes.filter((n) => n.type === "entity").length} people and orgs
          <br />click a card to read it · double click to re-center · drag to pin
          <div style={{ marginTop: 6 }}>
            <span style={{ color: "#ffc440" }}>━━</span> drawn by the newsroom &nbsp; <span style={{ color: "#ff3b5c" }}>━━</span> shared actors &nbsp; <span style={{ color: "#00e5ff" }}>╍╍</span> related coverage
          </div>
        </div>
      )}
    </>
  );
}
