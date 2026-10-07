export type View = "globe" | "map" | "board" | "newsroom";

export type Tier = "A" | "B" | "C" | "S" | "U";

export interface Desk {
  key: string;
  name: string;
  color: string;
  description: string;
}

export interface Story {
  id: number;
  title: string; // English when a translation exists
  title_original?: string;
  title_tier: Tier;
  desk: string | null;
  routed: boolean;
  excluded_reason: string | null;
  significance: number;
  relevance: number;
  breaking: boolean;
  velocity: number;
  lat: number | null;
  lon: number | null;
  item_count: number;
  source_count: number;
  country_count: number;
  language_count: number;
  first_seen: string;
  last_seen: string;
  primary_place_id?: number | null;
  location_confidence?: number;
  location_source?: "headline" | "text" | "tagger" | "jev" | "model" | "you" | null;
  location_locked?: boolean;
}

export interface PlaceHit {
  id: number | null;
  key: string;
  name: string;
  country: string | null;
  kind: string;
  lat: number;
  lon: number;
}

export interface Place {
  id: number;
  name: string;
  country: string | null;
  kind: "country" | "region" | "city";
  lat: number;
  lon: number;
  story_count: number;
  item_count: number;
  max_significance: number;
  breaking: boolean;
  top_desk: string | null;
}

export interface Link {
  a: number;
  b: number;
  kind: "related" | "same_actor" | string;
  weight: number;
  evidence: { similarity?: number; entities?: string[]; reason?: string; from?: number; to?: number; by?: string };
  created_by?: string; // "rule" or "agent:<key>"
}

export interface GlobeData {
  window: { start: string; end: string };
  stories: Story[];
  places: Place[];
  links: Link[];
}

export interface Item {
  id: number;
  title: string; // English when a translation exists
  title_original: string | null;
  summary: string | null;
  url: string;
  language: string | null;
  published_at: string;
  outlet: string;
  tier: Tier;
  state_media: boolean;
  source_kind: string;
  meta: { themes?: string[]; tone?: number; feed?: string; kind?: string };
  excerpt?: string | null;
  has_text?: boolean | null;
  lead_image?: string | null;
}

export interface Entity {
  id: number;
  kind: "person" | "org";
  name: string;
  mentions: number;
}

export interface Neighbor extends Story {
  kind: string;
  weight: number;
  evidence: Link["evidence"];
  created_by: string;
}

export interface Brief {
  id: number;
  kind: "story" | "daily" | "standup" | "answer" | "desk";
  story_id: number | null;
  agent_key: string;
  agent_name: string | null;
  title?: string | null;
  body: string;
  meta?: Record<string, unknown>;
  created_at: string;
}

export interface NewsroomAgent {
  key: string;
  kind: "eic" | "desk" | "surge";
  name: string;
  desk: string | null;
  focus_story_id: number | null;
  focus_title: string | null;
  status: "active" | "retired";
  last_run_at: string | null;
  last_summary: string | null;
  created_at: string;
  retired_at: string | null;
  briefs_24h: number;
  following: number;
}

export interface NewsroomEvent {
  id: number;
  agent_key: string | null;
  agent_name: string | null;
  kind: string;
  text: string;
  story_id: number | null;
  created_at: string;
}

export interface NewsroomStatus {
  connected: boolean;
  set_up: boolean;
  paperclip_url: string;
  active: number;
  surges: number;
}

export interface StoryDetail extends Story {
  triage: {
    backend?: string;
    confidence?: number;
    learned?: number;
    desk_score?: number;
    excluded?: string | null;
    include?: string[];
    matched?: { keywords?: string[]; themes?: string[]; geo?: boolean };
    llm?: Record<string, unknown>;
  };
  items: Item[];
  places: (Pick<Place, "id" | "name" | "country" | "kind" | "lat" | "lon"> & { weight: number; is_primary: boolean })[];
  entities: Entity[];
  links: Neighbor[];
  feedback: number;
  briefs: Brief[];
  followers: { agent_key: string; agent_name: string | null; reason: string | null }[];
}

export interface PlaceDetail {
  id: number;
  name: string;
  country: string | null;
  kind: string;
  lat: number;
  lon: number;
  stories: (Story & { place_weight: number })[];
}

export interface GraphNode {
  id: string;
  type: "story" | "entity";
  depth?: number;
  name?: string;
  kind?: string;
  stories?: number;
  title?: string;
  desk?: string | null;
  significance?: number;
  breaking?: boolean;
  item_count?: number;
  source_count?: number;
  x?: number;
  y?: number;
  fx?: number;
  fy?: number;
}

export interface GraphEdge {
  source: string | GraphNode;
  target: string | GraphNode;
  kind: string;
  weight: number;
  evidence?: Link["evidence"];
}

export interface GraphData {
  root: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface TimelineData {
  start: string;
  end: string;
  buckets: number;
  counts: { bucket: number; desk: string; n: number; breaking: number }[];
}

export interface Stats {
  items: number;
  items_last_hour: number;
  stories: number;
  routed: number;
  cold: number;
  breaking: number;
  links: number;
  sources: number;
  sources_failing: number;
  languages: number;
  embed_backend: string;
  triage_backend: string;
  jev?: { configured: boolean; spent_today_usd: number; spent_total_usd: number; requests_today: number; daily_budget_usd: number };
}

export interface Filters {
  start: Date;
  end: Date;
  live: boolean;
  desks: Set<string>;
  cold: boolean;
  minSig: number;
}

export type Selection = { type: "story"; id: number } | { type: "place"; id: number } | null;

export interface Track {
  id: number;
  key: string;
  name: string;
  kind: "front" | "movement";
  desk: string | null;
  source: string | null;
  description: string | null;
  story_id: number | null;
  first: string | null;
  last: string | null;
  snapshots: number;
  latest: TrackStats | null;
}

export interface TrackStats {
  occupied_km2?: number;
  contested_km2?: number;
  liberated_km2?: number;
  attacks?: number;
}

export interface TrackSnapshot {
  id: number;
  observed_at: string;
  stats: TrackStats;
}

export interface MovementSummary {
  reports: number;
  days: number;
  km: number;
  latest: { place: string; day: string } | null;
  people: number | null;
}

export interface TrackState {
  kind: "front" | "movement";
  snapshot: TrackSnapshot | null;
  previous: TrackSnapshot | null;
  summary?: MovementSummary;
  features: GeoJSON.FeatureCollection;
}
