"""Run audit and send violations report to Bitrix24 user via im.notify.personal.add."""

import asyncio
import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from config import get_settings, setup_logging  # noqa: E402
from graph import run_audit_v2  # noqa: E402
from notify import send_personal_notification  # noqa: E402

REPORT_USER_ID = 154
MAX_VIOLATIONS_IN_NOTIFY = 15


def format_violations(violations: list[dict]) -> str:
    """Format violations list into a readable report.

    Args:
        violations: List of violation dicts from Analyst.

    Returns:
        Human-readable report text.
    """
    if not violations:
        return "Нарушений регламента не найдено."

    lines = [f"Найдено нарушений: {len(violations)}", ""]

    high = [v for v in violations if v.get("severity") == "high"]
    medium = [v for v in violations if v.get("severity") == "medium"]
    low = [v for v in violations if v.get("severity") == "low"]

    lines.append(f"Критические (high): {len(high)}")
    lines.append(f"Средние (medium): {len(medium)}")
    lines.append(f"Низкие (low): {len(low)}")
    lines.append("")

    shown = violations[:MAX_VIOLATIONS_IN_NOTIFY]
    for v in shown:
        sev = v.get("severity", "?")
        icon = {"high": "[HIGH]", "medium": "[MED]", "low": "[LOW]"}.get(sev, "[?]")
        rule = v.get("rule", "?")
        desc = v.get("reason") or v.get("description", "—")
        lines.append(f"{icon} {rule}: {desc}")

    if len(violations) > MAX_VIOLATIONS_IN_NOTIFY:
        lines.append(f"... и ещё {len(violations) - MAX_VIOLATIONS_IN_NOTIFY} (см. logs/audit.log)")

    return "\n".join(lines)


async def run_audit_and_build_report() -> tuple[str, int, str]:
    """Run audit pipeline and build notification text.

    Returns:
        Tuple of (status, violation_count, full_message).
    """
    settings = get_settings()
    result = await run_audit_v2(settings)

    violations = result.get("violations", [])
    status = str(result.get("status", "?"))
    report = format_violations(violations)
    header = (
        "b24-ai-auditor v2 — тестовый отчёт\n"
        f"Лиды: {len(result.get('raw_leads', []))}, "
        f"Покупатели: {len(result.get('raw_buyers_deals', []))}\n\n"
    )
    return status, len(violations), header + report


def main() -> None:
    """Run audit and send report to user #154."""
    settings = get_settings()
    setup_logging(settings.log_level)

    print("Running audit (DRY_RUN mode — no mutations)...")
    status, count, full_message = asyncio.run(run_audit_and_build_report())

    print(f"Status: {status}")
    print(f"Violations: {count}")

    print("\n=== REPORT (preview) ===")
    print(full_message[:1500])
    if len(full_message) > 1500:
        print("...")
    print("==============")

    notify_id = send_personal_notification(REPORT_USER_ID, full_message)
    print(f"Sent to user #{REPORT_USER_ID}, notification id={notify_id}")


if __name__ == "__main__":
    main()
