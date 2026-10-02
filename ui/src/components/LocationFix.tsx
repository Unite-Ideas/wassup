import { useEffect, useState } from "react";
import { api } from "../lib/api";
import type { PlaceHit, StoryDetail } from "../lib/types";

const SOURCE_LABEL: Record<string, string> = {
  headline: "named in a headline",
  text: "found in the article text",
  tagger: "from GDELT's tagger, unverified",
  jev: "checked by Jev",
  model: "checked by the local model",
  you: "set by you",
};

interface Props {
  story: StoryDetail;
  onSelectPlace: (id: number) => void;
  onChanged: () => void;
}

/** Where the story is on the map, why, and a way to fix it. */
export default function LocationFix({ story, onSelectPlace, onChanged }: Props) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<PlaceHit[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    setOpen(false);
    setQ("");
  }, [story.id]);

  useEffect(() => {
    if (q.trim().length < 2) {
      setHits([]);
      return;
    }
    const t = setTimeout(() => api.placeSearch(q.trim()).then(setHits).catch(() => setHits([])), 200);
    return () => clearTimeout(t);
  }, [q]);

  const save = async (body: { place_id?: number | null; place_key?: string; off_map?: boolean }) => {
    setBusy(true);
    setErr(null);
    try {
      await api.fixLocation(story.id, body);
      setOpen(false);
      onChanged();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  const primary = story.places.find((p) => p.is_primary);
  const shown = story.places.filter((p) => p.weight > 0.15);
  const weak = story.places.filter((p) => p.weight > 0 && p.weight <= 0.15);
  const unverified = story.location_source === "tagger";

  return (
    <div className="sub">
      <div className="h">
        Location
        <button onClick={() => setOpen(!open)}>{open ? "Cancel" : "Fix location"}</button>
      </div>
      <div className={`loc-line ${unverified ? "unverified" : ""}`}>
        {primary ? (
          <>
            On the map at <b className="click" onClick={() => onSelectPlace(primary.id)}>{primary.name}{primary.country && primary.kind !== "country" ? `, ${primary.country}` : ""}</b>
          </>
        ) : (
          <>Not on the map</>
        )}
        <span className="dimmer"> · {SOURCE_LABEL[story.location_source ?? ""] ?? "no location evidence"}
          {story.location_confidence != null && story.location_source !== "you" ? ` · ${Math.round(story.location_confidence * 100)}% of the evidence` : ""}</span>
      </div>
      {!open && shown.length > 1 && (
        <div className="chips" style={{ marginTop: 8 }}>
          {shown.filter((p) => !p.is_primary).slice(0, 10).map((p) => (
            <span key={p.id} className="chip click" onClick={() => onSelectPlace(p.id)}>⌖ {p.name}{p.country && p.kind !== "country" ? `, ${p.country}` : ""}</span>
          ))}
        </div>
      )}
      {!open && weak.length > 0 && (
        <div className="dimmer" style={{ fontSize: 11, marginTop: 6 }}>
          Ignored as weak evidence: {weak.map((p) => p.name).join(", ")}
        </div>
      )}
      {open && (
        <div className="loc-edit">
          <div className="dim" style={{ fontSize: 12, marginBottom: 6 }}>Where is this story really about? The current place will be marked wrong for it.</div>
          {story.places.filter((p) => !p.is_primary && p.weight > 0).slice(0, 8).map((p) => (
            <button key={p.id} className="loc-opt" disabled={busy} onClick={() => save({ place_id: p.id })}>
              ⌖ {p.name}{p.country && p.kind !== "country" ? `, ${p.country}` : ""} <span className="dimmer">{p.kind}</span>
            </button>
          ))}
          <input className="loc-search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search for another place" />
          {hits.map((h) => (
            <button key={h.key} className="loc-opt" disabled={busy} onClick={() => save(h.id ? { place_id: h.id } : { place_key: h.key })}>
              ⌖ {h.name}{h.country && h.kind !== "country" ? `, ${h.country}` : ""} <span className="dimmer">{h.kind}</span>
            </button>
          ))}
          <button className="loc-opt off" disabled={busy} onClick={() => save({ off_map: true })}>Not about a place: take it off the map</button>
          {err && <div className="dim" style={{ color: "var(--alert)" }}>{err}</div>}
        </div>
      )}
    </div>
  );
}
