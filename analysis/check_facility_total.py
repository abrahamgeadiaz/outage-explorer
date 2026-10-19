"""Anomaly A1: compare the API's `total` with the rows it actually returns, for one day per route.

Run from backend/:  uv run python ../analysis/check_facility_total.py [YYYY-MM-DD]
Needs EIA_API_KEY in .env; the key is sent as the connector sends it and never printed.
"""

import sys

import httpx

from outage_explorer.config import get_settings

day = sys.argv[1] if len(sys.argv) > 1 else "2026-10-01"
s = get_settings()
for route in ("us-nuclear-outages", "facility-nuclear-outages", "generator-nuclear-outages"):
    params = [
        ("api_key", s.eia_api_key.get_secret_value()),
        ("frequency", "daily"),
        ("data[]", "capacity"),
        ("start", day),
        ("end", day),
        ("length", 5000),
    ]
    body = httpx.get(f"{s.eia_base_url}/{route}/data/", params=params, timeout=s.eia_timeout_seconds).json()
    response = body["response"]
    print(f"{route:28} day={day} total={response['total']:>4} rows_returned={len(response['data'])}")
