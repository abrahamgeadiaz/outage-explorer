"""Raw Parquet storage: one file per dataset and year, updated by replacing a date window.

Layout: <data>/raw/<dataset>/year=<YYYY>/data.parquet

Why re-runs are safe:
- The rows fetched for [start, end] *replace* whatever the file held for that window (an upsert on the
  natural key), so running the same extraction twice leaves the file unchanged; it never appends.
- The new file is written next to the old one and swapped in with os.replace, which is atomic: a crash
  mid-write leaves the previous file intact, never a half-written one.
- An empty fetch never wipes a window that already has data (UnsafeOverwriteError): an API glitch
  that returns zero rows must not look like "all outages were deleted".

Unchanged rows keep their original _run_id/_ingested_at, so lineage shows when a value last changed.
"""

import logging
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

import duckdb
import pyarrow as pa

log = logging.getLogger(__name__)

LINEAGE_COLUMNS = ("_run_id", "_ingested_at")


class UnsafeOverwriteError(Exception):
    """Refused to replace existing rows with an empty result."""


@dataclass
class MergeStats:
    rows_before: int = 0
    rows_after: int = 0
    inserted: int = 0
    updated: int = 0  # same key, different values: the source revised history
    unchanged: int = 0
    deleted: int = 0  # in the window before, absent from the source now

    def as_dict(self) -> dict:
        return asdict(self)


def partition_path(raw_dir: Path, dataset: str, year: int) -> Path:
    return raw_dir / dataset / f"year={year}" / "data.parquet"


def upsert_window(
    path: Path,
    incoming: pa.Table,
    key_columns: tuple[str, ...],
    start: date,
    end: date,
    *,
    run_id: str,
    ingested_at: datetime,
) -> MergeStats:
    incoming = incoming.append_column("_run_id", pa.array([run_id] * incoming.num_rows, pa.string()))
    incoming = incoming.append_column(
        "_ingested_at", pa.array([ingested_at] * incoming.num_rows, pa.timestamp("us"))
    )
    value_columns = [c for c in incoming.column_names if c not in key_columns and c not in LINEAGE_COLUMNS]

    keys = ", ".join(_q(c) for c in key_columns)
    join_on = " AND ".join(f"e.{_q(c)} = i.{_q(c)}" for c in key_columns)
    same_values = " AND ".join(f"e.{_q(c)} IS NOT DISTINCT FROM i.{_q(c)}" for c in value_columns)
    in_window = "period BETWEEN $start AND $end"
    params = {"start": start, "end": end}
    # The f-string SQL below (S608) only interpolates column names from our own Route/schema definitions,
    # always quoted with _q(); values are bound parameters. No user input reaches these statements.

    con = duckdb.connect()  # in-memory; only used to compute the merge
    try:
        con.register("incoming", incoming)
        if path.exists():
            con.read_parquet(str(path)).create_view("existing")
        else:
            con.execute("CREATE VIEW existing AS SELECT * FROM incoming LIMIT 0")

        stats = MergeStats()
        stats.rows_before = _count(con, "SELECT count(*) FROM existing")
        existing_in_window = _count(con, f"SELECT count(*) FROM existing WHERE {in_window}", params)
        if incoming.num_rows == 0 and existing_in_window > 0:
            raise UnsafeOverwriteError(
                f"{path}: source returned 0 rows for {start}..{end} but {existing_in_window} are stored"
            )

        con.execute(
            f"""
            CREATE TEMP TABLE matched AS
            SELECT {", ".join(f"i.{_q(c)}" for c in key_columns)}, ({same_values}) AS unchanged
            FROM (SELECT * FROM existing WHERE {in_window}) e JOIN incoming i ON {join_on}
            """,
            params,
        )
        matched = _count(con, "SELECT count(*) FROM matched")
        stats.unchanged = _count(con, "SELECT count(*) FROM matched WHERE unchanged")
        stats.updated = matched - stats.unchanged
        stats.inserted = incoming.num_rows - matched
        stats.deleted = existing_in_window - matched

        merged = f"""
            SELECT * FROM existing WHERE NOT ({in_window})
            UNION ALL BY NAME
            SELECT e.* FROM existing e SEMI JOIN (SELECT * FROM matched WHERE unchanged) i ON {join_on}
            UNION ALL BY NAME
            SELECT i.* FROM incoming i ANTI JOIN (SELECT * FROM matched WHERE unchanged) e ON {join_on}
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{run_id}.tmp")
        con.execute(
            f"COPY ({merged} ORDER BY {keys}) TO '{_lit(tmp)}' (FORMAT parquet, COMPRESSION zstd)", params
        )
        stats.rows_after = _count(con, "SELECT count(*) FROM read_parquet($p)", {"p": str(tmp)})
    finally:
        con.close()  # release the file handle before the swap (required on Windows)

    os.replace(tmp, path)
    log.info("%s: %s", path, stats.as_dict())
    return stats


def _count(con: duckdb.DuckDBPyConnection, sql: str, params: dict | None = None) -> int:
    return con.execute(sql, params or {}).fetchone()[0]


def _q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _lit(path: Path) -> str:
    # COPY TO takes a string literal, not a parameter; escape quotes in the path.
    return path.as_posix().replace("'", "''")
