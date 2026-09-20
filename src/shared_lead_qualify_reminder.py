"""Nag brokers who still have leads on «Общие лиды».

Personal chat (important) + notification-center ping: qualify the lead or
it will be reassigned.

Each cycle re-reads CRM, so a lead that left the stage drops out.

Окно, дедлайн и выключатель живут в настройках, а не в коде:
SHARED_LEAD_REMINDER_ENABLED / _START_HOUR / _DEADLINE_HOUR. По умолчанию
рассылка выключена — включать её должен тот, кто согласовал текст.

Usage:
  DRY_RUN=true  .venv/bin/python src/shared_lead_qualify_reminder.py
  DRY_RUN=false .venv/bin/python src/shared_lead_qualify_reminder.py

Один прогон — один цикл: темп задаёт cron, как у остальных напоминаний.
Режим --loop оставлен для ручной отладки; под cron_job.sh его пускать
нельзя — flock держится всё время работы процесса, и очередной тик тихо
выйдет с кодом 0.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from config import Settings, get_settings, setup_logging  # noqa: E402
from notify import (  # noqa: E402
    send_personal_notify,
    send_user_chat_message_chunked,
)
from tools import (  # noqa: E402
    LEAD_STATUS_SHARED,
    _as_list,
    _build_crm_link,
    _bx_get_all_sync,
    _coerce_int,
    load_active_user_ids,
)

logger = logging.getLogger(__name__)

MSK = ZoneInfo("Europe/Moscow")
DEFAULT_START_HOUR = 9
DEFAULT_INTERVAL_MIN = 10


def window_state(
    now: datetime,
    *,
    start_hour: int,
    deadline_hour: int,
) -> str:
    """Why the cycle may run now: "open", "weekend", "early" or "deadline".

    Верхняя граница была и раньше, нижней и календаря не было вовсе: запуск
    в субботу или в три часа ночи слал брокерам важные сообщения с пометкой
    «срочно». Все задачи по лидам в crontab.txt ограничены буднями, и
    напоминание — не исключение.
    """
    local = now.astimezone(MSK) if now.tzinfo else now.replace(tzinfo=MSK)
    if local.weekday() >= 5:
        return "weekend"
    if local.hour < start_hour:
        return "early"
    if local.hour >= deadline_hour:
        return "deadline"
    return "open"


def normalize_lead(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Map a crm.lead.list row to a compact record, or None if unusable."""
    lead_id = _coerce_int(raw.get("ID") or raw.get("id"))
    if lead_id <= 0:
        return None
    assigned = _coerce_int(raw.get("ASSIGNED_BY_ID") or raw.get("assigned_by_id"))
    title = str(raw.get("TITLE") or raw.get("title") or "").strip()
    return {
        "id": lead_id,
        "title": title,
        "assigned_by_id": assigned,
    }


def list_shared_leads() -> list[dict[str, Any]]:
    """All current leads on STATUS_ID «Общие лиды»."""
    raw = _bx_get_all_sync(
        "crm.lead.list",
        {
            "filter": {"STATUS_ID": LEAD_STATUS_SHARED},
            "select": ["ID", "TITLE", "ASSIGNED_BY_ID", "STATUS_ID"],
        },
    )
    leads: list[dict[str, Any]] = []
    for item in _as_list(raw):
        normalized = normalize_lead(item)
        if normalized is not None:
            leads.append(normalized)
    return leads


def group_leads_by_broker(
    leads: list[dict[str, Any]],
    *,
    active_user_ids: set[int] | None = None,
) -> dict[int, list[dict[str, Any]]]:
    """Group shared leads by ASSIGNED_BY_ID. Skip empty/inactive assignees."""
    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for lead in leads:
        uid = _coerce_int(lead.get("assigned_by_id"))
        if uid <= 0:
            continue
        if active_user_ids is not None and uid not in active_user_ids:
            continue
        groups[uid].append(lead)
    for items in groups.values():
        items.sort(key=lambda row: int(row["id"]))
    return dict(groups)


def _plural_leads(count: int) -> str:
    """«1 лид», «2 лида», «5 лидов» — и «21 лид», а не «21 лидов».

    Правило по последней цифре с оговоркой на второй десяток. Прежний вариант
    смотрел только на 1-4, и у брокера с двумя десятками карточек в важном
    сообщении оказывалось «21 лидов».
    """
    tail_100 = count % 100
    tail_10 = count % 10
    if 11 <= tail_100 <= 14:
        return "лидов"
    if tail_10 == 1:
        return "лид"
    if 2 <= tail_10 <= 4:
        return "лида"
    return "лидов"


def format_leads_block(leads: list[dict[str, Any]]) -> str:
    """CRM links for the personal-chat digest."""
    lines: list[str] = []
    for lead in leads:
        lead_id = int(lead["id"])
        title = lead.get("title") or f"Лид #{lead_id}"
        url = _build_crm_link("lead", lead_id)
        lines.append(f"• Лид #{lead_id} | {title}\n  {url}")
    return "\n".join(lines)


def format_chat_message(
    leads: list[dict[str, Any]],
    *,
    deadline_hour: int,
) -> str:
    """Full important message for personal chat."""
    count = len(leads)
    noun = _plural_leads(count)
    header = (
        f"[B]❗ ВАЖНО[/B]\n"
        f"У вас {count} {noun} на этапе «Общие лиды».\n\n"
        f"Необходимо квалифицировать лид, иначе объект будет переведён "
        f"на другого брокера до {deadline_hour:02d}:00."
    )
    return header + "\n\n" + format_leads_block(leads)


def format_notify_message(
    leads: list[dict[str, Any]],
    *,
    deadline_hour: int,
) -> str:
    """Shorter notification-center ping; details stay in chat."""
    count = len(leads)
    noun = _plural_leads(count)
    return (
        f"❗ ВАЖНО: квалифицируйте {count} {noun} на этапе «Общие лиды». "
        f"Иначе объект будет переведён на другого брокера "
        f"до {deadline_hour:02d}:00. Подробности — в личных сообщениях."
    )


def send_broker_alert(
    user_id: int,
    leads: list[dict[str, Any]],
    *,
    deadline_hour: int,
    dry_run: bool,
) -> dict[str, int]:
    """Send important personal chat + notify ping. Honors dry_run."""
    chat_text = format_chat_message(leads, deadline_hour=deadline_hour)
    notify_text = format_notify_message(leads, deadline_hour=deadline_hour)
    if dry_run:
        logger.info(
            "DRY_RUN: would notify user_id=%s leads=%s chars_chat=%s",
            user_id,
            [int(x["id"]) for x in leads],
            len(chat_text),
        )
        return {"chat_chunks": 0, "notify_id": 0}
    chunks = send_user_chat_message_chunked(
        user_id,
        chat_text,
        system=False,
        important=True,
    )
    notify_id = send_personal_notify(user_id, notify_text)
    return {"chat_chunks": chunks, "notify_id": notify_id}


def _skipped(reason: str, settings: Settings, now: datetime) -> dict[str, Any]:
    """Uniform summary for a cycle that sent nothing."""
    return {
        "skipped": True,
        "reason": reason,
        "leads": 0,
        "brokers": 0,
        "sent": 0,
        "errors": 0,
        "dry_run": settings.dry_run,
        "local_time": now.astimezone(MSK).isoformat(),
    }


def run_cycle(
    settings: Settings,
    *,
    deadline_hour: int,
    start_hour: int = DEFAULT_START_HOUR,
    now: datetime | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """One fetch + send pass. No-op outside the window or while disabled.

    ``force`` снимает проверку окна, но не выключатель: это для ручной
    проверки в неурочный час, а не способ обойти несогласованную рассылку.
    """
    now = now or datetime.now(MSK)
    if not settings.shared_lead_reminder_enabled:
        logger.info("SHARED_LEAD_REMINDER_ENABLED=false — skip")
        return _skipped("disabled", settings, now)

    state = window_state(now, start_hour=start_hour, deadline_hour=deadline_hour)
    if state != "open" and not force:
        logger.info(
            "Outside the reminder window (%s, %02d:00-%02d:00 MSK) — skip local=%s",
            state,
            start_hour,
            deadline_hour,
            now.astimezone(MSK).isoformat(),
        )
        return _skipped(
            "deadline_reached" if state == "deadline" else state,
            settings,
            now,
        )

    # Портал отвечает не всегда: QUERY_LIMIT_EXCEEDED и OPERATION_TIME_LIMIT
    # для crm.lead.list — штатные ошибки, а не экзотика. Раньше любая из них
    # проходила насквозь и убивала процесс до конца дня.
    try:
        leads = list_shared_leads()
    except Exception:
        logger.exception("crm.lead.list failed — skip this cycle")
        summary = _skipped("fetch_failed", settings, now)
        summary["errors"] = 1
        return summary

    active = load_active_user_ids()
    if not active:
        # Пустое множество — это «портал не ответил», а не «все уволены».
        # Слать в такой момент некому: следующий тик спросит заново.
        logger.warning("No active users known — nothing sent this cycle")
        summary = _skipped("no_active_users", settings, now)
        summary["leads"] = len(leads)
        return summary

    groups = group_leads_by_broker(leads, active_user_ids=active)
    sent = 0
    errors = 0
    for user_id, broker_leads in sorted(groups.items()):
        try:
            send_broker_alert(
                user_id,
                broker_leads,
                deadline_hour=deadline_hour,
                dry_run=settings.dry_run,
            )
            sent += 1
        except Exception:
            errors += 1
            logger.exception(
                "Failed to notify user_id=%s leads=%s",
                user_id,
                [int(x["id"]) for x in broker_leads],
            )

    summary = {
        "skipped": False,
        "reason": "",
        "leads": len(leads),
        "brokers": len(groups),
        "sent": sent,
        "errors": errors,
        "dry_run": settings.dry_run,
        "local_time": now.astimezone(MSK).isoformat(),
    }
    logger.info(
        "Shared-lead qualify reminder: leads=%s brokers=%s active=%s sent=%s "
        "errors=%s dry_run=%s",
        summary["leads"],
        summary["brokers"],
        len(active),
        summary["sent"],
        summary["errors"],
        summary["dry_run"],
    )
    return summary


def seconds_until_next_tick(
    now: datetime,
    *,
    interval_min: int,
    deadline_hour: int,
) -> int:
    """Sleep length, clipped to the deadline so the process never overshoots."""
    local = now.astimezone(MSK) if now.tzinfo else now.replace(tzinfo=MSK)
    deadline = local.replace(
        hour=deadline_hour, minute=0, second=0, microsecond=0,
    )
    if deadline <= local:
        return 0
    interval = timedelta(minutes=max(1, interval_min))
    return max(1, int(min(interval, deadline - local).total_seconds()))


def run_loop(
    settings: Settings,
    *,
    deadline_hour: int,
    interval_min: int,
    start_hour: int = DEFAULT_START_HOUR,
    force: bool = False,
) -> dict[str, Any]:
    """Repeat run_cycle until the deadline. Debug aid, not the cron path."""
    cycles = 0
    failures = 0
    last: dict[str, Any] = {}
    while True:
        # Сбой одного цикла не должен уносить с собой весь день: следующий
        # тик спросит портал заново.
        try:
            last = run_cycle(
                settings,
                deadline_hour=deadline_hour,
                start_hour=start_hour,
                force=force,
            )
        except Exception:
            failures += 1
            logger.exception("Cycle %s failed — retrying after the interval", cycles + 1)
            last = {"skipped": True, "reason": "cycle_failed", "errors": 1}
        cycles += 1
        if last.get("reason") in {"disabled", "deadline_reached", "weekend"}:
            break
        now = datetime.now(MSK)
        sleep_sec = seconds_until_next_tick(
            now, interval_min=interval_min, deadline_hour=deadline_hour,
        )
        if sleep_sec <= 0:
            break
        logger.info(
            "Next shared-lead reminder in %s min (cycle=%s)",
            round(sleep_sec / 60, 1),
            cycles,
        )
        time.sleep(sleep_sec)
    return {"cycles": cycles, "failures": failures, "last": last}


def job_failed(summary: dict[str, Any]) -> bool:
    """True when the run is worth waking the admin for.

    Штатное молчание (выключено, выходной, не начался день, дедлайн) — это
    ноль. Ненулевым выходят только настоящие сбои: портал не отдал лиды, не
    отдал список активных, или отправка кому-то не удалась.
    """
    return bool(
        summary.get("errors")
        or summary.get("reason") in {"fetch_failed", "no_active_users"}
    )


def main() -> None:
    # Разбор аргументов идёт до чтения настроек: иначе `--help` в свежем
    # клоне без .env падает трейсбеком pydantic вместо подсказки.
    parser = argparse.ArgumentParser(
        description=(
            "Important personal reminders for leads on «Общие лиды» "
            "until the reassignment deadline."
        ),
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Debug only: repeat every --interval-min until --until-hour MSK",
    )
    parser.add_argument(
        "--interval-min",
        type=int,
        default=DEFAULT_INTERVAL_MIN,
        help="Minutes between cycles in --loop mode (default 10)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore the time window (manual check). The enable flag still applies",
    )
    parser.add_argument(
        "--since-hour",
        type=int,
        default=None,
        help="Do not remind before this hour MSK (default SHARED_LEAD_REMINDER_START_HOUR)",
    )
    parser.add_argument(
        "--until-hour",
        type=int,
        default=None,
        help="Stop at this hour MSK, exclusive (default SHARED_LEAD_REMINDER_DEADLINE_HOUR)",
    )
    args = parser.parse_args()

    settings = get_settings()
    if args.since_hour is None:
        args.since_hour = int(settings.shared_lead_reminder_start_hour)
    if args.until_hour is None:
        args.until_hour = int(settings.shared_lead_reminder_deadline_hour)
    if args.interval_min <= 0:
        parser.error("--interval-min must be > 0")
    if not 0 <= args.since_hour <= 23:
        parser.error("--since-hour must be 0..23")
    if not 1 <= args.until_hour <= 23:
        parser.error("--until-hour must be 1..23")
    # Иначе окно пустое, задача молча не делает ничего, и понять это можно
    # только по отсутствию сообщений у брокеров.
    if args.since_hour >= args.until_hour:
        parser.error("--since-hour must be earlier than --until-hour")

    setup_logging(settings.log_level)
    if args.loop:
        result = run_loop(
            settings,
            deadline_hour=args.until_hour,
            interval_min=args.interval_min,
            start_hour=args.since_hour,
            force=args.force,
        )
        cycle = result.get("last") or {}
        broken = bool(result.get("failures")) or job_failed(cycle)
    else:
        result = run_cycle(
            settings,
            deadline_hour=args.until_hour,
            start_hour=args.since_hour,
            force=args.force,
        )
        broken = job_failed(result)
    logger.info("Done: %s", result)
    if broken:
        # Тревогу ADMIN_USER_ID поднимает scripts/cron_job.sh и только по
        # ненулевому коду возврата. Молчание по расписанию — не поломка,
        # а вот прогон, где ни одно сообщение не ушло из-за портала, — да.
        sys.exit(1)


if __name__ == "__main__":
    main()
