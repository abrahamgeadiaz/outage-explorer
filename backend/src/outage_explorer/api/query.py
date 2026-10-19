"""Read-only SQL (US-04), with the permission check applied to every table the query reads (US-05).

Order of work: capability, then sqlguard.check (one SELECT, plain table names, every table resolved),
then every table against the caller's grant, and only then the query runs, on the caller's serving
file (read-only, no file access, only their tables and columns), with a row cap, a size cap and a
timeout, and only while one of QUERY_MAX_CONCURRENT slots is free (DECISIONS #27).

Results are cached per grant and build (api/cache.py), but only after the checks above: a cached
result is never a way around authorization.

Results past max_rows are read page by page: send the next request with `offset = next_offset`. Each
page re-runs the query, so pages line up only if the query has an ORDER BY over a unique key
(`ordered` says whether it has one at all) and the build did not change in between (`build_id`).
"""

import threading
import time
from typing import Annotated, Any

import duckdb
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from outage_explorer.api import sqlguard, store
from outage_explorer.api.auth import require
from outage_explorer.api.catalog import live_manifest
from outage_explorer.api.errors import ApiError, forbidden
from outage_explorer.api.policy import Capability, granted_tables
from outage_explorer.api.preview import ColumnRef, json_value
from outage_explorer.api.users import User
from outage_explorer.model.tables import TABLES

DEFAULT_MAX_ROWS = 1000
MAX_ROWS = 5000
MAX_OFFSET = 10_000_000  # more rows than the whole model has
QUEUE_WAIT_SECONDS = 2.0  # how long a query waits for a free slot before 503 server_busy


class QueryIn(BaseModel):
    sql: str = Field(min_length=1, max_length=sqlguard.MAX_SQL_LENGTH)
    max_rows: int = Field(DEFAULT_MAX_ROWS, ge=1, le=MAX_ROWS)
    offset: int = Field(0, ge=0, le=MAX_OFFSET, description="Rows of the result to skip (paging).")


class QueryOut(BaseModel):
    build_id: str
    tables: list[str]  # the datasets the query read
    columns: list[ColumnRef]
    rows: list[list[Any]]
    row_count: int
    offset: int
    truncated: bool  # more rows exist after this page
    next_offset: int | None  # offset of the next page, or None if this was the last
    ordered: bool  # the query has a top-level ORDER BY, so pages are stable across requests
    elapsed_ms: int  # time the query took when it ran (also for a cached result)
    cached: bool = False


def authorize(user: User, sql: str) -> sqlguard.CheckedQuery:
    """Check the statement and every table it reads; raises 400 (bad SQL) or 403 (a table not granted)."""
    try:
        checked = sqlguard.check(sql, set(TABLES))
    except sqlguard.SqlRejected as exc:
        raise ApiError(400, exc.code, exc.message) from None
    denied = sorted(t for t in checked.tables if t not in granted_tables(user.role))
    if denied:
        raise forbidden(f"role '{user.role}' has no access to: {', '.join(denied)}")
    return checked


FETCH_BATCH = 500


def approx_size(value: Any) -> int:
    """Rough bytes of a result value: the length of text, a fixed cost for anything scalar."""
    if isinstance(value, str | bytes):
        return len(value)
    if isinstance(value, list | tuple):
        return 8 + sum(approx_size(v) for v in value)
    if isinstance(value, dict):
        return 8 + sum(approx_size(k) + approx_size(v) for k, v in value.items())
    return 8


def run(con: duckdb.DuckDBPyConnection, sql: str, timeout: float, max_bytes: int) -> tuple[list, list, int]:
    """Execute with a timeout and a size cap; returns the description, the rows and their approx_size.

    A timer interrupts the connection if the query is still running. The rows are fetched in batches
    and refused once they pass `max_bytes`: DuckDB's memory limit stops at the engine, and the row
    cap alone still lets 5,000 rows of long strings through to Python and into the JSON response.
    """
    timer = threading.Timer(timeout, con.interrupt)
    timer.start()
    try:
        result = con.execute(sql)
        rows, size = [], 0
        while batch := result.fetchmany(FETCH_BATCH):
            size += sum(approx_size(row) for row in batch)
            if size > max_bytes:
                raise ApiError(
                    400,
                    "result_too_large",
                    f"the result is larger than {max_bytes / 1e6:g} MB; select fewer columns or rows",
                )
            rows.extend(batch)
        return result.description or [], rows, size
    except duckdb.InterruptException:
        raise ApiError(408, "query_timeout", f"the query ran longer than {timeout:g} s") from None
    except duckdb.Error as exc:
        # Safe to show: the connection only holds data this user may see (DECISIONS #21).
        raise ApiError(400, "query_error", str(exc).splitlines()[0][:500]) from None
    finally:
        timer.cancel()


router = APIRouter(prefix="/api", tags=["query"])


@router.post("/query", response_model=QueryOut)
def query(
    body: QueryIn, user: Annotated[User, Depends(require(Capability.QUERY))], request: Request
) -> QueryOut:
    checked = authorize(user, body.sql)  # before any data is read
    page = sqlguard.with_page(checked, body.offset, body.max_rows + 1)  # one extra row: is there more?

    settings = request.app.state.settings
    manifest = live_manifest(settings)
    grant = granted_tables(user.role)
    cache = request.app.state.query_cache
    key = (store.grant_key(grant), manifest["build_id"], page.sql())
    if (hit := cache.get(key)) is not None:
        return hit.model_copy(update={"cached": True})

    slots: threading.BoundedSemaphore = request.app.state.query_slots
    if not slots.acquire(timeout=QUEUE_WAIT_SECONDS):
        raise ApiError(
            503,
            "server_busy",
            "too many queries are running; try again in a moment",
            headers={"Retry-After": "1"},
        )
    try:
        started = time.perf_counter()
        with store.connect(settings.data_path, manifest, grant) as con:
            description, rows, size = run(
                con, page.sql(), settings.query_timeout_seconds, settings.query_max_result_bytes
            )
        elapsed = int((time.perf_counter() - started) * 1000)
    finally:
        slots.release()

    truncated = len(rows) > body.max_rows
    rows = rows[: body.max_rows]
    result = QueryOut(
        build_id=manifest["build_id"],
        tables=sorted(checked.tables),
        columns=[ColumnRef(name=d[0], type=str(d[1])) for d in description],
        rows=[[json_value(v) for v in row] for row in rows],
        row_count=len(rows),
        offset=body.offset,
        truncated=truncated,
        next_offset=body.offset + len(rows) if truncated else None,
        ordered=checked.tree.args.get("order") is not None,
        elapsed_ms=elapsed,
    )
    cache.put(key, result, size=size)
    return result
