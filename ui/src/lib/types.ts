export type Tier = "A" | "B" | "C" | "S" | "U";

export interface Desk {
  key: string;
  name: string;
  color: string;
  description: string;
}

export interface Story {
  id: number;
  title: string;
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
  evidence: { similarity?: number; entities?: string[] };
}

export interface GlobeData {
  window: { start: string; end: string };
  stories: Story[];
  places: Place[];
  links: Link[];
}

export interface Item {
  id: number;
  title: string;
  summary: string | null;
  url: string;
  language: string | null;
  published_at: string;
  outlet: string;
  tier: Tier;
  state_media: boolean;
  source_kind: string;
  meta: { themes?: string[]; tone?: number; feed?: string; kind?: string };
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
  places: (Pick<Place, "id" | "name" | "country" | "kind" | "lat" | "lon"> & { weight: number })[];
  entities: Entity[];
  links: Neighbor[];
  feedback: number;
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
