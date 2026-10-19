# Engineering Notes

Running log, kept up to date while building — not written at the end.

## AI usage

| Part | AI-generated | Written / changed by hand |
|---|---|---|
| Repo skeleton (.gitignore, .env.example, README stub) | Claude Code | Reviewed |
| Backend scaffold, settings | Claude Code | Reviewed |
| EIA client (`connector/eia_client.py`) + tests | Claude Code | Reviewed; failure modes chosen from real API probes |
| Validation, storage, extraction orchestrator, CLI + tests | Claude Code | Reviewed; required-field and failure policies decided by me |
| Data model build (`model/`) + tests | Claude Code | Reviewed; types, versioning and the US-08 / missing-unit policy decided by me from the data findings |

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

## How I verified generated code (beyond "it ran")

- **Recorded fixtures, not invented ones.** Test fixtures in `backend/tests/fixtures/eia/` are real
  responses captured from the API (success, `API_KEY_INVALID`, `API_KEY_MISSING`, the HTML 504 page),
  checked to not contain the key before committing.
- **Mutation checks on the EIA client.** I deliberately broke the code and confirmed a test fails each time:
  removing `from None` on timeouts, retrying 403s, stopping pagination only on an empty page, and
  removing the log filter. The first round showed the timeout case was *not* covered (all tests still
  passed), so I added the missing test.
- **Mutation checks on the orchestrator**: watermark advancing on a partial run, no lookback window,
  lock ignored, and writing a chunk whose rows were all rejected. The last one survived at first because
  the storage-level "never wipe with an empty fetch" guard masked it; a first-load test now covers it.
