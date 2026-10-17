"""Command line entry point: `uv run outage-extract [--full] [--dataset NAME ...]`.

Exit codes: 0 success, 1 partial or failed run, 2 configuration/credential error or another run active.
"""

import argparse
import json
import logging
import sys
from datetime import date

from outage_explorer.config import get_settings
from outage_explorer.connector.eia_client import EIAAuthError
from outage_explorer.connector.extract import ExtractionLockedError, run_extraction
from outage_explorer.connector.routes import ROUTES


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract EIA nuclear outage data into local Parquet.")
    parser.add_argument("--dataset", action="append", choices=sorted(ROUTES), help="repeatable; default: all")
    parser.add_argument("--full", action="store_true", help="ignore watermarks and re-extract from 2007")
    parser.add_argument("--start", type=date.fromisoformat, help="override window start (YYYY-MM-DD)")
    parser.add_argument("--end", type=date.fromisoformat, help="override window end (YYYY-MM-DD)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    try:
        summary = run_extraction(get_settings(), args.dataset, full=args.full, start=args.start, end=args.end)
    except (EIAAuthError, ExtractionLockedError) as exc:
        logging.getLogger("outage_explorer").error("%s", exc)
        return 2

    brief = {
        name: {
            k: ds[k] for k in ("status", "rows_fetched", "rows_rejected", "inserted", "updated", "watermark")
        }
        for name, ds in summary["datasets"].items()
    }
    print(json.dumps({"run_id": summary["run_id"], "status": summary["status"], "datasets": brief}, indent=2))
    return 0 if summary["status"] == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
