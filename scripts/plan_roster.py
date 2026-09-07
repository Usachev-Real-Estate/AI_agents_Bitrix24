"""Ручные исключения планового состава: посмотреть, добавить, убрать.

Портал говорит про людей не всю правду, и часть неправды не исправить внутри
Битрикса, не сломав что-нибудь ещё. Руководитель отдела «Волкова»
административно числится в служебном подразделении «Битрикс»: её отдел
остаётся без РОПа и получает лишнюю норму, а служебный — норму на человека,
который не продаёт. Стажёру норму ещё не ставят, но в отделе он есть.

Такие случаи и живут в ростере. Он не заменяет портал, а перекрывает его в
одном месте — и делает это записью, которую видно, а не запросом в базу,
который сделали один раз и забыли.

Строка на все периоды (по умолчанию) переживает смену квартала: «РОП сидит не
в своём отделе» — это не про третий квартал, а про то, как заведён портал.
Строка на конкретный период перекрывает её, когда нужно.

    python scripts/plan_roster.py list
    python scripts/plan_roster.py set "Вера Волкова" --role rop --department 50 \\
        --note "числится в Битриксе"
    python scripts/plan_roster.py remove "Вера Волкова"
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
for extra in (_SRC_DIR, _SRC_DIR / "analytics"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from config import get_settings  # noqa: E402
from plan_norms import name_key  # noqa: E402
from plans import ANY_PERIOD, ROLE_BROKER, ROLE_EXCLUDED, ROLE_ROP  # noqa: E402
from schema import analytics_session, get_connection, init_analytics_db  # noqa: E402

logger = logging.getLogger(__name__)

LINE = "─" * 72
ROLES = (ROLE_BROKER, ROLE_ROP, ROLE_EXCLUDED)
ROLE_LABEL = {
    ROLE_BROKER: "брокер (несёт норму)",
    ROLE_ROP: "РОП (норму не несёт)",
    ROLE_EXCLUDED: "исключён из плана",
}


def find_user(conn, label: str) -> list[dict]:
    """Найти человека по имени. Сравнение — в Python: SQLite не умеет кириллицу."""
    key = name_key(label)
    surname_hits, exact = [], []
    for row in conn.execute(
        "SELECT user_id, name, last_name, department_id, department_name, is_active "
        "FROM dim_user"
    ).fetchall():
        user = dict(row)
        if name_key(user["name"]) == key:
            exact.append(user)
        elif (user["last_name"] or "").strip().lower().replace("ё", "е") in key:
            surname_hits.append(user)
    return exact or surname_hits


def show(period_code: str) -> int:
    conn = get_connection(get_settings().analytics_db_path, readonly=True)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT r.period_code, r.user_id, r.department_id, r.plan_role, r.note,"
            "       COALESCE(u.name, 'ID ' || r.user_id) AS name,"
            "       COALESCE(u.department_name, '') AS home"
            "  FROM plan_roster r LEFT JOIN dim_user u ON u.user_id = r.user_id"
            " WHERE r.period_code IN (?, ?) ORDER BY r.period_code, u.name",
            (period_code, ANY_PERIOD),
        ).fetchall()]
    finally:
        conn.close()

    if not rows:
        print("Ростер пуст — состав берётся из портала как есть.")
        return 0
    print(f"\n{LINE}\nРучные исключения ({len(rows)})\n{LINE}")
    for row in rows:
        moved = (
            f"  отдел → {row['department_id']} (в портале «{row['home']}»)"
            if row["department_id"] else ""
        )
        period = "все периоды" if row["period_code"] == ANY_PERIOD else row["period_code"]
        print(f"  id={row['user_id']:<5} {row['name'][:26]:<28} "
              f"{ROLE_LABEL.get(row['plan_role'], row['plan_role'])}{moved}")
        print(f"        {period}"
              + (f" · {row['note']}" if row["note"] else ""))
    return 0


def put(label: str, role: str, department: int | None, note: str,
        period_code: str) -> int:
    """Завести или обновить строку. Неоднозначное имя — отказ, а не догадка."""
    init_analytics_db()
    conn = get_connection(get_settings().analytics_db_path, readonly=True)
    try:
        found = find_user(conn, label)
    finally:
        conn.close()

    if not found:
        print(f"Не найдено: {label!r}. Проверьте написание — сверка идёт по имени.")
        return 1
    if len(found) > 1:
        print("Подходят несколько — уточните имя целиком:")
        for user in found:
            print(f"  id={user['user_id']:<5} {user['name'][:28]:<30} "
                  f"«{user['department_name']}» активен={user['is_active']}")
        print("\nНе тот человек означает норму не тому отделу, поэтому не угадываю.")
        return 2

    user = found[0]
    with analytics_session() as conn:
        conn.execute(
            "INSERT INTO plan_roster(period_code, user_id, department_id, plan_role,"
            " note, updated_at) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(period_code, user_id) DO UPDATE SET"
            "   department_id=excluded.department_id, plan_role=excluded.plan_role,"
            "   note=excluded.note, updated_at=excluded.updated_at",
            (period_code, user["user_id"], department, role, note,
             datetime.now(timezone.utc).isoformat()),
        )
    where = f", отдел → {department}" if department else ""
    print(f"Записано: {user['name']} (id={user['user_id']}) — "
          f"{ROLE_LABEL[role]}{where}")
    print("Строка вступает в силу при следующем открытии страницы, "
          "пересборка не нужна.")
    return 0


def drop(label: str, period_code: str) -> int:
    conn = get_connection(get_settings().analytics_db_path, readonly=True)
    try:
        found = find_user(conn, label)
    finally:
        conn.close()
    if len(found) != 1:
        print(f"Нужен ровно один человек, найдено {len(found)}. Уточните имя.")
        return 1
    with analytics_session() as conn:
        removed = conn.execute(
            "DELETE FROM plan_roster WHERE user_id = ? AND period_code = ?",
            (found[0]["user_id"], period_code),
        ).rowcount
    print(f"Удалено строк: {removed}. Состав вернулся к тому, что говорит портал.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ручные исключения планового состава")
    parser.add_argument("--period", default=ANY_PERIOD,
                        help="код периода; по умолчанию строка на все периоды")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="показать текущие исключения")

    setter = sub.add_parser("set", help="завести или обновить исключение")
    setter.add_argument("name", help="имя человека как в портале")
    setter.add_argument("--role", choices=ROLES, required=True)
    setter.add_argument("--department", type=int, default=None,
                        help="перекрыть отдел: за какой отдел человек отвечает")
    setter.add_argument("--note", default="", help="зачем строка заведена")

    remover = sub.add_parser("remove", help="убрать исключение")
    remover.add_argument("name")

    args = parser.parse_args(argv)
    if args.command == "list":
        return show(args.period)
    if args.command == "set":
        return put(args.name, args.role, args.department, args.note, args.period)
    return drop(args.name, args.period)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    sys.exit(main())
