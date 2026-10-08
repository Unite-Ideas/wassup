import { useCallback, useEffect, useState } from "react";
import { api } from "../lib/api";
import type { Desk, SocialAccount } from "../lib/types";
import { ago, deskColor } from "../lib/format";

interface Props {
  desks: Map<string, Desk>;
}

const k = (n: number | null | undefined) => (n == null ? "?" : n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}K` : String(n));
const pct = (a?: number, b?: number) => (b ? `${Math.round(((a ?? 0) / b) * 100)}%` : "–");

function Row({ a, desks, onAct }: { a: SocialAccount; desks: Map<string, Desk>; onAct: (a: SocialAccount, action: string) => void }) {
  const st = a.stats ?? {};
  return (
    <div className={`src-row ${a.status}`}>
      <div className="src-main">
        <div className="src-name">
          <a href={`https://t.me/${a.handle}`} target="_blank" rel="noreferrer noopener">{a.name ?? a.handle}</a>
          <span className="dimmer mono"> @{a.handle}</span>
          {a.pinned && <span className="chip">pinned</span>}
          {a.via === "api" && <span className="chip live" title="Read through the logged in Telegram account: posts arrive as they are published">live</span>}
          {a.kind && <span className={`chip ${a.kind === "state" ? "state" : ""}`}>{a.kind === "state" ? "state media" : a.kind}</span>}
          {a.lean && <span className="chip">{a.lean} side</span>}
          {a.desk && <span className="chip" style={{ color: deskColor(desks, a.desk), borderColor: deskColor(desks, a.desk) }}>{desks.get(a.desk)?.name ?? a.desk}</span>}
        </div>
        <div className="src-reason dim">{a.last_error ? `Last check failed: ${a.last_error}` : a.status_reason ?? (a.added_by === "seed" ? "Starting list." : "")}</div>
      </div>
      <div className="src-score" title="0 to 100: corroboration 35, earliness 35, relevance 20, originality 10">
        <b>{a.score == null ? "–" : Math.round(a.score)}</b>
        <div className="sigbar"><i style={{ width: `${a.score ?? 0}%` }} /></div>
      </div>
      <div className="src-stats mono">
        <span title="Posts in the last 30 days">{st.posts ?? 0} posts</span>
        <span title="Posts that two or more independent outlets also reported within 48 hours">{pct(st.corroborated, st.evaluable)} confirmed</span>
        <span title="Confirmed posts that came at least 10 minutes before the first outlet; median lead">
          {st.early ?? 0} early{st.median_lead_min ? `, ${Math.round(st.median_lead_min)} min ahead` : ""}
        </span>
        <span title="Posts on stories a desk tracks">{pct(st.routed, st.posts)} relevant</span>
        <span>{k(a.subscribers)} subs</span>
        <span>{a.last_post_at ? `posted ${ago(a.last_post_at)}` : "no posts yet"}</span>
      </div>
      <div className="src-actions">
        {a.status !== "following" && !a.banned && <button className="btn" onClick={() => onAct(a, "follow")}>FOLLOW</button>}
        {a.status === "following" && <button className="btn" onClick={() => onAct(a, "pause")}>PAUSE</button>}
        {a.pinned ? <button className="btn" onClick={() => onAct(a, "unpin")}>UNPIN</button> : !a.banned && <button className="btn" onClick={() => onAct(a, "pin")}>PIN</button>}
        {a.banned ? <button className="btn" onClick={() => onAct(a, "unban")}>UNBAN</button> : <button className="btn" onClick={() => onAct(a, "ban")}>BAN</button>}
      </div>
    </div>
  );
}

export default function SourcesView({ desks }: Props) {
  const [list, setList] = useState<SocialAccount[]>([]);
  const [handle, setHandle] = useState("");
  const [desk, setDesk] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [showOld, setShowOld] = useState(false);

  const load = useCallback(() => { api.socialAccounts().then(setList).catch((e) => setMsg(String(e))); }, []);
  useEffect(() => { load(); const t = setInterval(load, 60_000); return () => clearInterval(t); }, [load]);

  const act = (a: SocialAccount, action: string) => api.socialAct(a.id, action).then(load).catch((e) => setMsg(String(e)));
  const add = () => {
    if (!handle.trim()) return;
    api.socialAdd(handle.trim(), desk || undefined).then(() => { setHandle(""); setMsg("Added. Its posts appear within a few minutes."); load(); })
      .catch((e) => setMsg(String(e.message ?? e)));
  };

  const groups: [string, SocialAccount[]][] = [
    ["Following", list.filter((a) => a.status === "following")],
    ["Candidates: found by the scout, being watched", list.filter((a) => a.status === "candidate")],
  ];
  const old = list.filter((a) => a.status === "paused" || a.status === "removed");

  return (
    <div className="sources-view">
      <div className="src-head">
        <div>
          <h2>Telegram channels</h2>
          <div className="dim">
            Every hour the scout scores each channel on its last 30 days: how often independent outlets confirm what it posts,
            how often it is first and by how much, and how much of it lands on stories your desks track. Channels the followed ones keep
            forwarding are tried as candidates; good ones are followed, poor ones dropped. Pin to keep a channel whatever its score; ban to never see it.
          </div>
        </div>
        <div className="src-add">
          <input value={handle} onChange={(e) => setHandle(e.target.value)} onKeyDown={(e) => e.key === "Enter" && add()} placeholder="@channel or t.me link" />
          <select value={desk} onChange={(e) => setDesk(e.target.value)}>
            <option value="">any desk</option>
            {[...desks.values()].map((d) => <option key={d.key} value={d.key}>{d.name}</option>)}
          </select>
          <button className="btn primary" onClick={add}>FOLLOW</button>
        </div>
      </div>
      {msg && <div className="src-msg">{msg}</div>}
      {groups.map(([title, rows]) => (
        <div key={title} className="src-group">
          <div className="h">{title} <span className="dimmer">{rows.length}</span></div>
          {rows.length ? rows.map((a) => <Row key={a.id} a={a} desks={desks} onAct={act} />) : <div className="empty">None yet.</div>}
        </div>
      ))}
      <div className="src-group">
        <button className="linkish h" onClick={() => setShowOld(!showOld)}>{showOld ? "▾" : "▸"} Paused, dropped and banned <span className="dimmer">{old.length}</span></button>
        {showOld && old.map((a) => <Row key={a.id} a={a} desks={desks} onAct={act} />)}
      </div>
    </div>
  );
}
