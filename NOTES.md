# Engineering Notes

Running log, kept up to date while building — not written at the end.

## AI usage

| Part | AI-generated | Written / changed by hand |
|---|---|---|
| Repo skeleton (.gitignore, .env.example, README stub) | Claude Code | Reviewed |
| Backend scaffold, settings | Claude Code | Reviewed |
| EIA client (`connector/eia_client.py`) + tests | Claude Code | Reviewed; failure modes chosen from real API probes |
| Validation, storage, extraction orchestrator, CLI + tests | Claude Code | Reviewed; required-field and failure policies decided by me |
| Data model build (`model/`), DQ checks, `outage-sql`, FINDINGS.md queries + tests | Claude Code | Reviewed; types, versioning and the US-08 / missing-unit policy decided by me from the data findings |
| Web app (`frontend/`): session, routes, dataset, SQL, refresh and US-08 pages + component tests | Claude Code | Reviewed; screens checked in a real browser against the real API |
| API auth, policy, catalog, preview, serving store, SQL guard, query, refresh jobs (`api/`) + tests | Claude Code | Reviewed; Viewer column grant, role-per-request and the per-grant sandbox decided by me |
| Security review of the whole repo, the resource-limit fixes (DECISIONS.md #27) and the low findings (login limits, preview offset, logout, CSV) + tests | Claude Code | Reviewed; allow-list over block-list decided by me; scope and order of fixes chosen by me |

## Things the AI got wrong (and how I caught it)

1. **API key leaked into logs through httpx.** The generated client carefully raised its own exceptions
   with `from None` so tracebacks would not show the URL, and looked safe. But httpx itself logs
   `HTTP Request: GET https://...?api_key=<key>` at INFO on every call, so every refresh would have
   written the key into the logs. Caught by writing a test that captures *all* log output during a
   successful request and asserts the key is absent — it failed. Fixed with a logger-level redaction
   filter on `httpx`/`httpcore` (`RedactApiKeyFilter`).
2. **Exploration script crashed on Windows.** `Path.read_text()` without an encoding uses cp1252 on
   Windows and failed on the em dash in `.env`. All file I/O in the project passes `encoding="utf-8"`.
3. **Failed chunks lost their data quality details.** On error the orchestrator built a fresh, empty
   result, so a run where every row was rejected reported `rejected_by_reason: {}` — the one case where
   you most need the reasons. Caught by a test asserting the reasons appear in the run summary. Fixed by
   filling the result in place so partial information survives the exception.
4. **Placeholder code left in.** A generated `_summarise` contained two meaningless lines
   (`expected = ... and None; del expected`). Caught in code review before committing.
5. **Pruning old model builds could delete the live one.** Build ids were `<second>-<random>`, and old
   builds were pruned by sorting on the name. Two builds in the same second sort by the random suffix,
   so the newest build, the one `current.json` points to, could be deleted. Caught by a test that runs
   three builds in a row and checks which folders remain; it failed on the first run. Fixed with
   microseconds in the id and a guard that never prunes the current build.
6. **A data quality check whose test passed for the wrong reason.** The reporting-gap test used a 2-day
   gap. A mutation check (`> 1` changed to `> 2`, so 1-day gaps are no longer reported) still passed.
   The real data has 1-day gaps (River Bend, 2015-10-14), so I added a 1-day test; the mutation now fails.
7. **Wrong numbers from a findings query, and a claim the data did not support.** The seasonal query (A5)
   computed `lag()` after filtering to Apr 1 / Oct 1, so each "step" compared one season with the
   previous one. That produced values like −6,234.7 MW. Caught by comparing the output with the earlier
   exploration numbers; fixed by computing the step over every day first. Separately, a draft of
   FINDINGS.md said national capacity was 102,165.4 MW on complete days after 2015-10-27. Checking it
   showed there *are* no complete days in that stretch. Every number in FINDINGS.md was re-checked
   against a query before committing.

8. **A sandbox plan that would have let Viewers read generator data.** The plan for the API was "one
   DuckDB connection with views for the role's tables, file access disabled". Views need to read the
   Parquet files, so file access has to be allowed for the model folder (`allowed_directories`). A probe
   showed what that means: `read_parquet('.../fact_generator_daily/...')` then works from any SQL, and a
   view can't stop it. Caught by trying the bypass before writing the code. Each grant now gets its own
   read-only DuckDB file holding only its data (DECISIONS.md #21).
9. **The generated Viewer grant was going to be the whole metric table.** `missing_units` lists units
   as `'facility/generator'`, which is detail a Viewer must never see. I caught it reading the column
   list against US-05. The Viewer gets an allow-list of columns without it.

10. **CTE resolution silently did nothing.** The generated guard looked for a query's CTEs under
    `node.args["with"]`. sqlglot 30 renamed that key to `with_`, so the lookup always returned `None`,
    and `WITH t AS (...) SELECT * FROM t` was rejected as "no dataset named 't'". It failed safe, by
    over-rejecting, but CTEs were unusable. Caught by running the guard on a list of tricky queries
    before writing the tests. Fixed by finding the `With` node by type instead of by key name.
11. **DuckDB's Python replacement scans.** By default, `FROM name` in DuckDB's Python API falls back to
    a pandas or Arrow variable called `name` in the calling code. The plan didn't mention it. A probe
    confirmed it, and the sandbox now turns it off (`python_enable_replacements=false`).

12. **The US-08 chart showed a band nobody could see.** The generated chart drew the min–max range
    as a translucent area. It was there in the SVG (a 37k-character path), but a missing unit moves the
    share by under one percentage point, so over 5 years of daily points the line covered it. The
    "Incomplete days: 1,196" tile and an apparently complete chart disagreed. Caught by taking
    screenshots of the real app with a headless browser and comparing them with the numbers. Fixed by
    shading every run of incomplete days behind the line, keeping the band for short ranges and the exact
    range in the tooltip. The 2022–2025 Palisades gap and Vogtle 3 in 2023 are now visible.
13. **A side effect in render.** The refresh page first invalidated cached data from the render body
    when a job finished. That runs twice under StrictMode and can fire during a discarded render. I caught
    it in review and moved it into an effect keyed on the finished job.
14. **Retrying a state that can't fix itself.** The first query client retried every 5xx. `503
    model_not_built` lasts until an Admin refreshes, so users waited through the backoff to see "No data
    yet". The test for that screen timed out, which is how I found it.

15. **A SQL sandbox that bounded time but not memory.** The generated query path had a 10 s timeout
    and a 5,000-row cap, and it looked complete. A security review of the whole repo, probing the
    real guard and sandbox, showed what was missing: DuckDB ran each query with 80% of RAM and every
    core, `SELECT length(repeat('x', 500000000))` built 500 MB in 2.5 s for any role (Viewer
    included), and 5,000 rows of 1 MB strings would have gone into Python and JSON. The query cache
    was also bounded by rows, not size. Fixed with engine limits, a result size cap, a cache budget in
    bytes and a limit of 4 concurrent queries (DECISIONS.md #27).
16. **The first fix for it was not enough.** Setting DuckDB's `memory_limit` seemed to close the hole.
    Probing it showed that the limit counts memory held by operators (sorts, aggregates), not a single
    value built by a scalar function, and that an interrupt waits for that function to return:
    `repeat('x', 2e9)` still took 2 GB, and `range(1e9)` held a query for 50 s and ~8 GB past a 3 s
    interrupt. Almost every further probe found another amplifier (`lpad`, a width in `printf`, nested
    `replace`: 1 GB from 3 KB of SQL). A block-list would only have lasted until the next one, so the
    guard now accepts only an allow-list of 144 functions whose output is at most linear in their input.
17. **One endpoint bounded, its twin not.** The generated query endpoint capped `offset`; the preview
    endpoint only required it to be ≥ 0. The security review flagged it, and a probe confirmed it:
    `offset=2**63` reached DuckDB as a bound parameter and failed there with an unhandled conversion
    error, a `500`. Both now share one `MAX_OFFSET`.

## How I verified generated code (beyond "it ran")

- **Recorded fixtures, not invented ones.** Test fixtures in `backend/tests/fixtures/eia/` are real
  responses captured from the API (success, `API_KEY_INVALID`, `API_KEY_MISSING`, the HTML 504 page),
  checked to not contain the key before committing.
- **Mutation checks on the EIA client.** I deliberately broke the code and confirmed a test fails each time:
  removing `from None` on timeouts, retrying 403s, stopping pagination only on an empty page, and
  removing the log filter. The first round showed the timeout case was *not* covered (all tests still
  passed), so I added the missing test.
- **Model checked against the real data, not only fixtures.** After each model commit I rebuilt the
  model on the full history and compared it with the independent exploration queries. The grains
  reconcile to exactly `0.000` MW. `offline_pct` matches EIA's national percentage within 0.005 on all
  7,229 days. The DQ checks report the same units and dates the manual queries found.
- **Mutation checks on the model**: removing the dedupe step (the PRIMARY KEY must stop the build),
  pointing the ASOF join at the rating *after* a gap, weakening the gap rule, and dropping the
  "percentage without capacity is an error" rule. A test failed every time, after adding the 1-day
  gap test above.
- **Mutation checks on the orchestrator**: watermark advancing on a partial run, no lookback window,
  lock ignored, and writing a chunk whose rows were all rejected. The last one survived at first because
  the storage-level "never wipe with an empty fetch" guard masked it; a first-load test now covers it.
- **Mutation checks on the API** (each made a test fail): no issuer check, no required claims, the
  password ignored, an unknown `sub` accepted, the capability check removed, `missing_units` or
  `dim_facility` granted to Viewers, the column grant ignored, the manifest read before authorization,
  the serving file writable or with file access on, the wrong grant's file opened, the span filter
  reversed, start > end allowed, old serving files never pruned. What running them showed:
  - A mutation that only "failed" because it referenced an unimported name (`NameError`) was not a real
    detection; I redid it properly.
  - The file-access mutation made a `COPY` test write `out.csv` into the working directory, so the test
    now writes to `tmp_path`.
  - One mutation survives: removing the preview's `ORDER BY`. The model is stored sorted by key, so
    small data comes back in order anyway; the clause is there for parallel scans on larger data.
- **API on the real data**: catalog, preview and the Viewer 403 checked against the full model. The
  River Bend window 2015-02-13..16 returns 2 rows (the known weekend gap), and offset 700,000 of the
  generator table takes about 60 ms.
- **Mutation checks on the query path** (each made a test fail): CTEs visible inside their own body
  or to earlier CTEs, no CTE resolution, the forbidden-statement walk removed, table functions or
  qualified names allowed, several statements allowed, the per-table grant check removed, an
  off-by-one in `truncated`. One survived at first: running the query **without** the row cap. The
  endpoint slices the rows in Python too, so responses looked right while DuckDB built the full result.
  A test now checks the SQL that reaches the engine has the `LIMIT`.
- **Parser vs engine**: every FINDINGS.md query is run twice, as written and as sqlglot regenerated
  it, and the results must be identical.
- **Mutation checks on refresh** (each made a test fail): two jobs allowed at once, a running job not
  marked `interrupted` after a restart, `partial` reported as `succeeded`, the model rebuilt after a
  failed extraction, refresh open to every role.
- **Refresh against the real API**: an incremental refresh through `POST /api/refresh` answered `202`
  in 10 ms, refused a second one with `409`, finished in 34 s, and the next request served the new
  build. It inserted 0 rows, because EIA had nothing new yet, which confirms re-runs don't duplicate.
- **Mutation checks on SQL paging** (each made a test fail): the query's own `LIMIT` ignoring the
  page offset, its own `OFFSET` dropped, `LIMIT 10%` treated as a plain number, `next_offset` not
  counting from the current offset. The last one first made the paging test loop forever, because it
  kept requesting offset 3. That test now gives up after 10 pages, so a broken `next_offset` fails
  instead of hanging the suite.
- **Mutation checks on the query cache** (each made a test fail): the grant, the build or the page
  left out of the key, expiry one tick late, evicting the newest entry instead of the least recently
  used, a hit not counting as a use, a refresh not clearing the cache. The tests count the SQL that
  actually reaches DuckDB, so a "hit" that secretly re-runs the query would also fail.
- **Mutation checks on login rate limiting** (each made a test fail): the limit never checked, a
  success not clearing the count, the window boundary off by one, usernames not normalized ("Analyst"
  counted apart from "analyst"), the per-client total not counted, no cap on tracked keys.
  "Retry-After computed from the oldest failure" survived at first. In the normal flow it's
  equivalent: a blocked pair records no more failures, so there are never more than the limit.
  Concurrent requests can exceed it, though, so a unit test now covers that case, and the mutation
  fails. My first version of that test had its own arithmetic wrong; the code was right.
- **Web app**: 39 component tests, rendering the real routes against a stubbed `fetch`. They cover
  login and expiry, role-dependent navigation, "not allowed" with no follow-up data request, paging
  and filters sent to the API, SQL errors, history per user, CSV escaping, refresh polling to
  completion, and stale data invalidated after a refresh. Mutation checks, each of which made a test
  fail: rows requested before the dataset's 403, filters not resetting to page 1, the Prev stack never
  popping, history shared between users, no unordered-paging warning, no CSV escaping, a re-run
  keeping the old offset (this one survived at first, so I added the test), only the first page of
  the chart range read, no polling, no invalidation after a refresh, the full-reload flag ignored.
- **Screens in a real browser**: a headless Chrome script, kept outside the repo, logs in as each
  persona against the real API and Vite, then screenshots every screen in light, dark and phone
  width. That's how I found the invisible band (#12) and two layout problems on phones: summary tiles
  one per row, and a legend swatch wrapping away from its label.
- **Security review** (2026-10-18): 21 bypass probes run against the real `sqlguard` and a sandboxed
  connection (file reads, system catalogs, table functions, aliases, `PIVOT`/`SHOW`): all rejected or
  harmless. `npm audit` and `pip-audit` found no known vulnerabilities, and the git history holds no
  secrets. Then about 25 resource probes, each timed under a memory limit, to see which functions
  escape it (#15, #16). Each amplifier found is now a test that must be rejected, and a list of
  everyday expressions (`strftime`, `string_agg`, `FILTER`, window functions...) is a test that must
  still pass, together with the 15 FINDINGS.md queries.
- **Mutation checks on the resource limits** (each made a test fail): the memory limit and no-spill
  setting removed, the result size check disabled, the cache costed by row count instead of size, a
  query slot never released after an error, the slot never acquired. My first result-cap test never
  reached the check: the test model has 8 rows, so the "big" query stayed under the cap and came back
  200. It now pads each row to go past a small cap.
- **Mutation checks on the low findings of the review** (each made a test fail): the password length
  limit removed (an oversized login reached scrypt), the preview offset bound removed, revocation
  never checked, revoked ids never pruned, every token given the same `jti` (signing out one session
  signed out all), the web app never calling logout, the web app calling it after an expiry too, the
  CSV formula guard removed, the guard applied to numbers (`-5` became text).
