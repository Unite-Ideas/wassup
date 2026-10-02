import { useCallback, useEffect, useState } from "react";
import { api } from "../lib/api";
import type { Brief, Desk, NewsroomAgent, NewsroomEvent, NewsroomStatus } from "../lib/types";
import { ago, deskColor, stamp } from "../lib/format";
import { Markdown } from "../lib/markdown";

interface Props {
  desks: Map<string, Desk>;
  onSelectStory: (id: number) => void;
}

const EVENT_LABEL: Record<string, string> = {
  run: "check in", brief: "brief", link: "string", follow: "follow", surge_hired: "surge hired",
  surge_retired: "surge retired", escalation: "escalated", standup: "standup", error: "error",
};

function AgentCard({ a, desks, onSelectStory }: { a: NewsroomAgent; desks: Map<string, Desk>; onSelectStory: (id: number) => void }) {
  const color = a.kind === "eic" ? "#ffffff" : a.kind === "surge" ? "#ff3b5c" : deskColor(desks, a.desk);
  const stale = a.last_run_at && Date.now() - new Date(a.last_run_at).getTime() > 3 * 3600e3;
  return (
    <div className={`agent-card ${a.status === "retired" ? "retired" : ""}`} style={{ borderTopColor: color }}>
      <div className="agent-head">
        <span className="agent-name">{a.name}</span>
        <span className={`chip ${a.kind === "surge" ? "breaking" : ""}`}>{a.kind === "eic" ? "editor" : a.kind}</span>
      </div>
      <div className="agent-meta mono">
        {a.status === "retired" ? `retired ${ago(a.retired_at!)}` : a.last_run_at ? `last check in ${ago(a.last_run_at)}` : "not run yet"}
        {stale && a.status === "active" ? " · overdue" : ""}
        {` · ${a.briefs_24h} briefs today · following ${a.following}`}
      </div>
      {a.focus_story_id && (
        <div className="agent-focus" onClick={() => onSelectStory(a.focus_story_id!)}>Tracking: {a.focus_title}</div>
      )}
      {a.last_summary && <div className="agent-summary">{a.last_summary}</div>}
    </div>
  );
}

function BriefBlock({ b, onSelectStory }: { b: Brief; onSelectStory: (id: number) => void }) {
  return (
    <div className="brief">
      <div className="brief-head mono">
        <span>{b.agent_name ?? b.agent_key}</span>
        <span>{stamp(b.created_at)}</span>
      </div>
      {b.title && (
        <div className={`brief-title ${b.story_id ? "click" : ""}`} onClick={() => b.story_id && onSelectStory(b.story_id)}>{b.title}</div>
      )}
      <Markdown text={b.body} />
    </div>
  );
}

export default function NewsroomView({ desks, onSelectStory }: Props) {
  const [status, setStatus] = useState<NewsroomStatus | null>(null);
  const [agents, setAgents] = useState<NewsroomAgent[]>([]);
  const [events, setEvents] = useState<NewsroomEvent[]>([]);
  const [daily, setDaily] = useState<Brief | null>(null);
  const [standup, setStandup] = useState<Brief | null>(null);
  const [briefs, setBriefs] = useState<Brief[]>([]);
  const [calling, setCalling] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const load = useCallback(() => {
    api.newsroomStatus().then(setStatus).catch(() => setStatus(null));
    api.newsroomAgents().then(setAgents).catch(() => undefined);
    api.newsroomEvents(60).then(setEvents).catch(() => undefined);
    api.briefs({ hours: 72, kind: "daily", limit: 1 }).then((b) => setDaily(b[0] ?? null)).catch(() => undefined);
    api.briefs({ hours: 72, kind: "standup", limit: 1 }).then((b) => setStandup(b[0] ?? null)).catch(() => undefined);
    api.briefs({ hours: 24, kind: "story", limit: 30 }).then(setBriefs).catch(() => undefined);
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 20_000);
    return () => clearInterval(t);
  }, [load]);

  const standupNow = async () => {
    setCalling(true);
    try {
      const r = await api.callStandup();
      setNote(r.already_open ? "A standup is already collecting reports." : `Standup #${r.standup_id} called: ${r.desks_woken} desks are reporting.`);
      load();
    } catch (e) {
      setNote(String(e));
    } finally {
      setCalling(false);
    }
  };

  if (status && !status.set_up) {
    return (
      <div className="newsroom">
        <div className="nr-empty">
          <div className="mono" style={{ letterSpacing: ".2em", fontSize: 11 }}>NEWSROOM NOT SET UP</div>
          <p>The newsroom runs in Paperclip. Connect Wassup to it once, then build the newsroom:</p>
          <pre>docker compose exec app wassup newsroom connect{"\n"}docker compose exec app wassup newsroom setup</pre>
          <p className="dimmer">See docs/NEWSROOM.md for the full walkthrough.</p>
        </div>
      </div>
    );
  }

  const active = agents.filter((a) => a.status === "active");
  const retired = agents.filter((a) => a.status === "retired");
  const surges = active.filter((a) => a.kind === "surge");

  return (
    <div className="newsroom">
      <div className="nr-col nr-main">
        <div className="nr-bar">
          <div>
            <div className="h" style={{ margin: 0 }}>Newsroom</div>
            <div className="dim mono" style={{ fontSize: 11 }}>
              {active.length} agents on duty{surges.length ? ` · ${surges.length} surge` : ""}
              {status && <> · <a href={status.paperclip_url} target="_blank" rel="noreferrer">open Paperclip</a></>}
            </div>
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            {note && <span className="dim" style={{ fontSize: 12 }}>{note}</span>}
            <button className="btn primary" disabled={calling} onClick={standupNow}>{calling ? "CALLING..." : "CALL STANDUP"}</button>
          </div>
        </div>
        <section className="nr-section">
          <div className="h">Daily brief {daily && <span className="dimmer">{stamp(daily.created_at)}</span>}</div>
          {daily ? <Markdown text={daily.body} /> : <div className="dimmer">The Editor in Chief writes the first one at the next scheduled time.</div>}
        </section>
        {standup && (
          <section className="nr-section">
            <div className="h">Latest standup <span className="dimmer">{stamp(standup.created_at)}</span></div>
            <Markdown text={standup.body} />
          </section>
        )}
        <section className="nr-section">
          <div className="h">Briefs, last 24 hours</div>
          {briefs.length ? briefs.map((b) => <BriefBlock key={b.id} b={b} onSelectStory={onSelectStory} />)
            : <div className="dimmer">No briefs yet. Desks check in every hour.</div>}
        </section>
      </div>
      <div className="nr-col nr-side">
        <div className="h">On duty</div>
        <div className="agent-grid">
          {active.map((a) => <AgentCard key={a.key} a={a} desks={desks} onSelectStory={onSelectStory} />)}
        </div>
        {retired.length > 0 && (
          <>
            <div className="h" style={{ marginTop: 16 }}>Recently retired</div>
            <div className="agent-grid">{retired.map((a) => <AgentCard key={a.key} a={a} desks={desks} onSelectStory={onSelectStory} />)}</div>
          </>
        )}
        <div className="h" style={{ marginTop: 16 }}>Activity</div>
        <div className="feed">
          {events.map((e) => (
            <div key={e.id} className={`feed-row ${e.story_id ? "click" : ""} ${e.kind}`} onClick={() => e.story_id && onSelectStory(e.story_id)}>
              <span className="mono dimmer">{ago(e.created_at)}</span>
              <span className={`feed-kind ${e.kind}`}>{EVENT_LABEL[e.kind] ?? e.kind}</span>
              <span>{e.text}</span>
            </div>
          ))}
          {!events.length && <div className="dimmer">Nothing yet.</div>}
        </div>
      </div>
    </div>
  );
}
