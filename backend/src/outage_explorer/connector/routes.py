"""The three EIA nuclear outage routes and the shape we expect from each.

Fields come from the metadata endpoints (GET /v2/nuclear-outages/<route>/). All three share the same
value columns; they differ only in which identifier columns make a row unique.
"""

from dataclasses import dataclass

VALUE_COLUMNS = ("capacity", "outage", "percentOutage")


@dataclass(frozen=True)
class Route:
    dataset: str  # our local name, used for folders and later for the catalog
    api_path: str  # EIA route under /v2/nuclear-outages/
    key_columns: tuple[str, ...]  # natural key: one row per these values
    label_columns: tuple[str, ...] = ()  # descriptive, not part of the key

    @property
    def sort_columns(self) -> tuple[str, ...]:
        # A total order over the natural key keeps offset pagination stable between pages.
        return self.key_columns


NATIONAL = Route("national", "us-nuclear-outages", ("period",))
FACILITY = Route("facility", "facility-nuclear-outages", ("period", "facility"), ("facilityName",))
GENERATOR = Route(
    "generator", "generator-nuclear-outages", ("period", "facility", "generator"), ("facilityName",)
)

ROUTES: dict[str, Route] = {r.dataset: r for r in (NATIONAL, FACILITY, GENERATOR)}
