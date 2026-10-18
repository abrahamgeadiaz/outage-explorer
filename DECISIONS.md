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

_Pending: decided in step 4 (data model & reconciliation), based on what the real rows show._

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
