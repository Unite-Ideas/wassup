import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import type { Desk, SearchResult, Stats, View } from "../lib/types";
import { StoryCard } from "./StoryPanel";

interface Props {
  stats: Stats | null;
  view: View;
  onView: (v: View) => void;
  desks: Map<string, Desk>;
  onSelectStory: (id: number) => void;
}

export default function TopBar({ stats, view, onView, desks, onSelectStory }: Props) {
  const [q, setQ] = useState("");
  const [results, setResults] = useState<SearchResult | null>(null);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (q.trim().length < 3) {
      setResults(null);
      return;
    }
    const t = setTimeout(() => {
      api.search(q.trim()).then(setResults).catch(() => setResults({ words: [], matches: [], fuzzy: false, related: [] }));
    }, 300);
    return () => clearTimeout(t);
  }, [q]);

  useEffect(() => {
    const close = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setResults(null); };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  const k = (n: number | undefined) => (n == null ? "..." : n >= 10000 ? `${(n / 1000).toFixed(1)}k` : n.toLocaleString());

  return (
    <header className="top">
      <div className="brand"><span className="brand-mark" />WASSUP <small>GLOBAL WATCH</small></div>
      <div className="kpis">
        <div className="kpi"><b>{k(stats?.items_last_hour)}</b><span>Articles / hr</span></div>
        <div className="kpi"><b>{k(stats?.routed)}</b><span>Tracked</span></div>
        <div className="kpi"><b>{k(stats?.cold)}</b><span>Cold</span></div>
        <div className={`kpi ${stats?.breaking ? "alert" : ""}`}><b>{k(stats?.breaking)}</b><span>Breaking</span></div>
        <div className="kpi"><b>{k(stats?.links)}</b><span>Links</span></div>
        <div className="kpi"><b>{k(stats?.languages)}</b><span>Languages</span></div>
        {stats?.jev?.configured && (
          <div className="kpi" title={`Jev spend today, of a $${stats.jev.daily_budget_usd.toFixed(2)} daily cap. $${stats.jev.spent_total_usd.toFixed(2)} all time.`}>
            <b>${stats.jev.spent_today_usd.toFixed(stats.jev.spent_today_usd < 1 ? 3 : 2)}</b><span>Jev today</span>
          </div>
        )}
        <div className={`kpi ${stats?.sources_failing ? "alert" : ""}`} title="Sources failing / total. See /api/sources for details.">
          <b>{stats ? `${stats.sources - stats.sources_failing}/${stats.sources}` : "..."}</b><span>Sources up</span>
        </div>
      </div>
      <div className="spacer" />
      <div className="search" ref={box}>
        <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8"><circle cx="7" cy="7" r="5" /><path d="M11 11l3.5 3.5" /></svg>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search all articles, last 30 days" />
        {results && (
          <div className="search-results">
            {results.matches.filter((s) => !s.close_spelling).map((s) => (
              <StoryCard key={s.id} s={s} desks={desks} onClick={() => { onSelectStory(s.id); setResults(null); }} />
            ))}
            {results.fuzzy && (
              <div className="search-group">Close spellings{results.matches.some((s) => !s.close_spelling) ? "" : ` (nothing has ${results.words.map((w) => `"${w}"`).join(" ")} exactly)`}</div>
            )}
            {results.matches.filter((s) => s.close_spelling).map((s) => (
              <StoryCard key={s.id} s={s} desks={desks} onClick={() => { onSelectStory(s.id); setResults(null); }} />
            ))}
            {results.related.length > 0 && <div className="search-group">Close in meaning</div>}
            {results.related.map((s) => (
              <StoryCard key={s.id} s={s} desks={desks} onClick={() => { onSelectStory(s.id); setResults(null); }} />
            ))}
            {!results.matches.length && !results.related.length && (
              <div className="empty">No stories in the last 30 days have all of these words: {results.words.join(", ")}.
                Wassup may not have collected this story; try fewer or other words.</div>
            )}
          </div>
        )}
      </div>
      <div className="seg">
        <button className={view === "globe" ? "on" : ""} onClick={() => onView("globe")}>GLOBE</button>
        <button className={view === "map" ? "on" : ""} onClick={() => onView("map")}>MAP</button>
        <button className={view === "board" ? "on" : ""} onClick={() => onView("board")}>BOARD</button>
        <button className={view === "newsroom" ? "on" : ""} onClick={() => onView("newsroom")}>NEWSROOM</button>
        <button className={view === "sources" ? "on" : ""} onClick={() => onView("sources")}>SOURCES</button>
        <button className={view === "investigate" ? "on" : ""} onClick={() => onView("investigate")}>INVESTIGATE</button>
        <button className={view === "quality" ? "on" : ""} onClick={() => onView("quality")}>QUALITY</button>
        <button className={view === "usage" ? "on" : ""} onClick={() => onView("usage")}>USAGE</button>
      </div>
    </header>
  );
}
