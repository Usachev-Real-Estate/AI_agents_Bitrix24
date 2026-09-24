"""Сборка клиентского слоя из витрины и портала (раздел 4 ТЗ).

Порядок прогона и причина, по которой он именно такой:

1. журнал прогонов открывается ПЕРВЫМ и отдельной транзакцией. Прогон,
   упавший где угодно дальше, останется в базе с ``complete = 0`` — то
   есть неполным, а не отсутствующим;
2. витрина читается соединением только на чтение и целиком складывается
   в память;
3. контакты доносятся из портала;
4. ключи считаются по ВСЕМУ портфелю разом — иначе признак агента
   считается неверно (см. clients/keys.py);
5. и только теперь открывается транзакция записи в clients.db.

Между шагами 2–4 транзакции записи нет ни одной. Это то же правило, которым
живёт ETL после сентябрьских поломок: транзакция записи не переживает
обращение к порталу. Здесь оно даже проще — портал вызывается ровно один
раз и до начала записи.

Чего этот шаг НЕ делает, чтобы не искать потом: не считает ``triage_state``
(раздел 6, отдельный пункт плана), не ходит за расшифровками (раздел 5) и
не встраивается в прогон ``dossier`` (шаги 1–2 раздела 4). Поэтому
``calls_with_transcript`` и ``calls_pending`` остаются NULL — «не считано»,
а не ноль.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

if __package__ in (None, ""):  # запуск как `python src/clients/build.py`
    _HERE = Path(__file__).resolve().parent
    for _p in (str(_HERE.parent),):
        if _p not in sys.path:
            sys.path.insert(0, _p)

from analytics.schema import analytics_session  # noqa: E402
from clients import census  # noqa: E402
from clients.contacts import fetch_contacts  # noqa: E402
from clients.events import Event, Totals, build_events, totals  # noqa: E402
from clients.keys import Decision, assign_keys  # noqa: E402
from clients.mart import Portfolio, read_portfolio  # noqa: E402
from clients.merges import apply_migrations, current_links, plan_migrations  # noqa: E402
from clients.schema import (  # noqa: E402
    ALIAS_CONTACT, ALIAS_PHONE, ENTITY_DEAL, clients_session, init_clients_db,
)
from config import get_settings, setup_logging  # noqa: E402

logger = logging.getLogger(__name__)

MODE_FULL = "full"

# Псевдонимы, которые целиком выводятся из портфеля и потому переписываются
# каждым прогоном. Тип `key` сюда не входит: это история переездов, её не
# пересобрать.
DERIVED_ALIASES = (ALIAS_PHONE, ALIAS_CONTACT)

_CLIENT_UPSERT = """
INSERT INTO clients (
    client_key, phone_norm, phone_raw, phone_valid, is_agent, agent_reason,
    key_reason, contact_id, name, assignee_id, assignee_name, assignee_count,
    department_id, updated_at
) VALUES (
    :client_key, :phone_norm, :phone_raw, :phone_valid, :is_agent, :agent_reason,
    :key_reason, :contact_id, :name, :assignee_id, :assignee_name, :assignee_count,
    :department_id, :updated_at
)
ON CONFLICT(client_key) DO UPDATE SET
    phone_norm = excluded.phone_norm,
    phone_raw = excluded.phone_raw,
    phone_valid = excluded.phone_valid,
    is_agent = excluded.is_agent,
    agent_reason = excluded.agent_reason,
    key_reason = excluded.key_reason,
    contact_id = excluded.contact_id,
    name = excluded.name,
    assignee_id = excluded.assignee_id,
    assignee_name = excluded.assignee_name,
    assignee_count = excluded.assignee_count,
    department_id = excluded.department_id,
    updated_at = excluded.updated_at
"""

_TOTALS_UPDATE = """
UPDATE clients SET
    last_event_at = :last_event_at,
    last_touch_at = :last_touch_at,
    last_touch_kind = :last_touch_kind,
    last_touch_author_id = :last_touch_author_id,
    silence_days = :silence_days,
    comments_total = :comments_total,
    comments_by_assignee = :comments_by_assignee,
    comments_by_assignee_30d = :comments_by_assignee_30d,
    calls_total = :calls_total,
    next_step_at = :next_step_at,
    next_step_overdue = :next_step_overdue,
    aggregates_run_id = :run_id
WHERE client_key = :client_key
"""

_EVENT_UPSERT = """
INSERT INTO client_events (
    client_key, at, kind, source_id, entity_type, entity_id,
    author_id, author_is_assignee, is_system, payload_json
) VALUES (
    :client_key, :at, :kind, :source_id, :entity_type, :entity_id,
    :author_id, :author_is_assignee, :is_system, :payload_json
)
ON CONFLICT(kind, source_id) DO UPDATE SET
    client_key = excluded.client_key,
    at = excluded.at,
    entity_type = excluded.entity_type,
    entity_id = excluded.entity_id,
    author_id = excluded.author_id,
    author_is_assignee = excluded.author_is_assignee,
    is_system = excluded.is_system,
    -- Слияние, а не замена: расшифровка звонка приезжает отдельно и позже,
    -- а тело события пересобирается из витрины каждую ночь. Замена стирала
    -- бы самую дорогую часть системы молча, каждым прогоном `[V15]`.
    payload_json = json_patch(client_events.payload_json, excluded.payload_json)
"""


# --------------------------------------------------------------------------
# журнал прогонов
# --------------------------------------------------------------------------

def open_run(conn, mode: str, now: datetime) -> int:
    """Открыть строку журнала. complete остаётся 0 до самого конца."""
    cursor = conn.execute(
        "INSERT INTO client_runs(started_at, mode) VALUES (?, ?)",
        (now.isoformat(), mode),
    )
    return int(cursor.lastrowid)


def close_run(
    conn,
    run_id: int,
    *,
    now: datetime,
    cards: int,
    errors: int,
    complete: bool,
    degraded: Sequence[str],
    mart_full_sync_at: str | None,
) -> None:
    conn.execute(
        "UPDATE client_runs SET finished_at = ?, cards = ?, errors = ?,"
        " complete = ?, degraded_rules = ?, mart_full_sync_at = ? WHERE id = ?",
        (now.isoformat(), cards, errors, int(complete),
         ", ".join(degraded), mart_full_sync_at, run_id),
    )


# --------------------------------------------------------------------------
# запись
# --------------------------------------------------------------------------

def client_rows(
    decisions: Mapping[int, Decision],
    portfolio: Portfolio,
    contacts: Mapping[int, Mapping[str, Any]],
    now: datetime,
) -> dict[str, dict[str, Any]]:
    """Строки таблицы клиентов: по одной на ключ.

    Ответственный клиента — ответственный САМОЙ СВЕЖЕЙ его сделки. У клиента
    их может быть несколько и с разными брокерами; «свежая» отвечает на
    вопрос «кто ведёт его сейчас», а любое усреднение не отвечает ни на
    какой. При равенстве дат выигрывает больший номер сделки — чтобы ответ
    не зависел от порядка выдачи витрины.

    Рядом лежит ``assignee_count`` — сколько брокеров ведёт клиента на самом
    деле. Одного поля мало: агентство прямо говорит, что клиент бывает и
    собственником, и покупателем и работает с разными брокерами, а склейка
    по совпавшему имени такие карточки как раз и сводит вместе. Без счётчика
    колонка «ответственный» молча выдавала бы одного из нескольких за
    единственного, и РОП спрашивал бы с него за чужую работу.
    """
    by_key: dict[str, list[Decision]] = {}
    for decision in decisions.values():
        by_key.setdefault(decision.key, []).append(decision)

    rows: dict[str, dict[str, Any]] = {}
    for key, group in by_key.items():
        newest = max(
            group,
            key=lambda item: (
                portfolio.deals.get(item.deal_id, {}).get("date_create") or "",
                item.deal_id,
            ),
        )
        deal = portfolio.deals.get(newest.deal_id, {})
        assignee_id = deal.get("assigned_by_id")
        brokers = {
            portfolio.deals.get(item.deal_id, {}).get("assigned_by_id")
            for item in group
        }
        brokers.discard(None)
        user = portfolio.users.get(int(assignee_id)) if assignee_id else None
        contact = contacts.get(newest.contact_id) if newest.contact_id else None
        rows[key] = {
            "client_key": key,
            "phone_norm": newest.phone_norm,
            "phone_raw": newest.phone_raw,
            "phone_valid": int(newest.phone_valid),
            "is_agent": int(newest.is_agent),
            "agent_reason": newest.agent_reason,
            "key_reason": newest.key_reason,
            "contact_id": newest.contact_id,
            "name": _contact_name(contact) or deal.get("title") or "",
            "assignee_id": assignee_id,
            "assignee_name": _user_name(user),
            "assignee_count": len(brokers) or None,
            "department_id": user.get("department_id") if user else None,
            "updated_at": now.isoformat(),
        }
    return rows


def _contact_name(contact: Mapping[str, Any] | None) -> str:
    if not contact:
        return ""
    parts = (contact.get("LAST_NAME"), contact.get("NAME"), contact.get("SECOND_NAME"))
    return " ".join(str(part).strip() for part in parts if part)


def _user_name(user: Mapping[str, Any] | None) -> str:
    if not user:
        return ""
    parts = (user.get("last_name"), user.get("name"))
    return " ".join(str(part).strip() for part in parts if part) or str(user.get("name") or "")


def write_links(conn, decisions: Mapping[int, Decision], portfolio: Portfolio) -> int:
    """Переписать связи сделок с клиентами целиком.

    Связи выводятся из портфеля без остатка, поэтому переписываются, а не
    дописываются: сделка, выпавшая из воронок или удалённая, обязана
    перестать числиться за клиентом. Дозапись оставила бы её навсегда.
    """
    conn.execute("DELETE FROM client_links WHERE entity_type = ?", (ENTITY_DEAL,))
    rows = []
    for deal_id, decision in sorted(decisions.items()):
        deal = portfolio.deals.get(deal_id, {})
        category_id = deal.get("category_id")
        rows.append((
            decision.key, ENTITY_DEAL, deal_id, category_id,
            deal.get("stage_id") or "",
            portfolio.stages.get((deal.get("stage_id") or "", int(category_id or 0)), ""),
            deal.get("title") or "", deal.get("date_create"),
            int(bool(deal.get("is_closed"))),
        ))
    conn.executemany(
        "INSERT INTO client_links(client_key, entity_type, entity_id, category_id,"
        " stage_id, stage_name, title, date_create, closed)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def write_aliases(conn, decisions: Mapping[int, Decision]) -> int:
    """Переписать выводимые псевдонимы целиком.

    Та же причина, что у связей, и ещё одна: телефон, стёртый у контакта в
    портале, обязан перестать вести к клиенту. Дозапись сохранила бы его
    навсегда и продолжала бы склеивать людей, которых уже ничего не
    связывает. Псевдонимы типа `key` не трогаются — это история переездов.
    """
    marks = ", ".join("?" * len(DERIVED_ALIASES))
    conn.execute(f"DELETE FROM client_aliases WHERE alias_type IN ({marks})",
                 DERIVED_ALIASES)
    seen: dict[tuple[str, str], str] = {}
    for _deal_id, decision in sorted(decisions.items()):
        for alias_type, alias_value in decision.aliases:
            seen.setdefault((alias_type, alias_value), decision.key)
    conn.executemany(
        "INSERT INTO client_aliases(client_key, alias_type, alias_value)"
        " VALUES (?, ?, ?)",
        [(key, kind, value) for (kind, value), key in sorted(seen.items())],
    )
    return len(seen)


_CONFLICT_UPSERT = """
INSERT INTO merge_conflicts (phone_norm, contact_ids_json, detected_at, last_seen_at)
VALUES (:phone_norm, :contact_ids_json, :at, :at)
ON CONFLICT(phone_norm) DO UPDATE SET
    contact_ids_json = excluded.contact_ids_json,
    last_seen_at = excluded.last_seen_at
"""


def write_conflicts(conn, conflicts: Mapping[str, tuple[int, ...]], *, at: str) -> int:
    """Записать номера, по которым склейка запрещена (раздел 2.4 ТЗ).

    Единственное место, где история спорного телефона вообще остаётся.
    Разбор портфеля считает их в лог, но лог ротируется, а вопрос «этот
    номер давно так или со вчера» задаёт тот, кто разбирается с конкретным
    клиентом, а не читает ночную сводку.

    `detected_at` ставится один раз и переписыванию не подлежит — иначе
    конфликт, живущий полгода, каждую ночь выглядел бы свежим. `last_seen_at`
    обновляется каждым прогоном, который его увидел.

    Строки НЕ удаляются. Конфликт «рассасывается» не событием, а тем, что
    очередной прогон его больше не встретил; старый `last_seen_at` и
    читается как «в прошлый раз уже не повторилось». Удаление стёрло бы
    единственную запись о том, что эти два контакта когда-то делили номер.
    """
    rows = [
        {
            "phone_norm": phone,
            "contact_ids_json": json.dumps(list(ids)),
            "at": at,
        }
        for phone, ids in sorted(conflicts.items())
    ]
    conn.executemany(_CONFLICT_UPSERT, rows)
    return len(rows)


def write_events(conn, events: Iterable[Event], assignee_by_key: Mapping[str, int | None]) -> int:
    """Записать ленту upsert'ом по (вид, источник)."""
    rows = []
    for event in events:
        assignee = assignee_by_key.get(event.client_key)
        rows.append({
            "client_key": event.client_key,
            "at": event.at,
            "kind": event.kind,
            "source_id": event.source_id,
            "entity_type": event.entity_type,
            "entity_id": event.entity_id,
            "author_id": event.author_id,
            "author_is_assignee": (
                None if event.author_id is None or assignee is None
                else int(event.author_id == assignee)
            ),
            "is_system": int(event.is_system),
            "payload_json": event.payload_json,
        })
    conn.executemany(_EVENT_UPSERT, rows)
    return len(rows)


# --------------------------------------------------------------------------
# прогон
# --------------------------------------------------------------------------

def build(*, now: datetime | None = None, dry_run: bool = False,
          limit: int | None = None) -> dict[str, Any]:
    """Пересобрать клиентский слой. Возвращает сводку прогона."""
    settings = get_settings()
    moment = now or datetime.now(timezone.utc)
    categories = (settings.sellers_category_id, settings.buyers_category_id)
    degraded: list[str] = []

    # Прогон на части портфеля — всегда сухой: признак агента копится по
    # контакту, и над половиной карточек ключи получаются другими.
    dry = dry_run or bool(limit)
    run_id = 0
    if not dry:
        # База заводится только там, где в неё будут писать: сухой прогон,
        # оставляющий за собой пустой файл, — это уже не «ничего не пишет».
        init_clients_db()
        with clients_session() as conn:
            run_id = open_run(conn, MODE_FULL, moment)

    with analytics_session(readonly=True) as conn:
        portfolio = read_portfolio(conn, categories)

    cards = portfolio.cards
    if limit:
        cards = cards[:limit]
        logger.warning(
            "Прогон ограничен %d карточками — только сухой просмотр: признак "
            "агента копится по контакту, и на части портфеля ключи другие", limit,
        )

    # Справочник типов контакта не получен — правило агента ослаблено, но
    # ключи от этого не переезжают: тип лишь ДОБАВЛЯЕТ агентов, а не
    # отнимает. Прогон идёт, ослабление уходит в журнал.
    type_names = _contact_type_names(degraded)
    # Соединение с порталом закрывается здесь же: утёкший сокет держится до
    # сборщика мусора, а когда тот проснётся — не наше дело.
    with _portal(settings) as portal:
        fetched = fetch_contacts(portal, portfolio.contact_ids)
    complete = fetched.complete

    assignment = assign_keys(cards, fetched.contacts, type_names=type_names)
    decisions = assignment.decisions
    key_by_contact = _key_by_contact(decisions)
    events = build_events(portfolio, {d: v.key for d, v in decisions.items()},
                          key_by_contact)

    # Разбор портфеля числами. Считается всегда, а не только на сухом
    # прогоне: вопрос «верно ли задумано правило ключа» задаёт живой
    # портфель, и ответ на него должен быть в логе каждого прогона, а не
    # добываться отдельным запуском тогда, когда что-то уже разъехалось.
    counted = census.take(
        assignment, fetched.contacts,
        cards={
            deal_id: census.Linked(
                category_id=deal.get("category_id"),
                assignee_id=deal.get("assigned_by_id"),
            )
            for deal_id, deal in portfolio.deals.items()
        },
    )
    logger.info("Ключи: %s, по причинам: %s", counted.by_kind, counted.by_reason)
    logger.info("Агентов среди клиентов: %d, по признакам: %s",
                counted.agents, counted.agents_by_reason)
    logger.info("Клиентов в обеих воронках: %d, у нескольких брокеров: %d",
                counted.both_funnels, counted.several_brokers)
    logger.info("Склеено по совпавшему имени: номеров %d, контактов %d",
                counted.merged_phones, counted.merged_contacts)
    logger.info("Спорные телефоны: %s", counted.conflicts.as_dict())

    summary: dict[str, Any] = {
        "cards": len(cards),
        "clients": counted.clients,
        "events": len(events),
        "complete": complete,
        "degraded": degraded,
        "contacts_failed": len(fetched.failed),
        "mart_full_sync_at": portfolio.mart_full_sync_at,
        "written": False,
    }
    if dry:
        logger.info("Сухой прогон: %s", summary)
        return summary

    if not complete:
        # Не «агрегаты не перезаписываем», а вообще ничего, и это строже
        # раздела 4 ТЗ. Контакт, за которым не сходили, не имеет телефона —
        # значит его сделки уезжают с ключа p: на c:, а это переезд ключа:
        # прежняя строка клиента УДАЛЯЕТСЯ, разбор перенацеливается. Сделать
        # это из-за таймаута портала и откатить следующей ночью нельзя —
        # переезд необратим, строки уже нет. Вчерашний, но верный портфель
        # лучше сегодняшнего и выдуманного.
        with clients_session() as conn:
            close_run(
                conn, run_id, now=moment, cards=len(cards),
                errors=len(fetched.failed), complete=False,
                degraded=degraded, mart_full_sync_at=portfolio.mart_full_sync_at,
            )
        logger.warning(
            "Прогон неполон: %d контактов не забрано — портфель не тронут",
            len(fetched.failed),
        )
        return summary

    rows = client_rows(decisions, portfolio, fetched.contacts, moment)
    assignee_by_key = {key: row["assignee_id"] for key, row in rows.items()}

    with clients_session() as conn:
        links = current_links(conn)
        plan = plan_migrations(links, decisions)
        apply_migrations(conn, plan.migrations, at=moment.isoformat())

        conn.executemany(_CLIENT_UPSERT, list(rows.values()))
        write_links(conn, decisions, portfolio)
        write_aliases(conn, decisions)
        write_conflicts(conn, assignment.conflicts, at=moment.isoformat())
        write_events(conn, events, assignee_by_key)
        _write_totals(conn, events, rows, run_id, moment)

        close_run(
            conn, run_id, now=moment, cards=len(cards), errors=0, complete=True,
            degraded=degraded, mart_full_sync_at=portfolio.mart_full_sync_at,
        )

    summary["written"] = True
    summary["migrations"] = len(plan.migrations)
    summary["split"] = len(plan.split)
    logger.info("Клиентский слой пересобран: %s", summary)
    return summary


def _write_totals(conn, events: Sequence[Event], rows: Mapping[str, dict],
                  run_id: int, now: datetime) -> None:
    """Пересчитать числа списка. Только на полном прогоне."""
    by_key: dict[str, list[Event]] = {}
    for event in events:
        by_key.setdefault(event.client_key, []).append(event)
    payload = []
    for key, row in rows.items():
        summary: Totals = totals(
            by_key.get(key, ()), assignee_id=row["assignee_id"], now=now,
        )
        payload.append({
            "client_key": key,
            "run_id": run_id,
            "last_event_at": summary.last_event_at,
            "last_touch_at": summary.last_touch_at,
            "last_touch_kind": summary.last_touch_kind,
            "last_touch_author_id": summary.last_touch_author_id,
            "silence_days": summary.silence_days,
            "comments_total": summary.comments_total,
            "comments_by_assignee": summary.comments_by_assignee,
            "comments_by_assignee_30d": summary.comments_by_assignee_30d,
            "calls_total": summary.calls_total,
            "next_step_at": summary.next_step_at,
            "next_step_overdue": (
                None if summary.next_step_overdue is None
                else int(summary.next_step_overdue)
            ),
        })
    conn.executemany(_TOTALS_UPDATE, payload)


def _key_by_contact(decisions: Mapping[int, Decision]) -> dict[int, str]:
    """Контакт → ключ клиента. По нему находятся дела, висящие на контакте."""
    out: dict[int, str] = {}
    for decision in sorted(decisions.values(), key=lambda item: item.deal_id):
        if not decision.contact_id:
            continue
        known = out.setdefault(decision.contact_id, decision.key)
        if known != decision.key:
            # Невозможно при нынешнем правиле ключа: ключ принадлежит ровно
            # одному контакту. Если правило поменяют, дела контакта уедут к
            # одному из клиентов наугад — и это должно быть видно в логе,
            # а не обнаружиться чужим разговором в чужой карточке.
            logger.warning(
                "Контакт %d ведёт к двум ключам (%s и %s) — берём первый",
                decision.contact_id, known, decision.key,
            )
    return out


def _contact_type_names(degraded: list[str]) -> dict[str, str]:
    """Справочник типов контакта. Его отсутствие ослабляет правило агента."""
    try:
        from tools import fetch_contact_type_names

        names = fetch_contact_type_names() or {}
    except Exception as error:  # noqa: BLE001
        logger.warning("Справочник типов контакта не получен: %s", error)
        names = {}
    if not names:
        degraded.append("типы контактов")
    return names


def _portal(settings):
    from analytics.client import BitrixClient

    return BitrixClient(
        settings.b24_webhook_url, rps=settings.analytics_rate_limit_rps,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Пересборка клиентского слоя")
    parser.add_argument("--dry-run", action="store_true",
                        help="посчитать и показать, ничего не записывая")
    parser.add_argument("--limit", type=int, default=None,
                        help="взять N карточек; ВСЕГДА сухой прогон — на части "
                             "портфеля признак агента считается неверно")
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.log_level)

    summary = build(dry_run=args.dry_run, limit=args.limit)
    logger.info("Готово: %s", summary)

    # Прогон, который ничего не записал, — это НЕ успех, и молчать о нём
    # нельзя. Неполный прогон нарочно не трогает портфель ([V20]), и в этом
    # он прав; но повторись он каждую ночь — список клиентов замёрзнет на
    # позавчерашнем дне, оставаясь на вид живым. Ненулевой код поднимает
    # алерт админу через scripts/cron_job.sh; без него единственным следом
    # осталась бы строка WARN в общем логе, которую никто не читает.
    #
    # Сухой прогон не считается: он для того и запускается, чтобы не писать.
    dry = args.dry_run or bool(args.limit)
    if not dry and not summary.get("written"):
        logger.error("Портфель не пересобран: %s", summary)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
