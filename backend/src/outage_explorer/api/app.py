"""FastAPI application: `uv run outage-api` serves it on http://127.0.0.1:8000 (OpenAPI docs at /docs)."""

import argparse
import logging

import uvicorn
from fastapi import FastAPI

from outage_explorer.api import auth, catalog
from outage_explorer.api.errors import install_handlers
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
    install_handlers(app)
    app.include_router(auth.router)
    app.include_router(catalog.router)

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
