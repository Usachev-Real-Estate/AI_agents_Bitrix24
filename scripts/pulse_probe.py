"""Замер витрины перед сборкой «Пульса»: только агрегаты, никаких карточек.

Экран план-факта строится на допущениях, проверить которые из репозитория
нельзя — витрина лежит на боевом сервере. Этот скрипт отвечает на вопросы,
от которых зависят определения метрик, и печатает ТОЛЬКО числа: ни названий
сделок, ни имён клиентов, ни телефонов. Вывод можно целиком отправить в
переписку.

Запуск на сервере:

    python scripts/pulse_probe.py                # только витрина, без сети
    python scripts/pulse_probe.py --bitrix       # плюс два счётных запроса в Bitrix

Витрина открывается строго на чтение: скрипт ничего не меняет и не пишет.

На какие вопросы он отвечает:

1. Что лежит в поле суммы. Витрина складывает fact_deal.opportunity, а это
   OPPORTUNITY сделки, пока в ANALYTICS_AMOUNT_FIELD_BY_CATEGORY_JSON не
   названо поле-переопределение (по умолчанию там пусто). Если у «Продавцов»
   в этом поле стоимость объекта, а у «Покупателей» комиссия, складывать их
   нельзя: выполнение квартального плана уедет в сотни процентов. Разброс
   сумм по воронкам показывает это сразу.
2. Сколько сделок теряет окно витрины. ETL режет выборку по дате СОЗДАНИЯ
   (etl.py: filter >=DATE_CREATE), поэтому сделка, заведённая до начала окна
   и выигранная в этом квартале, в витрину не попадёт никогда. Для агентства
   с полугодовыми эксклюзивами это прямое занижение факта. Точное число
   даёт --bitrix.
3. Насколько можно верить дате закрытия. closedate — это редактируемое поле
   CLOSEDATE, а не момент перевода в выигранную стадию. Скрипт считает, у
   скольких закрытых сделок её нет и у скольких она расходится с моментом
   входа в выигранную стадию больше чем на сутки.
4. Из скольких людей сложится план. План — 4,5 млн на брокера за квартал,
   поэтому важно, кого витрина считает активным: уволенный перестаёт
   приходить в user.get, но остаётся в dim_user с is_active = 1 и вечно
   добавляет отделу 4,5 млн плана. Отставание synced_at это показывает.
5. Сколько отделов остались без опознанного РОПа: их брокеры попадут в план
   компании, но не в план отдела.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
for extra in (_SRC_DIR, _SRC_DIR / "analytics"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from config import get_settings  # noqa: E402
from schema import get_connection  # noqa: E402

logger = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))
LINE = "─" * 72


def main() -> int:
    parser = argparse.ArgumentParser(description="Агрегаты витрины для сборки «Пульса»")
    parser.add_argument(
        "--bitrix", action="store_true",
        help="дополнительно спросить Bitrix, сколько закрытых сделок не попало в окно",
    )
    args = parser.parse_args()

    settings = get_settings()
    db_path = Path(settings.analytics_db_path)
    if not db_path.exists():
        print(f"Витрина не найдена: {db_path}")
        print("Проверьте ANALYTICS_DB_PATH в .env или запустите ETL.")
        return 1

    conn = get_connection(db_path, readonly=True)
    try:
        _header("Витрина")
        window_since = _mart(conn, db_path)

        _header("1. Что лежит в поле суммы")
        _amounts(conn)

        _header("2. Окно витрины")
        _window(conn, window_since)

        _header("3. Дата закрытия")
        _closedate(conn)

        _header("4. Состав, из которого сложится план")
        _headcount(conn)

        _header("5. Отделы и РОПы")
        _departments(conn)

        _header("6. Масштаб по месяцам")
        _volume(conn)
    finally:
        conn.close()

    if args.bitrix:
        _header("7. Сколько теряет окно — точный счёт из Bitrix")
        _bitrix_gap(settings, window_since)
    else:
        print("\nПодсказка: запуск с --bitrix добавит точный счёт сделок, "
              "потерянных окном витрины (два запроса, только чтение).")
    return 0


# --------------------------------------------------------------------------


def _header(title: str) -> None:
    print(f"\n{LINE}\n{title}\n{LINE}")


def _rows(conn: sqlite3.Connection, sql: str, params=()) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _one(conn: sqlite3.Connection, sql: str, params=()) -> dict:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else {}


def _quarter_start() -> str:
    today = datetime.now(MSK).date()
    first_month = 3 * ((today.month - 1) // 3) + 1
    start = date(today.year, first_month, 1)
    return datetime(start.year, start.month, start.day, tzinfo=MSK).astimezone(
        timezone.utc).isoformat()


def _pct(part: float, whole: float) -> str:
    return f"{100.0 * part / whole:.1f}%" if whole else "—"


def _money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f}".replace(",", " ")


def _quantiles(values: list[float]) -> dict[str, float | None]:
    data = sorted(v for v in values if v is not None)
    if not data:
        return {"p25": None, "p50": None, "p75": None, "max": None}

    def at(share: float) -> float:
        index = max(0, min(len(data) - 1, int(round(share * (len(data) - 1)))))
        return float(data[index])

    return {"p25": at(0.25), "p50": at(0.5), "p75": at(0.75), "max": data[-1]}


# --------------------------------------------------------------------------


def _mart(conn: sqlite3.Connection, db_path: Path) -> str | None:
    meta = {
        row["key"]: row["value"]
        for row in _rows(conn, "SELECT key, value FROM analytics_meta")
    }
    window_since = meta.get("window_since")
    print(f"Файл:            {db_path}")
    print(f"Версия схемы:    {meta.get('schema_version', '—')}")
    print(f"Окно с:          {window_since or '—'}")
    print(f"История лидов:   "
          f"{'ведётся' if meta.get('lead_history_supported') == '1' else 'НЕ ведётся'}")
    last = _one(
        conn,
        "SELECT kind, finished_at FROM etl_run WHERE status = 'ok' "
        "AND kind IN ('incremental', 'full', 'backfill') ORDER BY id DESC LIMIT 1",
    )
    print(f"Последний ETL:   {last.get('kind', '—')} {last.get('finished_at', '—')}")
    counts = _one(
        conn,
        "SELECT (SELECT COUNT(*) FROM fact_deal WHERE is_deleted = 0) AS deals,"
        " (SELECT COUNT(*) FROM fact_lead WHERE is_deleted = 0) AS leads,"
        " (SELECT COUNT(*) FROM fact_stage_event) AS events,"
        " (SELECT COUNT(*) FROM dim_user) AS users",
    )
    print(f"Записей:         сделок {counts.get('deals', 0)}, "
          f"лидов {counts.get('leads', 0)}, "
          f"событий стадий {counts.get('events', 0)}, "
          f"пользователей {counts.get('users', 0)}")
    return window_since


def _amounts(conn: sqlite3.Connection) -> None:
    """Разброс сумм по воронкам — комиссия это или стоимость объекта."""
    settings = get_settings()
    overrides = settings.analytics_amount_field_by_category
    print(f"ANALYTICS_AMOUNT_FIELD_BY_CATEGORY_JSON: "
          f"{overrides if overrides else '{} — суммируется OPPORTUNITY сделки'}")
    print()
    print(f"{'Воронка':<28}{'выигр.':>8}{'запол.':>8}"
          f"{'p25':>12}{'медиана':>12}{'p75':>12}{'макс':>14}")
    for pipeline in _rows(
        conn, "SELECT category_id, name FROM dim_pipeline ORDER BY sort, category_id",
    ):
        cat = pipeline["category_id"]
        rows = _rows(
            conn,
            "SELECT opportunity FROM fact_deal WHERE is_deleted = 0 AND is_won = 1"
            " AND category_id = ? AND closedate IS NOT NULL",
            (cat,),
        )
        values = [float(r["opportunity"] or 0) for r in rows]
        filled = [v for v in values if v > 0]
        q = _quantiles(filled)
        name = f"{pipeline['name']} ({cat})"[:27]
        print(f"{name:<28}{len(values):>8}{_pct(len(filled), len(values)):>8}"
              f"{_money(q['p25']):>12}{_money(q['p50']):>12}"
              f"{_money(q['p75']):>12}{_money(q['max']):>14}")
    print()
    print("Как читать: если у одной воронки медиана в миллионах, а у другой в")
    print("сотнях тысяч — это разные величины (стоимость объекта и комиссия),")
    print("и складывать их в план нельзя без поля-переопределения.")

    foreign = _rows(
        conn,
        "SELECT COALESCE(NULLIF(upper(currency_id), ''), 'пусто') AS cur,"
        " COUNT(*) AS n FROM fact_deal WHERE is_deleted = 0"
        " GROUP BY cur ORDER BY n DESC",
    )
    print("\nВалюты сделок: " + ", ".join(f"{r['cur']} — {r['n']}" for r in foreign))


def _window(conn: sqlite3.Connection, window_since: str | None) -> None:
    quarter = _quarter_start()
    print(f"Начало квартала: {quarter}")
    if not window_since:
        print("window_since в витрине нет — окно определить нельзя.")
        return
    edge = _one(
        conn,
        "SELECT COUNT(*) AS n FROM fact_deal WHERE is_deleted = 0 AND is_won = 1"
        " AND closedate >= ? AND date_create < ?",
        (quarter, window_since),
    ).get("n", 0)
    early = _one(
        conn,
        "SELECT COUNT(*) AS n FROM fact_deal WHERE is_deleted = 0 AND is_won = 1"
        " AND closedate >= ? AND date_create < datetime(?, '+31 days')",
        (quarter, window_since),
    ).get("n", 0)
    total = _one(
        conn,
        "SELECT COUNT(*) AS n FROM fact_deal WHERE is_deleted = 0 AND is_won = 1"
        " AND closedate >= ?",
        (quarter,),
    ).get("n", 0)
    print(f"Выиграно в квартале (в витрине):            {total}")
    print(f"  из них заведены до начала окна:           {edge}  ← должно быть 0")
    print(f"  из них заведены в первый месяц окна:      {early}")
    print()
    print("Вторая строка обязана быть нулём: сделок старше окна в витрине быть")
    print("не может по построению. Третья — оценка снизу для тех, кого окно уже")
    print("срезало: чем она больше, тем больше выигранных сделок квартала")
    print("осталось за границей. Точный ответ даёт запуск с --bitrix.")


def _closedate(conn: sqlite3.Connection) -> None:
    closed = _one(
        conn,
        "SELECT COUNT(*) AS n FROM fact_deal WHERE is_deleted = 0 AND is_closed = 1",
    ).get("n", 0)
    no_date = _one(
        conn,
        "SELECT COUNT(*) AS n FROM fact_deal WHERE is_deleted = 0 AND is_closed = 1"
        " AND closedate IS NULL",
    ).get("n", 0)
    future = _one(
        conn,
        "SELECT COUNT(*) AS n FROM fact_deal WHERE is_deleted = 0 AND is_closed = 1"
        " AND closedate > datetime('now')",
    ).get("n", 0)
    print(f"Закрытых сделок:                            {closed}")
    print(f"  без даты закрытия (выпадают из метрик):   {no_date}  ({_pct(no_date, closed)})")
    print(f"  дата закрытия в будущем:                  {future}  ({_pct(future, closed)})")

    # Расхождение с моментом входа в выигранную стадию: closedate редактируется
    # руками, а событие стадии — нет.
    drift = _rows(
        conn,
        """
        SELECT ABS(julianday(d.closedate) - julianday(e.entered_at)) AS days
        FROM fact_deal d
        JOIN dim_stage s ON s.stage_id = d.stage_id AND s.category_id = d.category_id
        JOIN fact_stage_event e ON e.entity_type = 'deal' AND e.entity_id = d.deal_id
                               AND e.stage_id = d.stage_id
        WHERE d.is_deleted = 0 AND d.is_won = 1 AND d.closedate IS NOT NULL
          AND s.semantic = 'won'
        """,
    )
    days = [float(r["days"]) for r in drift if r["days"] is not None]
    if days:
        over_day = sum(1 for d in days if d > 1)
        q = _quantiles(days)
        print(f"\nСверка с моментом входа в выигранную стадию (по {len(days)} сделкам):")
        print(f"  расходится больше чем на сутки:           {over_day}"
              f"  ({_pct(over_day, len(days))})")
        print(f"  медиана расхождения, дней:                {q['p50']:.1f}")
        print(f"  максимум, дней:                           {q['max']:.0f}")
        print("\nЕсли расхождение массовое, деньги периода честнее считать по")
        print("событию стадии, а не по редактируемому полю CLOSEDATE.")
    else:
        print("\nСобытий выигранной стадии не нашлось — сверить закрытие не с чем.")


def _headcount(conn: sqlite3.Connection) -> None:
    last_sync = _one(
        conn, "SELECT MAX(synced_at) AS m FROM dim_user",
    ).get("m")
    print(f"Последняя синхронизация людей: {last_sync or '—'}")
    total = _one(conn, "SELECT COUNT(*) AS n FROM dim_user").get("n", 0)
    active = _one(
        conn, "SELECT COUNT(*) AS n FROM dim_user WHERE is_active = 1",
    ).get("n", 0)
    stale = _one(
        conn,
        "SELECT COUNT(*) AS n FROM dim_user WHERE is_active = 1 AND synced_at < ?",
        (last_sync or "",),
    ).get("n", 0)
    no_dept = _one(
        conn,
        "SELECT COUNT(*) AS n FROM dim_user WHERE is_active = 1"
        " AND (department_id IS NULL OR department_name = '')",
    ).get("n", 0)
    print(f"Всего в витрине:                            {total}")
    print(f"Помечены активными:                         {active}")
    print(f"  активны, но отстали от последней сверки:  {stale}  ← вероятно уволены")
    print(f"  активны и без отдела:                     {no_dept}")
    print()
    print("Третья строка — цена вопроса: каждый такой человек добавит своему")
    print("отделу 4,5 млн плана и будет тихо занижать выполнение. Уволенный")
    print("перестаёт приходить в user.get, и его is_active так и остаётся 1.")

    orphan = _one(
        conn,
        "SELECT COUNT(*) AS n FROM fact_deal WHERE is_deleted = 0 AND is_closed = 0"
        " AND (assigned_by_id IS NULL OR assigned_by_id NOT IN"
        " (SELECT user_id FROM dim_user))",
    ).get("n", 0)
    print(f"\nОткрытых сделок без известного ответственного: {orphan}")
    print("Такие карточки не принадлежат ни одному отделу: в план они не")
    print("попадут, а в факт компании попадут — расхождение придётся назвать.")


def _departments(conn: sqlite3.Connection) -> None:
    try:
        from qc_delivery import ROP_SURNAMES
    except Exception:
        ROP_SURNAMES = ()
        print("qc_delivery не импортировался — список РОПов недоступен.")

    rows = _rows(
        conn,
        """
        SELECT u.department_id, u.department_name AS name,
               COUNT(*) AS people,
               SUM(CASE WHEN u.is_active = 1 THEN 1 ELSE 0 END) AS active
        FROM dim_user u
        WHERE u.department_id IS NOT NULL AND u.department_name <> ''
        GROUP BY u.department_id ORDER BY u.department_name
        """,
    )
    surnames = {
        row["department_id"]: row["last_name"].strip().lower()
        for row in _rows(
            conn,
            "SELECT department_id, last_name FROM dim_user"
            " WHERE is_active = 1 AND department_id IS NOT NULL AND last_name <> ''",
        )
        if row["last_name"].strip().lower() in ROP_SURNAMES
    }
    print(f"{'Отдел':<32}{'людей':>8}{'активных':>10}{'РОП опознан':>14}")
    without_rop = 0
    for row in rows:
        known = row["department_id"] in surnames
        without_rop += 0 if known else 1
        name = str(row["name"])[:31]
        print(f"{name:<32}{row['people']:>8}{row['active']:>10}"
              f"{('да' if known else 'НЕТ'):>14}")
    print()
    print(f"Фамилий РОПов в списке: {len(ROP_SURNAMES)}. "
          f"Отделов без опознанного РОПа: {without_rop}.")
    print("У отдела без опознанного РОПа некого исключить из планового состава —")
    print("его план будет завышен ровно на одну норму.")


def _volume(conn: sqlite3.Connection) -> None:
    rows = _rows(
        conn,
        """
        SELECT substr(datetime(closedate, '+3 hours'), 1, 7) AS month,
               COUNT(*) AS won,
               SUM(CASE WHEN opportunity > 0 THEN 1 ELSE 0 END) AS filled,
               COALESCE(SUM(opportunity), 0) AS amount
        FROM fact_deal
        WHERE is_deleted = 0 AND is_won = 1 AND closedate IS NOT NULL
        GROUP BY month ORDER BY month DESC LIMIT 15
        """,
    )
    created = {
        r["month"]: r["n"] for r in _rows(
            conn,
            "SELECT substr(datetime(date_create, '+3 hours'), 1, 7) AS month,"
            " COUNT(*) AS n FROM fact_deal WHERE is_deleted = 0"
            " GROUP BY month",
        )
    }
    leads = {
        r["month"]: r["n"] for r in _rows(
            conn,
            "SELECT substr(datetime(date_create, '+3 hours'), 1, 7) AS month,"
            " COUNT(*) AS n FROM fact_lead WHERE is_deleted = 0"
            " GROUP BY month",
        )
    }
    print(f"{'Месяц':<10}{'лидов':>9}{'сделок':>9}{'выигр.':>9}"
          f"{'сумма':>16}{'покрытие':>10}")
    for row in reversed(rows):
        month = row["month"]
        print(f"{month:<10}{leads.get(month, 0):>9}{created.get(month, 0):>9}"
              f"{row['won']:>9}{_money(row['amount']):>16}"
              f"{_pct(row['filled'], row['won']):>10}")


def _bitrix_gap(settings, window_since: str | None) -> None:
    """Точный счёт сделок, потерянных окном витрины. Только чтение."""
    if not window_since:
        print("window_since неизвестен — считать нечего.")
        return
    try:
        from client import BitrixClient
    except Exception as exc:  # pragma: no cover — зависит от окружения сервера
        print(f"Клиент Bitrix недоступен: {exc}")
        return

    quarter = _quarter_start()
    since_day = quarter[:10]
    window_day = window_since[:10]
    try:
        with BitrixClient(
            settings.b24_webhook_url, rps=settings.analytics_rate_limit_rps,
        ) as client:
            total = _bx_total(client, {"CLOSED": "Y", ">=CLOSEDATE": since_day})
            missed = _bx_total(client, {
                "CLOSED": "Y", ">=CLOSEDATE": since_day, "<DATE_CREATE": window_day,
            })
    except Exception as exc:
        print(f"Запрос не удался: {exc}")
        return

    print(f"Закрыто сделок с {since_day} (по Bitrix):      {total}")
    print(f"  из них заведены до {window_day}:            {missed}")
    if total:
        print(f"  доля, которую окно витрины теряет:        {_pct(missed, total)}")
    print()
    print("Это те сделки, которых в витрине нет и не будет: ETL отбирает по дате")
    print("создания. На столько занижен факт выполнения плана — при полном плане.")


def _bx_total(client, deal_filter: dict) -> int:
    envelope = client.call_envelope(
        "crm.deal.list", {"filter": deal_filter, "select": ["ID"], "start": 0},
    )
    if isinstance(envelope, dict) and "total" in envelope:
        return int(envelope["total"] or 0)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    sys.exit(main())
