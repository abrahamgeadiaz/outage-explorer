"""Build the model from data/raw into a new versioned folder, then point data/model/current.json at it.

Layout:
    data/model/current.json                         which build is live, its tables and row counts
    data/model/builds/<build_id>/<table>/...        one folder per table; facts split into year=YYYY/

Why builds are versioned instead of rewritten in place:
- Readers (the API) always see one complete build: the new one is written to a temp folder, renamed,
  and only then does current.json switch to it (atomic os.replace). A crash mid-build changes nothing.
- On Windows a Parquet file that a reader has open cannot be replaced; a new folder sidesteps that.
- The previous builds stay for comparison and rollback (the newest `keep` are kept).

How the raw rows become model rows:
- Duplicates on the natural key cannot come from the connector (it upserts on that key), but the build
  does not rely on it: it keeps the most recently ingested row per key and reports how many it dropped.
- Every table is created with the PRIMARY KEY and FOREIGN KEYs from tables.py, so DuckDB rejects a
  duplicate key or an orphan row during the build (ModelBuildError) instead of writing a bad model.
- Odd but well-formed values (zero capacity, outage > capacity) are kept: they are findings, reported
  by the data quality checks, not reasons to change EIA's numbers.

The build holds the same lock as the connector, so it never reads a raw file while it is being swapped.
"""

import json
import logging
import os
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from outage_explorer.config import Settings
from outage_explorer.connector.extract import RunLock
from outage_explorer.connector.routes import ROUTES
from outage_explorer.model.tables import TABLES, Table

log = logging.getLogger(__name__)

KEEP_BUILDS = 3

# Raw column -> model column (only the ones that are renamed).
_RENAMES = {"facility": "facility_id", "generator": "generator_id"}

_VALUES_SQL = """
    capacity::DECIMAL(12,3) AS capacity_mw,
    outage::DECIMAL(12,3) AS outage_mw,
    CASE WHEN capacity > 0 THEN 100 * outage / capacity END AS percent_outage,
    percent_outage::DECIMAL(6,2) AS percent_outage_eia,
    _run_id, _ingested_at
"""

# Each table's rows, in dependency order (a table may only reference the ones above it).
_SELECTS: dict[str, str] = {
    "dim_facility": """
        WITH seen AS (
            SELECT facility AS facility_id, facility_name, period FROM src_facility
            UNION ALL
            SELECT facility, facility_name, period FROM src_generator
        )
        SELECT facility_id,
               arg_max(facility_name, period) AS facility_name,
               min(period) AS first_period,
               max(period) AS last_period,
               (SELECT count(DISTINCT generator) FROM src_generator g WHERE g.facility = s.facility_id)
                   AS generator_count,
               CASE WHEN max(period) = (SELECT max(period) FROM seen) THEN 'active' ELSE 'retired' END
                   AS status
        FROM seen s GROUP BY facility_id
    """,
    "dim_generator": """
        SELECT facility AS facility_id, generator AS generator_id,
               arg_max(facility_name, period) AS facility_name,
               min(period) AS first_period,
               max(period) AS last_period,
               count(*) AS reported_days,
               (max(period) - min(period) + 1) - count(*) AS missing_days,
               arg_max(capacity, period)::DECIMAL(12,3) AS latest_capacity_mw,
               CASE WHEN max(period) = (SELECT max(period) FROM src_generator)
                    THEN 'active' ELSE 'retired' END AS status
        FROM src_generator GROUP BY facility, generator
    """,
    "fact_national_daily": f"SELECT period, {_VALUES_SQL} FROM src_national",
    "fact_facility_daily": f"""
        SELECT period, facility AS facility_id, facility_name, {_VALUES_SQL} FROM src_facility
    """,
    "fact_generator_daily": f"""
        SELECT period, facility AS facility_id, generator AS generator_id, facility_name, {_VALUES_SQL}
        FROM src_generator
    """,
}


class ModelBuildError(Exception):
    """The model could not be built (no raw data, or a key constraint was violated)."""


def model_dir(data: Path) -> Path:
    return data / "model"


def load_current(data: Path) -> dict | None:
    """The live build's manifest, or None if no model has been built yet."""
    path = model_dir(data) / "current.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def table_globs(data: Path) -> dict[str, str]:
    """Absolute Parquet glob for each table of the live build (what readers pass to read_parquet)."""
    current = load_current(data)
    if current is None:
        raise ModelBuildError("no model built yet: run `uv run outage-model`")
    root = model_dir(data)
    return {name: (root / t["glob"]).as_posix() for name, t in current["tables"].items()}


def build_model(settings: Settings, *, keep: int = KEEP_BUILDS) -> dict:
    """Build every table from the raw extracts, switch current.json to it, and return its manifest."""
    data = settings.data_path
    # Microseconds keep ids in creation order (pruning sorts by name); the suffix only avoids collisions.
    build_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:6]
    builds = model_dir(data) / "builds"
    tmp = builds / f".tmp-{build_id}"

    with RunLock(data, f"model build {build_id}"):
        tmp.mkdir(parents=True)
        try:
            manifest = _build(data, tmp, build_id)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        os.replace(tmp, builds / build_id)  # the DuckDB connection is closed: no open files inside
        _write_json_atomic(model_dir(data) / "current.json", manifest)
        _prune(builds, keep, current=build_id)

    log.info("model build %s: %s", build_id, {n: t["rows"] for n, t in manifest["tables"].items()})
    return manifest


def _build(data: Path, out: Path, build_id: str) -> dict:
    manifest: dict = {
        "build_id": build_id,
        "built_at": datetime.now(UTC).isoformat(),
        "source": {},
        "tables": {},
    }
    con = duckdb.connect()  # in-memory; the model only lives in Parquet
    try:
        for name, route in ROUTES.items():
            manifest["source"][name] = _register_source(con, data / "raw" / name, name, route.key_columns)

        for name, select in _SELECTS.items():
            table = TABLES[name]
            con.execute(_ddl(table))
            try:
                con.execute(f"INSERT INTO {name} BY NAME {select}")
            except duckdb.ConstraintException as exc:
                raise ModelBuildError(f"{name}: {exc}") from exc

        for name, table in TABLES.items():
            manifest["tables"][name] = _write_table(con, table, out, build_id)
    finally:
        con.close()
    return manifest


def _register_source(
    con: duckdb.DuckDBPyConnection, folder: Path, name: str, raw_key: tuple[str, ...]
) -> dict:
    files = sorted(folder.glob("year=*/*.parquet"))
    if not files:
        raise ModelBuildError(f"no raw data for {name} in {folder}: run `uv run outage-extract` first")
    glob = (folder / "year=*" / "*.parquet").as_posix()
    keys = ", ".join(raw_key)
    # union_by_name: a column added in later years (schema drift) does not break older files.
    con.execute(
        f"CREATE VIEW raw_{name} AS SELECT * FROM read_parquet('{_lit(glob)}', union_by_name = true, "
        f"hive_partitioning = false)"
    )
    con.execute(
        f"CREATE TABLE src_{name} AS SELECT * FROM raw_{name} "
        f"QUALIFY row_number() OVER (PARTITION BY {keys} ORDER BY _ingested_at DESC, _run_id DESC) = 1"
    )
    raw_rows = con.execute(f"SELECT count(*) FROM raw_{name}").fetchone()[0]
    rows, max_period = con.execute(f"SELECT count(*), max(period) FROM src_{name}").fetchone()
    if raw_rows != rows:
        log.warning("%s: dropped %d duplicate rows on %s", name, raw_rows - rows, keys)
    return {
        "files": len(files),
        "rows": rows,
        "duplicates_dropped": raw_rows - rows,
        "max_period": max_period.isoformat() if max_period else None,
    }


def _ddl(table: Table) -> str:
    cols = [f"{c.name} {c.type}" for c in table.columns]
    cols.append(f"PRIMARY KEY ({', '.join(table.primary_key)})")
    for fk_cols, ref in table.foreign_keys.items():
        cols.append(f"FOREIGN KEY ({', '.join(fk_cols)}) REFERENCES {ref} ({', '.join(fk_cols)})")
    return f"CREATE TABLE {table.name} ({', '.join(cols)})"


def _write_table(con: duckdb.DuckDBPyConnection, table: Table, out: Path, build_id: str) -> dict:
    folder = out / table.name
    order = ", ".join(table.primary_key)
    if table.partitioned_by_year:
        # Partition folders are named year=YYYY; readers get the column back with hive_partitioning.
        target = folder
        query = f"SELECT *, year(period) AS year FROM {table.name} ORDER BY {order}"
        options = "FORMAT parquet, COMPRESSION zstd, PARTITION_BY (year)"
        glob = f"{table.name}/year=*/*.parquet"
    else:
        folder.mkdir()
        target = folder / "data.parquet"
        query = f"SELECT * FROM {table.name} ORDER BY {order}"
        options = "FORMAT parquet, COMPRESSION zstd"
        glob = f"{table.name}/*.parquet"
    con.execute(f"COPY ({query}) TO '{_lit(target)}' ({options})")
    rows = con.execute(f"SELECT count(*) FROM {table.name}").fetchone()[0]
    return {"rows": rows, "glob": f"builds/{build_id}/{glob}", "primary_key": list(table.primary_key)}


def _prune(builds: Path, keep: int, current: str) -> None:
    done = sorted((p for p in builds.iterdir() if p.is_dir() and not p.name.startswith(".")), reverse=True)
    for old in done[max(keep, 1) :]:
        if old.name == current:  # never delete the live build, whatever the ordering says
            continue
        try:
            shutil.rmtree(old)
        except OSError as exc:  # e.g. a reader still has a file open on Windows; retried next build
            log.warning("could not remove old build %s: %s", old.name, exc)


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _lit(path: Path | str) -> str:
    # COPY TO / read_parquet take string literals; paths come from our settings, quotes escaped anyway.
    return Path(path).as_posix().replace("'", "''")
