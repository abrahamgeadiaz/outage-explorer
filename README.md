# Outage Explorer

Explore and query U.S. nuclear outage data from the [EIA Open Data API](https://www.eia.gov/opendata/)
(national, facility and generator grains) without exporting it into spreadsheets by hand.

> Status: work in progress. This README grows as each part lands.

## Parts

1. **Data connector**: extracts the three EIA nuclear outage routes into local Parquet.
2. **Data model & verification**: typed model, fleet-offline metric, reconciliation and anomalies.
3. **Backend service** (FastAPI): catalog, data preview, read-only SQL, refresh, with role-based access.
4. **Web app** (React + Vite): login, datasets, data table, SQL box.

## Stack

Python 3.13 · uv · httpx · DuckDB · Parquet · FastAPI · sqlglot · React · TypeScript · Vite

## Setup

_Coming soon._

## Documents

- `DECISIONS.md`: open design questions, choices and rejected alternatives
- `FINDINGS.md`: reconciliation results and data anomalies
- `NOTES.md`: engineering notes (AI usage, verification)
