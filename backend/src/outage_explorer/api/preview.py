"""Data preview (US-03): rows of one dataset, filtered by date and facility, paginated by the backend.

Order of work: capability, then the dataset grant (403/404), then the filters (400), and only then is
the serving file opened. The SQL is built here from model column names; every request value is a bound
parameter.
"""

from datetime import date
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from outage_explorer.api import store
from outage_explorer.api.auth import require
from outage_explorer.api.catalog import date_filter_columns, live_manifest, visible_columns
from outage_explorer.api.errors import ApiError
from outage_explorer.api.policy import Capability, granted_tables
from outage_explorer.api.users import User
from outage_explorer.model.tables import TABLES, Column

MAX_LIMIT = 1000
MAX_FACILITIES = 100


class ColumnRef(BaseModel):
    name: str
    type: str


class PageOut(BaseModel):
    limit: int
    offset: int
    total: int  # rows matching the filters
    has_more: bool


class RowsOut(BaseModel):
    dataset: str
    build_id: str
    columns: list[ColumnRef]
    rows: list[list[Any]]  # one list per row, in `columns` order
    page: PageOut


def _where(
    table: str, columns: tuple[Column, ...], start: date | None, end: date | None, facilities: list[str]
) -> tuple[str, list]:
    clauses, params = [], []
    if start or end:
        dates = date_filter_columns(table, columns)
        if not dates:
            raise ApiError(400, "filter_not_supported", f"dataset '{table}' has no date to filter on")
        if start and end and start > end:
            raise ApiError(400, "invalid_filter", "start must not be after end")
        first, last = dates[0], dates[-1]  # the same column for daily tables; a span keeps overlapping rows
        if start:
            clauses.append(f'"{last}" >= ?')
            params.append(start)
        if end:
            clauses.append(f'"{first}" <= ?')
            params.append(end)
    if facilities:
        if not any(c.name == "facility_id" for c in columns):
            raise ApiError(400, "filter_not_supported", f"dataset '{table}' has no facility to filter on")
        if len(facilities) > MAX_FACILITIES:
            raise ApiError(400, "invalid_filter", f"at most {MAX_FACILITIES} facility_id values")
        clauses.append(f'"facility_id" IN ({", ".join("?" * len(facilities))})')
        params.extend(facilities)
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


def _json_value(value: Any) -> Any:
    # DECIMAL(12,3) fits a double exactly enough for display; JSON has no decimal type.
    return float(value) if isinstance(value, Decimal) else value


router = APIRouter(prefix="/api/datasets", tags=["data"])


@router.get("/{name}/rows", response_model=RowsOut)
def preview(
    name: str,
    user: Annotated[User, Depends(require(Capability.PREVIEW))],
    request: Request,
    start: Annotated[date | None, Query(description="First day (inclusive).")] = None,
    end: Annotated[date | None, Query(description="Last day (inclusive).")] = None,
    facility_id: Annotated[list[str], Query(description="Repeat to filter on several facilities.")] = [],  # noqa: B006
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RowsOut:
    columns = visible_columns(user, name)  # 404 / 403 before anything is read
    where, params = _where(name, columns, start, end, facility_id)

    settings = request.app.state.settings
    manifest = live_manifest(settings)
    select = ", ".join(f'"{c.name}"' for c in columns)
    order = ", ".join(f'"{k}"' for k in TABLES[name].primary_key)
    # Identifiers come from model/tables.py (already checked against the grant); values are parameters.
    with store.connect(settings.data_path, manifest, granted_tables(user.role)) as con:
        [(total,)] = con.execute(f'SELECT count(*) FROM "{name}"{where}', params).fetchall()  # noqa: S608
        rows = con.execute(
            f'SELECT {select} FROM "{name}"{where} ORDER BY {order} LIMIT ? OFFSET ?',  # noqa: S608
            [*params, limit, offset],
        ).fetchall()

    return RowsOut(
        dataset=name,
        build_id=manifest["build_id"],
        columns=[ColumnRef(name=c.name, type=c.type) for c in columns],
        rows=[[_json_value(v) for v in row] for row in rows],
        page=PageOut(limit=limit, offset=offset, total=total, has_more=offset + len(rows) < total),
    )
