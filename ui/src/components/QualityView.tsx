import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import type { QualityData } from "../lib/types";
import { ago } from "../lib/format";

const ASPECT: Record<string, { name: string; what: string }> = {
  desk: { name: "Desk", what: "Is the story on the right desk?" },
  missed: { name: "Missed stories", what: "Should this story in cold storage be on a desk?" },
  place: { name: "Place", what: "Is the story's main place right?" },
  grouping: { name: "Grouping", what: "Do all these articles belong in one story?" },
  importance: { name: "Importance", what: "Is the story ranked about right?" },
  link: { name: "Connections", what: "Is this connection between two stories real?" },
};

const pct = (wrong: number, checked: number, unsure: number) => {
  const judged = checked - unsure;
  return judged > 0 ? Math.round((wrong / judged) * 100) : null;
};

function Trend({ points }: { points: (number | null)[] }) {
  const vals = points.filter((p): p is number => p != null);
  if (vals.length < 2) return <span className="dimmer">not enough audits yet</span>;
  const w = 120, h = 26, max = Math.max(20, ...vals);
  const xs = points.map((_, i) => (i / Math.max(1, points.length - 1)) * w);
  const d = points.map((p, i) => (p == null ? null : `${xs[i].toFixed(1)},${(h - 2 - (p / max) * (h - 4)).toFixed(1)}`)).filter(Boolean);
  return (
    <svg width={w} height={h} className="q-trend" aria-label="Error rate per audit, oldest first">
      <polyline points={d.join(" ")} fill="none" stroke="currentColor" strokeWidth="1.4" />
    </svg>
  );
}

export default function QualityView({ onSelectStory }: { onSelectStory: (id: number) => void }) {
  const [data, setData] = useState<QualityData | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const load = useCallback(() => { api.quality().then(setData).catch((e) => setMsg(String(e))); }, []);
  useEffect(() => { load(); const t = setInterval(load, 30_000); return () => clearInterval(t); }, [load]);

  const rows = useMemo(() => {
    if (!data) return [];
    return data.aspects.map((a) => {
      const series = data.audits.map((au) => au.aspects[a]);
      const last = [...data.audits].reverse().find((au) => au.aspects[a]?.checked);
      const l = last?.aspects[a];
      const total = series.reduce((acc, s) => ({ c: acc.c + (s?.checked ?? 0), w: acc.w + (s?.wrong ?? 0), u: acc.u + (s?.unsure ?? 0) }), { c: 0, w: 0, u: 0 });
      return {
        aspect: a, last: l ? pct(l.wrong, l.checked, l.unsure) : null, lastChecked: l?.checked ?? 0,
        overall: pct(total.w, total.c, total.u), checked: total.c,
        trend: series.map((s) => (s ? pct(s.wrong, s.checked, s.unsure) : null)), reports: data.reports[a] ?? 0,
      };
    });
  }, [data]);

  if (!data) return <div className="quality-view"><div className="empty">{msg ?? "Loading..."}</div></div>;
  const lastAudit = data.audits[data.audits.length - 1];

  return (
    <div className="quality-view">
      <div className="src-head">
        <div>
          <h2>How accurate is Wassup?</h2>
          <div className="dim">
            Every night a random sample of the last day's decisions is checked by {data.standards_editor ? "the Standards Editor (Claude)" : "the local model (hire the Standards Editor with wassup newsroom setup for a stronger judge)"}.
            Mistakes it finds are fixed on the spot. The error rate counts wrong answers among those it could judge.
            Your own fixes from the story panel are counted under "your reports".
          </div>
        </div>
        <div className="src-add">
          <button className="btn primary" onClick={() => api.auditNow().then(() => setMsg("An audit starts within two minutes.")).catch((e) => setMsg(String(e)))}>RUN AN AUDIT NOW</button>
        </div>
      </div>
      {msg && <div className="src-msg">{msg}</div>}
      <div className="dim mono q-status">
        {data.open ? `Audit #${data.open.id} in progress (${data.open.judge === "claude" ? "Standards Editor" : "local model"}), ${data.open.left} checks left. ` : ""}
        {lastAudit ? `Last audit #${lastAudit.id} ${ago(lastAudit.created_at)}, judged by ${lastAudit.judge === "claude" ? "the Standards Editor" : "the local model"}.` : "No audit yet: the first runs within two minutes of starting Wassup."}
      </div>
      <table className="q-table">
        <thead><tr><th>What is checked</th><th>Last audit</th><th>Last 30 days</th><th>Trend</th><th>Your reports</th></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.aspect}>
              <td><b>{ASPECT[r.aspect]?.name ?? r.aspect}</b><div className="dimmer">{ASPECT[r.aspect]?.what}</div></td>
              <td className={`mono ${r.last == null ? "dimmer" : r.last >= 20 ? "q-bad" : r.last >= 10 ? "q-warn" : "q-ok"}`}>
                {r.last == null ? "–" : `${r.last}% wrong`}<div className="dimmer">{r.lastChecked} checked</div>
              </td>
              <td className="mono">{r.overall == null ? "–" : `${r.overall}%`}<div className="dimmer">{r.checked} checked</div></td>
              <td><Trend points={r.trend} /></td>
              <td className="mono">{r.reports || "–"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="src-group">
        <div className="h">Mistakes found, newest first <span className="dimmer">{data.mistakes.length}</span></div>
        {data.mistakes.length ? data.mistakes.map((m) => (
          <div key={m.id} className="q-mistake">
            <div className="mono dim">
              {ASPECT[m.aspect]?.name ?? m.aspect} · {m.by === "you" ? "you" : m.by === "claude" ? "Standards Editor" : "local model"} · {ago(m.judged_at)}
              {m.fixed && <span className="chip" style={{ marginLeft: 6 }}>fixed</span>}
            </div>
            {m.story_id ? <button className="linkish q-title" onClick={() => onSelectStory(m.story_id!)}>{m.story_title ?? m.subject?.title}</button>
              : <div className="q-title">{m.subject?.title}</div>}
            <div className="dim">
              {m.aspect === "desk" && <>was on <b>{m.subject?.desk}</b>; belongs on <b>{m.answer || "no desk"}</b></>}
              {m.aspect === "missed" && <>was left out; belongs on <b>{m.answer}</b></>}
              {m.aspect === "place" && <>was placed at <b>{m.subject?.place ?? "?"}</b>; belongs at <b>{m.answer}</b></>}
              {m.aspect === "grouping" && <>articles that did not belong: <b>{m.answer}</b></>}
              {m.aspect === "importance" && <>ranked <b>{m.subject?.band ?? m.subject?.significance}</b>; <b>{(m.answer ?? "").replace("_", " ")}</b></>}
              {m.aspect === "link" && <>wrong connection: {m.subject?.a_title} / {m.subject?.b_title}</>}
              {m.note && <> · {m.note}</>}
            </div>
          </div>
        )) : <div className="empty">None yet.</div>}
      </div>
    </div>
  );
}
