#!/usr/bin/env python3
"""CLI: fill «Базовая ставка» on buyer deals from motivation CSV."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from config import get_settings, setup_logging  # noqa: E402
from fill_buyer_base_rate import DEFAULT_CSV, run_fill  # noqa: E402

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fill buyer-deal base rate (UF) from broker motivation CSV"
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=DEFAULT_CSV,
        help="Path to motivation CSV",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only first N planned updates",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview only (overrides DRY_RUN env)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Force live updates (DRY_RUN=false)",
    )
    args = parser.parse_args()

    if args.dry_run and args.live:
        parser.error("Use either --dry-run or --live, not both")

    settings = get_settings()
    setup_logging(settings.log_level)

    dry_run: bool | None = None
    if args.dry_run:
        dry_run = True
    elif args.live:
        dry_run = False

    if dry_run is False:
        logger.warning("LIVE mode: will update deals in Bitrix24")

    summary = run_fill(
        settings,
        csv_path=args.csv,
        dry_run=dry_run,
        limit=args.limit,
    )
    print(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
