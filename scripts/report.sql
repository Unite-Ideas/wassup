-- Wassup status report. Run from ~/wassup:
--   docker compose exec -T db psql -U wassup -d wassup < scripts/report.sql
\pset footer off
\echo
\echo ===== ARTICLES =====
SELECT count(*) AS total_articles,
       count(*) FILTER (WHERE collected_at > now() - interval '24 hours') AS last_24h,
       count(*) FILTER (WHERE collected_at > now() - interval '72 hours') AS last_72h,
       count(*) FILTER (WHERE status = 'clustered') AS clustered,
       count(*) FILTER (WHERE status = 'new') AS waiting,
       count(*) FILTER (WHERE status = 'error') AS errors,
       count(*) FILTER (WHERE language IS NOT NULL AND language <> 'en') AS non_english,
       count(*) FILTER (WHERE title_en IS NOT NULL) AS translated,
       to_char(min(collected_at), 'Mon DD HH24:MI') AS first_collected
FROM items;

\echo ===== ARTICLES PER DAY =====
SELECT to_char(date_trunc('day', collected_at), 'Dy Mon DD') AS day, count(*) AS articles,
       count(DISTINCT story_id) AS stories_touched
FROM items WHERE collected_at > now() - interval '7 days'
GROUP BY date_trunc('day', collected_at) ORDER BY date_trunc('day', collected_at);

\echo ===== ARTICLES BY SOURCE TYPE (last 72h) =====
SELECT s.kind, count(*) AS articles, count(DISTINCT coalesce(i.outlet, s.name)) AS outlets
FROM items i JOIN sources s ON s.id = i.source_id
WHERE i.collected_at > now() - interval '72 hours' GROUP BY s.kind ORDER BY 2 DESC;

\echo ===== TOP LANGUAGES (last 72h) =====
SELECT coalesce(language, '?') AS language, count(*) AS articles FROM items
WHERE collected_at > now() - interval '72 hours' GROUP BY 1 ORDER BY 2 DESC LIMIT 10;

\echo ===== STORIES =====
SELECT count(*) AS total_stories,
       count(*) FILTER (WHERE routed) AS tracked,
       count(*) FILTER (WHERE NOT routed) AS cold_storage,
       count(*) FILTER (WHERE last_seen > now() - interval '24 hours') AS active_24h,
       count(*) FILTER (WHERE breaking) AS breaking_now,
       count(*) FILTER (WHERE lat IS NOT NULL) AS on_the_map,
       count(*) FILTER (WHERE item_count >= 10) AS ten_plus_articles
FROM stories;

\echo ===== WHY STORIES WENT TO COLD STORAGE =====
SELECT coalesce(excluded_reason, '(none)') AS reason, count(*) AS stories FROM stories
WHERE NOT routed GROUP BY 1 ORDER BY 2 DESC LIMIT 8;

\echo ===== STORIES PER DESK (active last 72h) =====
SELECT desk, count(*) AS stories, sum(item_count) AS articles, round(avg(significance)::numeric, 2) AS avg_sig
FROM stories WHERE routed AND last_seen > now() - interval '72 hours' GROUP BY desk ORDER BY 2 DESC;

\echo ===== BIGGEST STORIES (last 72h) =====
SELECT left(coalesce(title_en, title), 70) AS story, desk, item_count AS arts, source_count AS outlets,
       round(significance::numeric, 1) AS sig
FROM stories WHERE routed AND last_seen > now() - interval '72 hours'
ORDER BY significance DESC, item_count DESC LIMIT 15;

\echo ===== TRIAGE DECIDED BY =====
SELECT coalesce(triage->>'backend', '(not yet)') AS decided_by, count(*) AS stories FROM stories
WHERE updated_at > now() - interval '72 hours' GROUP BY 1 ORDER BY 2 DESC;

\echo ===== LOCATIONS =====
SELECT coalesce(location_source, '(none)') AS placed_by, count(*) AS stories,
       count(*) FILTER (WHERE location_locked) AS locked
FROM stories WHERE routed GROUP BY 1 ORDER BY 2 DESC;

\echo ===== NEWSROOM AGENTS =====
SELECT name, kind, status, to_char(last_run_at, 'Dy HH24:MI') AS last_run
FROM newsroom_agents ORDER BY kind, name;

\echo ===== NEWSROOM ACTIVITY (last 72h) =====
SELECT kind, count(*) AS events FROM newsroom_events
WHERE created_at > now() - interval '72 hours' GROUP BY kind ORDER BY 2 DESC;
SELECT kind AS brief_kind, count(*) AS briefs FROM briefs
WHERE created_at > now() - interval '72 hours' GROUP BY kind ORDER BY 2 DESC;
SELECT s.id AS standup, s.status, to_char(s.created_at, 'Dy HH24:MI') AS called,
       count(r.agent_key) AS reports
FROM standups s LEFT JOIN standup_reports r ON r.standup_id = s.id
GROUP BY s.id ORDER BY s.id DESC LIMIT 6;

\echo ===== RECENT NEWSROOM ERRORS =====
SELECT to_char(created_at, 'Dy HH24:MI') AS at, agent_key, left(text, 90) AS error FROM newsroom_events
WHERE kind = 'error' ORDER BY created_at DESC LIMIT 8;

\echo ===== FEEDS WITH ERRORS =====
SELECT key, left(last_error, 70) AS last_error FROM sources WHERE last_error IS NOT NULL ORDER BY key;

\echo ===== PAID API SPEND (Jev) =====
SELECT provider, sum(requests) AS requests, sum(input_tokens) AS tokens_in, sum(output_tokens) AS tokens_out,
       '$' || round(sum(cost_usd), 4) AS total_cost
FROM api_usage GROUP BY provider;
SELECT day, provider, requests, '$' || round(cost_usd, 4) AS cost FROM api_usage
WHERE day > current_date - 7 ORDER BY day, provider;

\echo ===== DISK USED BY THE DATABASE =====
SELECT pg_size_pretty(pg_database_size(current_database())) AS database_size;
SELECT relname AS table_name, pg_size_pretty(pg_total_relation_size(relid)) AS size
FROM pg_catalog.pg_statio_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 8;
