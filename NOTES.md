# Engineering Notes

Running log, kept up to date while building — not written at the end.

## AI usage

| Part | AI-generated | Written / changed by hand |
|---|---|---|
| Repo skeleton (.gitignore, .env.example, README stub) | Claude Code | Reviewed |
| Backend scaffold, settings | Claude Code | Reviewed |
| EIA client (`connector/eia_client.py`) + tests | Claude Code | Reviewed; failure modes chosen from real API probes |

## Things the AI got wrong (and how I caught it)

1. **API key leaked into logs through httpx.** The generated client carefully raised its own exceptions
   with `from None` so tracebacks would not show the URL, and looked safe. But httpx itself logs
   `HTTP Request: GET https://...?api_key=<key>` at INFO on every call, so every refresh would have
   written the key into the logs. Caught by writing a test that captures *all* log output during a
   successful request and asserts the key is absent — it failed. Fixed with a logger-level redaction
   filter on `httpx`/`httpcore` (`RedactApiKeyFilter`).
2. **Exploration script crashed on Windows.** `Path.read_text()` without an encoding uses cp1252 on
   Windows and failed on the em dash in `.env`. All file I/O in the project passes `encoding="utf-8"`.

## How I verified generated code (beyond "it ran")

- **Recorded fixtures, not invented ones.** Test fixtures in `backend/tests/fixtures/eia/` are real
  responses captured from the API (success, `API_KEY_INVALID`, `API_KEY_MISSING`, the HTML 504 page),
  checked to not contain the key before committing.
- **Mutation checks on the EIA client.** I deliberately broke the code and confirmed a test fails each time:
  removing `from None` on timeouts, retrying 403s, stopping pagination only on an empty page, and
  removing the log filter. The first round showed the timeout case was *not* covered (all tests still
  passed), so I added the missing test.
