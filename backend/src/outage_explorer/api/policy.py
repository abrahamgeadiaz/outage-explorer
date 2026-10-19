"""Who may do what. Every authorization rule in the API is read from this module.

Two layers, both checked before any data is read:
- capabilities: which endpoints a role may call at all;
- dataset grants: which tables, and which columns of them, a role may see. The catalog lists only
  these, the preview reads only these, and the query path will reject any other table.

Grants are allow-lists: a table or column added to the model later stays invisible to a role until
it is granted here.
"""

from enum import StrEnum

from outage_explorer.model.tables import TABLES, Column


class Role(StrEnum):
    ANALYST = "analyst"
    VIEWER = "viewer"
    ADMIN = "admin"


class Capability(StrEnum):
    CATALOG = "catalog"  # list datasets and their columns
    PREVIEW = "preview"  # read rows of a dataset
    QUERY = "query"  # run read-only SQL
    REFRESH = "refresh"  # reload the data from EIA


_READ = frozenset({Capability.CATALOG, Capability.PREVIEW, Capability.QUERY})

ROLE_CAPABILITIES: dict[Role, frozenset[Capability]] = {
    Role.VIEWER: _READ,
    Role.ANALYST: _READ,
    Role.ADMIN: _READ | {Capability.REFRESH},
}


def can(role: Role, capability: Capability) -> bool:
    return capability in ROLE_CAPABILITIES[role]


ALL_COLUMNS = None  # grant value: every column of the table

_EVERY_TABLE: dict[str, frozenset[str] | None] = dict.fromkeys(TABLES, ALL_COLUMNS)

DATASET_GRANTS: dict[Role, dict[str, frozenset[str] | None]] = {
    Role.ANALYST: _EVERY_TABLE,
    Role.ADMIN: _EVERY_TABLE,
    # National trends only (US-05): no table keyed by facility or generator, and no column that
    # names one. metric_fleet_offline_daily.missing_units lists 'facility_id/generator_id' values,
    # so the Viewer gets that table without it.
    Role.VIEWER: {
        "fact_national_daily": ALL_COLUMNS,
        "metric_fleet_offline_daily": frozenset(
            {
                "period",
                "capacity_mw",
                "outage_mw",
                "offline_pct",
                "percent_outage_eia",
                "units_reported",
                "units_missing",
                "missing_capacity_mw",
                "units_zero_capacity",
                "offline_pct_min",
                "offline_pct_max",
                "is_complete",
            }
        ),
    },
}


def granted_columns(role: Role, table: str) -> tuple[Column, ...] | None:
    """The columns of `table` that `role` may see, in model order; None if the table is not granted."""
    grants = DATASET_GRANTS[role]
    if table not in grants or table not in TABLES:
        return None
    allowed = grants[table]
    return tuple(c for c in TABLES[table].columns if allowed is ALL_COLUMNS or c.name in allowed)


def granted_tables(role: Role) -> dict[str, tuple[Column, ...]]:
    """Every table `role` may see, with its visible columns, in model order."""
    return {name: cols for name in TABLES if (cols := granted_columns(role, name)) is not None}
