"""Неизвестное в базе клиентов записано как неизвестное, а не как ноль.

Правило раздела 4 ТЗ: неполный прогон агрегаты не перезаписывает. Значит
между заведением клиента и первым полным прогоном его счётчики не посчитаны
вовсе. Если бы колонки стояли с `NOT NULL DEFAULT 0`, это состояние было бы
неотличимо от посчитанного нуля — и цена известна заранее: «ни одного
комментария от ответственного» у клиента, с которым работали каждый день,
первый приоритет в очереди расшифровок и «брошен» в списке у РОПа.

Здесь же закреплены ограничения схемы, на которых держится склейка: один
телефон ведёт к одному клиенту, одна карточка принадлежит одному клиенту,
одно событие портала лежит в ленте один раз.
"""

import sqlite3
from datetime import datetime, timezone

import pytest

from clients.build import _write_totals
from clients.events import Event

from clients.schema import (
    CLIENT_CHILD_TABLES,
    TRIAGE_LABELS,
    TRIAGE_ORDER,
    TRIAGE_UNKNOWN,
    clients_session,
    init_clients_db,
)

NOT_COUNTED = (
    "silence_days",
    "calls_total",
    "calls_with_transcript",
    "calls_pending",
    "comments_total",
    "comments_by_assignee",
    "comments_by_assignee_30d",
    "next_step_overdue",
)


@pytest.fixture
def book(tmp_path):
    """Пустая база клиентов на тест."""
    db_path = tmp_path / "clients.db"
    init_clients_db(db_path)
    return db_path


def test_counters_start_unknown_not_zero(book):
    """Заведённый клиент не утверждает, что у него ноль звонков и писем."""
    with clients_session(book) as conn:
        conn.execute("INSERT INTO clients(client_key) VALUES ('p:+79000000001')")

    with clients_session(book, readonly=True) as conn:
        row = conn.execute(
            "SELECT * FROM clients WHERE client_key = 'p:+79000000001'"
        ).fetchone()

    for column in NOT_COUNTED:
        assert row[column] is None, (
            f"{column} обязан читаться как «не считано», а не как посчитанный ноль"
        )


def test_a_client_nobody_triaged_says_so(book):
    """Состояние по умолчанию — девятое, служебное, а не одно из восьми правил."""
    with clients_session(book) as conn:
        conn.execute("INSERT INTO clients(client_key) VALUES ('c:17')")

    with clients_session(book, readonly=True) as conn:
        state = conn.execute(
            "SELECT triage_state FROM clients WHERE client_key = 'c:17'"
        ).fetchone()[0]

    assert state == TRIAGE_UNKNOWN
    assert state not in TRIAGE_ORDER[:8], (
        "молчание не должно выглядеть как сработавшее правило раздела 6"
    )


def test_the_sort_order_covers_every_state():
    """Порядок сортировки и словарь подписей описывают один и тот же набор.

    Список, оторванный от набора значений, расходится с ним молча: новое
    состояние просто не попадает в сортировку и уезжает в конец экрана, а
    подписи у него нет вовсе.
    """
    assert set(TRIAGE_ORDER) == set(TRIAGE_LABELS)
    assert len(TRIAGE_ORDER) == len(set(TRIAGE_ORDER)), "состояние названо дважды"


def test_every_table_that_names_a_client_is_on_the_move_list(book):
    """Список таблиц для переезда ключа выведен из схемы, а не из памяти.

    При переезде (раздел 2.5) строка прежнего клиента удаляется, и всё, что
    на неё ссылалось, обязано быть перенацелено. Забытая таблица стоит
    потерянной ленты или потерянного разбора — того самого, который нельзя
    пересобрать. Проверка идёт от схемы: добавили таблицу с client_key и не
    внесли её в список — тест падает здесь, а не на боевом переезде.
    """
    with clients_session(book, readonly=True) as conn:
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
            if not row[0].startswith("sqlite_")
        ]
        referring = {
            name
            for name in names
            if any(
                column[1] == "client_key"
                for column in conn.execute(f"PRAGMA table_info({name})").fetchall()
            )
        }

    assert referring - {"clients"} == set(CLIENT_CHILD_TABLES)


def test_one_phone_leads_to_one_client(book):
    """Телефон не может числиться за двумя клиентами.

    Это и есть конфликт раздела 2.4. Схема обязана не дать записать его
    молча: полагаться на внимательность сборщика тут нельзя, потому что
    ошибка проявится не при записи, а через месяц — чужим разбором в
    карточке клиента.
    """
    with clients_session(book) as conn:
        conn.execute(
            "INSERT INTO client_aliases(client_key, alias_type, alias_value) "
            "VALUES ('c:11', 'phone', '+79000000002')"
        )

    with pytest.raises(sqlite3.IntegrityError):
        with clients_session(book) as conn:
            conn.execute(
                "INSERT INTO client_aliases(client_key, alias_type, alias_value) "
                "VALUES ('c:12', 'phone', '+79000000002')"
            )


def test_one_card_belongs_to_one_client(book):
    """Сделка принадлежит ровно одному клиенту."""
    with clients_session(book) as conn:
        conn.execute(
            "INSERT INTO client_links(client_key, entity_type, entity_id) "
            "VALUES ('c:11', 'deal', 4242)"
        )

    with pytest.raises(sqlite3.IntegrityError):
        with clients_session(book) as conn:
            conn.execute(
                "INSERT INTO client_links(client_key, entity_type, entity_id) "
                "VALUES ('c:12', 'deal', 4242)"
            )


def test_a_deal_and_a_lead_with_the_same_number_are_different_cards(book):
    """Ключ карточки составной: номера сделок и лидов пересекаются."""
    with clients_session(book) as conn:
        conn.execute(
            "INSERT INTO client_links(client_key, entity_type, entity_id) "
            "VALUES ('c:11', 'deal', 7), ('c:11', 'lead', 7)"
        )

    with clients_session(book, readonly=True) as conn:
        count = conn.execute("SELECT COUNT(*) FROM client_links").fetchone()[0]

    assert count == 2


def test_a_late_transcript_can_replace_the_event_it_belongs_to(book):
    """Расшифровка, приехавшая через сутки, дописывается в то же событие.

    Проверяется ограничение схемы, которое делает это возможным:
    UNIQUE (kind, source_id). Повтор без ON CONFLICT обязан падать, а с ним
    — обновлять строку, а не заводить вторую. Сам загрузчик пишет upsert'ом
    (раздел 3.2) и приедет отдельным шагом; здесь закреплена та половина
    правила, которая живёт в DDL.
    """
    insert = (
        "INSERT INTO client_events(client_key, at, kind, source_id, payload_json) "
        "VALUES ('c:11', '2026-09-20T10:00:00+00:00', 'call', '900', ?)"
    )
    with clients_session(book) as conn:
        conn.execute(insert, ('{"transcript_status": "queued"}',))

    with pytest.raises(sqlite3.IntegrityError):
        with clients_session(book) as conn:
            conn.execute(insert, ('{"transcript_status": "ok"}',))

    with clients_session(book) as conn:
        conn.execute(
            insert + " ON CONFLICT(kind, source_id) DO UPDATE SET "
            "payload_json = excluded.payload_json",
            ('{"transcript_status": "ok"}',),
        )

    with clients_session(book, readonly=True) as conn:
        rows = conn.execute(
            "SELECT payload_json FROM client_events WHERE kind = 'call' AND source_id = '900'"
        ).fetchall()

    assert len(rows) == 1, "звонок обязан остаться одним событием ленты"
    assert "ok" in rows[0][0], "текст, приехавший позже, обязан дойти до читателя"


def test_two_comments_in_the_same_second_stay_two_events(book):
    """Личность события — его собственный ID, а не карточка со временем.

    Составной ключ (вид, карточка, время) из первой редакции ТЗ схлопнул бы
    два комментария по одной сделке, написанных в одну секунду, в один.
    """
    with clients_session(book) as conn:
        conn.execute(
            "INSERT INTO client_events(client_key, at, kind, source_id, entity_type,"
            " entity_id) VALUES "
            "('c:11', '2026-09-20T10:00:00+00:00', 'comment', '5001', 'deal', 7),"
            "('c:11', '2026-09-20T10:00:00+00:00', 'comment', '5002', 'deal', 7)"
        )

    with clients_session(book, readonly=True) as conn:
        count = conn.execute("SELECT COUNT(*) FROM client_events").fetchone()[0]

    assert count == 2


def test_a_conflict_seen_again_does_not_multiply(book):
    """Повторный прогон не плодит строк по тому же телефону."""
    seen = (
        "INSERT INTO merge_conflicts(phone_norm, contact_ids_json, detected_at,"
        " last_seen_at) VALUES ('+79000000003', '[11, 12]', ?, ?) "
        "ON CONFLICT(phone_norm) DO UPDATE SET last_seen_at = excluded.last_seen_at"
    )
    with clients_session(book) as conn:
        conn.execute(seen, ("2026-09-20T03:00:00+00:00", "2026-09-20T03:00:00+00:00"))
    with clients_session(book) as conn:
        conn.execute(seen, ("2026-09-21T03:00:00+00:00", "2026-09-21T03:00:00+00:00"))

    with clients_session(book, readonly=True) as conn:
        row = conn.execute("SELECT * FROM merge_conflicts").fetchall()

    assert len(row) == 1
    assert row[0]["detected_at"] == "2026-09-20T03:00:00+00:00", (
        "дата первой встречи конфликта обязана пережить повторный прогон"
    )
    assert row[0]["last_seen_at"] == "2026-09-21T03:00:00+00:00"


def test_a_run_that_did_not_finish_is_not_complete(book):
    """Прогон, не доживший до конца, читается как неполный.

    Умолчание выбрано в безопасную сторону нарочно: обратное объявляло бы
    полным всё, что упало, — и агрегаты перезаписывались бы по половине
    портфеля.
    """
    with clients_session(book) as conn:
        conn.execute(
            "INSERT INTO client_runs(started_at) VALUES ('2026-09-22T03:00:00+00:00')"
        )

    with clients_session(book, readonly=True) as conn:
        row = conn.execute("SELECT complete, finished_at FROM client_runs").fetchone()

    assert row["complete"] == 0
    assert row["finished_at"] is None


# ── счётчик расшифровок: сколько у клиента есть что читать ────────────
#
# Колонка заполняется полным прогоном и уходит в разбор моделью: прежде
# чем спрашивать «что с клиентом», надо знать, на чём отвечать.

RUN_AT = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
KEY = "p:+79000000001"
OTHER = "p:+79000000002"


def _call(key: str, source_id: int) -> Event:
    at = RUN_AT.replace(hour=9).isoformat()
    end = RUN_AT.replace(hour=9, minute=2).isoformat()
    return Event(key, at, "call", str(source_id), "deal", 1, 10, False,
                 {"direction": 2, "start_time": at, "end_time": end})


def _totals(book, events, transcribed):
    """Прогнать запись агрегатов по двум заведённым клиентам.

    Клиентов именно два, и у обоих есть расшифрованные звонки. На одном
    клиенте лента портфеля совпадает с лентой клиента, и перепутать их в
    коде можно было бы незаметно — а цена ошибки в том, что каждому в
    книге достался бы счётчик всей компании.
    """
    with clients_session(book) as conn:
        conn.executemany("INSERT INTO clients(client_key) VALUES (?)",
                         [(KEY,), (OTHER,)])
        _write_totals(
            conn, events,
            {KEY: {"assignee_id": 10}, OTHER: {"assignee_id": 11}},
            run_id=1, now=RUN_AT,
            decisions={}, deals={}, transcribed=transcribed,
            refused_calls=set(), markers=("передумал",),
        )
    with clients_session(book, readonly=True) as conn:
        return {
            row["client_key"]: row for row in conn.execute(
                "SELECT client_key, calls_total, calls_with_transcript FROM clients"
            )
        }


def test_a_full_run_fills_in_how_many_calls_were_transcribed(book):
    """Колонка объявлена давно, а заполнять её стало чем только сейчас.

    Сборка уже ходит за расшифровками ради правил 2 и 3, и множество
    расшифрованных лежит у неё в руках, пока она идёт по клиентам. Оставь
    колонку пустой — и разбор моделью, которому она и нужна, начнётся с
    отдельного похода в ту же базу за тем же самым.

    Числа у двух клиентов разные и оба меньше портфельного: проверяется,
    что каждому досталась его лента, а не общая.
    """
    events = [_call(KEY, 101), _call(KEY, 102), _call(KEY, 103),
              _call(OTHER, 201), _call(OTHER, 202)]

    rows = _totals(book, events, {101, 103, 201, 202})

    assert rows[KEY]["calls_total"] == 3
    assert rows[KEY]["calls_with_transcript"] == 2, "свои два, а не четыре по портфелю"
    assert rows[OTHER]["calls_with_transcript"] == 2


def test_a_run_without_the_transcript_cache_leaves_the_counter_unknown(book):
    """База аудита не открылась — колонка остаётся пустой, а не нулевой.

    Ноль тут читался бы как «у клиента нет ни одной расшифровки», и разбор
    сказал бы про живого клиента с десятью записанными разговорами, что
    отвечать по нему не на чем. Прогон при этом идёт: расшифровки —
    довесок к портфелю, а не портфель.
    """
    rows = _totals(book, [_call(KEY, 101), _call(OTHER, 201)], None)

    assert rows[KEY]["calls_total"] == 1
    assert rows[KEY]["calls_with_transcript"] is None
    assert rows[OTHER]["calls_with_transcript"] is None
