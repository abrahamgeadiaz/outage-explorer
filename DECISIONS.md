# Decisions

Each entry: **the choice**, **one alternative rejected**, and **why**. Questions 1–6 are the ones the brief
asks about; the rest are decisions of our own that shaped the solution. Entries marked _pending_ are
filled in with the step that settles them, in the same commit as the code.

---

## Required questions

### 1. Natural key for each grain

- **Choice:**
  - national → `period`
  - facility → `(period, facility)`
  - generator → `(period, facility, generator)`

  Ids are stored as strings, exactly as EIA sends them. `facilityName` is a label, not part of the key.
- **Rejected:** a surrogate key (an auto-increment or hash id per row) as the primary key.
- **Why:** the source defines identity: one daily value per entity. Keying on it is what makes the
  connector's upsert idempotent: a re-run matches the same rows and replaces them instead of adding
  copies. A surrogate key would hide duplicates rather than prevent them. Ids stay strings, because EIA
  sends them as text and a generator id is not guaranteed to be numeric. The generator id alone is not
  unique (unit "1" exists at many plants), so the facility is part of the key.
  Where it lives: `connector/routes.py` (`key_columns`).

### 2. What "kept current" means (US-01), and how refresh achieves it

- **Choice:** the local copy is current when it holds every day EIA publishes, up to the latest one, with
  EIA's latest values for recent days. Each refresh is incremental:
  - it fetches from each dataset's **watermark − 30 days** up to today;
  - it adds new days and overwrites values EIA has revised inside that window (the run reports them as `updated`);
  - `--full` reloads everything since 2007.
- **Rejected:** fetching only days after the watermark (append-only).
- **Why:**
  - Append-only never picks up revisions to days we already hold, so local numbers would quietly
    drift from EIA's.
  - Re-fetching 30 days costs about one extra page per dataset.
  - The watermark only advances when a dataset's whole window succeeded, so a failed refresh is
    retried next time instead of leaving a permanent gap.

  The 30-day window is a configuration value (`REFRESH_LOOKBACK_DAYS`). It is our assumption about
  how far back revisions go. Step 4 checks it against the data.
  Where it lives: `connector/extract.py` (`plan_window`), `connector/storage.py` (`upsert_window`).

### 3. A generator whose facility is missing from the facility route, or whose values don't add up

- **Choice:** keep every grain exactly as EIA publishes it and report the disagreement; never rewrite one
  grain from another. The `grain_mismatch_facility` check writes one `dq_issue` row per (day, facility)
  in these cases:
  - generators reported but no facility row;
  - a facility row with no generators;
  - facility capacity or outage ≠ the sum of its generators, with the gap in MW.

  `grain_mismatch_national` does the same between national and the other two grains. US-08 is computed
  from the generator grain (`metric_fleet_offline_daily`), so a missing facility row never changes it.
- **Rejected:** rebuilding `fact_facility_daily` as the sum of generators, so the grains always agree.
- **Why:**
  - The real data has **no such case**: over 19 years, every facility row equals the sum of its
    generators and every national row equals both sums, exactly (FINDINGS.md, Reconciliation). EIA builds the upper
    grains from the units.
  - So a future mismatch would be news, a change at EIA or a broken load, and the product must show
    it, not hide it by recomputing.
  - The disagreement that does exist is different: a unit missing from **all three** grains on some
    days. It is handled by the `unit_reporting_gap` check and by the coverage columns of US-08 (#17).

  Where it lives: `model/quality.py`.

### 4. Synchronous or asynchronous refresh

- **Choice:** asynchronous. `POST /api/refresh` (Admin only) starts a job in a background thread and
  answers `202` with its id in about 10 ms. `GET /api/refresh/{job_id}` reports the step (`extract`,
  `model`, `done`) and the outcome: `succeeded`, `partial` (some chunks failed, model rebuilt from
  what is stored), `failed` (nothing new served) or `interrupted`. It also reports per-dataset rows and
  watermarks, the new `build_id`, the DQ findings by severity, and the reason for any failure. One job
  runs at a time (`409`), and the connector's file lock also keeps out a CLI run. Job records are
  written to `data/_meta/refresh_jobs/`, so the outcome survives a restart; a job that was running when
  the server stopped shows as `interrupted`.
- **Rejected:** a synchronous endpoint that returns when the refresh is done.
- **Why:**
  - The full load takes 38 minutes. An incremental refresh took 34 s through the API, longer than
    proxies and browsers keep a request open. A request that times out tells the Admin nothing, but
    US-06 is precisely "know whether it succeeded".
  - The job reports `partial` rather than hiding it. A partial run is still worth serving (failed
    chunks keep their old data, #12), and the Admin sees which window failed.
  - Readers are never blocked or shown half a refresh. The model switches builds atomically (#15), and
    the next request opens the new build's serving file (#21).
  - Simplified: a thread inside the API process rather than a task queue (Celery, RQ). That's enough
    for one server with one job at a time; with several API workers the job would have to move to a
    queue, and the in-memory "one at a time" check would rely on the file lock alone.

  Where it lives: `api/refresh.py`; the work itself is `run_extraction` + `build_model`.

### 5. What SQL is supported, what is rejected, and why

- **Choice:** one statement that is a query: `SELECT` (including DuckDB's `FROM t` form), `UNION` /
  `INTERSECT` / `EXCEPT`, CTEs (`WITH`, `WITH RECURSIVE`), subqueries anywhere, joins, `LATERAL`,
  aggregates and window functions. Rejected: anything that writes, changes settings or describes the
  engine (`INSERT`, `UPDATE`, `DELETE`, `CREATE`, `DROP`, `ALTER`, `COPY`, `ATTACH`, `SET`, `PRAGMA`,
  `DESCRIBE`, `EXPLAIN`, `INSTALL`, `SELECT INTO`); more than one statement; table functions in `FROM`
  (`read_parquet`, `glob`, `range`, `duckdb_tables`, `query_table`...); file paths as tables;
  schema- or catalog-qualified names; any function not on an allow-list (#27), which leaves out those
  that read files or the environment and those that can build a huge value. Results are capped
  at `max_rows` (default 1,000, at most 5,000) per page, with `offset` paging past that (#23), and
  queries are interrupted after 10 s.
- **Rejected:** single-table `SELECT` only (the brief's minimum).
- **Why:**
  - The analysts' real questions need joins and CTEs. The reconciliation queries in `findings.sql` are
    all of that kind, and all 15 run through the API unchanged (a test checks this).
  - The rules are an allow-list: a node type sqlglot doesn't know becomes a `Command`, which is
    rejected. Every role gets the same rules, including Admin, so no API path writes data.
  - Rejecting a statement says why (`read_only`, `function_not_allowed`, `unknown_table`...), so the
    SQL box can show a useful error.
  - The parser is the first check, not the only one: the query runs on the role's read-only serving
    file with no file access (#21), so a statement the parser misjudged still can't write or read files.
  - The cap is part of the SQL that runs (`LIMIT max_rows + 1`; the extra row tells whether the result
    was cut), so DuckDB stops early instead of building the full result first. A smaller `LIMIT` in
    the query is kept, and so is its `ORDER BY`.

  Where it lives: `api/sqlguard.py` (`check`, `with_row_cap`), `api/query.py`.

### 6. How the query path finds every table a query touches before checking permissions

- **Choice:** parse with sqlglot (DuckDB dialect) and visit **every** `Table` node in the tree, wherever
  it is: `FROM`, joins, subqueries in `SELECT` / `WHERE` / `EXISTS`, CTE bodies, set operations,
  `LATERAL`. Each one is resolved to either a CTE that is visible at that point, or a model table. A
  name that is neither is rejected. Every model table found is then checked against the caller's grant;
  one table not granted refuses the whole query (`403`, naming the tables). Only then does the query
  run, and what runs is the SQL **regenerated from the checked tree**, not the text the user sent.
- **Rejected:** collecting table names with a regex, or asking DuckDB which tables a query reads by
  running `EXPLAIN` first.
- **Why:**
  - A regex can't tell a table from a CTE, a string or a comment, and misses subqueries. `EXPLAIN`
    would mean running the user's statement through the engine before it was checked.
  - CTE scope is handled the way DuckDB does it, because getting it wrong is a bypass. A CTE is
    visible to the query it's attached to and to everything nested in it. Inside a CTE's own body, only
    the CTEs defined before it are visible, plus itself under `WITH RECURSIVE`. So
    `WITH fact_generator_daily AS (SELECT * FROM fact_generator_daily) ...` checks the real table, and
    a CTE defined in one subquery can't hide a real table of the same name in another. A test runs a
    CTE named like a detail table as a Viewer and checks that DuckDB returns the CTE's rows, so the
    guard and the engine agree.
  - Running the regenerated SQL means the engine executes exactly the tree that was checked, even where
    sqlglot and DuckDB would read the original text differently. A test runs every FINDINGS.md query
    both ways and compares the results.
  - Columns aren't checked by the parser (`SELECT *` would need expanding). The Viewer's hidden column
    simply doesn't exist in its serving file, so DuckDB itself can't find it.

  Where it lives: `api/sqlguard.py` (`check`, `_is_cte_reference`), `api/query.py` (`authorize`).

---

## Our own decisions

### 7. DuckDB over Parquet files instead of a database server

- **Choice:** Parquet files are the storage. DuckDB runs inside the process, the way SQLite does,
  and is the query engine for the connector, the model and the API.
- **Rejected:** loading the data into Postgres.
- **Why:**
  - The brief requires Parquet. A server would mean a second copy of the data to keep in sync,
    plus a service to install and run.
  - DuckDB queries Parquet directly and handles this volume easily (1.16M rows, about 1.7 MB).
  - DuckDB can be restricted so that user SQL cannot read files or attach databases, which the
    query sandbox will rely on.

### 8. Stop paging on a short page, not on the reported `total`

- **Choice:** keep requesting pages of 5,000 rows until a page comes back with fewer than 5,000 rows.
- **Rejected:** computing the page count from the `total` field in the response.
- **Why:** `total` is wrong on the facility route. It reports 713,327, which is the generator row count;
  the real facility count is 437,349 (see FINDINGS.md). Trusting it would make the connector request
  hundreds of empty pages, or flag a correct load as incomplete. Requests are also sorted by the
  natural key, so offset pagination stays stable between pages.

### 9. Re-running safely: replace the fetched date window by natural key

- **Choice:** each run replaces the date window it fetched inside that year's Parquet file. Rows are
  matched on the natural key, and the run counts inserted, updated, unchanged and deleted rows. The
  new file is written next to the old one and swapped in atomically, and a lock file stops two runs
  from overlapping.
- **Rejected:** appending each run's rows and removing duplicates when the data is read.
- **Why:**
  - Appending grows storage with every refresh and pushes the duplicate problem onto every reader.
  - Replacing the window keeps the stored data correct at all times and makes two runs provably
    produce the same result (tested, and confirmed on the real full load).
  - The counts show when EIA revised history.
  - An empty response is never allowed to wipe a window that already holds rows.

### 10. One Parquet file per dataset and year

- **Choice:** `data/raw/<dataset>/year=<YYYY>/data.parquet`, and extraction works in one chunk per
  dataset and year.
- **Rejected:** one file per month (or per day).
- **Why:**
  - A year is at most about 35,000 rows (generator), so a file is small, and an incremental refresh
    rewrites only one or two files.
  - Each chunk owns exactly one file, so chunks run in parallel without coordinating.
  - Monthly files would mean about 12 times more requests and files for no gain at this volume.
  - Most queries filter by date, and the `year=` folders let DuckDB skip whole years.

### 11. Reject rows only when they can't be stored safely; keep odd values and count them

- **Choice:** a row is rejected only when it can't be stored safely: a missing or invalid period or
  key, missing or non-numeric capacity or outage, units other than MW and percent, or a period outside
  the requested window. Odd but well-formed values are kept and counted as warnings in the run's data
  quality summary: outage greater than capacity, negative values, duplicate keys.
- **Rejected:** dropping any row that looks suspicious.
- **Why:** a suspicious value is a finding about the source. Analysts need to see it, and the data
  model decides how to handle it. Dropping it would quietly change totals and hide exactly the
  disagreements US-07 asks us to explain.

### 12. Failure policy: a failed chunk keeps its data, and its dataset's watermark stays put

- **Choice:** if a chunk still fails after its retries, its file is left as it was, the other chunks
  finish, and the run is marked `partial`. That dataset's watermark does not advance. A rejected API
  key stops the run before anything is written.
- **Rejected:** failing the whole run on the first error and rolling back.
- **Why:** EIA returns frequent 504s (46 during the full load). Discarding 38 minutes of good work
  because of one bad year would be worse for US-06. Holding the watermark back means the next
  incremental run fetches the gap again.

### 13. Retry backoff: equal jitter instead of full jitter

- **Choice:** before retry *n*, wait between half and all of `2 × 2^(n−1)` seconds (capped at 120s).
  Honor `Retry-After` on HTTP 429.
- **Rejected:** full jitter (a random wait between 0 and the cap).
- **Why:** during the real full load, full jitter produced a 0.8-second wait before a 4th retry
  against a struggling gateway. Equal jitter guarantees that later retries wait longer, while the
  random half still keeps the parallel workers from retrying at the same moment.

### 14. Model types: DECIMAL for megawatts, the EIA percentage kept next to a recomputed one

- **Choice:** `capacity_mw` and `outage_mw` are `DECIMAL(12,3)`. `percent_outage` is recomputed from
  them (`DOUBLE`, NULL when capacity ≤ 0), and EIA's value is kept as `percent_outage_eia` (`DECIMAL(6,2)`).
- **Rejected:** keeping the raw `DOUBLE` columns.
- **Why:**
  - EIA sends at most 3 decimals for every MW value (checked on all 1.16M rows), so the cast loses nothing.
  - With doubles, national vs Σfacility vs Σgenerator differs by up to 2.9e-11 MW. That forces every
    reconciliation to pick a tolerance. With decimals the grains match exactly (`0.000`), and any
    non-zero gap is real.
  - EIA rounds the percentage to 2 decimals at facility and national level and reports one even when
    capacity is 0 (Indian Point 2, March 2020). Recomputing it gives one consistent definition; keeping
    EIA's lets users compare the two.

  Where it lives: `model/tables.py`, `model/build.py` (`_VALUES_SQL`).

### 15. The model is rebuilt into a new versioned folder; `current.json` points at the live one

- **Choice:** each `outage-model` run builds every table into `data/model/builds/<build_id>/`, then
  switches `data/model/current.json` to it with an atomic replace. The newest 3 builds are kept.
  Tables are created with their PRIMARY KEY and FOREIGN KEYs, so a duplicate key or an orphan row
  fails the build instead of being written.
- **Rejected:** overwriting the model files in place.
- **Why:**
  - Readers (the API, a running query) always see one complete, consistent build, never half old
    and half new.
  - On Windows a file that a reader has open can't be replaced; a new folder avoids that entirely.
  - A failed build changes nothing, and the previous builds stay for comparison or rollback.
  - A full rebuild takes ~25 s for 1.16M rows. That is simpler and safer than incremental model
    updates at this size (see the volume notes in docs/model.md).

  The build takes the connector's lock, so it never reads a raw file while an extraction swaps it.
  Where it lives: `model/build.py`.

### 16. Partitioning: fact tables by year, sorted by their key; small tables in one file

- **Choice:** `fact_*_daily` are written as `year=YYYY/` folders, each file sorted by
  `(period, facility_id, generator_id)`. Dimensions (≤ 107 rows) are single files.
- **Rejected:** partitioning by facility.
- **Why:** the query patterns all filter by date first: preview by date range (US-03), the daily
  US-08 series, the reconciliation windows. Year folders let DuckDB skip whole years. Sorting inside
  a file keeps each row group to a narrow date range, so Parquet min/max statistics skip most of a
  year as well. A facility filter (US-03) is cheap on top of that: a year holds at most ~35k
  generator rows. Partitioning by facility would mean ~66 folders × 20 years of tiny files, and
  every national or fleet query would have to open all of them.

  Where it lives: `model/tables.py` (`partitioned_by_year`), `model/build.py` (`_write_table`).

### 17. US-08 (share of fleet capacity offline per day): EIA's denominator, with missing units shown, not filled in

- **Choice:** `metric_fleet_offline_daily.offline_pct` = Σ outage / Σ capacity over the units EIA
  reported that day (capacity > 0). Every day also says what that number leaves out:
  - `units_missing` / `missing_units`: units inside their reporting lifetime with no row that day;
  - `missing_capacity_mw`: their last reported rating;
  - `offline_pct_min` / `offline_pct_max`: the share if all of them were online / offline;
  - `units_zero_capacity`;
  - `is_complete`.
- **Rejected:** filling in missing units (carrying their last row forward) to keep a stable denominator.
- **Why:**
  - On the full history `offline_pct` equals EIA's national `percentOutage` to within EIA's rounding
    (max 0.005), so the product agrees with the source users already know.
  - Units do drop out of all three grains: 1,892 of 7,229 days are incomplete (River Bend 564 days,
    Palisades 1,196, Vogtle 3/4, Watts Bar 2). On those days EIA's denominator quietly shrinks. For
    example, 2015-02-14 reads 4.89% but the true value lies between 4.84% and 5.80%.
  - We can't know whether a missing unit was running, so carrying a row forward would invent data.
    The range is honest and shows how much the gap matters.

  Where it lives: `model/build.py` (`metric_fleet_offline_daily`); definitions in `model/tables.py`.

### 18. Data quality checks run inside every model build and are stored as a table

- **Choice:** `model/quality.py` defines 8 checks, one SQL query each. Every build runs them and
  writes the findings to `dq_issue`, with consecutive days collapsed into one row. The build manifest
  keeps the counts by check and severity. Severity: `error` when values contradict each other, `warning`
  when data is valid but incomplete, `info` for fleet changes.
- **Rejected:** failing the build (or the refresh) when an anomaly is found.
- **Why:**
  - Every anomaly found is in the source and real (e.g. Indian Point 2). Refusing to load would leave
    users with older data and the same anomaly.
  - Storing the findings as a table lets analysts query them like any dataset (US-07), and lets the
    UI show them next to the numbers they affect.
  - The build still fails on what *we* would get wrong: a duplicate key or an orphan row (#15).

  Where it lives: `model/quality.py`, `dq_issue` in `model/tables.py`.

### 19. Authentication: short-lived signed tokens; the role is looked up on every request

- **Choice:** `POST /api/auth/login` checks the password (scrypt, salted) against three seeded users
  and returns an HS256 JWT valid for 60 minutes. The token holds only who the user is (`sub`), the
  issuer and the expiry. Every request decodes it with the algorithm pinned and those claims required,
  then reads the user's role from the user store.
- **Rejected:** putting the role in the token and trusting it.
- **Why:**
  - With the role in the token, demoting a user (Analyst → Viewer) would not take effect until the
    token expired. Here it takes effect on the next request, which matters for US-05.
  - A token can't be used to escalate: changing `sub` or anything else breaks the signature.
    `alg: none`, a wrong issuer, a missing `exp` and a deleted user are all rejected (tests cover each).
  - Login gives one answer for an unknown user and a wrong password. For an unknown user it still
    runs the hash check, so the response time doesn't reveal which usernames exist.
  - The app refuses to start without a `JWT_SECRET` of at least 32 characters, instead of signing
    tokens with an empty key.
  - Simplified, as the brief allows: there is no refresh token or logout. Failed logins are
    rate-limited (#25). Users live in code (`api/users.py`); swapping in a database or an OIDC
    provider only touches `auth.py` and `users.py`.

  Where it lives: `api/auth.py`, `api/users.py`, `api/policy.py` (role → capabilities).

### 20. Authorization: one policy module, with allow-lists down to the column

- **Choice:** `api/policy.py` holds every rule. Capabilities decide which endpoints a role may call.
  Dataset grants decide which tables, and which columns of each, a role may see. The Viewer gets
  `fact_national_daily` and `metric_fleet_offline_daily` **without `missing_units`**: that column lists
  units as `'facility_id/generator_id'`, which is generator-level detail. Every endpoint resolves the
  caller's grant before it reads anything, including the build manifest.
- **Rejected:** table-level grants only, with Viewers simply denied the US-08 metric table.
- **Why:**
  - US-08 is a national number and Viewers follow national trends, so they should have it. Only one
    column of it is detail, and column grants are what let them have the rest.
  - The Viewer's columns are listed, not excluded: a column added to the metric later stays hidden from
    Viewers until someone grants it. Tests check that every grant names real tables and columns, and that
    no Viewer column identifies a plant or unit.
  - A forbidden dataset is a `403`, an unknown one a `404`. The table names are in the README and give
    nothing away, and a 403 lets the web app show "not allowed" instead of "not found". A 403 never
    includes the table's columns.
  - The catalog, the preview and (step 6) the query path all read these same grants, so they can't
    disagree about what a role may see.

  Where it lives: `api/policy.py`; enforced in `api/catalog.py` (`visible_columns`).

### 21. Requests read a per-grant, read-only DuckDB file, not the Parquet model

- **Choice:** for each distinct grant, the API copies the live build's granted tables and columns
  into `data/serve/<build_id>/<grant_hash>.duckdb`. That takes about 1 s, done once per build. Each
  request opens that file with `read_only=True`, `enable_external_access=false` and
  `lock_configuration=true`. A Viewer's file contains only national rows. Analyst and Admin have the
  same grant, so they share one file.
- **Rejected:** one connection with views over the Parquet files, restricted with DuckDB's
  `allowed_directories`.
- **Why:**
  - I tried that first. To let the views read the model, the model folder must be allowed, and then
    `SELECT * FROM read_parquet('.../fact_generator_daily/...')` works for anyone, Viewers included.
    A view can hide a column, but it can't stop SQL from reading the file behind it.
  - With the per-grant file, the data a role may not see isn't in the database it's connected to.
    The checks in code (grants now, the SQL parser in step 6) are the first line of defense; this is
    the second, and it holds even if one of them has a bug. Tests run `CREATE`, `DELETE`, `SET`,
    `ATTACH`, `COPY` and `read_parquet` on a Viewer connection and expect each to fail.
  - Python replacement scans are off as well (`python_enable_replacements=false`). Otherwise DuckDB
    resolves an unknown `FROM name` to a pandas or Arrow object of that name in the calling Python code.
  - The grant hash is part of the file name, so a change to `policy.py` takes effect on the next
    request, with no stale copy served. The build id is part of it too, so a refresh does the same.
  - Cost: one copy of the model per distinct grant (15 MB for the full one, 0.8 MB for the Viewer).
    At much larger volumes I'd keep one database and give each role its own schema of views.

  Where it lives: `api/store.py`.

### 22. Preview: filters as bound parameters, offset pagination with a total

- **Choice:** `GET /api/datasets/{name}/rows?start=&end=&facility_id=&limit=&offset=`. Filters are
  checked against the dataset's granted columns: a filter the dataset doesn't support is a `400`, not
  ignored. For tables with a date span (`dim_*`, `dq_issue`), the date filter keeps rows overlapping it.
  Results are ordered by primary key. `limit` is at most 1,000. The response includes `total` for the
  filters and `has_more`.
- **Rejected:** keyset (cursor) pagination.
- **Why:** the web app's table needs "page N of M" and jumping to a page, which needs `total` and an
  offset. On the real data, offset 700,000 of the 713,327 generator rows takes about 60 ms. Keyset
  would matter at much larger volumes, or if rows could be inserted between page requests; here a
  build never changes, and each response says which `build_id` it read.

  Where it lives: `api/preview.py`.

### 23. Paging SQL results: offset paging merged into the query's own LIMIT / OFFSET

- **Choice:** `POST /api/query` takes an `offset`. The response has `next_offset` (`null` on the last
  page) and `ordered`, which says whether the query has a top-level `ORDER BY`. The page is written
  into the checked query's own `LIMIT` / `OFFSET`: page `offset=10`, size 5 of `... ORDER BY x LIMIT 12`
  runs as `LIMIT 2 OFFSET 10`. Only a `LIMIT` that isn't a plain number (`LIMIT (SELECT ...)`,
  `LIMIT 10%`) is wrapped as `SELECT * FROM (<query>) LIMIT .. OFFSET ..`.
- **Rejected:** always wrapping the query in a subquery, or keeping server-side cursors open between
  requests.
- **Why:**
  - SQL doesn't promise that an `ORDER BY` inside a subquery orders the outer result. Merging the page
    into the query's own clauses keeps its order exactly, and keeps a `LIMIT` the user wrote.
  - A cursor would hold a DuckDB connection, plus memory, per user between requests, and would need
    expiry and cleanup. Each page is a fresh request, read-only, through the same checks.
  - Cost: every page re-runs the query, and pages line up only if the query orders by a unique key
    and the build didn't change between pages. `ordered` and `build_id` let the client tell. The
    result cache (#24) makes the second and later pages cheap when the query is repeated.

  Where it lives: `api/sqlguard.py` (`with_page`), `api/query.py`.

### 24. Query result cache keyed by grant, build and the SQL that ran; looked up only after authorization

- **Choice:** an in-process LRU cache of query results under
  `(grant_key, build_id, SQL regenerated from the checked tree with its page)`. Entries expire after
  `QUERY_CACHE_TTL_SECONDS` (default 300). Memory is bounded by `QUERY_CACHE_MAX_BYTES` (default
  100 MB across all entries, measured like the result cap in #27; it was a row count until the security
  review showed one row can hold a long string), and a successful refresh clears the cache. The response says
  `cached: true`.
- **Rejected:** keying by user, or by the SQL text alone.
- **Why:**
  - "Can never serve a result to a user who couldn't run the query" is guaranteed twice over. First,
    the lookup happens after `authorize`, so a Viewer asking for a query an Analyst cached still gets
    `403` (test). Second, the grant key is a hash of every table and column the role may see. Two roles
    share an entry only when their grants are identical, which means the same data. Analyst and Admin
    do; Viewer and Analyst don't, even for a query over `fact_national_daily` that both may run.
  - Keying by user would give the same safety but fewer hits. Users with the same grant can see the
    same data, so sharing between them leaks nothing.
  - SQL text alone would be the bug the brief warns about: an Analyst's cached result served to a
    Viewer. Using the regenerated SQL also means whitespace and keyword case don't create misses, and
    each page (`LIMIT`/`OFFSET`) is its own entry.
  - The build id in the key means a result is never stale, even when the model is rebuilt outside the
    API (`uv run outage-model`), so clearing on refresh is only about freeing memory. Errors and
    timeouts aren't cached.
  - Simplified: per process. With several API workers each would have its own cache, still correct,
    with fewer hits; a shared one (Redis) would need the same key.

  Where it lives: `api/cache.py`; used in `api/query.py`; cleared from `api/refresh.py`.

### 25. Login rate limiting per (client, username) and per client, never per username alone

- **Choice:** failed logins are counted in a 5-minute sliding window. After 5 for one username from one
  client, or 20 from one client across all usernames, the next attempt gets `429 rate_limited` with
  `Retry-After`, **before** the password is checked, so even the right password is refused until the
  window frees up. A successful login clears that pair's count. The client is the connection's
  address. Limits come from `LOGIN_MAX_FAILURES`, `LOGIN_MAX_FAILURES_PER_CLIENT` and
  `LOGIN_WINDOW_SECONDS`.
- **Rejected:** locking a username after N failures from anywhere.
- **Why:**
  - A lock per username alone is itself an attack: anyone can keep a real Analyst locked out by
    failing on purpose. Per (client, username), an attacker only locks themselves out; the real user,
    on another address, logs in (test).
  - The per-client total stops the other pattern, one common password tried against many usernames.
  - The check runs before the password is verified. Otherwise a blocked attacker would still learn
    from which attempts succeed, and the limit would only slow the responses down.
  - `X-Forwarded-For` is ignored: a client can set it to a new address on every request (test).
    Behind a reverse proxy, the proxy's real-IP setting has to supply the address instead.
  - Limits: per process and in memory, with at most 10,000 tracked keys so a flood of usernames can't
    grow it. An attacker with many addresses is not stopped. In production, login would go through an
    identity provider with its own protections (#19).

  Where it lives: `api/ratelimit.py`; used in `api/auth.py` (`login`).

### 26. Web app: one origin through the Vite proxy; the token in localStorage; the server decides access

- **Choice:** React + TypeScript (Vite), TanStack Query for server data, React Router. In development
  the Vite server proxies `/api` to FastAPI, so the browser sees one origin. The access token is kept
  in `localStorage`, so a reload or a new tab keeps the session. On load the app asks
  `GET /api/auth/me` who the token belongs to and what the role may do. Any 401 signs the user out
  with "session expired". Login and logout clear every cached response.
- **Rejected:** an httpOnly cookie for the token, and enabling CORS on the API.
- **Why:**
  - A cookie keeps the token away from JavaScript. But then every request carries it automatically,
    which brings CSRF protection, and the API would need cookie handling next to the bearer tokens the
    tests and `curl` use. With a 60-minute token, a read-only API and React escaping all output,
    `localStorage` is an acceptable trade-off for this scope; it is the first thing I'd change for
    production.
  - With the proxy, the API needs no CORS configuration, which is one less policy that could be
    misconfigured. A production build would be served by the same server, or behind the same reverse
    proxy as the API.
  - The UI hides what a role can't use (the Refresh link, the SQL page), but only for convenience. The
    API checks every request itself, and the UI shows its `403` as "not allowed" when someone goes
    straight to the URL.
  - Retries happen only for network errors and transient 5xx. A 403 is never retried, and neither is a
    `503 model_not_built`, which lasts until an Admin refreshes.

  Where it lives: `frontend/src/api/client.ts`, `frontend/src/auth/`, `frontend/vite.config.ts`.

### 27. Resource limits on user SQL: one query can't take the server's memory or cores

- **Choice:** every serving connection (#21) opens with `memory_limit = 1GB`, `threads = 2` and
  `max_temp_directory_size = 0GB`, set before `lock_configuration`, so a query can't `SET` them back.
  A query over the limit fails with `400 query_error` (out of memory) instead of growing. On top of
  that, `sqlguard` accepts only functions on an allow-list (`ALLOWED_FUNCTIONS`, 144 names: aggregates,
  window, math, dates, common string and list functions), each returning a value at most linear in
  its inputs; anything else is `400 function_not_allowed`. The rows are fetched in batches and a page
  over `QUERY_MAX_RESULT_BYTES` (default 20 MB, text length plus 8 bytes per value) is refused with
  `400 result_too_large`. At most `QUERY_MAX_CONCURRENT` queries (default 4) run at once across all
  users; another waits up to 2 s for a slot, then gets `503 server_busy` with `Retry-After`. A cached
  result needs no slot.
- **Rejected:** DuckDB's defaults (80% of RAM and every core for each query), with the 10 s timeout as
  the only limit; and a block-list of the dangerous functions instead of an allow-list.
- **Why:**
  - Found in a security review: the timeout bounds time, not memory, and any signed-in role, Viewer
    included, can run SQL. A sort, join or aggregate over a crafted expression could ask for gigabytes
    within the 10 s.
  - Connections to one serving file share one DuckDB instance, so the limit holds for all requests
    reading that file together, not per request. The full model is ~15 MB, so 1 GB leaves room for
    any real analysis.
  - No spilling to disk: with a temp directory, the same query would fill the disk instead of the
    memory.
  - Limit of this layer: DuckDB counts memory held by its operators (sorts, hash tables, aggregate
    state), not a single huge value built by a scalar function. `repeat('x', 2000000000)` still
    allocates 2 GB, and its interrupt only lands when the function returns. In the review, `range(1e9)`
    held a query for 50 s and ~8 GB, past a 3 s interrupt. Same class: `lpad`/`rpad`, `space`, a width
    in `printf`/`format`, nested `replace` (three levels of 1,000 characters make 1 GB from 3 KB of
    SQL), `list_resize`, `array_to_string` over a `list()`, `list_reduce` doubling a string.
  - That is why the check is an allow-list, the same idea as the grants (#20): the review found a new
    amplifier with almost every probe, so a block-list would only hold until the next one. A function
    nobody has reviewed stays out until someone adds it. Cost: some DuckDB functions an analyst might
    want (`lpad` for formatting, `generate_series` for a date spine) are refused; a date spine can come
    from `fact_national_daily.period`. Lambdas (`list_transform`) are out too, to keep the list short.
  - Names are sqlglot's (`string_agg` parses as `group_concat`, `strftime` as `time_to_str`), so the
    15 queries in `analysis/findings.sql` and a list of everyday expressions are tested to pass.
  - The result cap covers the step after the engine: `max_rows` bounds the rows, not their size, and
    5,000 rows of 20 KB strings (a literal fits in the 20,000-character query) would be 100 MB as
    Python objects and more as JSON. Batches stop the fetch at the cap instead of after it. One value
    can still reach the 1 GB engine limit before it is measured; with the allowed functions that
    needs an aggregate like `string_agg`, and the concurrency limit bounds how many at once.
  - The concurrency limit turns the per-query bounds into a bound for the server: 4 queries × 2
    threads, and at most 4 results being fetched. FastAPI runs sync endpoints on a pool of 40 threads,
    so without it 40 queries could each take a 1 GB engine (Viewer and Analyst files are separate
    instances) and 20 MB of rows. Not per user: one user sending 4 slow queries makes others wait or
    get `503`; a per-user share would be the next step with real traffic.
  - Not covered: an amplifier among the allowed functions that nobody has found yet. The stronger fix
    is to run each query in a worker process that is killed on timeout, with an OS memory cap; I'd do
    that before exposing SQL to untrusted users.

  Where it lives: `api/store.py` (`SANDBOX`), `api/sqlguard.py` (`ALLOWED_FUNCTIONS`).
