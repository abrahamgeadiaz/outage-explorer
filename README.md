# Outage Explorer

Explore and query U.S. nuclear outage data from the [EIA Open Data API](https://www.eia.gov/opendata/)
(national, facility and generator grains) without exporting it into spreadsheets by hand.

> Status: all four parts are done: data connector, data model, API and web app.

## Parts

1. **Data connector**: extracts the three EIA nuclear outage routes into local Parquet.
2. **Data model & verification**: typed model, fleet-offline metric, reconciliation and anomalies.
3. **Backend service** (FastAPI): catalog, data preview, read-only SQL, refresh, with role-based access.
4. **Web app** (React + Vite): login, datasets, data table, SQL box.

## Stack

Python 3.13 · uv · httpx · DuckDB · Parquet · FastAPI · sqlglot · React · TypeScript · Vite

## Architecture

```
EIA API ──► connector ──► data/raw/<dataset>/year=YYYY/data.parquet        (outage-extract)
                              │
                              ▼
            model build ──► data/model/builds/<id>/ + current.json          (outage-model)
                              │   facts, dimensions, US-08 metric, dq_issue
                              ▼
            serving files ─► data/serve/<build>/<grant>.duckdb   one read-only copy per role grant
                              ▲
     FastAPI (outage-api) ────┘   auth · policy · catalog · preview · SQL · refresh job
                              ▲
     React web app ───────────┘   /api through the Vite dev proxy (one origin, no CORS)
```

- **Storage is files.** Parquet for raw extracts and model builds; DuckDB runs inside the process to
  build and query them (DECISIONS.md #7). A build is written to a new folder and `current.json`
  switches to it atomically, so readers never see a half-written model (#15).
- **Every request is authorized by one policy module** (`api/policy.py`): capabilities per role decide
  which endpoints it may call; column-level allow-lists decide which data it may see (#20).
- **The data a role may not see isn't in the database it queries.** Each grant gets its own
  read-only DuckDB file holding only its tables and columns, opened with file access off, the
  configuration locked and memory and threads capped (#21, #27).
- **A SQL query** goes: access token → `query` capability → `sqlguard` (one `SELECT`, plain table
  names, allow-listed functions, every table resolved through CTEs and subqueries) → every table
  checked against the grant → cache lookup → one of 4 query slots → the SQL *regenerated from the
  checked tree* runs on the caller's serving file, with the page merged into its `LIMIT`, a 10 s
  timeout and a 20 MB result cap (#5, #6, #23, #24, #27).
- **Refresh** is an Admin-only background job: extract incrementally, rebuild the model, clear the
  query cache; the next request reads the new build (#4).

```
backend/src/outage_explorer/
  connector/   EIA client (paging, retries), validation, Parquet storage, extraction runs
  model/       table definitions, model build, data quality checks, outage-sql
  api/         FastAPI app: auth, users, policy, catalog, preview, sqlguard, query, cache, store, refresh
frontend/src/  API client, session, pages (datasets, dataset, fleet/US-08, SQL, refresh), tests
analysis/      findings.sql and the A1 check behind FINDINGS.md
```

## Quick start

Requirements: [uv](https://docs.astral.sh/uv/) (it installs the right Python itself), Node 20+ and a
free [EIA API key](https://www.eia.gov/opendata/register.php).

```bash
cp .env.example .env          # then set EIA_API_KEY and JWT_SECRET (signs API tokens)
cd backend
uv sync                       # creates .venv with all dependencies
uv run pytest                 # all tests run offline against recorded EIA responses
uv run outage-extract --full  # first load: every day since 2007 (~40 min, EIA is slow; resumable)
uv run outage-model           # build the model (~25 s)
uv run outage-api             # API on http://127.0.0.1:8000

cd ../frontend                # in a second terminal
npm install
npm run dev                   # web app on http://localhost:5173
```

Sign in with a [test user](#test-users), for example `analyst` / `analyst-demo-pass`. After the
first load, `uv run outage-extract` (or Refresh as `admin` in the web app) only fetches what changed.

### Configuration

Set in `.env` (see `.env.example`) or the environment. Only the first two are required.

| Variable | Default | What it does |
|---|---|---|
| `EIA_API_KEY` | none | EIA Open Data key (sent as a query parameter; masked in every log) |
| `JWT_SECRET` | none | signs access tokens; at least 32 characters or the API refuses to start |
| `DATA_DIR` | `./data` | raw extracts, model builds, serving files, run metadata |
| `EIA_MAX_CONCURRENCY`, `EIA_MAX_RETRIES`, `EIA_TIMEOUT_SECONDS` | 3, 5, 180 | connector parallelism and retries |
| `REFRESH_LOOKBACK_DAYS` | 30 | incremental runs re-fetch this many days before the watermark |
| `JWT_TTL_MINUTES` | 60 | access token lifetime |
| `LOGIN_MAX_FAILURES`, `LOGIN_MAX_FAILURES_PER_CLIENT`, `LOGIN_WINDOW_SECONDS` | 5, 20, 300 | login rate limit |
| `QUERY_TIMEOUT_SECONDS`, `QUERY_MAX_RESULT_BYTES`, `QUERY_MAX_CONCURRENT` | 10, 20 MB, 4 | limits on user SQL |
| `QUERY_CACHE_TTL_SECONDS`, `QUERY_CACHE_MAX_BYTES` | 300, 100 MB | query result cache (`0` disables) |

## Data connector

```bash
cd backend
uv run outage-extract                      # incremental: from each dataset's watermark - 30 days
uv run outage-extract --full               # everything since 2007-01-01
uv run outage-extract --dataset national   # one dataset (repeatable)
uv run outage-extract --start 2026-09-01   # explicit window (--end too)
```

What it does:

- Calls `us-nuclear-outages`, `facility-nuclear-outages` and `generator-nuclear-outages` (daily),
  5000 rows per page, sorted by the natural key so offset pagination is stable.
- Validates every row (see below) and writes typed Parquet to
  `data/raw/<dataset>/year=<YYYY>/data.parquet`.
- Works in one chunk per dataset and year, up to `EIA_MAX_CONCURRENCY` (default 3) in parallel.
- Records every run in `data/_meta/runs/<run_id>.json` (status, rows fetched/rejected,
  inserted/updated/unchanged/deleted, data quality summary, errors) and the watermarks in
  `data/_meta/state.json`.

**Re-running is safe.** The rows fetched for a window *replace* that window in the partition, keyed on
the natural key, so two runs never duplicate data; the second reports `inserted: 0`. Files are written to
a temp file and swapped in atomically, and a lock file (`data/_meta/extract.lock`) stops two
extractions from running at the same time.

**Kept current.** Incremental runs re-fetch from 30 days before the stored watermark up to today, so they
add new days and also pick up values EIA revised recently (counted as `updated`).

### Validation

| Field | Rule | If it fails |
|---|---|---|
| `period`, natural-key ids (`facility`, `generator`) | present, valid date, inside the requested window | row rejected |
| `capacity`, `outage` | present, numeric, finite | row rejected |
| `*-units` | `megawatts` / `percent` | row rejected (a unit change would corrupt sums) |
| `percentOutage` | numeric if present | kept as null, counted as a warning |
| outage > capacity, negative values, duplicate keys | — | kept, counted as warnings for the data model |
| fields we don't know | — | kept out of the file, reported as `unknown_fields` (schema drift) |

### Behavior on failure

| Situation | Behavior | Exit code |
|---|---|---|
| `EIA_API_KEY` missing | fails before any request | 2 |
| Key rejected (HTTP 403 `API_KEY_INVALID`) | not retried; no further chunks start; nothing written; watermarks unchanged | 1 (run `failed`) |
| Another extraction running | refused (lock older than 6 h is treated as stale) | 2 |
| Timeout, connection error, HTTP 5xx (EIA sends 504s as HTML), 200 with non-JSON body | retried up to `EIA_MAX_RETRIES` (5) with jittered exponential backoff | — |
| HTTP 429 | retried after `Retry-After` | — |
| Still failing after retries / unexpected response shape / every row in a chunk invalid | that chunk's partition is left untouched, other chunks finish; run `partial`; that dataset's watermark does not move, so the next run re-fetches the gap | 1 |
| API returns 0 rows for a window we already hold | refused, the stored rows are kept | 1 |

The API key is sent as a query parameter (EIA's only option), so it is masked (`api_key=***`) in all logs,
including httpx's own request log, and never appears in error messages or run metadata.

## Data model

```bash
cd backend
uv run outage-model           # builds data/model from data/raw (~25 s for the full history)
uv run outage-sql ../analysis/findings.sql          # re-run every query behind FINDINGS.md
uv run outage-sql --duckdb ../data/explore.duckdb   # views for a database manager (DBeaver, DataGrip)
```

The ER diagram, keys and types are in [docs/model.md](docs/model.md).

Every build goes to a new folder, `data/model/builds/<build_id>/`. `data/model/current.json` then
switches to it atomically, so readers never see a half-written model. The newest 3 builds are kept.

| Table | Grain (primary key) | Contents |
|---|---|---|
| `fact_national_daily` | `period` | U.S. fleet capacity, outage and percentage per day |
| `fact_facility_daily` | `period, facility_id` | the same per plant |
| `fact_generator_daily` | `period, facility_id, generator_id` | the same per unit (finest grain) |
| `dim_facility` | `facility_id` | plant name, first and last day reported, units, status |
| `dim_generator` | `facility_id, generator_id` | unit lifetime, reported and missing days, latest capacity, status |
| `metric_fleet_offline_daily` | `period` | **US-08**: % of fleet capacity offline, plus missing units and a min/max range |
| `dq_issue` | `issue_id` | data quality findings from the checks run on every build |

MW values are `DECIMAL(12,3)`, so the grains add up exactly. `percent_outage` is recomputed from MW
(NULL when capacity is 0). EIA's rounded value is kept as `percent_outage_eia`. Column descriptions
live in `backend/src/outage_explorer/model/tables.py`.

**US-08 metric.** `offline_pct = 100 × Σ outage_mw / Σ capacity_mw` over the units EIA reported that
day, which matches EIA's national percentage. Some days a unit is missing from every grain: it is
not filled in. `missing_units`, `missing_capacity_mw` and `offline_pct_min`/`offline_pct_max` show
what is left out, and `is_complete` is false (DECISIONS.md #17).

**Data quality checks** run on every build and write `dq_issue` (counts are also in `current.json`):

| Check | Severity | Catches |
|---|---|---|
| `grain_mismatch_national` | error | national ≠ Σ facility or Σ generator for a day, or a grain missing a day |
| `grain_mismatch_facility` | error | a facility ≠ Σ its generators, or one side missing |
| `unit_reporting_gap` | warning | a unit absent from every grain inside its lifetime (with the MW at stake) |
| `non_positive_capacity` | error / warning | capacity ≤ 0; an error when EIA still reports an outage or percentage |
| `percent_mismatch` | error | EIA's percentage off by more than its rounding |
| `value_out_of_range` | error | negative values, outage > capacity |
| `unit_lifecycle` | info | a unit first reported late, or no longer reported |
| `duplicate_keys` | warning | raw rows sharing a natural key (latest kept) |

## API

```bash
cd backend
uv run outage-api             # http://127.0.0.1:8000, interactive OpenAPI docs at /docs
```

### Test users

One seeded user per persona (demo passwords, for local use only; only their hashes are in the code):

| Username | Password | Role | Can |
|---|---|---|---|
| `analyst` | `analyst-demo-pass` | Analyst | catalog, preview, SQL on every dataset |
| `viewer` | `viewer-demo-pass` | Viewer | the same, national datasets only |
| `admin` | `admin-demo-pass` | Admin | everything an Analyst can, plus refresh |

```bash
curl -s -X POST localhost:8000/api/auth/login -H 'Content-Type: application/json'      -d '{"username": "analyst", "password": "analyst-demo-pass"}'
# -> {"access_token": "...", "token_type": "bearer", "expires_in": 3600, "user": {...}}
curl -s localhost:8000/api/auth/me -H "Authorization: Bearer <access_token>"
```

| Endpoint | Capability | Returns |
|---|---|---|
| `POST /api/auth/login` | none | access token + user, role, capabilities |
| `GET /api/auth/me` | logged in | the current user |
| `POST /api/auth/logout` | logged in | `204`; revokes this token (later requests with it are `401`) |
| `GET /api/datasets` | catalog | datasets the caller may see: columns, types, definitions, row counts, filters |
| `GET /api/datasets/{name}` | catalog | one of them (`403` if not granted, `404` if unknown) |
| `GET /api/datasets/{name}/rows` | preview | rows filtered by `start`, `end` (dates, inclusive) and `facility_id` (repeatable), ordered by key; `limit` ≤ 1000, `offset` ≤ 10,000,000, `total` |
| `POST /api/query` | query | `{"sql": "...", "max_rows": 1000, "offset": 0}` → columns, rows, `truncated`, `next_offset`, the tables read |
| `POST /api/refresh` | refresh | `202` + job: extract from EIA, rebuild the model (`409` if one is running) |
| `GET /api/refresh[/{job_id}]` | refresh | recent jobs / one job: step, status, rows, new build, DQ counts, error |
| `GET /api/health` | none | liveness and the model build being served |

Viewers see `fact_national_daily` and `metric_fleet_offline_daily` (without `missing_units`, which
names units) and nothing else (DECISIONS.md #20). Requests read a copy of the model that holds
only the caller's datasets, opened read-only with file access off (`data/serve/`, DECISIONS.md #21).

**SQL.** One read-only query: `SELECT`, joins, CTEs, subqueries, set operations, aggregates and
window functions over the dataset names in the catalog. Every table the query reads, including tables
in subqueries and CTEs, must be granted to the caller, or the whole query is refused. Writes, settings,
table functions (`read_parquet`...), file paths and functions outside an allow-list (DECISIONS.md #27)
are rejected. Results come `max_rows` (≤ 5000) at
a time; send `offset = next_offset` for the next page (stable when the query has an `ORDER BY`).
Queries stop after 10 s (`QUERY_TIMEOUT_SECONDS`), and a page larger than 20 MB is refused
(`QUERY_MAX_RESULT_BYTES`). At most 4 queries run at once (`QUERY_MAX_CONCURRENT`); past that,
`503 server_busy`. Results are cached for 5 minutes per role grant and model build (`cached: true` in
the response; `QUERY_CACHE_TTL_SECONDS`, `QUERY_CACHE_MAX_BYTES`, `0` disables), and every refresh
clears the cache (DECISIONS.md #24). See DECISIONS.md #5 and #6.

```bash
curl -s -X POST localhost:8000/api/query -H "Authorization: Bearer <access_token>" \
     -H 'Content-Type: application/json' \
     -d '{"sql": "SELECT period, offline_pct FROM metric_fleet_offline_daily ORDER BY period DESC LIMIT 7"}'
```

**Refresh (Admin).** `POST /api/refresh` (body `{"full": true}` to re-extract from 2007) returns
`202` with a job; poll `GET /api/refresh/{job_id}` until `status` is `succeeded`, `partial`, `failed`
or `interrupted`. `GET /api/refresh` lists recent jobs. An incremental refresh takes about 35 s; the new
build is served from the next request (DECISIONS.md #4).

Every error has the same shape, `{"error": {"code": "...", "message": "..."}}`: `401 unauthenticated`
(no, invalid, expired or signed-out token), `401 invalid_credentials`, `403 forbidden`, `429 rate_limited`
(5 failed logins for a username from one address, or 20 from one address, within 5 minutes;
`Retry-After` says when to try again), `400 filter_not_supported` / `invalid_filter`,
`400` SQL rejections (`read_only`, `one_statement_only`, `function_not_allowed`, `source_not_allowed`,
`unknown_table`, `syntax_error`, `query_error`, `result_too_large`), `408 query_timeout`,
`409 refresh_in_progress`, `404 dataset_not_found`, `422 invalid_request`, `503 model_not_built`,
`503 server_busy`.

## Web app

Requirements: Node 20+ (tested with Node 24). The API must be running (`uv run outage-api`).

```bash
cd frontend
npm install
npm run dev                   # http://localhost:5173 (proxies /api to http://127.0.0.1:8000)
npm test                      # component tests (Vitest + Testing Library), offline
npm run build                 # type-check + production build into frontend/dist
```

Sign in with one of the test users above. What each screen does:

| Screen | Who | What |
|---|---|---|
| Datasets | everyone | the datasets your role may see, grouped, with row counts |
| Dataset page | everyone (`403` → "not allowed") | columns with types and definitions; rows filtered by date and facility, paged by the API; the URL keeps filters and page |
| Fleet offline | everyone | **US-08** chart: daily % of capacity offline, summary tiles, shading where units are missing, the min–max range in the tooltip, table view |
| SQL | roles with `query` | editor (Ctrl+Enter), results with paging, timing and `cached`, errors with their code, schema panel, history, CSV export (formula cells neutralized) |
| Refresh | Admin | start an incremental or full refresh, follow its steps, see rows, the new build, DQ findings or why it failed |

The session survives reloads (token in `localStorage`, checked with `/api/auth/me`). Sign out revokes
the token on the server too; when it expires, you're signed out with a notice. Hiding links is only a
convenience: every request is authorized by the API (DECISIONS.md #26).

## Assumptions

- **EIA is the source of truth, grain by grain.** Values are stored as EIA sends them; one grain is
  never rewritten from another. On the real data the grains agree exactly, so a future mismatch is
  reported (`dq_issue`), not repaired (DECISIONS.md #3).
- **A unit missing from every grain on a day is unknown, not offline and not zero.** US-08 uses EIA's
  own denominator (the units reported that day) and shows what is missing beside it, with a min–max
  range, instead of filling the gap (#17).
- **Natural keys** are `period`, `(period, facility)` and `(period, facility, generator)`. Ids stay
  strings as EIA sends them (#1). Days are EIA's calendar days; there is no time of day or time zone.
- **EIA revises recent days only.** Incremental runs re-fetch the last 30 days. Two re-fetches so far
  found no revisions at all, so the window is an assumption, not a measurement (#2, FINDINGS.md).
- **Capacity steps on Apr 1 and Oct 1 are real** (summer and winter ratings), and a unit that stops
  being reported has retired or is out of service: EIA publishes no marker for either (FINDINGS.md A5, A7).
- **"National trends only" for Viewers** means the national table and the fleet metric, without the
  one column that names units (`missing_units`). Every other table is keyed by facility or generator,
  so Viewers don't get it (#20).
- **The users are known staff.** Three seeded users (one per persona) are enough for this version, as
  the brief allows. SQL is for signed-in users, not the open internet (#19, #27).
- **One server, local use.** The API runs as one process on one machine, which is what makes the
  in-memory state below acceptable.

## Known limitations

- **State is per process and in memory:** the login rate limiter, the query cache, revoked tokens and
  the refresh job's "one at a time" check (the connector's file lock backs it up). Several API workers
  would need a shared store (Redis) for each, and a restart forgets them (#4, #19, #24, #25).
- **Refresh runs in a thread of the API process**, not a task queue. A job running when the server
  stops is reported as `interrupted`, and the next refresh fetches its window again (#4).
- **No production deployment.** The web app runs on the Vite dev server, which proxies `/api`. There is
  no HTTPS, no security headers (CSP) and no request body limit; those belong to the reverse proxy that
  would serve the built app and the API together. The token is in `localStorage` rather than an
  httpOnly cookie (#26).
- **Users live in code**, with no user management, password change or refresh token; login would move
  to an identity provider (OIDC) in production (#19).
- **User SQL is contained, not isolated.** Engine limits, a function allow-list, a result cap and 4
  query slots bound what one query can take, but a query doesn't run in a separate process that could
  be killed, and the slots are shared by all users, so one user can make others wait. Some DuckDB
  functions are refused on purpose (`lpad`, `generate_series`, `printf`...) (#27).
- **Paging re-runs the query.** SQL pages line up only when the query has an `ORDER BY` over a unique
  key and the build doesn't change between pages; the response says both (`ordered`, `build_id`) (#23).
- **Raw history is overwritten.** A re-fetched window replaces the stored values and the previous ones
  are not kept (model builds keep the last 3). New EIA fields are reported (`unknown_fields`), not added
  to the schema automatically.
- **The first full load is slow** (~40 minutes): facility and generator pages take 10–60 s each and EIA
  often answers 504. It is resumable: a failed window is fetched again on the next run (#12).
- **Login rate limiting is per connection address.** Behind a reverse proxy it needs the proxy's real
  client address, and an attacker with many addresses is not stopped (#25).

## Documents

- `DECISIONS.md`: open design questions, choices and rejected alternatives
- `FINDINGS.md`: reconciliation results and data anomalies (queries in `analysis/`)
- `docs/model.md`: ER diagram, keys, types, notes for larger volumes
- `NOTES.md`: engineering notes (AI usage, what the AI got wrong, verification)
- `PRODUCT.md`: stories added, key UX decisions, what was left out and what comes next
