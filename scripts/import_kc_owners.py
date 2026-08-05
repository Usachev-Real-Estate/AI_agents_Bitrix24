#!/usr/bin/env python3
"""CLI: import KC owner leads from Excel into Bitrix24."""

import argparse
import json
import logging
import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from config import get_settings, setup_logging  # noqa: E402
from kc_owner_import import DEFAULT_EXCEL, run_import  # noqa: E402

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Import KC owners from Excel to Bitrix24")
    parser.add_argument(
        "--excel",
        type=Path,
        default=DEFAULT_EXCEL,
        help="Path to Excel file",
    )
    parser.add_argument("--limit", type=int, default=None, help="Process only first N new rows")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview only (overrides DRY_RUN env)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Force live import (DRY_RUN=false)",
    )
    parser.add_argument(
        "--skip-excel-update",
        action="store_true",
        help="Do not write Bitrix IDs back to Excel",
    )
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.log_level)

    dry_run: bool | None = None
    if args.dry_run:
        dry_run = True
    elif args.live:
        dry_run = False

    if dry_run is False:
        logger.warning("LIVE import mode: will create contacts and deals in Bitrix24")

    summary = run_import(
        settings,
        excel_path=args.excel,
        limit=args.limit,
        dry_run=dry_run,
        skip_excel_update=args.skip_excel_update,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if summary.get("errors"):
        sys.exit(1)


if __name__ == "__main__":
    main()
