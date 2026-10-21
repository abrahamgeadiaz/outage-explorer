# Recommendations

How I would take Outage Explorer past a single local server. Each section says what the code does
today, what breaks when it grows, and the change I'd make, in the order I'd make it. PRODUCT.md lists
the product features that come next; this page is about the engineering underneath. Numbers like #27
refer to DECISIONS.md.

| # | Recommendation | Needed when |
|---|---|---|
| 1 | Shared state for several API workers | more than one process or machine serves the API |
| 2 | Refresh in a worker process, jobs in a database | refreshes must survive API restarts and deploys |
| 3 | Each SQL query in its own process | SQL is opened to more users (#27) |
| 4 | Permissions per user, then per row | someone needs access that differs from their persona |
| 5 | Users in a database, sign-in through OIDC | more than the three seeded users |
| 6 | Keep raw history and a schema evolution policy | EIA revises old days or changes a field |
| 7 | Production deployment and observability | anyone outside one machine uses it |

## 1. Concurrency: running more than one API worker

**Today.** One uvicorn process. Five pieces of state live in its memory (`api/app.py`): the login rate
limiter, revoked tokens, the query slots, the query cache and the refresh jobs. Thread locks protect
them, which is enough within one process and wrong across several.

**What breaks with N workers**, and what doesn't:

| State | With N workers | Fix |
|---|---|---|
| Login limiter (`ratelimit.py`) | an attacker gets N × 5 tries per (client, username) | Redis: one sorted set of failure timestamps per key, trimmed to the window |
| Revoked tokens (`auth.py`) | a signed-out token still works on the other workers | Redis key per `jti`, expiring at the token's `exp` |
| Query slots (`query.py`) | N × 4 queries at once, N × the memory budget | a global semaphore (Redis), or accept per worker and size N × 4 to the machine |
| Refresh jobs (`refresh.py`) | `GET /api/refresh/{id}` is a 404 on a worker that didn't start it; a worker that restarts marks a job running on another worker as `interrupted` (`_load`) | section 2 |
| Query cache (`cache.py`) | **still correct**: the key includes the build id, so no worker can serve a result of an old build; only memory is duplicated | keep it per worker; move it to Redis only to share hits |
| Serving files (`store.py`) | **still correct**: built to a temp file and renamed atomically; two workers may build the same file once | a file lock if the duplicate second matters |
| Extraction (`connector/extract.py`) | **still correct**: the `extract.lock` file keeps out a second extraction or model build from any process | none |

The model itself needs no change: builds are versioned folders and `current.json` switches them
atomically (#15), so readers never see a half-built model, whichever worker they hit.

**Order:** revoked tokens and the limiter first, because they are security controls that silently
weaken. Then refresh jobs (section 2). The cache last, if ever.

## 2. Refresh as a real background job

**Today.** `POST /api/refresh` starts a thread in the API process (#4). If the process stops, the job
is lost and shows as `interrupted` after the restart. A deploy during a 40-minute full load kills it.

**Recommendation.** The API only records the job (a row in a `refresh_job` table: id, status, step,
requested_by, heartbeat) and enqueues it. A separate worker process (RQ, arq or Celery) runs
`run_extraction` and `build_model`, updates the row and sends a heartbeat. A job counts as
`interrupted` only when its heartbeat is old, not when some API worker starts. "One at a time"
becomes a unique partial index on `status = 'running'` (or a Redis lock), backed by `extract.lock`.

The job contract (`JobOut`) and the web page stay as they are; only where the state lives changes.

## 3. SQL isolation

**Today.** User SQL runs inside the API process with engine limits, a function allow-list, a row cap,
a 10 s interrupt and 4 shared slots (#27). It is contained but not isolated: a query that ignores
`interrupt()` holds a thread, and one user can take every slot.

**Recommendation.**
1. Run each query in a pool of worker processes; on timeout, kill the process instead of asking
   DuckDB to stop. The serving file is read-only, so a killed process leaves nothing to clean up.
2. Fair queueing: at most k slots per user (say 2 of 4), so one user waits behind themselves only.
3. A per-user query budget per minute, reported with `429` like the login limiter.

The guard (`sqlguard.py`), the grant check and the per-grant serving file stay; they decide *what* a
query may read, and isolation only decides *how much* it may take.

## 4. Adding permissions per user

**Today.** Every rule lives in `api/policy.py` and is keyed by role: `ROLE_CAPABILITIES` (which
endpoints) and `DATASET_GRANTS` (which tables and columns). Five call sites read them, all with
`user.role`: `auth.require`, `catalog.py` (×2), `preview.py`, `query.py` (×2).

The design already does the hard part: the serving file and the cache are keyed by `grant_key`, a
hash of the tables and columns a request may see (#21, #24), not by role. A user with a different
grant automatically gets their own serving file and their own cache entries.

### Step 1: per-user grants that can only narrow the role

The smallest safe change, and the one I'd make first:

1. Add `USER_GRANTS: dict[str, dict[str, frozenset[str] | None]]` in `policy.py`, keyed by username.
2. Change `granted_columns` and `granted_tables` to take the `User`. The result is the role's grant
   **intersected** with the user's override: a table must be in both, and a column too.
3. Pass `user` instead of `user.role` at the five call sites.
4. Tests: a user with an override sees only the intersection in the catalog, the preview and SQL;
   an override that lists a table the role lacks grants nothing; plus one property test that every
   user's grant is a subset of their role's.

Intersecting, never adding, keeps the US-05 guarantee by construction: no per-user entry can give a
Viewer a facility table, even by mistake. A wider permission means a new role, which is a reviewed
change to `policy.py`.

A new **capability** (for example `export`) follows the same path: a value in `Capability`, an entry
in `ROLE_CAPABILITIES`, and `Depends(require(Capability.EXPORT))` on the endpoint, with a test like
`test_only_admin_can_refresh_or_see_refresh_jobs` (`tests/test_api_refresh.py`) that calls it as
every role.

### Step 2: row-level grants

For example, "this analyst sees only these three plants". Two parts:

1. The grant gets a row filter, e.g. `facility_id IN ('6462', '1715')`, applied to every granted
   table that has a `facility_id` column. `store._build` adds it as a `WHERE` when it copies the
   table into the serving file, so the rows the user can't see are not in their file at all, the
   same guarantee the Viewer has today.
2. `grant_key` must include the filter. Otherwise two users with the same columns but different
   plants would share a serving file and cache entries. This is the one line that must not be missed,
   and it deserves its own test, next to `test_analyst_and_admin_share_a_file_and_viewer_has_its_own`
   (`tests/test_api_preview.py`).

Decide explicitly what happens to tables without a `facility_id` (national totals, the fleet metric):
they reveal no plant, so I'd keep them granted. Two columns need care: `missing_units` in the fleet
metric names units of every plant, so it must be withheld, as it is for the Viewer; and `dq_issue`
has a `facility_id` that is empty for national findings, so a plain `IN` filter would also hide those.

### Step 3: grants as data

Once more than a handful of users have overrides, move roles and grants from code into tables
(`user`, `role`, `user_grant`), managed by a new `manage_users` capability, with an audit log of who
changed what. The role is already looked up on every request (#19), so a change applies at once.
Keep the defaults in code and the allow-list rule: a dataset added to the model stays invisible until
granted.

## 5. Users and authentication

**Today.** Three users in `api/users.py` with scrypt hashes, HS256 tokens for 60 minutes, the token in
`localStorage` (#19, #26).

**Recommendation.** Delegate login to the organization's identity provider (OIDC, authorization code
with PKCE) and map its groups to roles. Then the API needs no passwords and no login limiter. Keep the
session in an httpOnly, SameSite cookie instead of `localStorage`, so an XSS bug can't read it. Rotate
the signing key with a `kid` header. Nothing else changes: authorization only needs a username and a
role, which is why `users.py` is separate from `policy.py`.

## 6. Data history and schema changes

- **Keep revised values.** Today a re-fetched window replaces the stored rows. Appending each run's
  rows with its `_run_id` (or a table format with time travel, such as Delta or Iceberg) would show
  what EIA changed and measure the real revision window, which is still an assumed 30 days (#2).
- **Schema evolution.** New EIA fields are reported, not stored. A policy: a new nullable field is
  added to the raw files automatically and reaches the model only through `tables.py` (and so through
  a grant, section 4); a removed or renamed required field keeps failing the window, as today.

## 7. Deployment and observability

- One reverse proxy (Caddy or nginx) serving the built web app and the API on one origin: HTTPS, CSP
  and security headers, a request body limit, and the real client address for the login limiter
  (`--proxy-headers` with trusted proxy IPs only).
- Structured logs with a request id; metrics for request latency by endpoint, query timeouts and
  `server_busy`, cache hit rate, refresh duration and outcome.
- Alerts: a refresh that failed or ended `partial`, and a new `dq_issue` with severity `error` after
  a build (the checks already run on every build, #18).
