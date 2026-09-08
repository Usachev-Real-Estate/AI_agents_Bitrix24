"""Как в агентстве отмечают встречу и сколько звонков не приняли.

Два вопроса, оба всплыли после первого замера работы.

ВСТРЕЧИ. Активностей типа MEETING за год 68 при 215 карточках на стадии
«Назначение встречи». Собственник объяснил: встречу заводят ДЕЛОМ (TODO), а
после проведения ставят делу «выполнено»; кроме того, в карточке есть поле
с датой встречи. Значит первый замер работы считал неверно — он записал все
дела в планирование и исключил из разговоров, и карточка, по которой брокер
съездил на встречу, числится молчащей.

Чтобы починить, нужно знать: как называются эти дела и можно ли отличить
встречу от «подготовить документы» по теме. Скрипт печатает темы дел живьём
и считает, насколько изменится число молчащих карточек, если дела засчитать.

НЕПРИНЯТЫЕ ЗВОНКИ. Незавершённых входящих 5 168 — собственник подтвердил,
что это пропущенные. Скрипт проверяет это структурно (есть ли у них время
окончания) и считает, по скольким карточкам за пропущенным не последовало
ни одного ответного звонка: пропущенный, за которым перезвонили, — это
работа, а не потеря.

Только чтение. Из портала берётся один справочник полей сделки, чтобы найти
то самое поле с датой встречи; карточки не выгружаются.

Запуск:

    git show origin/claude/leadership-dashboard-ih3sjz:scripts/meeting_probe.py \\
        > /tmp/meeting_probe.py
    docker run --rm -v $(pwd)/data:/app/data \\
      -v /tmp/meeting_probe.py:/app/meeting_probe.py --env-file .env \\
      b24-ai-auditor:latest python /app/meeting_probe.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Скрипт запускают двумя способами: из репозитория (лежит в scripts/) и
# смонтированным в корень образа (-v ...:/app/meeting_probe.py). Во втором
# случае __file__/../.. уезжает в «/», и путь, посчитанный только от файла,
# даёт ModuleNotFoundError уже после того, как команда принята. Поэтому
# проверяются оба корня, и берётся тот, где витрина действительно лежит.
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
# Слова, по которым дело похоже на встречу. Список — гипотеза, которую этот
# замер и проверяет: если тем со встречами почти нет, признак не годится.
MEETING_WORDS = ("встреч", "показ", "просмотр", "выезд", "объект")


def head(text: str) -> None:
    print(f"\n{'=' * 64}\n{text}\n{'=' * 64}")


def lower_ru(text: str) -> str:
    """Питоновский lower(): в SQLite кириллица в нижний регистр не уходит."""
    return (text or "").lower()


def main() -> None:
    conn = get_connection(readonly=True)

    head("1. Темы дел (TODO): как выглядит встреча в портале")
    rows = conn.execute(
        """
        SELECT subject, completed, COUNT(*) n FROM fact_activity
        WHERE provider_type_id = 'TODO'
        GROUP BY subject, completed ORDER BY n DESC LIMIT 25
        """
    ).fetchall()
    for row in rows:
        done = "выполнено" if row["completed"] else "запланировано"
        print(f"  {row['n']:>6}  {done:<14} {(row['subject'] or '(пусто)')[:60]}")

    head("2. Сколько дел похожи на встречу по теме")
    todos = conn.execute(
        "SELECT subject, completed FROM fact_activity WHERE provider_type_id = 'TODO'"
    ).fetchall()
    hits = {word: 0 for word in MEETING_WORDS}
    matched_done = matched_open = 0
    for row in todos:
        subject = lower_ru(row["subject"])
        found = [word for word in MEETING_WORDS if word in subject]
        for word in found:
            hits[word] += 1
        if found:
            if row["completed"]:
                matched_done += 1
            else:
                matched_open += 1
    print(f"  всего дел: {len(todos)}")
    for word, count in sorted(hits.items(), key=lambda item: -item[1]):
        print(f"    «{word}»: {count}")
    print(f"  похожи на встречу: {matched_done + matched_open} "
          f"(выполнено {matched_done}, запланировано {matched_open})")

    head("3. Насколько изменится число молчащих карточек собственников")
    for label, kinds in (
        ("только CALL и MEETING (как сейчас)", "'CALL', 'MEETING'"),
        ("плюс выполненные дела", "'CALL', 'MEETING', 'TODO'"),
    ):
        done_only = " AND (a.provider_type_id <> 'TODO' OR a.completed = 1)"
        row = conn.execute(
            f"""
            SELECT COUNT(*) cards, SUM(CASE WHEN t.talks = 0 THEN 1 ELSE 0 END) mute
            FROM (
              SELECT d.deal_id, COUNT(a.activity_id) talks
              FROM fact_deal d
              LEFT JOIN fact_activity a
                     ON ((a.owner_type_id = 2 AND a.owner_id = d.deal_id)
                         OR (a.owner_type_id = 3 AND d.contact_id > 0
                             AND a.owner_id = d.contact_id))
                    AND a.provider_type_id IN ({kinds}){done_only}
              WHERE d.category_id = {SELLERS} AND d.is_deleted = 0 AND d.is_closed = 0
              GROUP BY d.deal_id
            ) t
            """
        ).fetchone()
        share = 100.0 * row["mute"] / row["cards"] if row["cards"] else 0
        print(f"  {label:<38} молчат {row['mute']:>4} из {row['cards']} "
              f"({share:.1f}%)")

    head("4. Непринятые звонки: подтверждается ли структурой")
    for row in conn.execute(
        """
        SELECT direction, completed,
               COUNT(*) n,
               SUM(CASE WHEN end_time IS NULL OR end_time = '' THEN 1 ELSE 0 END) no_end,
               SUM(CASE WHEN end_time = start_time THEN 1 ELSE 0 END) zero_len
        FROM fact_activity WHERE provider_type_id = 'CALL'
        GROUP BY direction, completed ORDER BY n DESC
        """
    ):
        way = {1: "входящий", 2: "исходящий"}.get(row["direction"], "—")
        done = "завершён" if row["completed"] else "НЕ завершён"
        print(f"  {way:<10} {done:<12} {row['n']:>6}   "
              f"без времени конца {row['no_end']:>6}   нулевой длины {row['zero_len']:>6}")

    head("5. Пропущенные без ответного звонка")
    for label, category in (("собственники", 0), ("покупатели", 18)):
        row = conn.execute(
            """
            SELECT COUNT(*) cards,
                   SUM(CASE WHEN t.missed > 0 THEN 1 ELSE 0 END) with_missed,
                   SUM(CASE WHEN t.missed > 0 AND t.back = 0 THEN 1 ELSE 0 END) unanswered
            FROM (
              SELECT d.deal_id,
                     SUM(CASE WHEN a.direction = 1 AND a.completed = 0
                              THEN 1 ELSE 0 END) missed,
                     SUM(CASE WHEN a.direction = 2 THEN 1 ELSE 0 END) back
              FROM fact_deal d
              LEFT JOIN fact_activity a
                     ON ((a.owner_type_id = 2 AND a.owner_id = d.deal_id)
                         OR (a.owner_type_id = 3 AND d.contact_id > 0
                             AND a.owner_id = d.contact_id))
                    AND a.provider_type_id = 'CALL'
              WHERE d.category_id = :cat AND d.is_deleted = 0 AND d.is_closed = 0
              GROUP BY d.deal_id
            ) t
            """,
            {"cat": category},
        ).fetchone()
        print(f"  {label:<14} открытых {row['cards']:>5}, "
              f"с пропущенным {row['with_missed']:>4}, "
              f"из них НИ РАЗУ не перезвонили {row['unanswered']:>4}")

    # Порог по числу входящих обязателен: у человека с пятью входящими и
    # двумя пропущенными выходит 40%, и он возглавил бы таблицу, ничего при
    # этом не значив. Пустой раздел означает, что таких людей нет.
    head("6. Кто чаще всех не берёт трубку (от 30 входящих)")
    for row in conn.execute(
        """
        SELECT COALESCE(u.name, 'id ' || a.responsible_id) AS name,
               COALESCE(u.department_name, '') AS dept,
               SUM(CASE WHEN a.direction = 1 AND a.completed = 0
                        THEN 1 ELSE 0 END) missed,
               SUM(CASE WHEN a.direction = 1 THEN 1 ELSE 0 END) incoming
        FROM fact_activity a
        LEFT JOIN dim_user u ON u.user_id = a.responsible_id
        WHERE a.provider_type_id = 'CALL'
        GROUP BY a.responsible_id HAVING incoming >= 30
        ORDER BY (1.0 * missed / incoming) DESC LIMIT 15
        """
    ):
        share = 100.0 * row["missed"] / row["incoming"]
        print(f"  {(row['name'] + ' · ' + row['dept'])[:40]:<42} "
              f"{row['missed']:>5} из {row['incoming']:>5}  ({share:.0f}%)")

    head("8. Встреча: назначена, просрочена или проведена")
    meetings = conn.execute(
        """
        SELECT a.subject, a.completed, a.start_time, a.created_at
        FROM fact_activity a
        WHERE a.provider_type_id IN ('TODO', 'MEETING')
        """
    ).fetchall()
    kept = [row for row in meetings
            if any(word in lower_ru(row["subject"]) for word in MEETING_WORDS)]
    with_start = [row for row in kept if row["start_time"]]
    print(f"  дел и активностей о встрече: {len(kept)}")
    print(f"  из них с датой начала:       {len(with_start)}"
          "   ← без неё «просрочена» не отличить")
    now = _now_iso(conn)
    done = sum(1 for row in kept if row["completed"])
    overdue = sum(1 for row in kept
                  if not row["completed"] and row["start_time"]
                  and row["start_time"] < now)
    ahead = sum(1 for row in kept
                if not row["completed"] and row["start_time"]
                and row["start_time"] >= now)
    unknown = sum(1 for row in kept if not row["completed"] and not row["start_time"])
    print(f"\n  проведена (выполнено):       {done}")
    print(f"  назначена, срок не наступил:  {ahead}")
    print(f"  ПРОСРОЧЕНА (срок прошёл):     {overdue}"
          "   ← встреча не состоялась либо о ней не отчитались")
    print(f"  не выполнено и без даты:      {unknown}")

    conn.close()
    _fields()


def _now_iso(conn) -> str:
    return conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now') AS now"
    ).fetchone()["now"]


def _fields() -> None:
    """Поле с датой встречи в карточке сделки. Один справочный вызов."""
    head("7. Поля сделки, похожие на дату встречи")
    try:
        from client import BitrixClient
        from config import get_settings

        settings = get_settings()
        with BitrixClient(settings.b24_webhook_url) as client:
            fields = client.call("crm.deal.fields", {}) or {}
    except Exception as error:  # pragma: no cover — портал может быть недоступен
        print(f"  портал недоступен: {error}")
        return
    found = False
    for code, meta in fields.items():
        title = lower_ru(str(meta.get("formLabel") or meta.get("title") or ""))
        if "встреч" in title or "показ" in title:
            found = True
            print(f"  {code:<28} {meta.get('type', ''):<12} "
                  f"{meta.get('formLabel') or meta.get('title')}")
    if not found:
        print("  ни одного поля со «встречей» в названии — "
              "возможно, поле заведено только в карточке продавцов")


if __name__ == "__main__":
    main()
