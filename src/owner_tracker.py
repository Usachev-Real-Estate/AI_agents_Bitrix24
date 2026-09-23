"""Owner tracker: statistics of new Owner-type contacts per broker."""

import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure src/ is on sys.path when running as script
_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from config import get_settings, quiet_http_clients  # noqa: E402
from fast_bitrix24 import Bitrix  # noqa: E402

logger = logging.getLogger(__name__)


# ── API-хелперы ────────────────────────────────────────
async def fetch_owner_contacts(bx: Bitrix, type_id: str, since_date: str) -> list[dict]:
    """Получить контакты типа «Собственник» за период."""
    contacts = await bx.get_all("crm.contact.list", {
        "filter": {
            "TYPE_ID": type_id,
            ">=DATE_CREATE": since_date,
        },
        "select": [
            "ID", "NAME", "LAST_NAME", "CREATED_BY_ID",
            "ASSIGNED_BY_ID", "DATE_CREATE", "TYPE_ID",
        ],
    })
    return contacts


async def fetch_user_names(bx: Bitrix, user_ids: set[int]) -> dict[int, dict]:
    """Получить имена и отделы для списка пользователей."""
    if not user_ids:
        return {}

    # We also need department names, so let's get departments once
    depts = await bx.get_all("department.get")
    dept_map = {int(d.get("ID") or d.get("id")): str(d.get("NAME") or d.get("name")) for d in depts}

    def build_info(u: dict) -> dict:
        name = ((u.get("NAME") or "") + " " + (u.get("LAST_NAME") or "")).strip()
        dept_ids = u.get("UF_DEPARTMENT", [])
        primary_dept = int(dept_ids[0]) if dept_ids else 0
        dept_name = dept_map.get(primary_dept, "Без отдела")
        active = u.get("ACTIVE")
        is_active = True
        if active is False or str(active).upper() == "N" or str(active).lower() == "false":
            is_active = False
        return {
            "name": name,
            "department": dept_name,
            "department_id": primary_dept,
            "active": is_active,
        }

    user_info = {}

    # Попытка 1: массовый запрос
    try:
        users = await bx.get_all("user.get", {
            "FILTER": {"ID": list(user_ids)}
        })
        for u in users:
            uid = int(u.get("ID"))
            user_info[uid] = build_info(u)
    except Exception:
        pass

    # Попытка 2: индивидуальные запросы для пропущенных ID
    missing_ids = user_ids - set(user_info.keys())
    for uid in missing_ids:
        try:
            u = await bx.call("user.get", {"ID": uid})
            if isinstance(u, list) and u:
                u_dict = u[0]
                if isinstance(u_dict, dict) and u_dict.get("ID"):
                    user_info[int(u_dict["ID"])] = build_info(u_dict)
            elif isinstance(u, dict):
                if u.get("ID"):
                    user_info[int(u["ID"])] = build_info(u)
                elif u.get("result") and isinstance(u["result"], list) and u["result"]:
                    u_dict = u["result"][0]
                    if isinstance(u_dict, dict) and u_dict.get("ID"):
                        user_info[int(u_dict["ID"])] = build_info(u_dict)
        except Exception as e:
            logger.warning("Failed to fetch user info for ID %s: %s", uid, e)
            pass

    return user_info


# ── Анализ ─────────────────────────────────────────────
def group_by_broker(contacts: list[dict]) -> dict[int, list[dict]]:
    """Сгруппировать контакты по CREATED_BY_ID (кто создал контакт)."""
    groups: dict[int, list[dict]] = {}
    for contact in contacts:
        created_by_raw = contact.get("CREATED_BY_ID")
        if not created_by_raw:
            logger.warning(
                "Contact %s has no CREATED_BY_ID, skipping",
                contact.get("ID"),
            )
            continue
        created_by = int(created_by_raw)
        if created_by not in groups:
            groups[created_by] = []
        groups[created_by].append(contact)
    return groups


def compute_broker_stats(
    contacts: list[dict],
    broker_ids: set[int],
) -> dict[int, dict[str, int]]:
    """Подсчитать созданные брокером и переданные (ответственный ≠ создатель)."""
    stats = {broker_id: {"created": 0, "assigned": 0} for broker_id in broker_ids}

    for contact in contacts:
        created_by_raw = contact.get("CREATED_BY_ID")
        if not created_by_raw:
            continue

        created_by = int(created_by_raw)
        assigned_by_raw = contact.get("ASSIGNED_BY_ID")
        assigned_by = int(assigned_by_raw) if assigned_by_raw else 0

        if created_by in broker_ids:
            stats[created_by]["created"] += 1

        if assigned_by in broker_ids and assigned_by != created_by:
            stats[assigned_by]["assigned"] += 1

    return stats


def _owner_suffix(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "собственник"
    if 2 <= count % 10 <= 4 and (count % 100 < 10 or count % 100 >= 20):
        return "собственника"
    return "собственников"


def _format_broker_count(created: int, assigned: int) -> str:
    line = f"{created} {_owner_suffix(created)}"
    if assigned > 0:
        line += f" ({assigned})"
    return line


# ── Форматирование ─────────────────────────────────────
def format_owners_report(
    broker_stats: dict[int, dict[str, int]],
    user_info: dict,
    since_date: str,
    now: datetime,
    broker_groups: dict[int, list[dict]]
) -> str:
    """Сформировать отчёт по добавленным собственникам с группировкой по отделам."""
    start_date_str = datetime.fromisoformat(since_date).strftime("%d.%m.%Y")
    end_date_str = now.strftime("%d.%m.%Y")

    lines = [
        "b24-ai-auditor — Новые собственники",
        f"Период: с {start_date_str} по {end_date_str}",
        "Учёт по полю «Кем создан»; в скобках — ответственный, если создал другой",
        "",
    ]

    dept_groups: dict[str, list[tuple[int, int, int, str]]] = {}
    total_created = 0
    total_assigned = 0

    for broker_id, stats in broker_stats.items():
        info = user_info.get(broker_id, {"name": f"ID: {broker_id}", "department": "Без отдела"})
        dept = info["department"]

        if dept == "Битрикс":
            continue

        created = stats["created"]
        assigned = stats["assigned"]
        total_created += created
        total_assigned += assigned
        dept_groups.setdefault(dept, []).append((broker_id, created, assigned, info["name"]))

    for dept in sorted(dept_groups.keys()):
        lines.extend([
            "═══════════════════════════════",
            f"🏢 Отдел: {dept}",
            "═══════════════════════════════",
            "",
        ])

        brokers = sorted(dept_groups[dept], key=lambda x: (x[1], x[2]), reverse=True)

        for idx, (_broker_id, created, assigned, broker_name) in enumerate(brokers, start=1):
            count_line = _format_broker_count(created, assigned)
            lines.append(f"{idx}. 👤 {broker_name} — {count_line}")
            if created == 0 and assigned == 0:
                lines.append("   ⚠️ Ни одного собственника не добавлено")
            lines.append("")

    total_line = f"📊 Всего создано брокерами: {total_created}"
    if total_assigned > 0:
        total_line += f" ({total_assigned} ответственный)"
    lines.extend([
        "═══════════════════════════════",
        total_line,
        "═══════════════════════════════",
    ])

    settings = get_settings()
    kpi_target = settings.owner_kpi_target
    monthly_contacts = [
        c for contacts_list in broker_groups.values()
        for c in contacts_list
        if (c.get("DATE_CREATE") or "")[:7] == now.strftime("%Y-%m")
    ]
    monthly_count = len(monthly_contacts)
    monthly_pct = int(monthly_count / kpi_target * 100) if kpi_target else 0

    lines.extend([
        f"🎯 KPI на {now.strftime('%B')}: {kpi_target} собственников",
        f"   Выполнено: {monthly_count} ({monthly_pct}%)",
    ])

    return "\n".join(lines)


# ── Главная ────────────────────────────────────────────
async def async_main():
    settings = get_settings()
    now = datetime.now(timezone.utc)

    # Enable logging output to console if needed
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
    # Задача настраивает лог сама, поэтому и глушить клиента портала ей
    # приходится самой: в адресе вебхука лежит токен.
    quiet_http_clients()

    bx = Bitrix(settings.b24_webhook_url)

    # 1. Получить контакты
    logger.info(
        "Fetching owner contacts (type_id=%s) since %s",
        settings.contact_owner_type_id,
        settings.owner_kpi_since,
    )
    contacts = await fetch_owner_contacts(
        bx, settings.contact_owner_type_id, settings.owner_kpi_since
    )
    logger.info("Found %d owner contacts", len(contacts))

    # 2. Сгруппировать по брокерам
    broker_groups = group_by_broker(contacts)

    # 2.1 Получить ВСЕХ активных брокеров
    all_broker_ids = set()

    # Получаем всех сотрудников из отделов продаж
    sales_depts = settings.owner_sales_dept_ids
    try:
        active_users = await bx.get_all("user.get", {"FILTER": {"ACTIVE": "true"}})
        for u in active_users:
            u_depts = u.get("UF_DEPARTMENT", [])
            if any(int(d) in sales_depts for d in u_depts):
                all_broker_ids.add(int(u["ID"]))
    except Exception as e:
        logger.warning("Failed to fetch active users from Bitrix: %s", e)

    # Также добавим брокеров из БД на случай, если кто-то не попал по отделам
    from db import db_session
    try:
        with db_session() as conn:
            rows = conn.execute(
                "SELECT responsible_id FROM brokers WHERE lead_count > 0 OR deal_count > 0"
            ).fetchall()
            all_broker_ids.update({row[0] for row in rows})
    except Exception as e:
        logger.warning("Failed to fetch brokers from DB: %s", e)
        pass

    # Если БД недоступна — используем только тех, у кого есть контакты
    if not all_broker_ids:
        all_broker_ids = set(broker_groups.keys())

    # Исключить технических пользователей / не-брокеров (из .env)
    for uid in settings.owner_exclude_user_ids:
        broker_groups.pop(uid, None)
        all_broker_ids.discard(uid)

    # Добавить брокеров с 0 собственников
    for bid in all_broker_ids:
        if bid not in broker_groups:
            broker_groups[bid] = []

    # 3. Получить имена брокеров
    all_needed_ids = set(broker_groups.keys()) | all_broker_ids
    user_info = await fetch_user_names(bx, all_needed_ids)

    # Исключить отдел «Битрикс»
    for uid, info in list(user_info.items()):
        if info.get("department") == "Битрикс":
            broker_groups.pop(uid, None)
            all_broker_ids.discard(uid)
            user_info.pop(uid, None)

    # Исключить уволенных сотрудников
    for uid, info in list(user_info.items()):
        if not info.get("active", True):
            broker_groups.pop(uid, None)
            all_broker_ids.discard(uid)
            user_info.pop(uid, None)

    # 4. Подсчитать статистику и сформировать отчёт
    eligible_broker_ids = set(broker_groups.keys())
    broker_stats = compute_broker_stats(contacts, eligible_broker_ids)
    main_report = format_owners_report(
        broker_stats, user_info,
        settings.owner_kpi_since, now,
        broker_groups
    )

    # 5. Отправляем только в общий чат
    if str(settings.dry_run).lower() != "true" and settings.dry_run is not True:
        logger.info("Sending MAIN report to chat %s", settings.report_chat_id)
        from notify import send_chat_message_chunked
        send_chat_message_chunked(settings.report_chat_id, main_report)
    else:
        logger.info("DRY_RUN=True, skipping sending MAIN message. Report:")
        print(main_report)


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
