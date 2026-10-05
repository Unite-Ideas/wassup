import { useCallback, useEffect, useMemo, useState } from "react";
import { api, filterParams } from "./lib/api";
import type { Desk, Filters, GlobeData, PlaceDetail, Selection, Stats, StoryDetail, TimelineData, View } from "./lib/types";
import GlobeView from "./components/GlobeView";
import BoardView from "./components/BoardView";
import Timeline from "./components/Timeline";
import LeftPanel from "./components/LeftPanel";
import StoryPanel from "./components/StoryPanel";
import PlacePanel from "./components/PlacePanel";
import TopBar from "./components/TopBar";
import NewsroomView from "./components/NewsroomView";
import MapView, { type Overlay } from "./components/MapView";
import TracksPanel from "./components/TracksPanel";

const HOUR = 3600e3;
const REFRESH_MS = 60_000;

function liveWindow(lengthMs: number): Pick<Filters, "start" | "end"> {
  const end = new Date();
  return { start: new Date(end.getTime() - lengthMs), end };
}

export default function App() {
  const [desks, setDesks] = useState<Desk[]>([]);
  const deskMap = useMemo(() => new Map(desks.map((d) => [d.key, d])), [desks]);
  const allKeys = useMemo(() => desks.map((d) => d.key), [desks]);

  const [extentHours, setExtentHours] = useState(72);
  const [filters, setFilters] = useState<Filters>(() => ({ ...liveWindow(24 * HOUR), live: true, desks: new Set(), cold: false, minSig: 0 }));
  const [view, setView] = useState<View>("globe");
  const [mapOpened, setMapOpened] = useState(false);
  const [overlays, setOverlays] = useState<Overlay[]>([]);
  useEffect(() => { if (view === "map") setMapOpened(true); }, [view]);
  const [selection, setSelection] = useState<Selection>(null);
  const [boardRoot, setBoardRoot] = useState<number | null>(null);
  const [showLinks, setShowLinks] = useState(true);
  const [showLabels, setShowLabels] = useState(true);
  const [autoRotate, setAutoRotate] = useState(true);
  const [playing, setPlaying] = useState(false);

  const [globe, setGlobe] = useState<GlobeData | null>(null);
  const [timeline, setTimeline] = useState<TimelineData | null>(null);
  const [stats, setStats] = useState<Stats | null>(null);
  const [story, setStory] = useState<StoryDetail | null>(null);
  const [place, setPlace] = useState<PlaceDetail | null>(null);
  const [loading, setLoading] = useState({ globe: true, detail: false });
  const [error, setError] = useState<string | null>(null);
  const [refreshTick, setRefreshTick] = useState(0);

  useEffect(() => {
    api.desks().then((d) => {
      setDesks(d);
      setFilters((f) => ({ ...f, desks: new Set(d.map((x) => x.key)) }));
    }).catch((e) => setError(`Cannot reach the Wassup API: ${e}`));
  }, []);

  // Stats and live refresh.
  useEffect(() => {
    const load = () => api.stats().then(setStats).catch(() => undefined);
    load();
    const t = setInterval(() => {
      load();
      setRefreshTick((x) => x + 1);
    }, REFRESH_MS);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    if (!refreshTick) return;
    setFilters((f) => (f.live ? { ...f, ...liveWindow(f.end.getTime() - f.start.getTime()) } : f));
  }, [refreshTick]);

  const params = useMemo(() => (desks.length ? filterParams(filters, allKeys) : null), [filters, allKeys, desks.length]);

  // Globe data, debounced so dragging the timeline does not flood the API.
  useEffect(() => {
    if (!params) return;
    let cancel = false;
    const t = setTimeout(() => {
      setLoading((l) => ({ ...l, globe: true }));
      api.globe(params)
        .then((d) => { if (!cancel) { setGlobe(d); setError(null); } })
        .catch((e) => !cancel && setError(String(e)))
        .finally(() => !cancel && setLoading((l) => ({ ...l, globe: false })));
    }, 220);
    return () => { cancel = true; clearTimeout(t); };
  }, [params]);

  useEffect(() => {
    if (!params) return;
    const end = new Date();
    const start = new Date(end.getTime() - extentHours * HOUR);
    const buckets = extentHours <= 24 ? 96 : extentHours <= 72 ? 144 : extentHours <= 168 ? 168 : 240;
    api.timeline({ since: start.toISOString(), until: end.toISOString(), desks: params.desks, cold: filters.cold, buckets })
      .then(setTimeline).catch(() => undefined);
  }, [extentHours, params === null, params?.desks, filters.cold, refreshTick]); // eslint-disable-line react-hooks/exhaustive-deps

  // Detail panels.
  const storyId = selection?.type === "story" ? selection.id : null;
  const placeId = selection?.type === "place" ? selection.id : null;
  const loadStory = useCallback((id: number) => {
    setLoading((l) => ({ ...l, detail: true }));
    api.story(id).then(setStory).catch(() => setStory(null)).finally(() => setLoading((l) => ({ ...l, detail: false })));
  }, []);
  useEffect(() => {
    if (storyId == null) { setStory(null); return; }
    loadStory(storyId);
  }, [storyId, loadStory, refreshTick]);
  useEffect(() => {
    if (placeId == null || !params) { setPlace(null); return; }
    setLoading((l) => ({ ...l, detail: true }));
    api.place(placeId, params).then(setPlace).catch(() => setPlace(null)).finally(() => setLoading((l) => ({ ...l, detail: false })));
  }, [placeId, params]);

  // Replay: slide the window forward through the timeline.
  useEffect(() => {
    if (!playing) return;
    const t = setInterval(() => {
      setFilters((f) => {
        const len = f.end.getTime() - f.start.getTime();
        const step = Math.max(15 * 60e3, (extentHours * HOUR) / 60);
        const end = f.end.getTime() + step;
        if (end >= Date.now()) {
          setPlaying(false);
          return { ...f, ...liveWindow(len), live: true };
        }
        return { ...f, start: new Date(end - len), end: new Date(end), live: false };
      });
    }, 900);
    return () => clearInterval(t);
  }, [playing, extentHours]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setSelection(null); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const counts = useMemo(() => {
    const m = new Map<string, number>();
    globe?.stories.forEach((s) => s.desk && m.set(s.desk, (m.get(s.desk) ?? 0) + 1));
    return m;
  }, [globe]);

  const selectStory = useCallback((id: number) => setSelection({ type: "story", id }), []);
  const selectPlace = useCallback((id: number) => setSelection({ type: "place", id }), []);
  const openBoard = useCallback((id: number) => { setBoardRoot(id); setView("board"); }, []);

  const onWindow = useCallback((start: Date, end: Date) => {
    setPlaying(false);
    setFilters((f) => ({ ...f, start, end, live: Date.now() - end.getTime() < 5 * 60e3 }));
  }, []);

  const onExtent = useCallback((h: number) => {
    setExtentHours(h);
    setFilters((f) => {
      const len = Math.min(f.end.getTime() - f.start.getTime(), h * HOUR);
      return { ...f, ...liveWindow(len), live: true };
    });
  }, []);

  const toggleDesk = useCallback((key: string, solo: boolean) => {
    setFilters((f) => {
      const next = new Set(f.desks);
      if (solo) return { ...f, desks: new Set([key]) };
      if (next.has(key)) next.delete(key); else next.add(key);
      return { ...f, desks: next };
    });
  }, []);

  const feedback = useCallback((id: number, v: 1 | -1) => {
    api.feedback(id, v).then(() => {
      loadStory(id);
      setTimeout(() => setRefreshTick((x) => x + 1), 20_000); // triage picks it up within seconds
    }).catch((e) => setError(String(e)));
  }, [loadStory]);

  const panelOpen = selection != null;

  return (
    <div className="app">
      <TopBar stats={stats} view={view} onView={setView} desks={deskMap} onSelectStory={selectStory} />
      <LeftPanel
        desks={desks} deskMap={deskMap} enabled={filters.desks} counts={counts} stories={globe?.stories ?? []}
        cold={filters.cold} minSig={filters.minSig} showLinks={showLinks} showLabels={showLabels} autoRotate={autoRotate}
        onToggleDesk={toggleDesk} onAllDesks={() => setFilters((f) => ({ ...f, desks: new Set(allKeys) }))}
        onCold={(cold) => setFilters((f) => ({ ...f, cold }))} onMinSig={(minSig) => setFilters((f) => ({ ...f, minSig }))}
        onLinks={setShowLinks} onLabels={setShowLabels} onRotate={setAutoRotate} onSelectStory={selectStory}
      />
      <main className="main">
        <div style={{ position: "absolute", inset: 0, visibility: view === "globe" ? "visible" : "hidden" }}>
          <GlobeView
            data={globe} desks={deskMap} selection={selection} storyDetail={story}
            showLinks={showLinks} showLabels={showLabels} autoRotate={autoRotate}
            onSelectPlace={selectPlace} onSelectStory={selectStory}
          />
          <div className="overlay tl">
            <div className="big">{globe ? `${globe.places.length} places · ${globe.stories.length} stories · ${globe.links.length} links` : ""}</div>
            {filters.cold ? "including cold storage" : "tracked stories only"}
          </div>
          <div className="legend">
            <span><i style={{ background: "#ff3b5c" }} />shared actors</span>
            <span><i style={{ background: "linear-gradient(90deg,#4FA3FF,#FFC24F)" }} />related coverage</span>
            <span><span className="live-dot" />breaking</span>
          </div>
        </div>
        {mapOpened && (
          <div style={{ position: "absolute", inset: 0, visibility: view === "map" ? "visible" : "hidden" }}>
            <MapView data={globe} desks={deskMap} selection={selection} storyDetail={story} visible={view === "map"}
              overlays={overlays} onSelectStory={selectStory} />
            <TracksPanel timelineEnd={filters.end} onOverlays={setOverlays} />
          </div>
        )}
        {view === "newsroom" && <NewsroomView desks={deskMap} onSelectStory={selectStory} />}
        {view === "board" && (
          <BoardView rootId={boardRoot ?? storyId} selectedId={storyId} desks={deskMap} onSelectStory={selectStory} onReRoot={setBoardRoot} />
        )}
        {loading.globe && !globe && <div className="loading">ACQUIRING SIGNAL...</div>}
        {error && <div className="error-banner">{error}</div>}
      </main>
      <aside className={`right panel ${panelOpen ? "" : "closed"}`}>
        {selection?.type === "story" && (
          <StoryPanel story={story} loading={loading.detail} desks={deskMap} onClose={() => setSelection(null)}
            onSelectStory={selectStory} onSelectPlace={selectPlace} onOpenBoard={openBoard} onFeedback={feedback}
            onChanged={() => { loadStory(story!.id); setRefreshTick((x) => x + 1); }} />
        )}
        {selection?.type === "place" && (
          <PlacePanel place={place} loading={loading.detail} desks={deskMap} onClose={() => setSelection(null)} onSelectStory={selectStory} />
        )}
      </aside>
      <Timeline
        extentHours={extentHours} onExtent={onExtent} data={timeline} desks={desks} deskMap={deskMap}
        start={filters.start} end={filters.end} live={filters.live} playing={playing}
        onWindow={onWindow} onLive={() => onExtent(extentHours)} onPlay={() => setPlaying((p) => !p)}
      />
    </div>
  );
}
