"""Catalog (US-02): the datasets the caller may use, with their columns, types and definitions.

Built from model/tables.py (descriptions, keys) and the live build's manifest (row counts), filtered
through the caller's grants. Reads no data files.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from outage_explorer.api.auth import require
from outage_explorer.api.errors import ApiError, forbidden
from outage_explorer.api.policy import Capability, granted_columns, granted_tables
from outage_explorer.api.users import User
from outage_explorer.config import Settings
from outage_explorer.model.build import load_current
from outage_explorer.model.tables import TABLES, Column

# Model tables with a reporting span instead of a single day; a date filter keeps rows overlapping it.
SPAN_COLUMNS = {"dim_facility": ("first_period", "last_period"), "dq_issue": ("period_start", "period_end")}
SPAN_COLUMNS["dim_generator"] = SPAN_COLUMNS["dim_facility"]

_KIND_BY_PREFIX = {"fact_": "fact", "dim_": "dimension", "metric_": "metric", "dq_": "quality"}


class ColumnOut(BaseModel):
    name: str
    type: str
    description: str


class FiltersOut(BaseModel):
    date: list[str]  # columns the start/end filter applies to; empty if the dataset has no dates
    facility: bool  # whether facility_id can be filtered on


class DatasetOut(BaseModel):
    name: str
    kind: str
    description: str
    grain: str
    primary_key: list[str]
    columns: list[ColumnOut]
    rows: int
    filters: FiltersOut


class BuildOut(BaseModel):
    build_id: str
    built_at: str


class CatalogOut(BaseModel):
    build: BuildOut
    datasets: list[DatasetOut]


def date_filter_columns(table: str, columns: tuple[Column, ...]) -> list[str]:
    names = {c.name for c in columns}
    span = [c for c in SPAN_COLUMNS.get(table, ()) if c in names]
    return span if len(span) == 2 else (["period"] if "period" in names else [])


def visible_columns(user: User, table: str) -> tuple[Column, ...]:
    """The caller's columns of `table`: 404 if no such dataset, 403 if it exists but isn't granted."""
    if table not in TABLES:
        raise ApiError(404, "dataset_not_found", f"no dataset named '{table}'")
    columns = granted_columns(user.role, table)
    if columns is None:
        raise forbidden(f"role '{user.role}' has no access to dataset '{table}'")
    return columns


def live_manifest(settings: Settings) -> dict:
    current = load_current(settings.data_path)
    if current is None:
        raise ApiError(503, "model_not_built", "no data model has been built yet; an Admin must refresh")
    return current


def _dataset(table: str, columns: tuple[Column, ...], manifest: dict) -> DatasetOut:
    t = TABLES[table]
    return DatasetOut(
        name=table,
        kind=next(k for p, k in _KIND_BY_PREFIX.items() if table.startswith(p)),
        description=t.description,
        grain=t.grain,
        primary_key=list(t.primary_key),
        columns=[ColumnOut(name=c.name, type=c.type, description=c.description) for c in columns],
        rows=manifest["tables"][table]["rows"],
        filters=FiltersOut(
            date=date_filter_columns(table, columns), facility=any(c.name == "facility_id" for c in columns)
        ),
    )


router = APIRouter(prefix="/api/datasets", tags=["catalog"])
CatalogUser = Annotated[User, Depends(require(Capability.CATALOG))]


@router.get("", response_model=CatalogOut)
def list_datasets(user: CatalogUser, request: Request) -> CatalogOut:
    manifest = live_manifest(request.app.state.settings)
    datasets = [_dataset(name, cols, manifest) for name, cols in granted_tables(user.role).items()]
    return CatalogOut(
        build=BuildOut(build_id=manifest["build_id"], built_at=manifest["built_at"]), datasets=datasets
    )


@router.get("/{name}", response_model=DatasetOut)
def get_dataset(name: str, user: CatalogUser, request: Request) -> DatasetOut:
    columns = visible_columns(user, name)  # authorization first, before the manifest is read
    return _dataset(name, columns, live_manifest(request.app.state.settings))
