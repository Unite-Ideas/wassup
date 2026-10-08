# Wassup Architecture

Wassup is a global news intelligence system. It collects news and social media from everywhere, filters it against a learned interest profile, lets a newsroom of AI agents (run by Paperclip) research and link stories, and shows the result on an interactive globe and evidence board.

Guiding rules:

1. **Local first.** The workstation (1 TB RAM, RTX 6000 Ada 48 GB, 64 core Threadripper) does the heavy lifting. Cloud models are used only where they clearly earn their cost.
2. **Tokens are spent last.** Plain code, then local embeddings, then cheap typed decisions (Jev or a local classifier), then local LLMs. Frontier cloud models come last.
3. **Start small, scale out.** Everything runs in containers with a single Postgres, so it can move from one PC to a server or cluster without being rewritten.

---

## 1. The tiered cadence (how we avoid spending thousands a minute)

The key idea is that **collecting is cheap and thinking is expensive**, so the two run at very different speeds.

| Tier | Name | Runs | What it does | Cost |
| --- | --- | --- | --- | --- |
| T0 | Collectors | Continuously | Fetch RSS, GDELT, APIs, scrapers, social firehoses. Store raw items. No AI. | Zero tokens |
| T1 | Normalizer | Continuously | Extract article text, detect language, dedupe, embed (local), geotag, cluster into "story" groups. | Local GPU only |
| T2 | Triage | Continuously, per item or cluster | Fast typed decisions: Is this relevant? Which desk? Is this breaking? Is it part of a bigger story? | Jev (fractions of a cent) or local classifier |
| T3 | Desk agents | Hourly heartbeat (Paperclip) | Read their desk queue, update story threads, write briefs, propose links to other desks. | Mostly local LLM |
| T4 | Surge agents | Every 2 to 5 min, time boxed | Spawned when a breaking event is detected. They take over a fast-moving story, then retire. | Local LLM, capped budget |
| T5 | Editor in Chief | Daily, plus on demand | Daily brief, calls standups, approves or denies spawns, resolves cross-desk conflicts. | Claude (small, bounded usage) |

### Breaking news detection (T2 into T4)

A story cluster is flagged as **breaking** when several signals line up:

- **Velocity:** items per 15 minutes jumps well above that cluster's baseline.
- **Spread:** independent sources, countries, and languages start reporting it.
- **Jev check:** a typed question such as "Is this a major developing event?" returns yes with high confidence.

When a cluster crosses the threshold, the pipeline fires a **Paperclip webhook routine**. The Editor in Chief wakes up and decides whether to hire a surge agent for it. When velocity decays below a floor for a set period (or a hard time limit is reached), the surge agent hands its notes back to the parent desk and is terminated.

### Guardrails and caps

- Max active agents: **20** (configurable). Max concurrent surge agents: **5**.
- Every agent has a monthly budget in Paperclip. Hitting it pauses the agent.
- A global daily spend cap on cloud APIs. When it is hit, everything falls back to local models.
- Desks have an explicit **scope list**. Nobody researches the Super Bowl just because it is trending.

---

## 2. Where Jev fits

[Jev](https://docs.typesafe.ai/introduction) (TypeSafe AI) is a "System One" model. You send state plus typed questions (Choice, Score, true/false) and get structured answers with **calibrated confidence** back in about 100 ms, at roughly $0.04 per million input tokens and free output. That is exactly the shape of triage. It is a cloud API in early access, so we wrap it behind our own interface.

**The `Decider` interface** has two interchangeable backends:

- `JevDecider`: calls TypeSafe.
- `LocalDecider`: a local model through Ollama with constrained JSON output, plus a small classifier trained on your feedback.

**Confidence gated routing** (a pattern TypeSafe documents):

```
item -> embedding prefilter (local, drops obvious junk)
     -> Decider (Jev or local)
          high confidence yes -> route to desk
          high confidence no  -> cold storage
          low confidence      -> escalate to a local LLM for a second look
```

Example triage questions asked in one call, all evaluated in parallel:

- Choice: which desk? (`us_politics`, `russia_ukraine`, `iran_mideast`, `un_international`, `gov_releases`, `migration`, `none`)
- Score 0 to 5: geopolitical significance
- True/false: "This is primarily celebrity, sports, or entertainment news."
- True/false: "This story connects to an active government, military, or international conflict."
- True/false: "This is a primary source document release (FOIA, declassification, court filing)."

The excluded category combined with the "connects to a larger story" check handles your celebrity example: it gets through only when both are true.

Significance (0 to 5) has two parts. Triage judges importance: how much the event matters on its
own, whatever its coverage. Up to 2 points come from that. Up to 3 points come from coverage:
outlets on a log scale (7 outlets about 1.4, 30 about 2.2, 100 or more the full 3) plus a little
for each extra country. The coverage part is recomputed in the database (`wassup_significance`)
every time a story gains articles, so a story climbs as the world picks it up.

We run both backends side by side at first and compare them. If Jev is clearly better per dollar we lean on it. If the local model is close enough, we stay local.

**Status:** Jev is wired in as `JevDecider` (`pipeline/wassup/triage/jev.py`), asked only when the rules are unsure, with the local LLM after it when Jev is unsure too, and a daily spending cap. Earlier note: The `Decider` interface is in place (`pipeline/wassup/triage/decider.py`) with two working backends: `RulesDecider` (keywords, URL patterns, GDELT themes) and `OllamaDecider` (local LLM with a JSON schema). `HybridDecider` is the confidence gated router described above. A `JevDecider` is one new class when access arrives.

---

## 3. Sources

Start with a small set, then keep adding collectors. Each collector is a small plugin with the same output format.

**Phase 0 (proof of concept)**
- GDELT 2.0 event and GKG feeds (global, 100+ languages, updated every 15 minutes, already geotagged)
- Curated RSS list (wire services, major international outlets, regional outlets)
- Government: Congress.gov API, Federal Register API, UN press and meetings coverage

**Phase 2 and later**
- Bluesky Jetstream firehose
- Telegram public channels (important for Ukraine, Russia, Iran, Middle East)
- Reddit
- YouTube (API plus transcripts; Whisper locally when no transcript exists)
- X and Truth Social via headless Chromium scraping
- TikTok via scraping plus local Whisper on audio
- Agency FOIA reading rooms, National Archives, DoD and AARO releases, court dockets (CourtListener)
- Many more local and regional outlets per country, in original languages

**Source credibility.** Every source gets a record with country, owner, type, and a trust tier:

- Tier A: primary documents, wire services
- Tier B: established independent outlets
- Tier C: partisan or low reliability outlets
- Tier S: state media (RT, Press TV, Xinhua, and similar). Always flagged. Kept because what a state is telling its people is itself signal.

When claims conflict, independent sources outweigh state media, and the UI shows both sides.

---

## 4. Interest profile (learnable filter)

- **Rules:** explicit lists of topics you want and topics you don't. You edit them in the UI.
- **Learned layer:** thumbs up and thumbs down on any story in the UI train a small local model over story embeddings. It retrains nightly. Nothing leaves the machine.
- **Cold storage:** excluded items are still stored, embedded, and geotagged, but **not routed to any desk**. Agents can search cold storage on purpose (for example, "is this celebrity linked to the scandal we are tracking?"). Nothing in cold storage triggers research on its own.

---

## 5. The newsroom (Paperclip company)

```
Board (you)
 └── Editor in Chief (Claude)
      ├── US Politics and Congress desk
      ├── Russia and Ukraine desk
      ├── Iran and Middle East desk
      ├── UN and International Bodies desk
      ├── Government Releases desk (Epstein, JFK, UAP, FOIA)
      ├── Migration desk
      └── Surge agents (hired and retired on demand, report to a desk or the EiC)
```

- **Desk agents** use Paperclip's `http` or `process` adapter to call Wassup's own agent runtime, which talks to local models and the Wassup API (search stories, read cold storage, write briefs, propose links).
- **Heartbeats:** desks run hourly on a schedule. Surge agents run every 2 to 5 minutes.
- **Standups:** a daily scheduled standup routine. The Editor in Chief can also **call one at any time**: it creates a standup issue and assigns a child issue to each relevant desk. Desks answer with what they are tracking, what they suspect links to other desks, and what they need. The EiC merges this into a standup note that appears in the UI.
- **Spawning:** the EiC hires surge or topic agents on its own, within the caps in section 1. Going over a cap needs your approval through Paperclip's hire approval gate.
- **Shared memory:** agents do not pass giant documents around. They read and write to the shared story graph (section 6), so a Russia desk note about a Putin trip is instantly visible to the US desk.

---

## 6. Data model (the story graph)

One Postgres database with **pgvector** (similarity search) and **PostGIS** (geography).

- `sources`: outlet, country, language, trust tier, state media flag
- `items`: one article, post, video, or document. Raw text, translation, embedding, time, geo points
- `stories`: clusters of items about the same event. Summary, status, desk, significance, breaking flag. An article joins the closest
  live story when it is at least 0.75 similar (bge-m3, measured on real headlines), rising to
  0.80 as a story grows, so a big story's averaged centre cannot swallow the next event on the
  same subject; wire labels
  such as "(LEAD)" or "BREAKING:" are ignored for this, and junk pages (legal pages, site
  sections, job ads) are dropped at collection. Every 5 minutes, stories that grew side by side
  but are the same event (0.78 or closer, more for bigger stories) are merged, and their briefs, follows and feedback move
  with them
- `entities`: people, organizations, countries, places (with aliases across languages)
- `links`: typed, weighted edges between stories and entities: `involves`, `causes`, `responds_to`, `contradicts`, `same_actor`, `escalates`, `related`. Each link records who proposed it (agent or rule), confidence, and evidence item ids
- `briefs`, `standups`: agent written notes tied to stories
- `feedback`: your thumbs up and down

Links are what become the strings on the wall.

---

## 7. The UI (Palantir style)

A dark, dense, analyst style desktop web app.

- **Globe view:** 3D globe (three.js via globe.gl, or deck.gl). Nodes sized by story significance and colored by desk. Arcs connect linked stories across the world. Pulsing rings mark breaking stories.
- **Click a location:** side panel with every story and item from that place, filterable by desk, source tier, and time.
- **Click a story:** full story view with source items (original language plus translation), state media flagged, agent briefs, and every adjacent linked story.
- **Evidence board view:** toggle into a flat force directed graph (Sigma.js or Cytoscape) of a selected cluster, with stories, people, and organizations as cards and the links as red string. Drag, pin, expand neighbors.
- **Timeline scrubber:** rewind and replay how a story and its links grew over hours or days.
- **Newsroom panel:** active agents, what each is tracking, surge agents live, standup notes, daily brief.
- **Feedback:** thumbs up and down on everything to train the interest profile.

---

## 8. Technology choices

| Area | Choice | Why |
| --- | --- | --- |
| Pipeline workers | Python | Best libraries for scraping, text extraction, NLP, embeddings, Whisper |
| API | Python (FastAPI) | Shares code and models with the pipeline. Phase 0 change from the original plan |
| Agent runtime (Phase 1) | TypeScript (Node) | Matches Paperclip. Talks to the Wassup API over HTTP |
| UI | React, Vite, globe.gl / three.js, force-graph | Rich 3D globe and canvas evidence board |
| Database | Postgres 16 with pgvector and PostGIS | One store for text, vectors, geo, and graph edges |
| Queue | Item status column in Postgres, NATS or Redis later | Fewer moving parts for the proof of concept |
| Local models | Ollama at first, vLLM later for throughput | Ollama is easy. vLLM gets far more out of the GPU |
| Embeddings | bge-m3 (multilingual) | Strong cross-language matching, so a Farsi and an English article about the same event cluster together |
| Translation | Local LLM or NLLB | Stays local |
| Speech to text | Whisper large-v3 (local) | For video sources |
| Geotagging | GDELT geo when present, otherwise named entity recognition plus a local GeoNames gazetteer | No paid geocoding |
| Scraping | Playwright with headless Chromium, trafilatura for article text | |
| Orchestration | Paperclip (local install) | Org chart, heartbeats, budgets, hiring |

### Running on Windows 11

Recommended: **WSL2 (Ubuntu) plus Docker Desktop with GPU support.** Wassup, Postgres, and Paperclip run inside WSL2 and containers, which gives a Linux environment identical to a future server. The NVIDIA driver on Windows exposes the GPU to WSL2 and containers. Ollama can run natively on Windows or inside WSL2. vLLM needs WSL2 or Linux.

Give WSL2 a large memory allowance in `.wslconfig` (for example 512 GB) so models can spill into system RAM.

Alternative if you want maximum performance later: dual boot or dedicate the machine to Ubuntu. Not needed for the proof of concept.

### Local model plan for this hardware (starting point)

- Triage fallback: a small instruct model (7B to 14B class) for fast constrained JSON decisions
- Desk agents: a strong 30B to 70B class model, quantized to fit in 48 GB VRAM
- Very large mixture of experts models can use the 1 TB of system RAM for offload when a desk needs deeper reasoning (slower, still free)
- Exact models are chosen by benchmarking on real Wassup triage and brief tasks during Phase 1

---

## 9. Roadmap

**Phase 0: prove the data and the visual (no agents yet)** (built)
- Docker Compose: Postgres (pgvector, PostGIS)
- Collectors: GDELT, about 50 RSS feeds, Congress.gov, Federal Register
- Normalizer: text extraction, language detection, dedupe, bge-m3 embeddings, geotag, clustering
- Triage through the `Decider` interface (local first, Jev behind a flag)
- Globe UI with real stories, clickable locations and stories, similarity based links, and a basic timeline
- Done when: you open the globe and see today's world news filtered to your interests, and clicking around works
- Also built ahead of schedule: breaking detection, the evidence board, timeline replay, thumbs up and down feedback, Senate roll call votes

Phase 0 implementation notes:
- Clustering is per item against the stories active in the last 72 hours, using an exact in memory search over their centroids (no index tuning, millisecond lookups at this scale). A database only ever holds vectors from one embedder; `wassup rebuild-stories` re-clusters after a model change.
- Triage runs per story, not per article, and again whenever a story doubles in size, which keeps model calls to a small fraction of incoming articles.
- Each story's location is the place most of its articles mention, with places named in headlines counting triple.
- Links: "related" when centroid similarity is just below the same story threshold, "same_actor" when two stories share at least two people or organizations that appear in fewer than 15 stories (so a head of state does not link everything).
- Known gaps to address in Phase 1: GDELT geotags are noisy for some stories (a Bogota neighborhood called Kennedy); near duplicate stories still appear when phrasing differs a lot; no translation of non English headlines yet.

**Phase 1: the newsroom** (built; see [NEWSROOM.md](NEWSROOM.md))
- Install Paperclip locally, create the Wassup company
- Editor in Chief plus two desks (Russia and Ukraine, US Politics)
- Agent runtime and tools (search, cold storage lookup, write brief, propose link)
- Hourly heartbeats, daily standup, daily brief shown in the UI
- Agent proposed links drawn on the globe

**Phase 2: scale out**
- All six desks, surge detection, surge agents with caps
- Bluesky, Telegram, Reddit, YouTube collectors
- Evidence board view, full timeline replay, feedback trained interest model
- Move from Ollama to vLLM if throughput needs it

**Phase 3: go wide**
- X, Truth Social, TikTok scrapers, video transcription at scale
- Thousands of local and regional sources across many languages
- Source credibility scoring refinements
- Prepare for release: auth, multi user, moving off the single PC

## Map and tracks

- **Base map:** Protomaps PMTiles files (OpenStreetMap) in `data/maps`, served by the API from
  memory mapped files (`pipeline/wassup/maps.py`): `world.pmtiles` to zoom 9 everywhere, plus
  street level region files. The UI draws them with MapLibre GL in globe projection.
- **Tracks** (`tracks`, `track_snapshots`, `track_observations`): a front is a series of
  snapshots, each with unioned and lightly simplified areas (occupied, contested, liberated) and
  attack directions; a movement is a series of dated points. `/api/tracks/{id}/state?at=` returns
  the picture at any moment, with changes since an earlier snapshot computed in PostGIS.
- **Sources:** DeepStateMap for Ukraine (`pipeline/wassup/tracks/deepstate.py`), and migrant
  caravans read from the news by the local model (`pipeline/wassup/tracks/movement.py`), placed
  with GDELT's tags, known places and a list of towns along the routes (`data/towns.tsv`), the
  route cleaned day by day. Strikes read from Telegram posts by the local model
  (`pipeline/wassup/tracks/strikes.py`, its own lane), placed with GeoNames villages and towns in
  every script (`data/conflict_places.tsv.gz`, built by `scripts/build_conflict_places.py`),
  never with coordinates the model guesses; `strike_reads` records posts already read. Reports
  are grouped into strikes when the state is asked for, with the number of channels, sides and
  independent news outlets behind each. Photos of strike posts (`social/media.py`, table
  `item_media`, files in `data/telegram/media`, served at `/api/media/{item}/{n}`): web page
  posts by the strikes lane, posts from the logged in account by the live reader; photos and
  video preview images only. Next: GDELT and ACLED events, and photos placed on the map.

## Investigations

- `pipeline/wassup/investigate/`: `fetch.py` reads a link of any kind into one shape (text,
  date, author, outlet, thumbnail, and the links the source itself gives): articles with
  trafilatura (the date in the address wins over a page template's), YouTube with yt-dlp
  (description and the spoken language's transcript), Telegram posts from their embed page or
  the logged in account, X posts through fxtwitter, Facebook through its link preview (partial).
  `core.py` is the investigate lane: fetch, related items (same stories and nearest bge-m3
  vectors), per source analysis by the local model, citation tracing (two steps), outward
  search (GDELT DOC API, YouTube search, Telegram global search of joined channels) and the
  summary. Tables `investigations` and `investigation_sources`; read sources become items with
  their full text, so they join stories like anything else.

## Social channels

- **Telegram** (`pipeline/wassup/social/telegram.py`): public channels read from t.me/s pages
  every 15 minutes (candidates every 2 hours). Posts become items with their text as full text;
  forwards and t.me links are recorded in `social_mentions`.
- **Scout** (`pipeline/wassup/social/scout.py`): hourly. Scores each channel on 30 days of posts
  against independent (non social) coverage of the same stories: corroboration, earliness (with
  median lead time), relevance and originality, smoothed so small samples do not dominate.
  Discovers candidates from forwards and links, promotes, pauses and removes channels; your pins
  and bans always win. Thresholds are in `config/social.yaml`.

