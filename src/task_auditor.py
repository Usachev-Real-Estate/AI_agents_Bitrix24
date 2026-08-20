"""Task auditor: Back-Office + ROP task deadline monitoring."""

import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure src/ is on sys.path when running as script
_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from config import get_settings  # noqa: E402
from notify import send_chat_message_chunked  # noqa: E402
from fast_bitrix24 import Bitrix  # noqa: E402

logger = logging.getLogger(__name__)

# ── Константы ──────────────────────────────────────────
ACTIVE_TASK_STATUSES = [2, 3, 4]  # принята, выполняется, на проверке


def _get_excluded_users() -> set[str]:
    settings = get_settings()
    excluded = settings.task_auditor_exclude_users
    if not excluded:
        return {"Агентство Недвижимости", "Вера Волкова", "Светлана Щербакова", "Марина Володина"}
    return excluded


# ── API-хелперы ────────────────────────────────────────
async def fetch_users_by_dept(bx: Bitrix, dept_id: int) -> list[dict]:
    """Найти активных пользователей по UF_DEPARTMENT."""
    if not dept_id:
        return []
    users = await bx.get_all("user.get", {
        "FILTER": {"UF_DEPARTMENT": dept_id, "ACTIVE": True}
    })
    return users


async def fetch_users_by_position(bx: Bitrix, position: str) -> list[dict]:
    """Найти активных пользователей по WORK_POSITION."""
    users = await bx.get_all("user.get", {
        "FILTER": {"WORK_POSITION": position, "ACTIVE": True}
    })
    return users


async def fetch_user_tasks(bx: Bitrix, user_id: int) -> list[dict]:
    """Получить активные задачи пользователя."""
    tasks = await bx.get_all("tasks.task.list", {
        "filter": {"RESPONSIBLE_ID": user_id, "REAL_STATUS": ACTIVE_TASK_STATUSES},
        "select": ["ID", "TITLE", "DEADLINE", "STATUS", "CREATED_DATE", "RESPONSIBLE_ID"],
    })
    if isinstance(tasks, dict) and "tasks" in tasks:
        return tasks["tasks"]
    elif isinstance(tasks, dict) and "task" in tasks:
        return tasks["task"]
    elif isinstance(tasks, list):
        return tasks
    return []


# ── Анализ ─────────────────────────────────────────────
def parse_date(date_str: str) -> datetime | None:
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str)
    except Exception:
        return None


def classify_tasks(tasks: list[dict], now: datetime) -> dict:
    """Разделить задачи на просроченные / активные / остальные.
    Returns: {"overdue": [...], "active": [...], "total": N}"""
    overdue = []
    active = []

    for task in tasks:
        deadline_str = task.get("deadline") or task.get("DEADLINE")
        status = int(task.get("status") or task.get("STATUS") or 0)
        deadline = parse_date(deadline_str)

        task_info = {
            "id": task.get("id") or task.get("ID"),
            "title": task.get("title") or task.get("TITLE"),
            "deadline": deadline,
            "deadline_str": deadline_str,
            "status": status,
        }

        if deadline and deadline < now and status in ACTIVE_TASK_STATUSES:
            task_info["is_overdue"] = True
            task_info["days_overdue"] = max(1, (now - deadline).days)
            overdue.append(task_info)
        elif status in ACTIVE_TASK_STATUSES:
            active.append(task_info)

    return {
        "overdue": overdue,
        "active": active,
        "total": len(overdue) + len(active)
    }


# ── Форматирование ─────────────────────────────────────
def format_global_report(back_office_data: list[dict], rop_data: list[dict], now: datetime) -> str:
    """Сформировать текстовый отчёт."""
    lines = [
        "b24-ai-auditor — Контроль задач",
        f"Дата: {now.strftime('%d.%m.%Y')}",
        "",
        "═══════════════════════════════",
        f"🔴 БЭК-ОФИС — {len(back_office_data)} сотрудников",
        "═══════════════════════════════",
        ""
    ]

    def format_user_tasks(user_data):
        stats = user_data["stats"]
        name = user_data.get("NAME", "") + " " + user_data.get("LAST_NAME", "")
        name = name.strip() or str(user_data.get("ID"))
        dept = ""
        if user_data.get("department_name"):
            dept = f" ({user_data['department_name']})"

        overdue_cnt = len(stats["overdue"])
        lines.append(f"👤 {name}{dept} — {stats['total']} задач ({overdue_cnt} просрочено)")

        sorted_overdue = sorted(
            stats["overdue"],
            key=lambda x: x.get("days_overdue", 0),
            reverse=True,
        )
        idx = 1
        for t in sorted_overdue:
            days = t["days_overdue"]
            d_str = t["deadline"].strftime("%d.%m.%Y") if t["deadline"] else "?"
            lines.append(
                f"   {idx}. 🔴 #{t['id']} «{t['title']}» — "
                f"просрочена на {days} дн. (дедлайн: {d_str})"
            )
            idx += 1

        sorted_active = sorted(
            stats["active"],
            key=lambda x: (
                x.get("deadline").timestamp()
                if x.get("deadline")
                else float("inf")
            ),
        )
        for t in sorted_active:
            if idx > 10:  # limit to top 10 tasks to avoid huge lists
                break
            d_str = t["deadline"].strftime("%d.%m.%Y") if t["deadline"] else "без дедлайна"
            lines.append(f"   {idx}. 🟡 #{t['id']} «{t['title']}» — дедлайн {d_str}")
            idx += 1

        lines.append("")

    for ud in back_office_data:
        format_user_tasks(ud)

    lines.extend([
        "═══════════════════════════════",
        f"🔴 РОПы — {len(rop_data)} руководителей",
        "═══════════════════════════════",
        ""
    ])

    for ud in rop_data:
        format_user_tasks(ud)

    bo_tasks = sum(u["stats"]["total"] for u in back_office_data)
    bo_overdue = sum(len(u["stats"]["overdue"]) for u in back_office_data)
    bo_pct = int(bo_overdue / bo_tasks * 100) if bo_tasks else 0

    rop_tasks = sum(u["stats"]["total"] for u in rop_data)
    rop_overdue = sum(len(u["stats"]["overdue"]) for u in rop_data)
    rop_pct = int(rop_overdue / rop_tasks * 100) if rop_tasks else 0

    total_tasks = bo_tasks + rop_tasks
    total_overdue = bo_overdue + rop_overdue
    total_pct = int(total_overdue / total_tasks * 100) if total_tasks else 0

    all_users = back_office_data + rop_data
    top_overdue = sorted(
        all_users,
        key=lambda u: len(u["stats"]["overdue"]),
        reverse=True,
    )[:3]

    if top_overdue and any(len(u["stats"]["overdue"]) > 0 for u in top_overdue):
        lines.extend([
            "═══════════════════════════════",
            "🔴 ТОП-3 ПО ПРОСРОЧКАМ",
            "═══════════════════════════════",
        ])
        for i, u in enumerate(top_overdue, 1):
            name = (u.get("NAME", "") + " " + u.get("LAST_NAME", "")).strip()
            overdue_cnt = len(u["stats"]["overdue"])
            if overdue_cnt > 0:
                lines.append(f"{i}. {name} — {overdue_cnt} просроченных задач")
        lines.append("")

    lines.extend([
        "═══════════════════════════════",
        "📊 СВОДКА",
        "═══════════════════════════════",
        f"Бэк-Офис: {bo_tasks} задач, {bo_overdue} просрочено ({bo_pct}%)",
        f"РОПы: {rop_tasks} задач, {rop_overdue} просрочено ({rop_pct}%)",
        f"Всего: {total_tasks} задач, {total_overdue} просрочено ({total_pct}%)"
    ])

    return "\n".join(lines)


# ── Главная ────────────────────────────────────────────
async def async_main():
    settings = get_settings()
    now = datetime.now(timezone.utc)

    # Enable logging output to console if needed
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))

    bx = Bitrix(settings.b24_webhook_url)

    # 1. Fetch departments to resolve dept_id to name
    depts = await bx.get_all("department.get")
    dept_map = {int(d.get("ID") or d.get("id")): str(d.get("NAME") or d.get("name")) for d in depts}

    # 2. Fetch users
    logger.info("Fetching back-office users (dept_id=%s)", settings.back_office_dept_id)
    back_office_users = await fetch_users_by_dept(bx, settings.back_office_dept_id)

    logger.info("Fetching ROP users")
    rop_users = await fetch_users_by_position(bx, "Руководитель отдела продаж (РОП)")

    # Remove duplicates and excluded users
    excluded_users = _get_excluded_users()

    seen_users = set()
    bo_unique = []
    for u in back_office_users:
        uid = int(u.get("ID"))
        name = (u.get("NAME", "") + " " + u.get("LAST_NAME", "")).strip()
        if uid not in seen_users and name not in excluded_users:
            seen_users.add(uid)
            dept_ids = u.get("UF_DEPARTMENT", [])
            primary_dept = int(dept_ids[0]) if dept_ids else 0
            u["department_name"] = dept_map.get(primary_dept, "")
            bo_unique.append(u)

    rop_unique = []
    for u in rop_users:
        uid = int(u.get("ID"))
        name = (u.get("NAME", "") + " " + u.get("LAST_NAME", "")).strip()
        if uid not in seen_users and name not in excluded_users:
            seen_users.add(uid)
            dept_ids = u.get("UF_DEPARTMENT", [])
            primary_dept = int(dept_ids[0]) if dept_ids else 0
            u["department_name"] = dept_map.get(primary_dept, "")
            rop_unique.append(u)

    logger.info(
        "Found %d unique back-office users, %d unique ROP users",
        len(bo_unique),
        len(rop_unique),
    )

    # 3. Fetch tasks
    for u in bo_unique + rop_unique:
        u["tasks"] = await fetch_user_tasks(bx, u["ID"])
        u["stats"] = classify_tasks(u["tasks"], now)
        logger.info(
            "User %s: %d total tasks, %d overdue",
            u.get("ID"),
            u["stats"]["total"],
            len(u["stats"]["overdue"]),
        )

    # 4. Generate report
    report = format_global_report(bo_unique, rop_unique, now)

    if not settings.dry_run:
        logger.info("Sending report to chat %s", settings.report_chat_id)
        send_chat_message_chunked(settings.report_chat_id, report)

        dept_map = settings.dept_chat_map
        if dept_map:
            for u in rop_unique:
                dept_ids = u.get("UF_DEPARTMENT", [])
                if dept_ids:
                    primary_dept = int(dept_ids[0])
                    chat_id = dept_map.get(primary_dept)
                    if chat_id and chat_id != settings.report_chat_id:
                        rop_name = (u.get("NAME", "") + " " + u.get("LAST_NAME", "")).strip()
                        rop_report = (
                            f"b24-ai-auditor — Ваши задачи\n"
                            f"Дата: {now.strftime('%d.%m.%Y')}\n\n"
                            f"👤 {rop_name}\n"
                            f"Активных задач: {u['stats']['total']}\n"
                            f"Просрочено: {len(u['stats']['overdue'])}\n"
                        )
                        send_chat_message_chunked(chat_id, rop_report)
    else:
        logger.info("DRY_RUN=True, skipping sending message. Report:")
        print(report)


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
