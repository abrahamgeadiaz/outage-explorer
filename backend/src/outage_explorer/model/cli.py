"""Command line entry point: `uv run outage-model` builds the model from the raw extracts.

Exit codes: 0 success, 2 no raw data, a key constraint violated, or another extraction/build running.
"""

import argparse
import json
import logging
import sys

from outage_explorer.config import get_settings
from outage_explorer.connector.extract import ExtractionLockedError
from outage_explorer.model.build import KEEP_BUILDS, ModelBuildError, build_model


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the data model from data/raw into data/model.")
    parser.add_argument("--keep", type=int, default=KEEP_BUILDS, help="model builds to keep (default 3)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        manifest = build_model(get_settings(), keep=args.keep)
    except (ModelBuildError, ExtractionLockedError) as exc:
        logging.getLogger("outage_explorer").error("%s", exc)
        return 2

    brief = {name: t["rows"] for name, t in manifest["tables"].items()}
    print(
        json.dumps({"build_id": manifest["build_id"], "source": manifest["source"], "rows": brief}, indent=2)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
