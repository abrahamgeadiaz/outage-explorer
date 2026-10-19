"""Dataset grants and the catalog (US-02, US-05)."""

import pytest

from outage_explorer.api.policy import ALL_COLUMNS, DATASET_GRANTS, Role, granted_tables
from outage_explorer.model.tables import TABLES
from tests.conftest import auth_headers

# Anything that identifies a plant or a unit. A Viewer must never see one (US-05).
DETAIL_COLUMNS = {"facility_id", "generator_id", "facility_name", "missing_units"}


def test_every_grant_names_a_real_table_and_real_columns():
    for role, grants in DATASET_GRANTS.items():
        for table, columns in grants.items():
            assert table in TABLES, (role, table)
            if columns is not ALL_COLUMNS:
                assert columns <= set(TABLES[table].column_names), (role, table)


def test_viewer_is_granted_national_data_only():
    visible = granted_tables(Role.VIEWER)
    assert set(visible) == {"fact_national_daily", "metric_fleet_offline_daily"}
    for table, columns in visible.items():
        assert not {c.name for c in columns} & DETAIL_COLUMNS, table


def test_analyst_and_admin_see_every_table_and_column():
    for role in (Role.ANALYST, Role.ADMIN):
        assert {t: tuple(c.name for c in cols) for t, cols in granted_tables(role).items()} == {
            t.name: t.column_names for t in TABLES.values()
        }


@pytest.mark.parametrize("username", ["analyst", "admin"])
def test_catalog_lists_every_dataset_with_columns_types_and_rows(client, api_model, username):
    body = client.get("/api/datasets", headers=auth_headers(client, username)).json()
    assert body["build"]["build_id"] == api_model["build_id"]
    by_name = {d["name"]: d for d in body["datasets"]}
    assert set(by_name) == set(TABLES)
    gen = by_name["fact_generator_daily"]
    assert gen["kind"] == "fact"
    assert gen["primary_key"] == ["period", "facility_id", "generator_id"]
    assert gen["rows"] == 3 * 3 - 1  # 3 units x 3 days, one unit missing one day
    assert {"name": "capacity_mw", "type": "DECIMAL(12,3)"}.items() <= gen["columns"][4].items()
    assert gen["filters"] == {"date": ["period"], "facility": True}
    assert by_name["dim_generator"]["filters"] == {"date": ["first_period", "last_period"], "facility": True}
    assert by_name["dq_issue"]["filters"] == {"date": ["period_start", "period_end"], "facility": True}
    assert by_name["fact_national_daily"]["filters"] == {"date": ["period"], "facility": False}


def test_metric_definitions_are_served_by_the_catalog(client, api_model):
    metric = client.get("/api/datasets/metric_fleet_offline_daily", headers=auth_headers(client, "analyst"))
    body = metric.json()
    assert body["kind"] == "metric"
    assert body["description"].startswith("US-08")
    offline = next(c for c in body["columns"] if c["name"] == "offline_pct")
    assert "100 * outage_mw / capacity_mw" in offline["description"]


def test_viewer_catalog_has_no_facility_or_generator_detail(client, api_model):
    body = client.get("/api/datasets", headers=auth_headers(client, "viewer")).json()
    names = [d["name"] for d in body["datasets"]]
    assert names == ["fact_national_daily", "metric_fleet_offline_daily"]
    for d in body["datasets"]:
        assert not {c["name"] for c in d["columns"]} & DETAIL_COLUMNS
        assert d["filters"]["facility"] is False


@pytest.mark.parametrize(
    "dataset", ["fact_facility_daily", "fact_generator_daily", "dim_facility", "dim_generator", "dq_issue"]
)
def test_viewer_cannot_describe_a_detail_dataset(client, api_model, dataset):
    resp = client.get(f"/api/datasets/{dataset}", headers=auth_headers(client, "viewer"))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    assert "columns" not in resp.text


def test_unknown_dataset_is_404(client, api_model):
    resp = client.get("/api/datasets/users", headers=auth_headers(client, "analyst"))
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "dataset_not_found"


@pytest.mark.parametrize("path", ["/api/datasets", "/api/datasets/fact_national_daily"])
def test_catalog_requires_login(client, api_model, path):
    assert client.get(path).status_code == 401


def test_without_a_model_the_catalog_says_so(client):
    resp = client.get("/api/datasets", headers=auth_headers(client, "analyst"))
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "model_not_built"


def test_authorization_is_decided_before_the_model_is_read(client):
    # No model built: a forbidden request is still answered 403, not 503, so it never got to the data.
    resp = client.get("/api/datasets/fact_generator_daily", headers=auth_headers(client, "viewer"))
    assert resp.status_code == 403
