"""FastAPI application: `uv run outage-api` serves it on http://127.0.0.1:8000 (OpenAPI docs at /docs)."""

import argparse
import logging

import uvicorn
from fastapi import FastAPI

from outage_explorer.api import auth, catalog, preview, query, refresh
from outage_explorer.api.cache import QueryCache
from outage_explorer.api.errors import install_handlers
from outage_explorer.api.ratelimit import LoginLimiter
from outage_explorer.config import Settings, get_settings
from outage_explorer.model.build import load_current


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    auth.check_secret(settings)  # fail at startup, not on the first login

    app = FastAPI(
        title="Outage Explorer API",
        version="0.1.0",
        description="Catalog, preview, read-only SQL and refresh over the EIA nuclear outage model.",
    )
    app.state.settings = settings
    app.state.login_limiter = LoginLimiter(
        settings.login_max_failures, settings.login_max_failures_per_client, settings.login_window_seconds
    )
    app.state.query_cache = QueryCache(settings.query_cache_ttl_seconds, settings.query_cache_max_rows)
    app.state.refresh_jobs = refresh.RefreshJobs(settings, on_new_build=app.state.query_cache.clear)
    install_handlers(app)
    app.include_router(auth.router)
    app.include_router(catalog.router)
    app.include_router(preview.router)
    app.include_router(query.router)
    app.include_router(refresh.router)

    @app.get("/api/health", tags=["health"])
    def health() -> dict:
        """Liveness, plus which model build is being served (no auth: it reveals no data)."""
        current = load_current(settings.data_path)
        return {"status": "ok", "model_build": current["build_id"] if current else None}

    return app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the Outage Explorer API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(), host=args.host, port=args.port)
