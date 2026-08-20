"""Entry point for b24-ai-auditor."""

import asyncio
import logging
import sys
from pathlib import Path

from pydantic import ValidationError

# Ensure src/ is on sys.path when running as `python src/main.py`
_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from config import get_settings, setup_logging  # noqa: E402
from graph import run_audit_v2  # noqa: E402

logger = logging.getLogger(__name__)


async def main() -> None:
    """Load config, run v2 audit pipeline, log result."""
    settings = get_settings()
    setup_logging(settings.log_level)

    logger.info(
        "Starting b24-ai-auditor V2 (DRY_RUN=%s, BUYERS_CAT=%d, SELLERS_CAT=%d)",
        settings.dry_run,
        settings.buyers_category_id,
        settings.sellers_category_id,
    )
    result = await run_audit_v2(settings)
    skipped = int(result.get("skipped_incomplete") or 0)
    logger.info(
        "Audit V2 finished: status=%s, leads=%d, buyer_deals=%d, "
        "seller_deals=%d, violations=%d, skipped_incomplete=%d",
        result.get("status"),
        len(result.get("raw_leads", [])),
        len(result.get("raw_buyers_deals", [])),
        len(result.get("raw_sellers_deals", [])),
        len(result.get("violations", [])),
        skipped,
    )
    if skipped:
        logger.warning(
            "%d cards were excluded from this audit because their evidence "
            "could not be read — treat the result as incomplete",
            skipped,
        )


def cli() -> None:
    """CLI entry: run async main or exit on config / keyboard errors."""
    try:
        asyncio.run(main())
    except ValidationError as exc:
        logging.basicConfig(level=logging.ERROR)
        logger.error(
            "Configuration error. Copy .env.example to .env and set credentials: %s",
            exc,
        )
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Interrupted")
        sys.exit(130)


if __name__ == "__main__":
    cli()
