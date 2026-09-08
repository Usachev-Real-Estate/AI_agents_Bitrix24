"""Замер работы по карточкам собственников: кому звонили, а кому нет.

Только чтение. Ничего не считает «отчётом» — отвечает на вопросы, от которых
зависит, можно ли отчёт вообще построить:

1. Держится ли связка сделка → контакт. Звонок висит на контакте (их больше,
   чем на сделках), и если у карточек собственников контакт не проставлен,
   любой отчёт про молчание окажется отчётом про пустое поле.
2. Что считать касанием. У портала есть CALL, MEETING, TODO и TASKS_TASK,
   и это разные вещи: задача, заведённая себе, — не разговор с человеком.
3. Как выглядит распределение. Пока неизвестно, сколько карточек без единого
   касания, порог «давно не звонили» брать неоткуда.

Запуск (на сервере, из каталога проекта). Файл достаётся из ветки через
git show, а не выкладывается в рабочее дерево: пересобирать образ ради замера
незачем, и трогать выкаченный main тем более.

    git fetch origin claude/leadership-dashboard-ih3sjz
    git show origin/claude/leadership-dashboard-ih3sjz:scripts/work_probe.py \\
        > /tmp/work_probe.py
    docker run --rm -v $(pwd)/data:/app/data \\
      -v /tmp/work_probe.py:/app/work_probe.py --env-file .env \\
      b24-ai-auditor:latest python /app/work_probe.py
"""

import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# Скрипт запускают двумя способами: из репозитория (лежит в scripts/) и
# смонтированным в корень образа. Путь, посчитанный одним способом, во
# втором случае даёт ModuleNotFoundError уже после того, как команда
# принята, — поэтому проверяются оба корня, и берётся тот, где витрина есть.
for _root in (Path(__file__).resolve().parent.parent, Path.cwd(), Path("/app")):
    _src = _root / "src"
    if (_src / "analytics" / "schema.py").exists():
        for _path in (_src, _src / "analytics"):
            if str(_path) not in sys.path:
                sys.path.insert(0, str(_path))
        break
else:  # pragma: no cover — на сервере каталог есть всегда
    raise SystemExit("не найден каталог src: запускайте из корня проекта")

from schema import get_connection  # noqa: E402

SELLERS = 0
# OWNER_TYPE_ID Bitrix. Проверяется замером, а не принимается на веру.
OWNER_NAMES = {1: "лид", 2: "сделка", 3: "контакт", 4: "компания"}
# Разговор с человеком. Задача себе и дело в CRM — не разговор.
TALK = ("CALL", "MEETING")


def head(text: str) -> None:
    print(f"\n{'=' * 62}\n{text}\n{'=' * 62}")


def days_since(stamp: str | None, now: datetime) -> float | None:
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (now - moment).total_seconds() / 86400.0


def buckets(values, edges, labels):
    counter = Counter()
    for value in values:
        for edge, label in zip(edges, labels):
            if value <= edge:
                counter[label] += 1
                break
        else:
            counter[labels[-1]] += 1
    return counter


_STAGES: dict[str, str] = {}


def stage_of(conn, stage_id: str) -> str:
    if not _STAGES:
        _STAGES.update({
            row["stage_id"]: row["name"] for row in conn.execute(
                "SELECT stage_id, name FROM dim_stage WHERE category_id = 0")
        })
    return _STAGES.get(stage_id, stage_id)


def main() -> None:
    conn = get_connection(readonly=True)
    now = datetime.now(timezone.utc)

    head("1. Действия: на чём висят и что это такое")
    for row in conn.execute(
        "SELECT owner_type_id, COUNT(*) n FROM fact_activity "
        "GROUP BY owner_type_id ORDER BY n DESC"
    ):
        name = OWNER_NAMES.get(row["owner_type_id"], "?")
        print(f"  {name:<10} (тип {row['owner_type_id']:>2}): {row['n']:>7}")

    print()
    for row in conn.execute(
        "SELECT provider_type_id, direction, completed, COUNT(*) n "
        "FROM fact_activity GROUP BY provider_type_id, direction, completed "
        "ORDER BY n DESC LIMIT 12"
    ):
        way = {1: "входящее", 2: "исходящее"}.get(row["direction"], "—")
        done = "завершено" if row["completed"] else "не завершено"
        print(f"  {row['provider_type_id'] or '(пусто)':<14} {way:<10} "
              f"{done:<14} {row['n']:>7}")

    head("2. Карточки собственников: держится ли связка с контактом")
    row = conn.execute(
        """
        SELECT COUNT(*) n,
               SUM(CASE WHEN contact_id IS NOT NULL AND contact_id > 0
                        THEN 1 ELSE 0 END) with_contact,
               SUM(CASE WHEN is_closed = 0 THEN 1 ELSE 0 END) open_deals,
               SUM(CASE WHEN is_closed = 0 AND (contact_id IS NULL OR contact_id = 0)
                        THEN 1 ELSE 0 END) open_without_contact
        FROM fact_deal WHERE category_id = :cat AND is_deleted = 0
        """,
        {"cat": SELLERS},
    ).fetchone()
    print(f"  всего карточек:        {row['n']:>6}")
    print(f"  из них с контактом:    {row['with_contact']:>6}")
    print(f"  открытых:              {row['open_deals']:>6}")
    print(f"  открытых без контакта: {row['open_without_contact']:>6}"
          "   ← по ним отчёт слеп")

    conn.execute(
        """
        CREATE TEMP VIEW touch AS
        SELECT d.deal_id, d.assigned_by_id, d.stage_id, d.is_closed,
               d.date_create, d.contact_id,
               a.activity_id, a.provider_type_id, a.direction,
               a.owner_type_id, a.created_at
        FROM fact_deal d
        LEFT JOIN fact_activity a
               ON (a.owner_type_id = 2 AND a.owner_id = d.deal_id)
               OR (a.owner_type_id = 3 AND d.contact_id IS NOT NULL
                   AND d.contact_id > 0 AND a.owner_id = d.contact_id)
        WHERE d.category_id = 0 AND d.is_deleted = 0
        """
    )

    head("3. Общий контакт: чей звонок мы засчитаем")
    shared = conn.execute(
        """
        SELECT COUNT(*) contacts, COALESCE(SUM(deals), 0) deals
        FROM (SELECT contact_id, COUNT(*) deals FROM fact_deal
               WHERE is_deleted = 0 AND contact_id IS NOT NULL AND contact_id > 0
               GROUP BY contact_id HAVING COUNT(*) > 1)
        """
    ).fetchone()
    print(f"  контактов больше чем с одной сделкой: {shared['contacts']:>6}")
    print(f"  сделок на них:                        {shared['deals']:>6}")
    cross = conn.execute(
        """
        SELECT COUNT(DISTINCT s.deal_id) n
        FROM fact_deal s
        JOIN fact_deal o ON o.contact_id = s.contact_id AND o.deal_id <> s.deal_id
                        AND o.category_id <> 0 AND o.is_deleted = 0
        WHERE s.category_id = 0 AND s.is_deleted = 0 AND s.is_closed = 0
          AND s.contact_id IS NOT NULL AND s.contact_id > 0
        """
    ).fetchone()
    print(f"  открытых карточек продавца, чей контакт есть и в другой воронке:"
          f" {cross['n']:>5}")
    print("  ← по ним звонок покупателя засчитался бы работой по объекту")

    head("4. Откуда берутся касания открытых карточек")
    for row in conn.execute(
        "SELECT owner_type_id, COUNT(*) n FROM touch "
        "WHERE is_closed = 0 AND activity_id IS NOT NULL "
        "GROUP BY owner_type_id ORDER BY n DESC"
    ):
        name = OWNER_NAMES.get(row["owner_type_id"], "?")
        print(f"  через {name:<10}: {row['n']:>7}")

    placeholders = ", ".join(f"'{kind}'" for kind in TALK)
    rows = conn.execute(
        f"""
        SELECT deal_id, stage_id, assigned_by_id,
               SUM(CASE WHEN provider_type_id IN ({placeholders})
                        THEN 1 ELSE 0 END) talks,
               SUM(CASE WHEN activity_id IS NOT NULL THEN 1 ELSE 0 END) any_touch,
               MAX(CASE WHEN provider_type_id IN ({placeholders})
                        THEN created_at END) last_talk
        FROM touch WHERE is_closed = 0 GROUP BY deal_id
        """
    ).fetchall()

    head("5. Разговоры на одну открытую карточку собственника")
    talks = [row["talks"] for row in rows]
    edges = (0, 2, 5, 10)
    labels = ("ни одного", "1–2", "3–5", "6–10", "больше 10")
    spread = buckets(talks, edges, labels)
    for label in labels:
        count = spread.get(label, 0)
        share = 100.0 * count / len(rows) if rows else 0
        print(f"  {label:<12} {count:>5}  ({share:>4.1f}%)")
    print(f"\n  карточек всего {len(rows)}, разговоров {sum(talks)}")
    silent_any = [row for row in rows if not row["any_touch"]]
    print(f"  без единого действия любого вида: {len(silent_any)}")

    head("6. Сколько дней молчания")
    ages = [days_since(row["last_talk"], now) for row in rows if row["last_talk"]]
    age_labels = ("до недели", "1–2 недели", "2–4 недели",
                  "1–3 месяца", "больше 3 месяцев")
    spread = buckets(ages, (7, 14, 30, 90), age_labels)
    for label in age_labels:
        print(f"  {label:<16} {spread.get(label, 0):>5}")
    print(f"  ни одного разговора: {len(rows) - len(ages)}")

    head("7. Самые «отработанные» карточки — проверка порядка величины")
    for row in sorted(rows, key=lambda item: -item["talks"])[:8]:
        label = stage_of(conn, row["stage_id"])
        print(f"  сделка {row['deal_id']:>7}  разговоров {row['talks']:>4}  "
              f"{label[:34]}")

    head("8. По брокерам: карточек в работе и сколько из них молчат")
    per_user: dict[int, list[int]] = {}
    for row in rows:
        per_user.setdefault(row["assigned_by_id"] or 0, []).append(row["talks"])
    names = {
        user["user_id"]: f"{user['name']} · {user['department_name']}"
        for user in conn.execute(
            "SELECT user_id, name, department_name FROM dim_user"
        )
    }
    ranked = sorted(per_user.items(), key=lambda item: -len(item[1]))
    print(f"  {'брокер':<38} {'карточек':>9} {'молчат':>7} {'звонков':>8}")
    for user_id, values in ranked[:25]:
        mute = sum(1 for value in values if value == 0)
        print(f"  {names.get(user_id, f'id {user_id}')[:37]:<38} "
              f"{len(values):>9} {mute:>7} {sum(values):>8}")

    head("9. По стадиям: где карточки стоят и молчат")
    per_stage: dict[str, list[int]] = {}
    for row in rows:
        per_stage.setdefault(row["stage_id"], []).append(row["talks"])
    for stage_id, values in sorted(per_stage.items(), key=lambda item: -len(item[1])):
        mute = sum(1 for value in values if value == 0)
        label = stage_of(conn, stage_id)
        print(f"  {label[:30]:<32} карточек {len(values):>5}  "
              f"молчат {mute:>5}  звонков {sum(values):>6}")

    conn.close()


if __name__ == "__main__":
    main()
