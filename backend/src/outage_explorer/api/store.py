"""Serving databases: the only data an API request can physically reach.

For each distinct grant (policy.granted_tables of a role), the live model build is copied into a DuckDB
file holding just the granted tables and columns: data/serve/<build_id>/<grant_key>.duckdb. Requests
open it read-only with file access disabled and the configuration locked, so even SQL that slipped past
every check could not write, read Parquet or other files, ATTACH a database, or switch the sandbox off.
A Viewer's file contains no facility or generator rows at all.

The file name includes a hash of the grant, so changing policy.py serves a new file at once; it also
includes the build id, so a refresh does too. Older builds' files are deleted once a newer one exists.
Building one takes about a second for the full grant (1.16M rows, ~15 MB).
"""

import hashlib
import json
import logging
import shutil
import threading
import uuid
from pathlib import Path

import duckdb

from outage_explorer.model.build import model_dir
from outage_explorer.model.tables import Column

log = logging.getLogger(__name__)

# Applied when a request opens a serving file (together with read_only=True). Without
# python_enable_replacements=False, `FROM name` would also resolve Python DataFrames / Arrow tables in
# the calling code's scope. The resource limits keep one query from taking the whole machine
# (DECISIONS #27): DuckDB's defaults are 80% of RAM and every core. Connections to one file share a
# database instance, so the memory limit holds for all requests reading that file together.
SANDBOX = {
    "enable_external_access": False,
    "python_enable_replacements": False,
    "memory_limit": "1GB",  # the full grant's file is ~15 MB; this is room for sorts and joins
    "threads": 2,
    "max_temp_directory_size": "0GB",  # no spilling to disk: over the limit, the query fails
    "lock_configuration": True,  # last: the limits above can't be SET back by a query
}

_build_lock = threading.Lock()  # one process (uvicorn's default): a thread lock is enough


def grant_key(tables: dict[str, tuple[Column, ...]]) -> str:
    spec = {name: [c.name for c in cols] for name, cols in sorted(tables.items())}
    return hashlib.sha256(json.dumps(spec).encode("utf-8")).hexdigest()[:16]


def serve_dir(data: Path) -> Path:
    return data / "serve"


def connect(data: Path, manifest: dict, tables: dict[str, tuple[Column, ...]]) -> duckdb.DuckDBPyConnection:
    """A read-only, sandboxed connection to the serving file for this build and grant (built if needed)."""
    path = serve_dir(data) / manifest["build_id"] / f"{grant_key(tables)}.duckdb"
    if not path.exists():
        with _build_lock:
            if not path.exists():  # another request may have built it while we waited
                _build(data, manifest, tables, path)
    return duckdb.connect(str(path), read_only=True, config=SANDBOX)


def _build(data: Path, manifest: dict, tables: dict[str, tuple[Column, ...]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".tmp-{uuid.uuid4().hex}.duckdb")
    try:
        with duckdb.connect(str(tmp)) as con:
            for name, columns in tables.items():
                glob = (model_dir(data) / manifest["tables"][name]["glob"]).as_posix()
                cols = ", ".join(f'"{c.name}"' for c in columns)
                # Names come from model/tables.py and paths from our own manifest, never from a request.
                con.execute(
                    f'CREATE TABLE "{name}" AS SELECT {cols} '  # noqa: S608
                    f"FROM read_parquet('{glob}', hive_partitioning = true)"
                )
        tmp.replace(path)  # atomic: a reader sees no file or a complete one
    finally:
        tmp.unlink(missing_ok=True)
    log.info("serving file %s built for tables %s", path.name, sorted(tables))
    _prune(data, keep=manifest["build_id"])


def _prune(data: Path, keep: str) -> None:
    for child in serve_dir(data).iterdir():
        if child.is_dir() and child.name != keep:
            # A request may still hold an old file open (Windows refuses to delete it); retry next build.
            shutil.rmtree(child, ignore_errors=True)
