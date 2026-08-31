"""Сборка fact_stage_event из crm.stagehistory.list.

Это ядро всей аналитики движения: текущая STAGE_ID знает только, где сделка
сейчас, а вопросы «где встают сделки», «сколько дошли до договора» и «сколько
времени теряется на стадии» требуют истории переходов.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterable

from client import to_utc_iso, utc_now_iso

logger = logging.getLogger(__name__)

ENTITY_LEAD = "lead"
ENTITY_DEAL = "deal"

# entityTypeId в crm.stagehistory.list
ENTITY_TYPE_IDS = {ENTITY_LEAD: 1, ENTITY_DEAL: 2}


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _duration_sec(entered_at: str, left_at: str | None) -> int | None:
    """Длительность завершённого интервала.

    Для открытого интервала (left_at IS NULL) намеренно возвращаем None:
    записанное «сколько уже стоит» протухнет к следующему запросу, и на
    дашборде появится возраст на момент последнего ETL вместо текущего.
    Возраст открытой стадии считается в SQL на момент чтения.
    """
    if not left_at:
        return None
    start, end = _parse(entered_at), _parse(left_at)
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds()))


def normalize_history_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Привести строки crm.stagehistory.list к (stage_id, entered_at, category_id)."""
    out: list[dict[str, Any]] = []
    for row in rows:
        stage_id = str(row.get("STAGE_ID") or "").strip()
        entered_at = to_utc_iso(row.get("CREATED_TIME"))
        if not stage_id or not entered_at:
            continue
        out.append({
            "stage_id": stage_id,
            "entered_at": entered_at,
            "category_id": _int(row.get("CATEGORY_ID")),
            "row_id": _int(row.get("ID")),
        })
    out.sort(key=lambda r: (r["entered_at"], r["row_id"]))
    return out


def build_stage_events(
    entity_type: str,
    entity_id: int,
    history_rows: Iterable[dict[str, Any]],
    *,
    category_id: int,
    date_create: str | None,
    current_stage_id: str,
) -> list[dict[str, Any]]:
    """Собрать непрерывную ленту интервалов пребывания на стадиях.

    Две ситуации, в которых наивная реализация молча теряет данные:

    1. **Сделка ни разу не двигалась.** crm.stagehistory.list возвращает
       строки только для тех, кто менял стадию. Свежая сделка на первой
       стадии истории не имеет — и без синтеза первого события просто
       выпадет из воронки. Именно свежие сделки чаще всего и интересны.
    2. **История начинается позже создания.** История стадий включена не с
       первого дня жизни портала. Если первое известное событие позже
       DATE_CREATE, растягиваем его до даты создания: иначе часть срока
       жизни сделки исчезает, и цикл сделки систематически занижается.
       Выдумывать стадию, на которой сделка стояла до начала истории, мы не
       можем — и не выдумываем.

    Последний интервал остаётся открытым даже у закрытой сделки: left_at
    означает «ушла на другую стадию», а выигранная или проигранная сделка со
    своей финальной стадии никуда не уходит. Закрывать интервал по closedate
    значило бы получить «на стадии „Проиграна“ осталось 0» при сотне
    карточек, реально на ней стоящих. Длительность жизни сделки считается
    отдельно, от создания до закрытия.

    Returns:
        Список словарей под вставку в fact_stage_event, seq с нуля.
    """
    rows = normalize_history_rows(history_rows)

    # Дубли одной и той же стадии подряд — артефакт портала (пересохранение
    # карточки без смены стадии). Считать их двумя заходами значит завысить
    # число переходов в отчёте о движении.
    deduped: list[dict[str, Any]] = []
    for row in rows:
        if deduped and deduped[-1]["stage_id"] == row["stage_id"]:
            continue
        deduped.append(row)

    if not deduped:
        if not date_create or not current_stage_id:
            return []
        deduped = [{
            "stage_id": current_stage_id,
            "entered_at": date_create,
            "category_id": category_id,
        }]
    elif date_create and deduped[0]["entered_at"] > date_create:
        deduped[0] = dict(deduped[0], entered_at=date_create)

    events: list[dict[str, Any]] = []
    for seq, row in enumerate(deduped):
        is_last = seq == len(deduped) - 1
        left_at = None if is_last else deduped[seq + 1]["entered_at"]
        events.append({
            "entity_type": entity_type,
            "entity_id": entity_id,
            "category_id": row.get("category_id") or category_id,
            "stage_id": row["stage_id"],
            "entered_at": row["entered_at"],
            "left_at": left_at,
            "duration_sec": _duration_sec(row["entered_at"], left_at),
            "seq": seq,
        })
    return events


def replace_stage_events(conn, entity_type: str, entity_id: int, events: list[dict[str, Any]]):
    """Переписать ленту стадий сущности целиком.

    Апдейт по месту невозможен: новый переход меняет left_at и duration
    предыдущего интервала, а перенумерация seq при пропущенном ранее событии
    сдвигает всю ленту. Полная замена внутри транзакции ETL проще и не
    оставляет полулент.
    """
    conn.execute(
        "DELETE FROM fact_stage_event WHERE entity_type = ? AND entity_id = ?",
        (entity_type, entity_id),
    )
    if not events:
        return
    conn.executemany(
        """
        INSERT INTO fact_stage_event
            (entity_type, entity_id, category_id, stage_id,
             entered_at, left_at, duration_sec, seq)
        VALUES (:entity_type, :entity_id, :category_id, :stage_id,
                :entered_at, :left_at, :duration_sec, :seq)
        """,
        events,
    )


def fetch_history_for_entities(
    client,
    entity_type: str,
    entity_ids: list[int],
    *,
    chunk_size: int = 50,
) -> dict[int, list[dict[str, Any]]]:
    """Загрузить историю стадий пачками по OWNER_ID.

    Размер пачки 50 — как в src/tools.py:131 (STAGE_HISTORY_OWNER_CHUNK):
    на больших списках портал отдаёт OPERATION_TIME_LIMIT.
    """
    entity_type_id = ENTITY_TYPE_IDS[entity_type]
    by_entity: dict[int, list[dict[str, Any]]] = {}
    ids = [i for i in entity_ids if i > 0]

    for offset in range(0, len(ids), chunk_size):
        chunk = ids[offset:offset + chunk_size]
        try:
            rows = list(client.list_paged(
                "crm.stagehistory.list",
                {
                    "entityTypeId": entity_type_id,
                    "filter": {"OWNER_ID": chunk},
                    "select": ["ID", "OWNER_ID", "STAGE_ID", "CREATED_TIME",
                               "TYPE_ID", "CATEGORY_ID"],
                },
            ))
        except Exception:
            logger.warning(
                "crm.stagehistory.list не отдал пачку из %d %s — пробуем поштучно",
                len(chunk), entity_type,
            )
            rows = []
            for entity_id in chunk:
                try:
                    rows.extend(client.list_paged(
                        "crm.stagehistory.list",
                        {
                            "entityTypeId": entity_type_id,
                            "filter": {"OWNER_ID": entity_id},
                            "select": ["ID", "OWNER_ID", "STAGE_ID", "CREATED_TIME",
                                       "TYPE_ID", "CATEGORY_ID"],
                        },
                    ))
                except Exception:
                    logger.warning("История стадий недоступна для %s %s",
                                   entity_type, entity_id)

        for row in rows:
            owner_id = _int(row.get("OWNER_ID"))
            if owner_id:
                by_entity.setdefault(owner_id, []).append(row)

    return by_entity


def probe_lead_history_support(client) -> bool:
    """Проверить, ведёт ли портал историю стадий для лидов (entityTypeId=1).

    В проекте crm.stagehistory.list вызывается только для сделок, и поддержка
    лидов зависит от версии портала. Если истории нет, воронка лидов строится
    по текущему STATUS_ID без времени в стадии — знать это надо до того, как
    цифры уедут на дашборд, а не после.
    """
    try:
        rows = list(client.list_paged(
            "crm.stagehistory.list",
            {"entityTypeId": 1, "select": ["ID", "OWNER_ID", "STAGE_ID", "CREATED_TIME"]},
        ))
    except Exception as exc:
        logger.warning("История стадий для лидов недоступна: %s", exc)
        return False
    return bool(rows)


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


__all__ = [
    "ENTITY_DEAL",
    "ENTITY_LEAD",
    "build_stage_events",
    "fetch_history_for_entities",
    "normalize_history_rows",
    "probe_lead_history_support",
    "replace_stage_events",
    "utc_now_iso",
]
