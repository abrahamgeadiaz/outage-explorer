import json
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from outage_explorer.connector.routes import ROUTES
from outage_explorer.connector.validate import schema_for

FIXTURES = Path(__file__).parent / "fixtures" / "eia"


@pytest.fixture
def fixture_json():
    def load(name: str) -> dict:
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    return load


@pytest.fixture
def fixture_text():
    return lambda name: (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def write_raw(tmp_path):
    """Write raw rows the way the connector stores them: <tmp>/raw/<dataset>/year=YYYY/data.parquet.

    Appends to a partition that already exists, so a test can add duplicate or conflicting rows.
    """

    def write(dataset: str, rows: list[dict], *, run_id="r1", ingested_at=datetime(2026, 10, 1)) -> None:
        schema = schema_for(ROUTES[dataset])
        schema = schema.append(pa.field("_run_id", pa.string()))
        schema = schema.append(pa.field("_ingested_at", pa.timestamp("us")))
        by_year = defaultdict(list)
        for r in rows:
            full = {"percent_outage": None, "_run_id": run_id, "_ingested_at": ingested_at, **r}
            if dataset != "national":
                full.setdefault("facility_name", "Plant " + r["facility"])
            by_year[r["period"].year].append(full)
        for year, year_rows in by_year.items():
            path = tmp_path / "raw" / dataset / f"year={year}" / "data.parquet"
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = pq.read_table(path).to_pylist() if path.exists() else []
            pq.write_table(pa.Table.from_pylist(existing + year_rows, schema=schema), path)

    return write


@pytest.fixture
def write_fleet(write_raw):
    """Write the rows of fleet_rows() for all three grains."""

    def write(rows: dict[str, list[dict]]) -> None:
        for dataset, ds_rows in rows.items():
            write_raw(dataset, ds_rows)

    return write


def fleet_rows(
    units: dict[tuple[str, str], float],
    days: list[date],
    outages: dict[tuple[date, str, str], float] | None = None,
    missing: set[tuple[date, str, str]] | None = None,
) -> dict[str, list[dict]]:
    """Rows for all three grains, built the way EIA builds them: facility and national rows are the
    sums of the units reported that day.

    units: {(facility, generator): capacity MW}; outages: {(day, facility, generator): outage MW};
    missing: {(day, facility, generator)} units absent from every grain on that day.
    """
    outages, missing = outages or {}, missing or set()
    rows: dict[str, list[dict]] = {"national": [], "facility": [], "generator": []}
    for day in days:
        totals: dict[str, list[float]] = {}
        for (fac, gen), cap in units.items():
            if (day, fac, gen) in missing:
                continue
            out = outages.get((day, fac, gen), 0.0)
            rows["generator"].append(
                {
                    "period": day,
                    "facility": fac,
                    "generator": gen,
                    "capacity": cap,
                    "outage": out,
                    "percent_outage": _pct(out, cap),
                }
            )
            tot = totals.setdefault(fac, [0.0, 0.0])
            tot[0] += cap
            tot[1] += out
        for fac, (cap, out) in totals.items():
            rows["facility"].append(
                {
                    "period": day,
                    "facility": fac,
                    "capacity": cap,
                    "outage": out,
                    "percent_outage": _pct(out, cap),
                }
            )
        cap = sum(c for c, _ in totals.values())
        out = sum(o for _, o in totals.values())
        rows["national"].append(
            {"period": day, "capacity": cap, "outage": out, "percent_outage": _pct(out, cap)}
        )
    return rows


def _pct(outage: float, capacity: float) -> float:
    return round(100 * outage / capacity, 2) if capacity > 0 else 0.0
