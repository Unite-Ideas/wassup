import { useMemo, useState } from "react";
import type { Desk, Item, StoryDetail } from "../lib/types";
import { EXCLUDED_LABEL, TIER_LABEL, ago, deskColor, languageName, stamp } from "../lib/format";
import { Markdown } from "../lib/markdown";
import LocationFix from "./LocationFix";

interface Props {
  story: StoryDetail | null;
  loading: boolean;
  desks: Map<string, Desk>;
  onClose: () => void;
  onSelectStory: (id: number) => void;
  onSelectPlace: (id: number) => void;
  onOpenBoard: (id: number) => void;
  onFeedback: (id: number, v: 1 | -1) => void;
  onChanged: () => void;
}

const TIER_ORDER = { A: 0, B: 1, U: 2, C: 3, S: 4 } as const;
type Tab = "links" | "sources" | "entities";

export function StoryCard({ s, desks, onClick, extra }: {
  s: { title: string; desk: string | null; significance: number; item_count: number; source_count: number; breaking: boolean; last_seen: string; routed: boolean };
  desks: Map<string, Desk>;
  onClick: () => void;
  extra?: React.ReactNode;
}) {
  return (
    <div className="story-card" onClick={onClick}>
      <div className="bar" style={{ background: deskColor(desks, s.desk) }} />
      <div>
        <div className="t">{s.title}</div>
        <div className="m">
          {extra}
          {s.breaking && <span className="chip breaking">Breaking</span>}
          {!s.routed && <span className="chip cold">Cold</span>}
          <span>SIG {s.significance.toFixed(1)}</span>
          <span>{s.item_count} art · {s.source_count} out</span>
          <span>{ago(s.last_seen)}</span>
        </div>
      </div>
    </div>
  );
}

function Codes({ list }: { list: string[] }) {
  return <>{list.map((k) => <code key={k} style={{ marginRight: 4 }}>{k}</code>)}</>;
}

function ItemRow({ it }: { it: Item & { copies: number } }) {
  return (
    <div className="item">
      <div className="item-head">
        <span className={`tier tier-${it.tier}`} title={TIER_LABEL[it.tier]}>{it.tier}</span>
        <span className="outlet">{it.outlet}</span>
        {it.state_media && <span className="chip state">State media</span>}
        {it.language && it.language !== "en" && <span className="chip">{languageName(it.language)}</span>}
        <span>{ago(it.published_at)}</span>
        {it.copies > 1 && <span title="Syndicated copies of the same article">×{it.copies}</span>}
      </div>
      <a className="t" href={it.url} target="_blank" rel="noreferrer noopener">{it.title}</a>
      {it.title_original && <div className="original">{it.title_original}</div>}
      {it.summary && <p>{it.summary}</p>}
    </div>
  );
}

export default function StoryPanel({ story, loading, desks, onClose, onSelectStory, onSelectPlace, onOpenBoard, onFeedback, onChanged }: Props) {
  const [tab, setTab] = useState<Tab>("links");
  const [hideState, setHideState] = useState(false);

  const items = useMemo(() => {
    if (!story) return [];
    const list = hideState ? story.items.filter((i) => !i.state_media) : story.items;
    // Syndicated copies (same outlet, same headline) collapse into one row with a count.
    const groups = new Map<string, Item & { copies: number }>();
    for (const it of list) {
      const key = `${it.outlet}|${it.title.toLowerCase()}`;
      const g = groups.get(key);
      if (g) g.copies += 1;
      else groups.set(key, { ...it, copies: 1 });
    }
    return [...groups.values()].sort((a, b) => TIER_ORDER[a.tier] - TIER_ORDER[b.tier] || b.published_at.localeCompare(a.published_at));
  }, [story, hideState]);

  if (!story) return <div className="empty">{loading ? "Loading story..." : "Story not found."}</div>;

  const desk = story.desk ? desks.get(story.desk) : undefined;
  const t = story.triage ?? {};
  const stateCount = story.items.filter((i) => i.state_media).length;
  const languages = [...new Set(story.items.map((i) => i.language).filter(Boolean))];

  return (
    <>
      <div className="detail-head">
        <button className="close" onClick={onClose} aria-label="Close">✕</button>
        <div className="crumbs">
          <span className="chip" style={{ color: deskColor(desks, story.desk), borderColor: deskColor(desks, story.desk) }}>
            {desk?.name ?? "No desk"}
          </span>
          {story.breaking && <span className="chip breaking">Breaking · {story.velocity}/hr</span>}
          {!story.routed && <span className="chip cold">Cold storage</span>}
          {stateCount > 0 && <span className="chip state">{stateCount} state media</span>}
        </div>
        <h2 className="title">{story.title}</h2>
        {story.title_original && story.title_original !== story.title && (
          <div className="original">Translated · original: {story.title_original}</div>
        )}
        <div className="dim mono" style={{ fontSize: 11, marginBottom: 10 }}>
          {stamp(story.first_seen)} to {stamp(story.last_seen)}
        </div>
        <div className="metrics">
          <div className="metric"><b>{story.significance.toFixed(1)}</b><span>Significance</span>
            <div className="sigbar"><i style={{ width: `${(story.significance / 5) * 100}%` }} /></div></div>
          <div className="metric"><b>{story.item_count}</b><span>Articles</span></div>
          <div className="metric"><b>{story.source_count}</b><span>Outlets</span></div>
          <div className="metric"><b>{story.country_count}</b><span>Countries</span></div>
        </div>
        <div className="actions">
          <button className="btn primary" onClick={() => onOpenBoard(story.id)}>⌗ EVIDENCE BOARD</button>
          <button className={`btn ${story.feedback === 1 ? "on-up" : ""}`} onClick={() => onFeedback(story.id, 1)} title="More like this">▲ MORE</button>
          <button className={`btn ${story.feedback === -1 ? "on-down" : ""}`} onClick={() => onFeedback(story.id, -1)} title="Less like this. Moves it to cold storage">▼ LESS</button>
        </div>
      </div>

      <div className="detail-body">
        <div className="sub why">
          <div className="h">Why it is here</div>
          {story.routed ? (
            <>Routed to <b style={{ color: deskColor(desks, story.desk) }}>{desk?.name}</b></>
          ) : (
            <>In cold storage: <b>{EXCLUDED_LABEL[story.excluded_reason ?? ""] ?? story.excluded_reason}</b></>
          )}
          {t.matched?.keywords?.length ? <> · matched <Codes list={t.matched.keywords.slice(0, 6)} /></> : null}
          {t.matched?.themes?.length ? <> · themes <Codes list={t.matched.themes.slice(0, 3)} /></> : null}
          {t.excluded && story.routed ? <> · overrides the <code>{t.excluded}</code> filter because it ties to a larger story</> : null}
          <div className="dimmer mono" style={{ fontSize: 10.5, marginTop: 6 }}>
            decided by {t.backend ?? "unknown"}{t.confidence != null ? ` · confidence ${(t.confidence * 100).toFixed(0)}%` : ""}
            {t.learned ? ` · your feedback ${t.learned > 0 ? "+" : ""}${t.learned}` : ""}
            {languages.length > 1 ? ` · ${languages.length} languages` : ""}
          </div>
        </div>

        {(story.briefs?.length > 0 || story.followers?.length > 0) && (
          <div className="sub">
            <div className="h">
              Newsroom
              {story.followers?.length > 0 && <span className="dimmer">followed by {story.followers.map((f) => f.agent_name ?? f.agent_key).join(", ")}</span>}
            </div>
            {story.briefs?.slice(0, 3).map((b) => (
              <div key={b.id} className="brief compact">
                <div className="brief-head mono"><span>{b.agent_name ?? b.agent_key}</span><span>{ago(b.created_at)}</span></div>
                <Markdown text={b.body} />
              </div>
            ))}
          </div>
        )}

        <LocationFix story={story} onSelectPlace={onSelectPlace} onChanged={onChanged} />

        <div className="tabs">
          <button className={tab === "links" ? "on" : ""} onClick={() => setTab("links")}>Connected<span>{story.links.length}</span></button>
          <button className={tab === "sources" ? "on" : ""} onClick={() => setTab("sources")}>Sources<span>{story.items.length}</span></button>
          <button className={tab === "entities" ? "on" : ""} onClick={() => setTab("entities")}>Actors<span>{story.entities.length}</span></button>
        </div>

        {tab === "links" && (
          story.links.length ? story.links.map((n) => (
            <StoryCard key={`${n.id}-${n.kind}`} s={n} desks={desks} onClick={() => onSelectStory(n.id)}
              extra={n.created_by?.startsWith("agent:") ? (
                <span className="link-kind agent" title={n.evidence?.reason}>{n.kind.replace("_", " ")}: {n.evidence?.reason}</span>
              ) : (
                <span className={`link-kind ${n.kind}`} title={n.evidence?.entities?.join(", ")}>
                  {n.kind === "same_actor" ? `actors: ${(n.evidence?.entities ?? []).slice(0, 2).join(", ")}` : `related ${Math.round((n.evidence?.similarity ?? n.weight) * 100)}%`}
                </span>
              )} />
          )) : <div className="empty">No connections found yet. Links are rebuilt every 10 minutes as coverage grows.</div>
        )}

        {tab === "sources" && (
          <>
            {stateCount > 0 && (
              <div className="sub">
                <div className="toggle-row" onClick={() => setHideState(!hideState)}>
                  <span>Hide state media ({stateCount})</span><span className={`switch ${hideState ? "on" : ""}`} />
                </div>
              </div>
            )}
            {items.map((it) => <ItemRow key={it.id} it={it} />)}
          </>
        )}

        {tab === "entities" && (
          story.entities.length ? (
            <div className="sub">
              <div className="h">People</div>
              <div className="chips" style={{ marginBottom: 14 }}>
                {story.entities.filter((e) => e.kind === "person").map((e) => <span key={e.id} className="chip">{e.name} · {e.mentions}</span>)}
              </div>
              <div className="h">Organizations</div>
              <div className="chips">
                {story.entities.filter((e) => e.kind === "org").map((e) => <span key={e.id} className="chip">{e.name} · {e.mentions}</span>)}
              </div>
            </div>
          ) : <div className="empty">No people or organizations extracted for this story yet.</div>
        )}
      </div>
    </>
  );
}
