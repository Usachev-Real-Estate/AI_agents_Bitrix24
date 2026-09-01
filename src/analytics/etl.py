"""Загрузка данных Bitrix24 в аналитическую витрину.

Три режима:

* ``--backfill`` — первичная загрузка за N месяцев. Разовая, идёт долго.
* ``--incremental`` — догрузка изменённого с прошлого раза. Раз в 15 минут.
* ``--full`` — полная сверка: измерения, факты за окно и вычисление удалённых.
  Раз в сутки ночью.

Почему нужны все три: инкремент дёшев, но принципиально не видит удалений —
удалённая сделка навсегда осталась бы в счётчиках воронки. Ночная сверка это
чинит, а бэкфилл отделён, потому что 12 месяцев истории нельзя тянуть каждые
15 минут.
"""

from __future__ import annotations

import argparse
import logging
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

if __package__ in (None, ""):  # запуск как `python src/analytics/etl.py`
    _HERE = Path(__file__).resolve().parent
    for _p in (str(_HERE), str(_HERE.parent)):
        if _p not in sys.path:
            sys.path.insert(0, _p)

from client import BitrixClient, to_utc_iso, utc_now_iso  # noqa: E402
from config import get_settings, setup_logging  # noqa: E402
from schema import (  # noqa: E402
    SEMANTIC_IN_PROGRESS,
    SEMANTIC_LOST,
    SEMANTIC_WON,
    analytics_session,
    init_analytics_db,
)
from stages import (  # noqa: E402
    ENTITY_DEAL,
    ENTITY_LEAD,
    build_stage_events,
    fetch_history_for_entities,
    probe_lead_history_support,
    replace_stage_events,
)

logger = logging.getLogger(__name__)

DEAL_SELECT_BASE = [
    "ID", "TITLE", "CATEGORY_ID", "STAGE_ID", "STAGE_SEMANTIC_ID",
    "ASSIGNED_BY_ID", "SOURCE_ID", "OPPORTUNITY", "CURRENCY_ID",
    "DATE_CREATE", "DATE_MODIFY", "BEGINDATE", "CLOSEDATE", "CLOSED",
    "LEAD_ID", "CONTACT_ID", "COMPANY_ID", "MOVED_TIME", "MOVED_BY_ID",
]
LEAD_SELECT = [
    "ID", "TITLE", "STATUS_ID", "STATUS_SEMANTIC_ID", "SOURCE_ID",
    "ASSIGNED_BY_ID", "OPPORTUNITY", "CURRENCY_ID",
    "DATE_CREATE", "DATE_MODIFY", "DATE_CLOSED", "CONTACT_ID", "COMPANY_ID",
]

# Стадии истории тянутся пачками; на большом бэкфилле это самая дорогая часть,
# поэтому обрабатываем порциями и коммитим по ходу.
HISTORY_BATCH = 500

META_WINDOW_SINCE = "window_since"


# --------------------------------------------------------------------------
# семантика стадий
# --------------------------------------------------------------------------

_SEMANTIC_BY_CODE = {"S": SEMANTIC_WON, "F": SEMANTIC_LOST, "P": SEMANTIC_IN_PROGRESS}
_WON_SUFFIXES = ("WON",)
_LOST_SUFFIXES = ("LOSE", "APOLOGY", "JUNK")


def infer_semantic(
    stage_id: str,
    semantic_code: Any = None,
    overrides: dict[str, str] | None = None,
) -> str:
    """Определить, «в работе» стадия, «выиграна» или «проиграна».

    Порядок: явное переопределение в конфиге → STAGE_SEMANTIC_ID портала →
    суффикс идентификатора. Портальный код надёжнее суффикса: воронка может
    объявить успешной произвольную стадию, и по имени этого не видно.
    """
    stage_id = str(stage_id or "").strip()
    if overrides and stage_id in overrides:
        value = overrides[stage_id]
        if value in (SEMANTIC_WON, SEMANTIC_LOST, SEMANTIC_IN_PROGRESS):
            return value
    code = str(semantic_code or "").strip().upper()
    if code in _SEMANTIC_BY_CODE:
        return _SEMANTIC_BY_CODE[code]
    tail = stage_id.split(":")[-1].upper()
    if tail in _WON_SUFFIXES:
        return SEMANTIC_WON
    if tail in _LOST_SUFFIXES:
        return SEMANTIC_LOST
    return SEMANTIC_IN_PROGRESS


def status_semantic_code(row: dict[str, Any]) -> str:
    """Семантика стадии так, как её объявил сам портал.

    Bitrix кладёт её в двух разных местах: у статусов лида — в поле SEMANTICS,
    у стадий сделки — в EXTRA.SEMANTICS. Читаем оба.

    Без этого справочник стадий оставался бы на угадывании по суффиксу, а
    самодельные стадии суффиксом ничего не говорят: «Закрытая продажа»
    (UC_A94BGF) выглядела бы «в работе» на странице воронки, хотя те же
    сделки в win rate считаются закрытыми. Два ответа на один вопрос на
    соседних экранах стоят доверия ко всему дашборду.
    """
    code = _str(row.get("SEMANTICS"))
    if code:
        return code
    extra = row.get("EXTRA")
    if isinstance(extra, dict):
        return _str(extra.get("SEMANTICS"))
    return ""


# --------------------------------------------------------------------------
# измерения
# --------------------------------------------------------------------------

def sync_pipelines(client: BitrixClient, conn, now: str) -> list[int]:
    """Загрузить воронки сделок. Возвращает список category_id."""
    rows: list[dict[str, Any]] = []
    try:
        result = client.call("crm.category.list", {"entityTypeId": 2})
        if isinstance(result, dict):
            rows = [r for r in (result.get("categories") or []) if isinstance(r, dict)]
    except Exception as exc:
        logger.warning("crm.category.list недоступен (%s) — пробуем crm.dealcategory.list", exc)

    pipelines: list[tuple[int, str, int]] = []
    if rows:
        for row in rows:
            pipelines.append((_int(row.get("id")), _str(row.get("name")), _int(row.get("sort"))))
    else:
        legacy = client.call("crm.dealcategory.list", {}) or []
        for row in legacy if isinstance(legacy, list) else []:
            pipelines.append((_int(row.get("ID")), _str(row.get("NAME")), _int(row.get("SORT"))))
        # crm.dealcategory.list не возвращает воронку по умолчанию (ID 0) —
        # без неё «Продавцы» просто исчезли бы с дашборда.
        if not any(pid == 0 for pid, _, _ in pipelines):
            pipelines.insert(0, (0, "Основная воронка", 0))

    for category_id, name, sort in pipelines:
        conn.execute(
            """
            INSERT INTO dim_pipeline(category_id, name, is_active, sort, synced_at)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(category_id) DO UPDATE SET
                name=excluded.name, is_active=1, sort=excluded.sort,
                synced_at=excluded.synced_at
            """,
            (category_id, name or f"Воронка {category_id}", sort, now),
        )
    ids = [pid for pid, _, _ in pipelines]
    logger.info("Воронок загружено: %d %s", len(ids), ids)
    return ids


def sync_stages(
    client: BitrixClient,
    conn,
    category_ids: Iterable[int],
    now: str,
    overrides: dict[str, str],
) -> None:
    """Стадии всех воронок из crm.status.list."""
    total = 0
    for category_id in category_ids:
        entity_id = "DEAL_STAGE" if int(category_id) == 0 else f"DEAL_STAGE_{int(category_id)}"
        try:
            rows = client.call("crm.status.list", {"filter": {"ENTITY_ID": entity_id}}) or []
        except Exception:
            logger.warning("Не удалось загрузить стадии воронки %s", category_id)
            continue
        for row in rows if isinstance(rows, list) else []:
            stage_id = _str(row.get("STATUS_ID"))
            if not stage_id:
                continue
            conn.execute(
                """
                INSERT INTO dim_stage(stage_id, category_id, name, sort, semantic, synced_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(stage_id, category_id) DO UPDATE SET
                    name=excluded.name, sort=excluded.sort,
                    semantic=excluded.semantic, synced_at=excluded.synced_at
                """,
                (
                    stage_id, int(category_id), _str(row.get("NAME")) or stage_id,
                    _int(row.get("SORT")),
                    infer_semantic(stage_id, status_semantic_code(row), overrides), now,
                ),
            )
            total += 1
    logger.info("Стадий загружено: %d", total)


def sync_lead_statuses(client: BitrixClient, conn, now: str, overrides: dict[str, str]) -> None:
    rows = client.call("crm.status.list", {"filter": {"ENTITY_ID": "STATUS"}}) or []
    for row in rows if isinstance(rows, list) else []:
        status_id = _str(row.get("STATUS_ID"))
        if not status_id:
            continue
        conn.execute(
            """
            INSERT INTO dim_lead_status(status_id, name, sort, semantic, synced_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(status_id) DO UPDATE SET
                name=excluded.name, sort=excluded.sort,
                semantic=excluded.semantic, synced_at=excluded.synced_at
            """,
            (
                status_id, _str(row.get("NAME")) or status_id, _int(row.get("SORT")),
                infer_semantic(status_id, status_semantic_code(row), overrides), now,
            ),
        )
    logger.info("Статусов лидов загружено: %d", len(rows) if isinstance(rows, list) else 0)


def sync_sources(client: BitrixClient, conn, now: str) -> None:
    rows = client.call("crm.status.list", {"filter": {"ENTITY_ID": "SOURCE"}}) or []
    for row in rows if isinstance(rows, list) else []:
        source_id = _str(row.get("STATUS_ID"))
        if not source_id:
            continue
        conn.execute(
            """
            INSERT INTO dim_source(source_id, name, synced_at) VALUES (?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                name=excluded.name, synced_at=excluded.synced_at
            """,
            (source_id, _str(row.get("NAME")) or source_id, now),
        )


def sync_users(client: BitrixClient, conn, now: str) -> None:
    """Пользователи + названия отделов."""
    departments: dict[int, str] = {}
    try:
        rows = client.call("department.get", {}) or []
        for row in rows if isinstance(rows, list) else []:
            departments[_int(row.get("ID"))] = _str(row.get("NAME"))
    except Exception:
        logger.warning("department.get недоступен — отделы останутся пустыми")

    count = 0
    for row in client.list_paged("user.get", {}):
        user_id = _int(row.get("ID"))
        if not user_id:
            continue
        dept_ids = row.get("UF_DEPARTMENT") or []
        dept_id = _int(dept_ids[0]) if isinstance(dept_ids, list) and dept_ids else None
        last_name = _str(row.get("LAST_NAME"))
        name = " ".join(
            part for part in (_str(row.get("NAME")), last_name) if part
        ) or _str(row.get("EMAIL")) or f"ID {user_id}"
        conn.execute(
            """
            INSERT INTO dim_user(user_id, name, last_name, department_id,
                                 department_name, is_active, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                name=excluded.name, last_name=excluded.last_name,
                department_id=excluded.department_id,
                department_name=excluded.department_name,
                is_active=excluded.is_active, synced_at=excluded.synced_at
            """,
            (
                user_id, name, last_name, dept_id, departments.get(dept_id or 0, ""),
                1 if str(row.get("ACTIVE")).upper() in ("Y", "TRUE", "1") else 0, now,
            ),
        )
        count += 1
    logger.info("Пользователей загружено: %d", count)


def sync_dimensions(client: BitrixClient, conn, settings) -> list[int]:
    """Полный рефреш всех измерений. Дёшево — счёт идёт на десятки запросов."""
    now = utc_now_iso()
    overrides = settings.analytics_stage_semantic_overrides
    category_ids = sync_pipelines(client, conn, now)
    sync_stages(client, conn, category_ids, now, overrides)
    sync_lead_statuses(client, conn, now, overrides)
    sync_sources(client, conn, now)
    sync_users(client, conn, now)
    return category_ids


# --------------------------------------------------------------------------
# факты
# --------------------------------------------------------------------------

def _deal_row(raw: dict[str, Any], settings, now: str) -> dict[str, Any]:
    category_id = _int(raw.get("CATEGORY_ID"))
    amount_field = settings.analytics_amount_field_by_category.get(category_id)
    opportunity = _float(raw.get(amount_field)) if amount_field else _float(raw.get("OPPORTUNITY"))
    stage_id = _str(raw.get("STAGE_ID"))
    semantic = infer_semantic(
        stage_id, raw.get("STAGE_SEMANTIC_ID"), settings.analytics_stage_semantic_overrides,
    )
    return {
        "deal_id": _int(raw.get("ID")),
        "title": _str(raw.get("TITLE")),
        "category_id": category_id,
        "stage_id": stage_id,
        "assigned_by_id": _int(raw.get("ASSIGNED_BY_ID")) or None,
        "source_id": _str(raw.get("SOURCE_ID")),
        "opportunity": opportunity,
        "currency_id": _str(raw.get("CURRENCY_ID")),
        "date_create": to_utc_iso(raw.get("DATE_CREATE")),
        "date_modify": to_utc_iso(raw.get("DATE_MODIFY")),
        "begindate": to_utc_iso(raw.get("BEGINDATE")),
        "closedate": to_utc_iso(raw.get("CLOSEDATE")),
        "is_closed": 1 if str(raw.get("CLOSED")).upper() == "Y" else 0,
        "is_won": 1 if semantic == SEMANTIC_WON else 0,
        "is_lost": 1 if semantic == SEMANTIC_LOST else 0,
        "lead_id": _int(raw.get("LEAD_ID")) or None,
        "contact_id": _int(raw.get("CONTACT_ID")) or None,
        "company_id": _int(raw.get("COMPANY_ID")) or None,
        "moved_time": to_utc_iso(raw.get("MOVED_TIME")),
        "moved_by_id": _int(raw.get("MOVED_BY_ID")) or None,
        "synced_at": now,
    }


_DEAL_UPSERT = """
INSERT INTO fact_deal (
    deal_id, title, category_id, stage_id, assigned_by_id, source_id,
    opportunity, currency_id, date_create, date_modify, begindate, closedate,
    is_closed, is_won, is_lost, lead_id, contact_id, company_id,
    moved_time, moved_by_id, is_deleted, synced_at
) VALUES (
    :deal_id, :title, :category_id, :stage_id, :assigned_by_id, :source_id,
    :opportunity, :currency_id, :date_create, :date_modify, :begindate, :closedate,
    :is_closed, :is_won, :is_lost, :lead_id, :contact_id, :company_id,
    :moved_time, :moved_by_id, 0, :synced_at
)
ON CONFLICT(deal_id) DO UPDATE SET
    title=excluded.title, category_id=excluded.category_id, stage_id=excluded.stage_id,
    assigned_by_id=excluded.assigned_by_id, source_id=excluded.source_id,
    opportunity=excluded.opportunity, currency_id=excluded.currency_id,
    date_create=excluded.date_create, date_modify=excluded.date_modify,
    begindate=excluded.begindate, closedate=excluded.closedate,
    is_closed=excluded.is_closed, is_won=excluded.is_won, is_lost=excluded.is_lost,
    lead_id=excluded.lead_id, contact_id=excluded.contact_id,
    company_id=excluded.company_id, moved_time=excluded.moved_time,
    moved_by_id=excluded.moved_by_id, is_deleted=0, synced_at=excluded.synced_at
"""

_LEAD_UPSERT = """
INSERT INTO fact_lead (
    lead_id, title, status_id, source_id, assigned_by_id, date_create,
    date_modify, date_closed, opportunity, currency_id, is_converted,
    contact_id, company_id, is_deleted, synced_at
) VALUES (
    :lead_id, :title, :status_id, :source_id, :assigned_by_id, :date_create,
    :date_modify, :date_closed, :opportunity, :currency_id, :is_converted,
    :contact_id, :company_id, 0, :synced_at
)
ON CONFLICT(lead_id) DO UPDATE SET
    title=excluded.title, status_id=excluded.status_id, source_id=excluded.source_id,
    assigned_by_id=excluded.assigned_by_id, date_create=excluded.date_create,
    date_modify=excluded.date_modify, date_closed=excluded.date_closed,
    opportunity=excluded.opportunity, currency_id=excluded.currency_id,
    is_converted=excluded.is_converted, contact_id=excluded.contact_id,
    company_id=excluded.company_id, is_deleted=0, synced_at=excluded.synced_at
"""


def _lead_row(raw: dict[str, Any], now: str) -> dict[str, Any]:
    status_id = _str(raw.get("STATUS_ID"))
    return {
        "lead_id": _int(raw.get("ID")),
        "title": _str(raw.get("TITLE")),
        "status_id": status_id,
        "source_id": _str(raw.get("SOURCE_ID")),
        "assigned_by_id": _int(raw.get("ASSIGNED_BY_ID")) or None,
        "date_create": to_utc_iso(raw.get("DATE_CREATE")),
        "date_modify": to_utc_iso(raw.get("DATE_MODIFY")),
        "date_closed": to_utc_iso(raw.get("DATE_CLOSED")),
        "opportunity": _float(raw.get("OPPORTUNITY")),
        "currency_id": _str(raw.get("CURRENCY_ID")),
        "is_converted": 1 if status_id.upper() == "CONVERTED" else 0,
        "contact_id": _int(raw.get("CONTACT_ID")) or None,
        "company_id": _int(raw.get("COMPANY_ID")) or None,
        "synced_at": now,
    }


def sync_deals(
    client: BitrixClient,
    conn,
    settings,
    *,
    since: str,
    modified_since: str | None = None,
) -> list[int]:
    """Загрузить сделки окна. Возвращает ID тронутых сделок."""
    now = utc_now_iso()
    deal_filter: dict[str, Any] = {">=DATE_CREATE": since}
    if modified_since:
        deal_filter[">=DATE_MODIFY"] = modified_since

    select = list(DEAL_SELECT_BASE)
    select.extend(
        field for field in settings.analytics_amount_field_by_category.values()
        if field not in select
    )

    touched: list[int] = []
    batch: list[dict[str, Any]] = []
    for raw in client.list_by_id("crm.deal.list", {"filter": deal_filter, "select": select}):
        row = _deal_row(raw, settings, now)
        if not row["deal_id"]:
            continue
        batch.append(row)
        touched.append(row["deal_id"])
        if len(batch) >= HISTORY_BATCH:
            conn.executemany(_DEAL_UPSERT, batch)
            batch.clear()
    if batch:
        conn.executemany(_DEAL_UPSERT, batch)

    logger.info("Сделок загружено/обновлено: %d", len(touched))
    return touched


def sync_leads(
    client: BitrixClient,
    conn,
    *,
    since: str,
    modified_since: str | None = None,
) -> list[int]:
    """Загрузить лиды окна. Возвращает ID тронутых лидов."""
    now = utc_now_iso()
    lead_filter: dict[str, Any] = {">=DATE_CREATE": since}
    if modified_since:
        lead_filter[">=DATE_MODIFY"] = modified_since

    touched: list[int] = []
    batch: list[dict[str, Any]] = []
    for raw in client.list_by_id(
        "crm.lead.list", {"filter": lead_filter, "select": LEAD_SELECT},
    ):
        row = _lead_row(raw, now)
        if not row["lead_id"]:
            continue
        batch.append(row)
        touched.append(row["lead_id"])
        if len(batch) >= HISTORY_BATCH:
            conn.executemany(_LEAD_UPSERT, batch)
            batch.clear()
    if batch:
        conn.executemany(_LEAD_UPSERT, batch)

    logger.info("Лидов загружено/обновлено: %d", len(touched))
    return touched


def link_leads_to_deals(conn) -> None:
    """Проставить лидам ID созданной из них сделки.

    Связь живёт на стороне сделки (LEAD_ID), у лида такого поля нет.
    is_converted при этом берётся из STATUS_ID лида и здесь не трогается:
    лид может быть квалифицирован, а сделка из него — создана раньше окна
    витрины, и тогда ссылки просто не будет.
    """
    conn.execute(
        """
        UPDATE fact_lead SET converted_deal_id = (
            SELECT MIN(d.deal_id) FROM fact_deal d
            WHERE d.lead_id = fact_lead.lead_id AND d.is_deleted = 0
        )
        WHERE EXISTS (
            SELECT 1 FROM fact_deal d
            WHERE d.lead_id = fact_lead.lead_id AND d.is_deleted = 0
        )
        """
    )


def sync_stage_history(
    client: BitrixClient,
    conn,
    entity_type: str,
    entity_ids: list[int],
) -> int:
    """Перестроить ленту стадий для перечисленных сущностей."""
    if not entity_ids:
        return 0

    table, pk = (
        ("fact_deal", "deal_id") if entity_type == ENTITY_DEAL else ("fact_lead", "lead_id")
    )
    stage_col = "stage_id" if entity_type == ENTITY_DEAL else "status_id"
    cat_col = "category_id" if entity_type == ENTITY_DEAL else "0 AS category_id"

    written = 0
    for offset in range(0, len(entity_ids), HISTORY_BATCH):
        chunk = entity_ids[offset:offset + HISTORY_BATCH]
        placeholders = ",".join("?" * len(chunk))
        meta = {
            row[pk]: row for row in conn.execute(
                f"SELECT {pk}, {cat_col}, {stage_col} AS cur_stage, date_create "
                f"FROM {table} WHERE {pk} IN ({placeholders})",
                chunk,
            )
        }
        history = fetch_history_for_entities(client, entity_type, chunk)
        for entity_id in chunk:
            info = meta.get(entity_id)
            if info is None:
                continue
            events = build_stage_events(
                entity_type,
                entity_id,
                history.get(entity_id, []),
                category_id=int(info["category_id"] or 0),
                date_create=info["date_create"],
                current_stage_id=info["cur_stage"] or "",
            )
            replace_stage_events(conn, entity_type, entity_id, events)
            written += len(events)
    logger.info("Событий стадий записано (%s): %d", entity_type, written)
    return written


def reconcile_deleted(client: BitrixClient, conn, entity_type: str, since: str) -> int:
    """Пометить удалённые в Bitrix записи.

    Инкремент по DATE_MODIFY принципиально не видит удалений: удалённая
    сделка просто перестаёт приходить, и без сверки навсегда остаётся в
    счётчиках воронки. Все витринные запросы фильтруют is_deleted = 0.
    """
    method = "crm.deal.list" if entity_type == ENTITY_DEAL else "crm.lead.list"
    table, pk = (
        ("fact_deal", "deal_id") if entity_type == ENTITY_DEAL else ("fact_lead", "lead_id")
    )
    live = {
        _int(row.get("ID"))
        for row in client.list_by_id(method, {"filter": {">=DATE_CREATE": since}, "select": ["ID"]})
    }
    live.discard(0)
    known = {row[0] for row in conn.execute(f"SELECT {pk} FROM {table} WHERE is_deleted = 0")}
    gone = known - live
    for entity_id in gone:
        conn.execute(f"UPDATE {table} SET is_deleted = 1 WHERE {pk} = ?", (entity_id,))
        conn.execute(
            "DELETE FROM fact_stage_event WHERE entity_type = ? AND entity_id = ?",
            (entity_type, entity_id),
        )
    if gone:
        logger.info("Помечено удалёнными (%s): %d", entity_type, len(gone))
    return len(gone)


# --------------------------------------------------------------------------
# окно, watermark, журнал прогонов
# --------------------------------------------------------------------------

def get_window_since(conn, settings, override: str | None = None) -> str:
    """Начало окна витрины. Вычисляется один раз и фиксируется.

    Пересчитывать «сегодня минус 12 месяцев» на каждом прогоне нельзя: старые
    записи выпадали бы из фильтра, оставаясь в базе, и витрина расходилась бы
    с собственными счётчиками.
    """
    if override:
        value = override
    else:
        row = conn.execute(
            "SELECT value FROM analytics_meta WHERE key = ?", (META_WINDOW_SINCE,),
        ).fetchone()
        if row:
            return row[0]
        months = max(1, int(settings.analytics_months_back))
        value = (datetime.now(timezone.utc) - timedelta(days=30 * months)).date().isoformat()
    conn.execute(
        "INSERT INTO analytics_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (META_WINDOW_SINCE, value),
    )
    return value


def get_watermark(conn, entity: str, overlap_minutes: int) -> str | None:
    """Верхняя граница прошлой загрузки минус перекрытие."""
    row = conn.execute(
        "SELECT last_date_modify FROM etl_watermark WHERE entity = ?", (entity,),
    ).fetchone()
    if not row or not row[0]:
        return None
    try:
        moment = datetime.fromisoformat(row[0]) - timedelta(minutes=overlap_minutes)
    except ValueError:
        return None
    return moment.isoformat()


def set_watermark(conn, entity: str, *, full_sync: bool = False) -> None:
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO etl_watermark(entity, last_date_modify, last_full_sync_at)
        VALUES (?, ?, ?)
        ON CONFLICT(entity) DO UPDATE SET
            last_date_modify=excluded.last_date_modify,
            last_full_sync_at=COALESCE(excluded.last_full_sync_at,
                                       etl_watermark.last_full_sync_at)
        """,
        (entity, now, now if full_sync else None),
    )


@contextmanager
def etl_run(conn, kind: str, entity: str = "") -> Iterator[dict[str, int]]:
    """Журналировать прогон: страница «Качество данных» показывает лаг и ошибки."""
    cursor = conn.execute(
        "INSERT INTO etl_run(kind, entity, started_at, status) VALUES (?, ?, ?, 'running')",
        (kind, entity, utc_now_iso()),
    )
    run_id = cursor.lastrowid
    counters = {"rows": 0}
    try:
        yield counters
    except Exception as exc:
        conn.execute(
            "UPDATE etl_run SET finished_at=?, status='error', error=? WHERE id=?",
            (utc_now_iso(), str(exc)[:500], run_id),
        )
        raise
    conn.execute(
        "UPDATE etl_run SET finished_at=?, status='ok', rows_upserted=? WHERE id=?",
        (utc_now_iso(), counters["rows"], run_id),
    )


# --------------------------------------------------------------------------
# режимы запуска
# --------------------------------------------------------------------------

def run_sync(kind: str, *, since_override: str | None = None) -> dict[str, Any]:
    """Выполнить прогон ETL. kind ∈ {incremental, full, backfill}."""
    settings = get_settings()
    init_analytics_db()
    summary: dict[str, Any] = {"kind": kind}

    with BitrixClient(
        settings.b24_webhook_url, rps=settings.analytics_rate_limit_rps,
    ) as client, analytics_session() as conn:
        with etl_run(conn, kind) as counters:
            since = get_window_since(conn, settings, since_override)
            logger.info("Режим=%s, окно с %s", kind, since)

            incremental = kind == "incremental"
            if incremental:
                # Люди меняются часто: наняли менеджера, перевели между
                # отделами. Пока справочник не обновлён, сделки новичка не
                # принадлежат ни одному отделу и его РОП их не видит — при
                # обновлении раз в сутки это провал длиной в рабочий день.
                # Стадии и воронки, наоборот, меняются раз в квартал, поэтому
                # тянем только пользователей: это 2-3 запроса против двух
                # десятков на полный набор измерений.
                sync_users(client, conn, utc_now_iso())
            else:
                sync_dimensions(client, conn, settings)

            overlap = settings.analytics_etl_overlap_minutes
            deal_since = get_watermark(conn, ENTITY_DEAL, overlap) if incremental else None
            lead_since = get_watermark(conn, ENTITY_LEAD, overlap) if incremental else None

            deal_ids = sync_deals(
                client, conn, settings, since=since, modified_since=deal_since,
            )
            lead_ids = sync_leads(client, conn, since=since, modified_since=lead_since)
            link_leads_to_deals(conn)

            sync_stage_history(client, conn, ENTITY_DEAL, deal_ids)
            if lead_ids and _lead_history_supported(conn, client):
                sync_stage_history(client, conn, ENTITY_LEAD, lead_ids)

            if kind == "full":
                summary["deleted_deals"] = reconcile_deleted(client, conn, ENTITY_DEAL, since)
                summary["deleted_leads"] = reconcile_deleted(client, conn, ENTITY_LEAD, since)

            set_watermark(conn, ENTITY_DEAL, full_sync=not incremental)
            set_watermark(conn, ENTITY_LEAD, full_sync=not incremental)

            counters["rows"] = len(deal_ids) + len(lead_ids)
            summary["deals"] = len(deal_ids)
            summary["leads"] = len(lead_ids)
            summary["requests"] = client.request_count

    logger.info("Готово: %s", summary)
    return summary


def _lead_history_supported(conn, client: BitrixClient) -> bool:
    """Проверить поддержку истории стадий лидов один раз и запомнить."""
    row = conn.execute(
        "SELECT value FROM analytics_meta WHERE key = 'lead_history_supported'",
    ).fetchone()
    if row:
        return row[0] == "1"
    supported = probe_lead_history_support(client)
    conn.execute(
        "INSERT INTO analytics_meta(key, value) VALUES('lead_history_supported', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        ("1" if supported else "0",),
    )
    logger.info(
        "История стадий лидов %s",
        "доступна" if supported else "недоступна — воронка лидов без времени в стадии",
    )
    return supported


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _str(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def main() -> int:
    parser = argparse.ArgumentParser(description="ETL аналитической витрины Bitrix24")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--incremental", action="store_true",
                      help="догрузить изменённое (по умолчанию, для cron раз в 15 мин)")
    mode.add_argument("--full", action="store_true",
                      help="полная сверка окна + вычисление удалённых (ночью)")
    mode.add_argument("--backfill", action="store_true",
                      help="первичная загрузка за N месяцев")
    mode.add_argument("--probe", action="store_true",
                      help="проверить доступность API и историю стадий лидов")
    parser.add_argument("--since", default=None, help="переопределить начало окна (YYYY-MM-DD)")
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.log_level)

    if args.probe:
        return _run_probe(settings)

    kind = "full" if args.full else "backfill" if args.backfill else "incremental"
    try:
        run_sync(kind, since_override=args.since)
    except Exception:
        logger.exception("ETL завершился с ошибкой")
        return 1
    return 0


def _run_probe(settings) -> int:
    init_analytics_db()
    with BitrixClient(
        settings.b24_webhook_url, rps=settings.analytics_rate_limit_rps,
    ) as client:
        profile = client.call("profile", {})
        print(f"Портал доступен, вебхук принадлежит: {profile}")
        categories = client.call("crm.category.list", {"entityTypeId": 2})
        names = [
            f"{c.get('id')}: {c.get('name')}"
            for c in (categories or {}).get("categories", [])
        ]
        print("Воронки сделок:", ", ".join(names) or "не получены")
        supported = probe_lead_history_support(client)
        print(
            "История стадий лидов:",
            "доступна" if supported
            else "НЕДОСТУПНА — воронка лидов будет без времени в стадии",
        )
        print(f"Запросов потрачено: {client.request_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
