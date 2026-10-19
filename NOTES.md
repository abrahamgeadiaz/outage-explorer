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
