import type { InvestigationDetail, InvestigationListItem, QualityData } from "./types";
import type { Brief, PlaceHit, Desk, Filters, GlobeData, GraphData, NewsroomAgent, NewsroomEvent, NewsroomStatus, PlaceDetail, Stats, Story, SocialAccount, StoryDetail, TimelineData, Track, TrackState } from "./types";

async function get<T>(path: string, params?: Record<string, string | number | boolean | undefined>): Promise<T> {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params ?? {})) if (v !== undefined && v !== "") qs.set(k, String(v));
  const res = await fetch(`/api${path}${qs.size ? `?${qs}` : ""}`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} on ${path}`);
  return res.json() as Promise<T>;
}

export function filterParams(f: Filters, allDesks: string[]) {
  const desks = f.desks.size === allDesks.length ? undefined : [...f.desks].join(",") || "__none__";
  return { since: f.start.toISOString(), until: f.end.toISOString(), desks, cold: f.cold, min_sig: f.minSig };
}

async function post<T = { ok: boolean }>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail ?? `${res.status} on ${path}`);
  return res.json() as Promise<T>;
}

export const api = {
  storyReport: (id: number, body: { aspect: "desk" | "grouping" | "importance"; answer?: string; item_id?: number; note?: string }) =>
    post<{ ok: boolean; fixed: boolean; new_story: number | null }>(`/stories/${id}/report`, body),
  quality: (days = 30) => get<QualityData>("/quality", { days }),
  auditNow: () => post("/quality/audit", {}),
  investigations: () => get<InvestigationListItem[]>("/investigations"),
  investigation: (id: number) => get<InvestigationDetail>(`/investigations/${id}`),
  newInvestigation: (title: string, brief: string, links: string, watch_days: number) =>
    post<{ id: number }>("/investigations", { title, brief, links, watch_days }),
  investigationLinks: (id: number, links: string) => post<{ added: number }>(`/investigations/${id}/links`, { links }),
  investigationText: (id: number, body: { text: string; url?: string; title?: string; author?: string }) =>
    post<{ id: number }>(`/investigations/${id}/text`, body),
  investigationEdit: (id: number, body: { title?: string; brief?: string; status?: string; watch_days?: number }) =>
    post(`/investigations/${id}`, body),
  investigationAsk: (id: number, note: string) => post(`/investigations/${id}/ask`, { note }),
  investigationAddLead: (id: number, title: string, why: string) => post<{ id: number }>(`/investigations/${id}/leads`, { title, why, added_by: "you" }),
  investigationLead: (id: number, leadId: number, body: { status?: string }) => post(`/investigations/${id}/leads/${leadId}`, body),
  investigationRefresh: (id: number) => post(`/investigations/${id}/refresh`, {}),
  investigationSource: (id: number, sourceId: number, action: string) => post(`/investigations/${id}/sources/${sourceId}`, { action }),
  investigationSourceText: (id: number, sourceId: number) => get<{ text: string }>(`/investigations/${id}/sources/${sourceId}/text`),
  desks: () => get<Desk[]>("/desks"),
  stats: () => get<Stats>("/stats"),
  globe: (p: ReturnType<typeof filterParams>) => get<GlobeData>("/globe", p),
  stories: (p: ReturnType<typeof filterParams> & { q?: string; breaking?: boolean; limit?: number }) => get<Story[]>("/stories", p),
  story: (id: number) => get<StoryDetail>(`/stories/${id}`),
  place: (id: number, p: ReturnType<typeof filterParams>) => get<PlaceDetail>(`/places/${id}`, p),
  graph: (id: number, depth = 2) => get<GraphData>(`/graph/${id}`, { depth, max_nodes: 70 }),
  timeline: (p: { since: string; until: string; desks?: string; cold: boolean; buckets: number }) => get<TimelineData>("/timeline", p),
  itemText: (id: number) => get<{ status: string; body: string | null; lead_image: string | null; images: { src: string; caption: string }[] }>(`/items/${id}/text`),
  socialAccounts: () => get<SocialAccount[]>("/social/accounts"),
  socialAdd: async (handle: string, desk?: string) => {
    const res = await fetch("/api/social/accounts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ handle, desk }) });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail ?? `could not add: ${res.status}`);
  },
  socialAct: async (id: number, action: string) => {
    const res = await fetch(`/api/social/accounts/${id}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action }) });
    if (!res.ok) throw new Error(`could not ${action}: ${res.status}`);
  },
  tracks: () => get<Track[]>("/tracks"),
  trackState: (id: number, at: Date, compareDays: number) => get<TrackState>(`/tracks/${id}/state`, { at: at.toISOString(), compare_days: compareDays }),
  setObservation: async (id: number, status: "confirmed" | "rejected" | "auto") => {
    const res = await fetch(`/api/tracks/observations/${id}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status }),
    });
    if (!res.ok) throw new Error(`could not update report: ${res.status}`);
  },
  trackSeries: (id: number) => get<{ t: string; stats: Record<string, number> }[]>(`/tracks/${id}/series`),
  placeSearch: (q: string) => get<PlaceHit[]>("/search/places", { q }),
  fixLocation: async (id: number, body: { place_id?: number | null; place_key?: string; off_map?: boolean }) => {
    const res = await fetch(`/api/stories/${id}/location`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`location fix failed: ${res.status}`);
  },
  newsroomStatus: () => get<NewsroomStatus>("/newsroom/status"),
  newsroomAgents: () => get<NewsroomAgent[]>("/newsroom/agents"),
  newsroomEvents: (limit = 80) => get<NewsroomEvent[]>("/newsroom/events", { limit }),
  briefs: (p: { hours?: number; kind?: string; limit?: number }) => get<Brief[]>("/newsroom/briefs", p),
  callStandup: async (topic?: string) => {
    const res = await fetch("/api/newsroom/standup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ topic: topic || null }),
    });
    if (!res.ok) throw new Error(`standup failed: ${res.status}`);
    return res.json() as Promise<{ standup_id: number; desks_woken: number; already_open?: boolean }>;
  },
  feedback: async (id: number, value: 1 | -1) => {
    const res = await fetch(`/api/stories/${id}/feedback`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value }),
    });
    if (!res.ok) throw new Error(`feedback failed: ${res.status}`);
  },
};
