-- Wassup database schema (Phase 0).
-- Safe to run repeatedly: every statement is idempotent.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Where content comes from. trust_tier: A primary/wire, B independent, C partisan/low, S state media.
CREATE TABLE IF NOT EXISTS sources (
    id              serial PRIMARY KEY,
    key             text UNIQUE NOT NULL,
    name            text NOT NULL,
    kind            text NOT NULL,              -- rss | gdelt | congress | federal_register
    url             text,
    country         text,                       -- ISO 3166-1 alpha-2
    language        text,
    trust_tier      char(1) NOT NULL DEFAULT 'B',
    state_media     boolean NOT NULL DEFAULT false,
    enabled         boolean NOT NULL DEFAULT true,
    last_polled_at  timestamptz,
    last_error      text,
    item_count      bigint NOT NULL DEFAULT 0
);

-- One article, post, video, or document.
CREATE TABLE IF NOT EXISTS items (
    id              bigserial PRIMARY KEY,
    source_id       integer NOT NULL REFERENCES sources(id),
    outlet          text,                       -- publication name when the source is an aggregator (GDELT)
    outlet_tier     char(1),                    -- tier of the outlet itself, overrides the source tier
    outlet_state    boolean NOT NULL DEFAULT false,
    url             text NOT NULL,
    title           text NOT NULL,
    summary         text,
    language        text,
    published_at    timestamptz NOT NULL,
    collected_at    timestamptz NOT NULL DEFAULT now(),
    status          text NOT NULL DEFAULT 'new', -- new | clustered | error
    embedding       vector(1024),
    story_id        bigint,
    meta            jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (url)
);
CREATE INDEX IF NOT EXISTS items_status_idx ON items (status) WHERE status = 'new';
CREATE INDEX IF NOT EXISTS items_story_idx ON items (story_id);
CREATE INDEX IF NOT EXISTS items_published_idx ON items (published_at);

-- Named locations. key is stable per gazetteer: "gn:<geonameid>", "cc:<iso2>", "gdelt:<featureid>".
CREATE TABLE IF NOT EXISTS places (
    id              serial PRIMARY KEY,
    key             text UNIQUE NOT NULL,
    name            text NOT NULL,
    country         text,
    kind            text NOT NULL,              -- country | region | city
    lat             double precision NOT NULL,
    lon             double precision NOT NULL,
    geom            geography(Point, 4326) GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography) STORED
);
CREATE INDEX IF NOT EXISTS places_geom_idx ON places USING gist (geom);

CREATE TABLE IF NOT EXISTS item_places (
    item_id         bigint NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    place_id        integer NOT NULL REFERENCES places(id),
    in_title        boolean NOT NULL DEFAULT false,  -- named in the headline, so it counts more
    PRIMARY KEY (item_id, place_id)
);
CREATE INDEX IF NOT EXISTS item_places_place_idx ON item_places (place_id);

CREATE TABLE IF NOT EXISTS entities (
    id              serial PRIMARY KEY,
    kind            text NOT NULL,              -- person | org
    name            text NOT NULL,
    norm            text NOT NULL,
    UNIQUE (kind, norm)
);

CREATE TABLE IF NOT EXISTS item_entities (
    item_id         bigint NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    entity_id       integer NOT NULL REFERENCES entities(id),
    PRIMARY KEY (item_id, entity_id)
);
CREATE INDEX IF NOT EXISTS item_entities_entity_idx ON item_entities (entity_id);

-- A cluster of items about the same event.
CREATE TABLE IF NOT EXISTS stories (
    id                  bigserial PRIMARY KEY,
    title               text NOT NULL,
    title_tier          char(1) NOT NULL DEFAULT 'C',
    centroid            vector(1024) NOT NULL,
    item_count          integer NOT NULL DEFAULT 0,
    source_count        integer NOT NULL DEFAULT 0,
    country_count       integer NOT NULL DEFAULT 0,
    language_count      integer NOT NULL DEFAULT 0,
    first_seen          timestamptz NOT NULL,
    last_seen           timestamptz NOT NULL,
    desk                text,                   -- desk key, null when not routed
    routed              boolean NOT NULL DEFAULT false, -- false means cold storage
    excluded_reason     text,
    significance        real NOT NULL DEFAULT 0, -- 0..5
    relevance           real NOT NULL DEFAULT 0, -- 0..1, interest profile match
    breaking            boolean NOT NULL DEFAULT false,
    velocity            real NOT NULL DEFAULT 0, -- items in the last hour
    lat                 double precision,
    lon                 double precision,
    primary_place_id    integer REFERENCES places(id),
    triage              jsonb NOT NULL DEFAULT '{}'::jsonb,
    triaged_item_count  integer NOT NULL DEFAULT 0,
    updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS stories_last_seen_idx ON stories (last_seen);
CREATE INDEX IF NOT EXISTS stories_routed_idx ON stories (routed, last_seen);
CREATE INDEX IF NOT EXISTS stories_title_trgm_idx ON stories USING gin (title gin_trgm_ops);

CREATE TABLE IF NOT EXISTS story_places (
    story_id        bigint NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    place_id        integer NOT NULL REFERENCES places(id),
    weight          integer NOT NULL DEFAULT 1,
    PRIMARY KEY (story_id, place_id)
);
CREATE INDEX IF NOT EXISTS story_places_place_idx ON story_places (place_id);

-- The strings on the wall. a < b so each pair is stored once per kind.
CREATE TABLE IF NOT EXISTS story_links (
    a               bigint NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    b               bigint NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    kind            text NOT NULL,              -- related | same_actor | (agent kinds in Phase 1)
    weight          real NOT NULL,
    evidence        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_by      text NOT NULL DEFAULT 'rule',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (a, b, kind),
    CHECK (a < b)
);
CREATE INDEX IF NOT EXISTS story_links_b_idx ON story_links (b);

-- Thumbs up (+1) and down (-1) from the UI. Trains the interest profile.
CREATE TABLE IF NOT EXISTS feedback (
    id              bigserial PRIMARY KEY,
    story_id        bigint NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    value           smallint NOT NULL CHECK (value IN (-1, 1)),
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- Small key/value store for pipeline bookkeeping (last GDELT file seen, etc).
CREATE TABLE IF NOT EXISTS kv (
    key             text PRIMARY KEY,
    value           jsonb NOT NULL,
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Upgrades for databases created by earlier versions.
ALTER TABLE items ADD COLUMN IF NOT EXISTS title_en text;            -- English translation of a non English headline
ALTER TABLE items ADD COLUMN IF NOT EXISTS translated_at timestamptz;
ALTER TABLE stories ADD COLUMN IF NOT EXISTS title_en text;          -- headline in English (original or translated)
CREATE INDEX IF NOT EXISTS items_untranslated_idx ON items (story_id)
    WHERE translated_at IS NULL AND language IS NOT NULL AND language <> 'en';
ALTER TABLE item_places ADD COLUMN IF NOT EXISTS in_title boolean NOT NULL DEFAULT false;
DROP INDEX IF EXISTS stories_centroid_idx;

-- Phase 1: the newsroom. Agents run in Paperclip; what they produce lives here so the
-- dashboard can show it next to the stories.

-- Every Paperclip agent Wassup manages. key is stable: "eic", "desk:<desk>", "surge:<story id>".
CREATE TABLE IF NOT EXISTS newsroom_agents (
    key                 text PRIMARY KEY,
    kind                text NOT NULL,               -- eic | desk | surge
    name                text NOT NULL,
    desk                text,
    focus_story_id      bigint REFERENCES stories(id) ON DELETE SET NULL,  -- surge agents
    paperclip_agent_id  text UNIQUE,
    paperclip_api_key   text,                        -- the agent's own key, used to report back
    log_issue_id        text,                        -- long lived Paperclip issue the agent writes its reports on
    status              text NOT NULL DEFAULT 'active', -- active | retired
    last_run_at         timestamptz,
    last_summary        text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    retired_at          timestamptz,
    meta                jsonb NOT NULL DEFAULT '{}'::jsonb
);

-- Written analysis: story briefs, desk summaries, the daily brief, standup notes.
CREATE TABLE IF NOT EXISTS briefs (
    id              bigserial PRIMARY KEY,
    kind            text NOT NULL,                   -- story | desk | daily | standup | answer
    story_id        bigint REFERENCES stories(id) ON DELETE CASCADE,
    agent_key       text NOT NULL,
    title           text,
    body            text NOT NULL,
    meta            jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS briefs_story_idx ON briefs (story_id, created_at DESC);
CREATE INDEX IF NOT EXISTS briefs_kind_idx ON briefs (kind, created_at DESC);

-- Stories an agent has decided to keep tracking.
CREATE TABLE IF NOT EXISTS follows (
    story_id        bigint NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    agent_key       text NOT NULL,
    reason          text,
    active          boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (story_id, agent_key)
);

-- What happened in the newsroom, newest first, for the dashboard feed.
CREATE TABLE IF NOT EXISTS newsroom_events (
    id              bigserial PRIMARY KEY,
    agent_key       text,
    kind            text NOT NULL,                   -- run | brief | link | follow | surge_hired | surge_retired | escalation | standup | error
    text            text NOT NULL,
    story_id        bigint REFERENCES stories(id) ON DELETE SET NULL,
    meta            jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS newsroom_events_created_idx ON newsroom_events (created_at DESC);

-- Standups: the Editor in Chief (or the schedule) calls one, every desk reports, and the
-- Editor in Chief is handed the reports to write them up.
CREATE TABLE IF NOT EXISTS standups (
    id              bigserial PRIMARY KEY,
    requested_by    text NOT NULL,
    topic           text,
    status          text NOT NULL DEFAULT 'collecting', -- collecting | handed_off | done
    created_at      timestamptz NOT NULL DEFAULT now(),
    handed_off_at   timestamptz
);
CREATE TABLE IF NOT EXISTS standup_reports (
    standup_id      bigint NOT NULL REFERENCES standups(id) ON DELETE CASCADE,
    agent_key       text NOT NULL,
    body            text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (standup_id, agent_key)
);

-- Breaking stories already escalated to the Editor in Chief, so each is raised once.
CREATE TABLE IF NOT EXISTS escalations (
    story_id        bigint PRIMARY KEY REFERENCES stories(id) ON DELETE CASCADE,
    paperclip_issue_id text,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- Paid API usage per day (Jev), so spend is visible and capped.
CREATE TABLE IF NOT EXISTS api_usage (
    day             date NOT NULL,
    provider        text NOT NULL,
    requests        integer NOT NULL DEFAULT 0,
    input_tokens    bigint NOT NULL DEFAULT 0,
    output_tokens   bigint NOT NULL DEFAULT 0,
    cost_usd        numeric(12, 6) NOT NULL DEFAULT 0,
    PRIMARY KEY (day, provider)
);

-- Location quality. Each place an article mentions carries an evidence weight; each story
-- records how sure Wassup is of its location and where that came from. Verified or corrected
-- locations are locked so new articles do not move them.
ALTER TABLE item_places ADD COLUMN IF NOT EXISTS weight real NOT NULL DEFAULT 1;
ALTER TABLE story_places ALTER COLUMN weight TYPE real;
ALTER TABLE stories ADD COLUMN IF NOT EXISTS location_confidence real NOT NULL DEFAULT 0;
ALTER TABLE stories ADD COLUMN IF NOT EXISTS location_source text;     -- headline | text | tagger | jev | model | you
ALTER TABLE stories ADD COLUMN IF NOT EXISTS location_locked boolean NOT NULL DEFAULT false;
ALTER TABLE stories ADD COLUMN IF NOT EXISTS location_checked_at timestamptz;
ALTER TABLE stories ADD COLUMN IF NOT EXISTS location_checked_items integer NOT NULL DEFAULT 0;
ALTER TABLE places ADD COLUMN IF NOT EXISTS false_positive_count integer NOT NULL DEFAULT 0;

-- Your location fixes, kept so recurring mistakes can be spotted and learned from.
CREATE TABLE IF NOT EXISTS location_corrections (
    id              bigserial PRIMARY KEY,
    story_id        bigint NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    from_place_id   integer REFERENCES places(id),
    to_place_id     integer REFERENCES places(id),    -- null means "not about a place"
    by              text NOT NULL DEFAULT 'you',
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- Significance = how much a story matters on its own (importance, judged at triage, 0..5)
-- plus how widely it is covered (outlets on a log scale, and countries). Coverage is computed
-- here so the score keeps up as a story grows, without asking triage again. A story with a
-- handful of outlets can no longer reach the top just by being on an important subject.
CREATE OR REPLACE FUNCTION wassup_significance(importance double precision, sources double precision, countries double precision)
RETURNS real LANGUAGE sql IMMUTABLE AS $$
    SELECT round(least(5.0,
        0.4 * least(greatest(coalesce(importance, 0), 0), 5)
        + least(3.0, 0.45 * ln(1 + greatest(coalesce(sources, 0), 0)) / ln(2)
                     + 0.1 * least(greatest(coalesce(countries, 0) - 1, 0), 5)))::numeric, 2)::real
$$;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'stories' AND column_name = 'importance') THEN
        ALTER TABLE stories ADD COLUMN importance real;
        -- Older stories only have the old blended score; use it as their importance.
        UPDATE stories SET importance = significance WHERE triaged_item_count > 0;
        UPDATE stories SET significance = wassup_significance(importance, source_count, country_count)
            WHERE importance IS NOT NULL;
    END IF;
END $$;

-- Retention (retention.py) clears the vectors of stories older than a month.
ALTER TABLE stories ALTER COLUMN centroid DROP NOT NULL;

-- Tracks: things that move over time on the MAP view. A front line (areas held, contested and
-- changing hands, from daily snapshots) or a movement such as a migrant caravan (dated points).
CREATE TABLE IF NOT EXISTS tracks (
    id              serial PRIMARY KEY,
    key             text UNIQUE NOT NULL,
    name            text NOT NULL,
    kind            text NOT NULL,                  -- front | movement | strikes
    desk            text,
    source          text,                           -- where the observations come from
    description     text,
    story_id        bigint REFERENCES stories(id) ON DELETE SET NULL,
    active          boolean NOT NULL DEFAULT true,
    meta            jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);
-- One full picture of a front at a moment (a DeepStateMap update, say), with summary numbers.
CREATE TABLE IF NOT EXISTS track_snapshots (
    id              bigserial PRIMARY KEY,
    track_id        integer NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    observed_at     timestamptz NOT NULL,
    source_ref      text NOT NULL,
    stats           jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (track_id, source_ref)
);
CREATE INDEX IF NOT EXISTS track_snapshots_time_idx ON track_snapshots (track_id, observed_at);
CREATE TABLE IF NOT EXISTS track_observations (
    id              bigserial PRIMARY KEY,
    track_id        integer NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    snapshot_id     bigint REFERENCES track_snapshots(id) ON DELETE CASCADE,
    observed_at     timestamptz NOT NULL,
    category        text NOT NULL,                  -- occupied | contested | liberated | attack | position | strike
    geom            geometry(Geometry, 4326) NOT NULL,
    label           text,
    props           jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_url      text,
    item_id         bigint REFERENCES items(id) ON DELETE SET NULL,
    confidence      real,
    status          text NOT NULL DEFAULT 'auto',   -- auto | confirmed | rejected
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS track_observations_time_idx ON track_observations (track_id, observed_at);
CREATE INDEX IF NOT EXISTS track_observations_snapshot_idx ON track_observations (snapshot_id);
-- Fills holes smaller than min_m2 in (multi)polygons: the slivers left where neighbouring
-- areas drawn by hand do not quite meet.
CREATE OR REPLACE FUNCTION wassup_fill_small_holes(g geometry, min_m2 double precision)
RETURNS geometry LANGUAGE sql IMMUTABLE AS $$
    SELECT ST_Multi(ST_Collect(ST_MakePolygon(ST_ExteriorRing(p.geom), ARRAY(
        SELECT ST_ExteriorRing(h.geom) FROM ST_DumpRings(p.geom) h
        WHERE h.path[1] > 0 AND ST_Area(h.geom::geography) >= min_m2))))
    FROM ST_Dump(g) p
$$;

-- Full text of articles (reader.py), kept apart from items so the items table stays lean.
CREATE TABLE IF NOT EXISTS item_texts (
    item_id         bigint PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    status          text NOT NULL,                  -- ok | short | blocked | failed | skipped
    http_status     integer,
    body            text,
    chars           integer NOT NULL DEFAULT 0,
    lead_image      text,
    images          jsonb NOT NULL DEFAULT '[]'::jsonb,   -- [{src, caption}] photos with captions
    fetched_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS item_texts_fetched_idx ON item_texts (fetched_at);

-- Articles a movement track has already read (whether or not they gave a position).
CREATE TABLE IF NOT EXISTS track_reads (
    track_id        integer NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    item_id         bigint NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    positions       integer NOT NULL DEFAULT 0,
    read_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (track_id, item_id)
);
-- Telegram posts already read for strikes (tracks/strikes.py), whether or not they reported any.
CREATE TABLE IF NOT EXISTS strike_reads (
    item_id         bigint PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    strikes         integer NOT NULL DEFAULT 0,
    read_at         timestamptz NOT NULL DEFAULT now()
);
-- done | none | failed, once the post's photos have been looked for (social/media.py)
ALTER TABLE strike_reads ADD COLUMN IF NOT EXISTS media_status text;
-- Photos saved from posts (data/telegram/media), shown on the map.
CREATE TABLE IF NOT EXISTS item_media (
    item_id         bigint NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    n               integer NOT NULL,
    path            text NOT NULL,                  -- relative to data/telegram/media
    kind            text NOT NULL,                  -- photo | video_preview
    bytes           integer,
    saved_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (item_id, n)
);

-- Social channels (Telegram first). Each has a source row so its posts flow through the
-- pipeline like articles; the scout (social/scout.py) scores, discovers, promotes and drops them.
CREATE TABLE IF NOT EXISTS social_accounts (
    id              serial PRIMARY KEY,
    platform        text NOT NULL,                  -- telegram
    handle          text NOT NULL,
    name            text,
    status          text NOT NULL DEFAULT 'candidate', -- following | candidate | paused | removed
    pinned          boolean NOT NULL DEFAULT false,  -- you want it followed whatever its score
    banned          boolean NOT NULL DEFAULT false,  -- you never want it
    added_by        text NOT NULL DEFAULT 'discovered', -- seed | you | discovered
    discovered_from text,
    desk            text,
    kind            text,                           -- news | osint | official | milblogger | state
    lean            text,
    subscribers     integer,
    source_id       integer REFERENCES sources(id),
    last_post_id    bigint NOT NULL DEFAULT 0,
    last_checked_at timestamptz,
    next_check_at   timestamptz NOT NULL DEFAULT now(),
    last_error      text,
    score           real,
    stats           jsonb NOT NULL DEFAULT '{}'::jsonb,
    status_reason   text,
    status_changed_at timestamptz NOT NULL DEFAULT now(),
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (platform, handle)
);
CREATE INDEX IF NOT EXISTS social_accounts_due_idx ON social_accounts (next_check_at) WHERE status IN ('following', 'candidate');
-- Channels that followed channels forward or link to: where new candidates come from.
CREATE TABLE IF NOT EXISTS social_mentions (
    platform        text NOT NULL,
    handle          text NOT NULL,
    from_account    integer NOT NULL REFERENCES social_accounts(id) ON DELETE CASCADE,
    kind            text NOT NULL,                  -- forward | link
    n               integer NOT NULL DEFAULT 1,
    last_seen       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (platform, handle, from_account, kind)
);
-- Channels read through a logged in Telegram account (social/telegram_live.py) rather than
-- their web page: real time, and channels with no public page.
ALTER TABLE social_accounts ADD COLUMN IF NOT EXISTS via text NOT NULL DEFAULT 'web';   -- web | api
ALTER TABLE social_accounts ADD COLUMN IF NOT EXISTS tg_id bigint;
CREATE INDEX IF NOT EXISTS social_accounts_tg_idx ON social_accounts (tg_id) WHERE tg_id IS NOT NULL;

-- Investigations: a story you want to get to the bottom of (investigate/). You give links and
-- what you want to know; Wassup reads them, finds related reports, follows what each cites back
-- toward the originals, searches for more, and keeps an evidence-based summary.
CREATE TABLE IF NOT EXISTS investigations (
    id              serial PRIMARY KEY,
    title           text NOT NULL,
    brief           text NOT NULL DEFAULT '',       -- what you want to know
    status          text NOT NULL DEFAULT 'active', -- active | paused | done
    watch_until     timestamptz,
    queries         jsonb NOT NULL DEFAULT '[]'::jsonb,
    summary         jsonb,
    summary_at      timestamptz,
    summary_sources integer NOT NULL DEFAULT 0,     -- relevant sources the summary was written from
    related_at      timestamptz,
    searched_at     timestamptz,
    meta            jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS investigation_sources (
    id              bigserial PRIMARY KEY,
    investigation_id integer NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
    url             text NOT NULL,
    item_id         bigint REFERENCES items(id) ON DELETE SET NULL,
    kind            text NOT NULL DEFAULT 'article', -- article | youtube | telegram | x | facebook | document | pasted
    found_by        text NOT NULL,                  -- you | wassup | search | traced
    parent_id       bigint REFERENCES investigation_sources(id) ON DELETE SET NULL,  -- the source that cited it
    depth           integer NOT NULL DEFAULT 0,
    status          text NOT NULL DEFAULT 'pending', -- pending | fetched | analyzed | unrelated | failed
    title           text,
    outlet          text,
    author          text,
    published_at    timestamptz,
    thumbnail       text,
    similarity      real,
    analysis        jsonb,
    meta            jsonb NOT NULL DEFAULT '{}'::jsonb,
    error           text,
    pinned          boolean NOT NULL DEFAULT false,
    hidden          boolean NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (investigation_id, url)
);
CREATE INDEX IF NOT EXISTS investigation_sources_status_idx ON investigation_sources (investigation_id, status);
-- Leads: what to check next, planned and followed up by the Investigator agent (or added by you).
CREATE TABLE IF NOT EXISTS investigation_leads (
    id              bigserial PRIMARY KEY,
    investigation_id integer NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
    title           text NOT NULL,
    why             text NOT NULL DEFAULT '',
    how             text NOT NULL DEFAULT '',
    status          text NOT NULL DEFAULT 'open',   -- open | working | done | dead_end | blocked | dropped
    finding         text,
    urls            jsonb NOT NULL DEFAULT '[]'::jsonb,
    added_by        text NOT NULL DEFAULT 'investigator',  -- investigator | you
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS investigation_leads_idx ON investigation_leads (investigation_id, status);
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS memo text;              -- the Investigator's notes
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS memo_at timestamptz;
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS investigator_at timestamptz;  -- last hand-off to the Investigator
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS investigator_issue text;
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS ask_note text;          -- you asked the Investigator to look now
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS ask_at timestamptz;
