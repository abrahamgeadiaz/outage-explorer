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

_Pending: decided in step 6 (refresh endpoint). Input so far: a full load took 38 minutes and an
incremental one about 35 seconds, both far longer than an HTTP request should stay open._

### 5. What SQL is supported, what is rejected, and why

_Pending: decided in step 6 (query path)._

### 6. How the query path finds every table a query touches before checking permissions

_Pending: decided in step 6 (query path)._

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
  - Simplified, as the brief allows: there is no refresh token, logout or rate limiting. Users live in
    code (`api/users.py`); swapping in a database or an OIDC provider only touches `auth.py` and `users.py`.

  Where it lives: `api/auth.py`, `api/users.py`, `api/policy.py` (role → capabilities).
