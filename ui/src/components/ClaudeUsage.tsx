import { useEffect, useState } from "react";

/** How much Claude the newsroom uses (pipeline/wassup/newsroom/usage.py). */

interface Bucket { tokens: number; input: number; cached: number; output: number; runs: number }
interface Usage {
  connected: boolean;
  hourly: (Bucket & { hour: string })[];
  daily: (Bucket & { day: string })[];
  agents: { agent_name: string | null; day: number; week: number; month: number; day_output: number; day_runs: number }[];
  meters: { ok: boolean; error?: string; at: string; windows?: { label: string; usedPercent: number | null; resetsAt: string | null; valueLabel: string | null }[] } | null;
}

const fmt = (n: number) => (n >= 1e6 ? `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(n >= 1e4 ? 0 : 1)}k` : String(Math.round(n)));
const full = (n: number) => Math.round(n).toLocaleString();

function resets(s: string | null): string {
  if (!s) return "";
  const ms = Date.parse(s) - Date.now();
  if (ms <= 0) return "resets now";
  const h = Math.floor(ms / 3600e3), m = Math.round((ms % 3600e3) / 60e3);
  return h >= 24 ? `resets ${new Date(s).toLocaleDateString(undefined, { weekday: "short", hour: "numeric" })}` : `resets in ${h ? `${h} h ` : ""}${m} min`;
}

/** Bars over time, one series. Hover a bar to read it; the line above names the biggest bar. */
function Bars({ rows, label, describe }: { rows: Bucket[]; label: (i: number) => string; describe: string }) {
  const [hover, setHover] = useState<number | null>(null);
  const w = 372, h = 84, gap = 2;
  const max = Math.max(1, ...rows.map((r) => r.tokens));
  const bw = Math.max(1, (w - gap * (rows.length - 1)) / Math.max(1, rows.length));
  const peak = rows.reduce((best, r, i) => (r.tokens > rows[best].tokens ? i : best), 0);
  const shown = hover ?? null;
  return (
    <div className="cu-chart">
      <div className="cu-readout mono">
        {shown != null && rows[shown]
          ? <>{label(shown)} · <b>{full(rows[shown].tokens)}</b> tokens · {full(rows[shown].output)} written · {rows[shown].runs} run{rows[shown].runs === 1 ? "" : "s"}</>
          : rows.length ? <span className="dimmer">Peak {label(peak)}: {fmt(rows[peak].tokens)} tokens. Hover a bar.</span> : <span className="dimmer">No runs yet.</span>}
      </div>
      <svg width={w} height={h + 14} role="img" aria-label={describe} onMouseLeave={() => setHover(null)}>
        <text x={0} y={9} className="cu-axis">{fmt(max)}</text>
        <line x1={0} x2={w} y1={14} y2={14} className="cu-grid" />
        {rows.map((r, i) => {
          const x = i * (bw + gap);
          const bh = r.tokens ? Math.max(2, (r.tokens / max) * (h - 4)) : 0;
          const y = h + 14 - bh, rad = Math.min(4, bw / 2, bh);
          return (
            <g key={i} onMouseEnter={() => setHover(i)}>
              {/* A tall invisible target, so thin bars are easy to hover. */}
              <rect x={x} y={14} width={bw + gap} height={h} fill="transparent" />
              {bh > 0 && <path className={`cu-bar ${hover === i ? "on" : ""}`}
                d={`M${x},${h + 14} V${y + rad} Q${x},${y} ${x + rad},${y} H${x + bw - rad} Q${x + bw},${y} ${x + bw},${y + rad} V${h + 14} Z`} />}
            </g>
          );
        })}
        <line x1={0} x2={w} y1={h + 14} y2={h + 14} className="cu-base" />
      </svg>
    </div>
  );
}

export default function ClaudeUsage() {
  const [u, setU] = useState<Usage | null>(null);
  const [view, setView] = useState<"hours" | "days">("hours");
  const [table, setTable] = useState(false);
  useEffect(() => {
    const load = () => fetch("/api/newsroom/claude-usage?hours=48&days=30").then((r) => r.json()).then(setU).catch(() => undefined);
    load();
    const t = setInterval(load, 60_000);
    return () => clearInterval(t);
  }, []);
  if (!u || !u.connected) return null;

  // Fill empty hours so the bars line up with the clock.
  const now = new Date(); now.setMinutes(0, 0, 0);
  const byHour = new Map(u.hourly.map((r) => [new Date(r.hour).getTime(), r]));
  const zero = { tokens: 0, input: 0, cached: 0, output: 0, runs: 0 };
  const hours = Array.from({ length: 48 }, (_, i) => now.getTime() - (47 - i) * 3600e3);
  const hourly = hours.map((t) => byHour.get(t) ?? zero);
  const today = hourly.slice(-24).reduce((a, r) => a + r.tokens, 0);
  const week = u.agents.reduce((a, r) => a + r.week, 0);
  const rows = view === "hours" ? hourly : u.daily;
  const label = (i: number) => view === "hours"
    ? new Date(hours[i]).toLocaleString(undefined, { weekday: "short", hour: "numeric" })
    : new Date(`${u.daily[i].day}T12:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" });

  return (
    <div className="claude-usage">
      <div className="h">Claude usage</div>
      {u.meters?.ok && u.meters.windows?.length ? (
        <div className="cu-meters">
          {u.meters.windows.filter((m) => m.usedPercent != null).map((m) => (
            <div key={m.label} className="cu-meter">
              <div className="cu-meter-head mono"><span>{m.label}</span><span><b>{Math.round(m.usedPercent!)}%</b> used {m.resetsAt ? `· ${resets(m.resetsAt)}` : ""}</span></div>
              <div className="cu-track"><div className={`cu-fill ${m.usedPercent! >= 80 ? "warn" : ""}`} style={{ width: `${Math.min(100, m.usedPercent!)}%` }} /></div>
            </div>
          ))}
          <div className="dimmer mono cu-note">Your subscription's own limits, all Claude use on your account (not only Wassup).</div>
        </div>
      ) : (
        <div className="dimmer cu-note">
          Subscription meters unavailable{u.meters?.error ? `: ${u.meters.error}` : ""}. Token counts below still work. See docs/NEWSROOM.md.
        </div>
      )}
      <div className="cu-totals mono">
        <span><b>{fmt(today)}</b> tokens last 24 h</span>
        <span><b>{fmt(week)}</b> last 7 days</span>
        <span className="cu-toggle">
          <button className={`btn ${view === "hours" ? "on" : ""}`} onClick={() => setView("hours")}>HOURS</button>
          <button className={`btn ${view === "days" ? "on" : ""}`} onClick={() => setView("days")}>DAYS</button>
        </span>
      </div>
      <Bars rows={rows} label={label} describe={view === "hours" ? "Claude tokens per hour, last 48 hours" : "Claude tokens per day, last 30 days"} />
      <div className="cu-agents">
        <div className="cu-agent-row mono dimmer"><span>Agent</span><span>24 h</span><span>7 days</span><span>30 days</span></div>
        {u.agents.map((a) => (
          <div key={a.agent_name ?? "?"} className="cu-agent-row mono" title={`${full(a.day_output)} tokens written and ${a.day_runs} runs in the last 24 hours`}>
            <span>{a.agent_name ?? "?"}</span><span>{fmt(a.day)}</span><span>{fmt(a.week)}</span><span>{fmt(a.month)}</span>
          </div>
        ))}
        {!u.agents.length && <div className="dimmer">No Claude runs recorded yet.</div>}
      </div>
      <button className="linkish mono cu-note" onClick={() => setTable(!table)}>{table ? "hide the numbers" : "show the numbers"}</button>
      {table && (
        <table className="cu-table mono">
          <thead><tr><th>{view === "hours" ? "Hour" : "Day"}</th><th>New in</th><th>Cached in</th><th>Written</th><th>Runs</th></tr></thead>
          <tbody>
            {rows.map((r, i) => r.tokens ? (
              <tr key={i}><td>{label(i)}</td><td>{full(r.input)}</td><td>{full(r.cached)}</td><td>{full(r.output)}</td><td>{r.runs}</td></tr>
            ) : null).reverse()}
          </tbody>
        </table>
      )}
      <div className="dimmer cu-note">
        Tokens: what Claude reads (new or from its cache) and writes. Most of each run is cached reading,
        which counts for much less against your limits than writing does.
      </div>
    </div>
  );
}
