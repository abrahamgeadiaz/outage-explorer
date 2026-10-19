"""Run SQL files against the live model, or expose it to a database manager.

    uv run outage-sql ../analysis/findings.sql          print the result of every block in the file
    uv run outage-sql --duckdb ../data/explore.duckdb   write the views into a DuckDB file (DBeaver...)

Views: every model table by name (from data/model/current.json) and raw_<dataset> for the raw extracts.
A file is split into blocks at lines starting with `-- name:`; each block is one statement, and its
name is printed above its result. This is a developer tool; it is not the API's query path.
"""

import argparse
import re
import sys
from pathlib import Path

import duckdb

from outage_explorer.config import get_settings
from outage_explorer.connector.routes import ROUTES
from outage_explorer.model.build import ModelBuildError, table_globs

_BLOCK = re.compile(r"^-- name:\s*(.+)$", re.MULTILINE)


def create_views(con: duckdb.DuckDBPyConnection, data: Path) -> None:
    for name, glob in table_globs(data).items():
        con.execute(
            f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_parquet('{glob}', hive_partitioning = true)"
        )
    for name in ROUTES:
        glob = (data / "raw" / name / "year=*" / "*.parquet").as_posix()
        con.execute(
            f"CREATE OR REPLACE VIEW raw_{name} AS SELECT * FROM read_parquet('{glob}', union_by_name = true)"
        )


def blocks(sql: str) -> list[tuple[str, str]]:
    """[(name, statement)] for each `-- name:` block; text before the first block is ignored."""
    marks = list(_BLOCK.finditer(sql))
    ends = [m.start() for m in marks[1:]] + [len(sql)]
    return [(m.group(1).strip(), sql[m.end() : end].strip()) for m, end in zip(marks, ends, strict=True)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run SQL against the model, or export its views.")
    parser.add_argument("file", nargs="?", type=Path, help="SQL file with `-- name:` blocks")
    parser.add_argument("--duckdb", type=Path, help="write the views into this DuckDB database file")
    parser.add_argument("--max-rows", type=int, default=40)
    args = parser.parse_args(argv)
    if not args.file and not args.duckdb:
        parser.error("give a SQL file, --duckdb PATH, or both")

    data = get_settings().data_path
    try:
        if args.duckdb:
            with duckdb.connect(str(args.duckdb)) as db:
                create_views(db, data)
            print(f"views written to {args.duckdb} (they read data/ in place; re-run after each model build)")
        if args.file:
            con = duckdb.connect()
            create_views(con, data)
            for name, statement in blocks(args.file.read_text(encoding="utf-8")):
                print(f"\n-- {name}")
                con.sql(statement).show(max_rows=args.max_rows, max_width=220)
    except ModelBuildError as exc:
        print(exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
