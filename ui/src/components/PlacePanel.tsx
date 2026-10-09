import { useMemo, useState } from "react";
import type { Desk, PlaceDetail } from "../lib/types";
import { StoryCard } from "./StoryPanel";

interface Props {
  place: PlaceDetail | null;
  loading: boolean;
  desks: Map<string, Desk>;
  onClose: () => void;
  onSelectStory: (id: number) => void;
}

export default function PlacePanel({ place, loading, desks, onClose, onSelectStory }: Props) {
  const [showMentions, setShowMentions] = useState(false);
  const about = useMemo(() => (place?.stories ?? []).filter((s) => s.about), [place]);
  const mentions = useMemo(() => (place?.stories ?? []).filter((s) => !s.about), [place]);
  const byDesk = useMemo(() => {
    const m = new Map<string, number>();
    about.forEach((s) => m.set(s.desk ?? "none", (m.get(s.desk ?? "none") ?? 0) + 1));
    return [...m.entries()].sort((a, b) => b[1] - a[1]);
  }, [about]);

  if (!place) return <div className="empty">{loading ? "Loading..." : "Place not found."}</div>;
  const articles = about.reduce((a, s) => a + s.item_count, 0);
  const breaking = about.filter((s) => s.breaking).length;

  return (
    <>
      <div className="detail-head">
        <button className="close" onClick={onClose} aria-label="Close">✕</button>
        <div className="crumbs">
          <span className="chip">⌖ {place.kind}</span>
          {place.country && <span className="chip">{place.country}</span>}
          {breaking > 0 && <span className="chip breaking">{breaking} breaking</span>}
        </div>
        <h2 className="title">{place.name}</h2>
        <div className="metrics" style={{ gridTemplateColumns: "repeat(3, 1fr)" }}>
          <div className="metric" title="Stories mainly about this place"><b>{about.length}</b><span>Stories</span></div>
          <div className="metric"><b>{articles}</b><span>Articles</span></div>
          <div className="metric"><b className="mono" style={{ fontSize: 12 }}>{place.lat.toFixed(2)}, {place.lon.toFixed(2)}</b><span>Coordinates</span></div>
        </div>
        {byDesk.length > 0 && (
          <div className="chips" style={{ marginTop: 10 }}>
            {byDesk.map(([k, n]) => (
              <span key={k} className="chip" style={{ color: desks.get(k)?.color }}>{desks.get(k)?.name ?? "No desk"} · {n}</span>
            ))}
          </div>
        )}
      </div>
      <div className="detail-body">
        {about.length ? about.map((s) => (
          <StoryCard key={s.id} s={s} desks={desks} onClick={() => onSelectStory(s.id)} />
        )) : <div className="empty">No stories mainly about {place.name} in this time window.</div>}
        {mentions.length > 0 && (
          <>
            <button className="linkish mentions-toggle" onClick={() => setShowMentions(!showMentions)}
              title="Stories mainly about somewhere else that mention this place">
              {showMentions ? "▾" : "▸"} Also mention {place.name} <span className="dimmer">{mentions.length}</span>
            </button>
            {showMentions && mentions.map((s) => (
              <StoryCard key={s.id} s={s} desks={desks} onClick={() => onSelectStory(s.id)} />
            ))}
          </>
        )}
      </div>
    </>
  );
}
