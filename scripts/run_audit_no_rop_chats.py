#!/usr/bin/env python3
"""Run main audit without sending reports to ROP (or any) chats."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

os.environ["DRY_RUN"] = "true"
os.environ["DEPT_CHAT_MAP_JSON"] = "{}"

_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from config import get_settings, setup_logging  # noqa: E402
import graph as graph_mod  # noqa: E402
import notify  # noqa: E402
from graph import run_audit_v2  # noqa: E402

logger = logging.getLogger(__name__)

OUT_JSON = Path("data/audit_manual_no_rop.json")
OUT_TXT = Path("data/audit_manual_no_rop.txt")

SELLER_RULES = {
    "seller_stage_stale",
    "seller_deferred_no_activity",
    "seller_negotiations_max",
    "seller_lost_no_reason",
    "seller_afina_id_missing",
}
BUYER_RULES = {
    "buyer_stage_1",
    "buyer_stage_2",
    "buyer_stage_3",
    "buyer_stage_4",
    "buyer_stage_5",
    "buyer_podbor_stale",
    "buyer_ofer_comment",
    "buyer_lost_no_reason",
    "buyer_agent_no_comment",
    "buyer_missed_callback",
}
LEAD_RULES = {
    "lead_rule_1",
    "lead_rule_2",
    "lead_rule_3",
    "lead_new_over_24h",
    "lead_missed_callback",
}


def _no_send(*_args: Any, **_kwargs: Any) -> int:
    logger.info("Chat send skipped (manual audit, no ROP/chats)")
    return 0


def _funnel_for_rule(rule: str) -> str:
    if rule in SELLER_RULES:
        return "sellers"
    if rule in BUYER_RULES:
        return "buyers"
    if rule in LEAD_RULES:
        return "leads"
    if rule == "general_base_no_plan":
        return "general_base"
    if rule.startswith("seller_"):
        return "sellers"
    if rule.startswith("buyer_"):
        return "buyers"
    if rule.startswith("lead_"):
        return "leads"
    return "other"


def _compact_violation(v: dict[str, Any]) -> dict[str, Any]:
    details = v.get("details") if isinstance(v.get("details"), dict) else {}
    return {
        "entity_type": v.get("entity_type"),
        "entity_id": v.get("entity_id"),
        "rule": v.get("rule"),
        "severity": v.get("severity"),
        "reason": v.get("reason"),
        "responsible_id": v.get("responsible_id"),
        "stage_id": details.get("stage_id"),
        "stage_name": details.get("stage_name"),
    }


def _format_report(result: dict[str, Any]) -> str:
    violations = result.get("violations") or []
    seen: set[tuple[Any, ...]] = set()
    unique: list[dict[str, Any]] = []
    for v in violations:
        key = (v.get("entity_type"), v.get("entity_id"), v.get("rule"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(v)

    funnel_counts: Counter[str] = Counter()
    rule_counts: Counter[str] = Counter()
    for v in unique:
        rule = str(v.get("rule") or "")
        funnel_counts[_funnel_for_rule(rule)] += 1
        rule_counts[rule] += 1

    lines = [
        "b24-ai-auditor — ручной прогон (без чатов РОП)",
        f"Время: {result.get('current_time', '')}",
        f"DRY_RUN: {result.get('dry_run')}",
        f"status: {result.get('status')}",
        "",
        f"Лидов: {len(result.get('raw_leads') or [])}",
        f"Сделок покупателей: {len(result.get('raw_buyers_deals') or [])}",
        f"Сделок продавцов: {len(result.get('raw_sellers_deals') or [])}",
        f"Сделок «Общая база»: {len(result.get('raw_general_base_deals') or [])}",
        "",
        f"Всего нарушений (уник.): {len(unique)}",
        "",
        "По воронкам:",
    ]
    labels = {
        "leads": "Лиды",
        "buyers": "Покупатели",
        "sellers": "Продавцы",
        "general_base": "Общая база",
        "other": "Прочее",
    }
    for key in ("leads", "buyers", "sellers", "general_base", "other"):
        if funnel_counts[key]:
            lines.append(f"  {labels[key]}: {funnel_counts[key]}")

    lines.append("")
    lines.append("По правилам:")
    for rule, cnt in rule_counts.most_common():
        lines.append(f"  {rule}: {cnt}")
    return "\n".join(lines)


async def main() -> None:
    get_settings.cache_clear()
    settings = get_settings()
    setup_logging(settings.log_level)

    notify.send_chat_message = _no_send  # type: ignore[assignment]
    notify.send_chat_message_chunked = _no_send  # type: ignore[assignment]
    notify.send_user_chat_message = _no_send  # type: ignore[assignment]
    graph_mod.send_chat_message_chunked = _no_send  # type: ignore[assignment]
    graph_mod.send_user_chat_message = _no_send  # type: ignore[assignment]

    logger.info(
        "Manual audit: DRY_RUN=%s dept_chat_map=%s (ROP chats disabled)",
        settings.dry_run,
        settings.dept_chat_map,
    )
    result = await run_audit_v2(settings)

    unique = []
    seen: set[tuple[Any, ...]] = set()
    for v in result.get("violations") or []:
        key = (v.get("entity_type"), v.get("entity_id"), v.get("rule"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(_compact_violation(v))

    report = _format_report(result)
    payload = {
        "run_time": datetime.now(timezone.utc).isoformat(),
        "status": result.get("status"),
        "dry_run": result.get("dry_run"),
        "totals": {
            "leads": len(result.get("raw_leads") or []),
            "buyer_deals": len(result.get("raw_buyers_deals") or []),
            "seller_deals": len(result.get("raw_sellers_deals") or []),
            "general_base_deals": len(result.get("raw_general_base_deals") or []),
            "violations": len(unique),
        },
        "report": report,
        "violations": unique,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_TXT.write_text(report, encoding="utf-8")
    print(report)
    print(f"\nJSON: {OUT_JSON}")
    print(f"TXT: {OUT_TXT}")


if __name__ == "__main__":
    asyncio.run(main())
