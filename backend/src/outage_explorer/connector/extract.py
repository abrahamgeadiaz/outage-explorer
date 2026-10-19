"""Extraction run: plan windows, fetch in parallel, validate, upsert, and record what happened.

Incremental by default: each dataset keeps a watermark (latest period stored). A run re-fetches from
`watermark - refresh_lookback_days` to today, so new days are added and recent revisions by EIA are
picked up (the upsert counts them as `updated`). Without a watermark, or with full=True, it starts at
the beginning of the data (2007-01-01).

Work is split into one chunk per dataset and calendar year, matching the storage partitions, so each
chunk writes exactly one file and chunks can run in parallel without touching the same file.

Failure policy:
- Missing key: fail before any request. Rejected key (403): no further chunks start, nothing is
  written (every request fails the same way), status "failed", watermarks unchanged.
- A chunk that still fails after retries (network, 5xx, bad data): its partition is left as it was;
  other chunks still complete. Status "partial", and that dataset's watermark does not move, so the
  next run re-fetches the missing window.
- Each run's metadata (status, counts, data quality summary, errors) goes to _meta/runs/<run_id>.json.
"""

import json
import logging
import os
import threading
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from outage_explorer.config import Settings
from outage_explorer.connector.eia_client import EIAAuthError, EIAClient, EIAError
from outage_explorer.connector.routes import ROUTES, Route
from outage_explorer.connector.storage import UnsafeOverwriteError, partition_path, upsert_window
from outage_explorer.connector.validate import validate_rows

log = logging.getLogger(__name__)

STALE_LOCK_AFTER = timedelta(hours=6)


class ExtractionLockedError(Exception):
    """Another extraction is already running."""


@dataclass
class ChunkResult:
    dataset: str
    start: date
    end: date
    ok: bool = False
    error: str | None = None
    rows_fetched: int = 0
    rows_rejected: int = 0
    max_period: date | None = None
    rejected: Counter = field(default_factory=Counter)
    warnings: Counter = field(default_factory=Counter)
    rejected_examples: list[dict] = field(default_factory=list)
    unknown_fields: set[str] = field(default_factory=set)
    merge: dict = field(default_factory=dict)


# ---- paths and state ----------------------------------------------------------------------------


def raw_dir(data: Path) -> Path:
    return data / "raw"


def meta_dir(data: Path) -> Path:
    return data / "_meta"


def load_state(data: Path) -> dict:
    path = meta_dir(data) / "state.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


class RunLock:
    """Exclusive lock file so two writers (refreshes from CLI or API, a model build) never overlap."""

    def __init__(self, data: Path, run_id: str) -> None:
        self.path = meta_dir(data) / "extract.lock"
        self.run_id = run_id

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            age = datetime.now(UTC) - datetime.fromtimestamp(self.path.stat().st_mtime, UTC)
            if age < STALE_LOCK_AFTER:
                owner = self.path.read_text("utf-8")
                raise ExtractionLockedError(f"another extraction or model build is running ({owner})")
            log.warning("removing stale lock older than %s", STALE_LOCK_AFTER)
            self.path.unlink()
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)  # atomic: fails if it exists
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(self.run_id)
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


# ---- planning -----------------------------------------------------------------------------------


def plan_window(state: dict, dataset: str, settings: Settings, today: date, full: bool) -> tuple[date, date]:
    watermark = state.get(dataset, {}).get("watermark")
    if full or not watermark:
        return settings.eia_start_date, today
    start = date.fromisoformat(watermark) - timedelta(days=settings.refresh_lookback_days)
    return max(start, settings.eia_start_date), today


def yearly_chunks(start: date, end: date) -> list[tuple[date, date]]:
    return [(max(start, date(y, 1, 1)), min(end, date(y, 12, 31))) for y in range(start.year, end.year + 1)]


# ---- execution ----------------------------------------------------------------------------------


def _run_chunk(
    client: EIAClient, route: Route, result: ChunkResult, data: Path, run_id: str, now: datetime
) -> None:
    """Fill `result` in place, so whatever was learned before a failure (e.g. rejection reasons) is kept."""
    start, end = result.start, result.end
    rows = [row for page in client.iter_pages(route, start, end) for row in page]
    checked = validate_rows(route, rows, start, end)
    result.rows_fetched = checked.rows_in
    result.rows_rejected = checked.rows_rejected
    result.rejected, result.warnings = checked.rejected, checked.warnings
    result.rejected_examples, result.unknown_fields = checked.rejected_examples, checked.unknown_fields
    if checked.unknown_fields:
        log.warning(
            "%s: unexpected fields from EIA (schema drift?): %s", route.dataset, checked.unknown_fields
        )
    if rows and checked.table.num_rows == 0:
        raise EIAError(f"all {len(rows)} rows rejected: {dict(checked.rejected)}")

    path = partition_path(raw_dir(data), route.dataset, start.year)
    stats = upsert_window(
        path,
        checked.table,
        route.key_columns,
        start,
        end,
        run_id=run_id,
        ingested_at=now.replace(tzinfo=None),
    )
    result.merge = stats.as_dict()
    if checked.table.num_rows:
        result.max_period = max(checked.table.column("period").to_pylist())
    result.ok = True


def run_extraction(
    settings: Settings,
    datasets: list[str] | None = None,
    *,
    full: bool = False,
    start: date | None = None,
    end: date | None = None,
    client: EIAClient | None = None,
    today: date | None = None,
) -> dict:
    """Run one extraction and return its summary (also persisted under _meta/runs/)."""
    data = settings.data_path
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    now = datetime.now(UTC)
    today = today or now.date()
    names = datasets or list(ROUTES)
    unknown = set(names) - set(ROUTES)
    if unknown:
        raise ValueError(f"unknown dataset(s): {sorted(unknown)}")

    summary: dict = {"run_id": run_id, "started_at": now.isoformat(), "status": "running", "datasets": {}}

    with RunLock(data, run_id):
        state = load_state(data)
        own_client = client is None
        if own_client:
            client = EIAClient(
                settings.eia_api_key.get_secret_value(),
                settings.eia_base_url,
                timeout=settings.eia_timeout_seconds,
                max_retries=settings.eia_max_retries,
            )
        tasks: list[tuple[Route, date, date]] = []
        for name in names:
            s, e = plan_window(state, name, settings, today, full)
            s, e = start or s, end or e
            summary["datasets"][name] = {"window": [s.isoformat(), e.isoformat()], "chunks": []}
            tasks += [(ROUTES[name], cs, ce) for cs, ce in yearly_chunks(s, e)]
        log.info("run %s: %d chunks across %s", run_id, len(tasks), names)

        abort = threading.Event()  # set on bad credentials so queued chunks don't even start
        results: list[ChunkResult] = []

        def work(route: Route, cs: date, ce: date) -> ChunkResult:
            result = ChunkResult(route.dataset, cs, ce)
            if abort.is_set():
                result.error = "skipped: run aborted"
                return result
            try:
                _run_chunk(client, route, result, data, run_id, now)
            except EIAAuthError:
                abort.set()
                raise
            except (EIAError, UnsafeOverwriteError) as exc:
                log.error("%s %s..%s failed: %s", route.dataset, cs, ce, exc)
                result.error = f"{type(exc).__name__}: {exc}"
            except Exception as exc:  # disk errors, a file locked on Windows...: fail the chunk, not the run
                log.exception("%s %s..%s failed unexpectedly", route.dataset, cs, ce)
                result.error = f"unexpected {type(exc).__name__}: {exc}"
            return result

        auth_error: str | None = None
        try:
            with ThreadPoolExecutor(max_workers=settings.eia_max_concurrency) as pool:
                futures = [pool.submit(work, *t) for t in tasks]
                for fut in as_completed(futures):
                    try:
                        results.append(fut.result())
                    except EIAAuthError as exc:
                        auth_error = str(exc)
        finally:
            if own_client:
                client.close()

        _summarise(summary, results, state, auth_error)
        summary["finished_at"] = datetime.now(UTC).isoformat()
        _write_json_atomic(meta_dir(data) / "runs" / f"{run_id}.json", summary)
        _write_json_atomic(meta_dir(data) / "state.json", state)

    log.info("run %s finished: %s", run_id, summary["status"])
    return summary


def _summarise(summary: dict, results: list[ChunkResult], state: dict, auth_error: str | None) -> None:
    for name, ds in summary["datasets"].items():
        chunks = sorted((r for r in results if r.dataset == name), key=lambda r: r.start)
        quality = {"rejected": Counter(), "warnings": Counter(), "unknown_fields": set(), "examples": []}
        for r in chunks:
            quality["rejected"] += r.rejected
            quality["warnings"] += r.warnings
            quality["unknown_fields"] |= r.unknown_fields
            quality["examples"] += r.rejected_examples[: 5 - len(quality["examples"])]
            ds["chunks"].append(
                {
                    "start": r.start.isoformat(),
                    "end": r.end.isoformat(),
                    "ok": r.ok,
                    "error": r.error,
                    "rows_fetched": r.rows_fetched,
                    "rows_rejected": r.rows_rejected,
                    "merge": r.merge,
                }
            )
        planned = len(yearly_chunks(*map(date.fromisoformat, ds["window"])))
        all_ok = len(chunks) == planned and all(r.ok for r in chunks)
        ds["status"] = "success" if all_ok else ("failed" if not any(r.ok for r in chunks) else "partial")
        ds["rows_fetched"] = sum(r.rows_fetched for r in chunks)
        ds["rows_rejected"] = sum(r.rows_rejected for r in chunks)
        ds["quality"] = {
            "rejected_by_reason": dict(quality["rejected"]),
            "warnings_by_reason": dict(quality["warnings"]),
            "unknown_fields": sorted(quality["unknown_fields"]),
            "rejected_examples": quality["examples"],
        }
        for key in ("inserted", "updated", "unchanged", "deleted"):
            ds[key] = sum(r.merge.get(key, 0) for r in chunks)

        # The watermark only moves when every chunk of the dataset succeeded; otherwise the next run
        # would skip the window that failed.
        if all_ok:
            periods = [r.max_period for r in chunks if r.max_period]
            prev = state.get(name, {}).get("watermark")
            new = max([*periods, *([date.fromisoformat(prev)] if prev else [])], default=None)
            state[name] = {
                "watermark": new.isoformat() if new else None,
                "last_success_run": summary["run_id"],
            }
        ds["watermark"] = state.get(name, {}).get("watermark")

    statuses = {ds["status"] for ds in summary["datasets"].values()}
    if auth_error:
        summary["status"], summary["error"] = "failed", auth_error
    elif statuses == {"success"}:
        summary["status"] = "success"
    elif statuses == {"failed"}:
        summary["status"] = "failed"
    else:
        summary["status"] = "partial"
