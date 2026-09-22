"""Клиент, сменивший ключ, увозит с собой всё, что о нём знали.

Ключ — не имя клиента, а способ его собрать: телефон, контакт или сделка.
Всё это правят в CRM, и ключ меняется без всякого участия человека —
исправили номер, признали контрагента агентом, вписали тот же телефон
второму контакту.

Переезд обязан быть полным. Карточки, лента и псевдонимы пересобираются из
витрины и портала, и потерять их значит подождать до утра. Разбор
пересобрать нельзя: его написал человек или модель. Поэтому правило
переезда сформулировано от разбора — прежняя строка удаляется только тогда,
когда всё доехало до одного нового ключа, и никогда просто потому, что
карточек у неё не осталось.
"""

import pytest

from clients.keys import Decision
from clients.merges import apply_migrations, current_links, plan_migrations
from clients.schema import CLIENT_CHILD_TABLES, clients_session, init_clients_db

AT = "2026-09-22T03:00:00+00:00"
OLD = "p:+79001112233"
NEW = "p:+79002223344"

# По одной строке в каждую дочернюю таблицу. Список берётся из схемы, а не
# переписывается здесь: тест обязан падать, когда таблицу завели и забыли.
CHILD_ROWS = {
    "client_links":
        ("INSERT INTO client_links(client_key, entity_type, entity_id)"
         " VALUES (?, 'deal', 7)"),
    "client_aliases":
        ("INSERT INTO client_aliases(client_key, alias_type, alias_value)"
         " VALUES (?, 'contact', '77')"),
    "client_events":
        ("INSERT INTO client_events(client_key, at, kind, source_id)"
         " VALUES (?, '2026-09-21T10:00:00+00:00', 'comment', '5001')"),
    "client_reviews":
        ("INSERT INTO client_reviews(client_key, created_at, reviewed_through, summary)"
         " VALUES (?, '2026-09-10T10:00:00+00:00', '2026-09-10T09:00:00+00:00',"
         " 'разобрано')"),
}


@pytest.fixture
def book(tmp_path):
    db_path = tmp_path / "clients.db"
    init_clients_db(db_path)
    return db_path


def _client(conn, key: str) -> None:
    conn.execute("INSERT INTO clients(client_key, name) VALUES (?, 'Пётр')", (key,))


def _child_rows(conn, key: str) -> None:
    for statement in CHILD_ROWS.values():
        conn.execute(statement, (key,))


def _decision(deal_id: int, key: str, reason: str = "склейка по телефону") -> Decision:
    return Decision(
        deal_id=deal_id, key=key, key_reason=reason, contact_id=77,
        phone_norm=None, phone_raw="", phone_valid=False,
        is_agent=False, agent_reason="",
    )


def _keys_of(conn, table: str) -> list[str]:
    return [row[0] for row in conn.execute(f"SELECT client_key FROM {table}")]


def test_every_child_table_is_carried_to_the_new_key(book):
    """Ни одна таблица не остаётся у мёртвого ключа.

    Список таблиц берётся из схемы. Завели новую с `client_key`, забыли
    внести в перенос — падает здесь, а не на боевом переезде потерянным
    разбором.
    """
    with clients_session(book) as conn:
        _client(conn, OLD)
        _child_rows(conn, OLD)

    with clients_session(book) as conn:
        moved = apply_migrations(
            conn, [_migration()], at=AT,
        )
        assert moved == 1
        for table in CLIENT_CHILD_TABLES:
            keys = _keys_of(conn, table)
            assert OLD not in keys, f"{table} осталась у прежнего ключа"
            assert NEW in keys, f"{table} не доехала до нового ключа"


def _migration():
    from clients.merges import Migration

    return Migration(old_key=OLD, new_key=NEW, reason="телефон исправлен", deals=(7,))


def test_the_old_row_is_gone_and_the_old_key_still_finds_the_client(book):
    """Прежней строки нет, но прежний ключ по-прежнему ведёт к человеку."""
    with clients_session(book) as conn:
        _client(conn, OLD)
        _child_rows(conn, OLD)

    with clients_session(book) as conn:
        apply_migrations(conn, [_migration()], at=AT)

    with clients_session(book, readonly=True) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM clients WHERE client_key = ?", (OLD,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT client_key FROM client_aliases WHERE alias_type = 'key'"
            " AND alias_value = ?", (OLD,)
        ).fetchone()[0] == NEW
        merge = conn.execute("SELECT old_key, new_key, reason, detected_at"
                             " FROM client_merges").fetchone()
        assert tuple(merge) == (OLD, NEW, "телефон исправлен", AT)


def test_a_review_read_by_one_history_is_not_declared_read_by_another(book):
    """Отметка прочитанного не сдвигается переездом.

    У нового ключа события могут быть новее прочитанного. Разбор обязан
    честно оказаться устаревшим: `stale` считается сравнением
    `last_event_at > reviewed_through`, и подпереть его переносом отметки
    значит объявить прочитанным то, что прочитано по другой истории.
    """
    with clients_session(book) as conn:
        _client(conn, OLD)
        _child_rows(conn, OLD)
        conn.execute(
            "INSERT INTO client_events(client_key, at, kind, source_id)"
            " VALUES (?, '2026-09-21T18:00:00+00:00', 'call', '900')", (NEW,)
        )

    with clients_session(book) as conn:
        apply_migrations(conn, [_migration()], at=AT)

    with clients_session(book, readonly=True) as conn:
        review = conn.execute(
            "SELECT client_key, reviewed_through FROM client_reviews"
        ).fetchone()
        newest = conn.execute(
            "SELECT MAX(at) FROM client_events WHERE client_key = ?", (NEW,)
        ).fetchone()[0]

    assert review["client_key"] == NEW, "разбор обязан доехать"
    assert review["reviewed_through"] == "2026-09-10T09:00:00+00:00"
    assert newest > review["reviewed_through"], "то есть разбор устарел, и это видно"


@pytest.mark.parametrize("reverse", [False, True], ids=["как-есть", "наоборот"])
def test_two_keys_that_swap_places_do_not_drag_each_other(tmp_path, reverse):
    """Ключи умеют меняться местами, и перенос обязан это пережить.

    Телефон, перешедший с одного контакта на другой, даёт разом
    `p:X → c:77` и `c:88 → p:X`. Построчный перенос утащил бы строки
    второго ключа следом за первым: сначала c:88 → p:X, потом всё, что
    лежит на p:X, — включая только что приехавшее — уехало бы на c:77.

    Оба порядка проверяются нарочно. Первая редакция этого теста подавала
    переезды в безопасном порядке и проходила с построчным переносом —
    то есть не проверяла ничего. Опасный порядок как раз и есть тот,
    который выдаёт `plan_migrations`: он сортирует по прежнему ключу, а
    `c:88` идёт раньше `p:+7…`.
    """
    from clients.merges import Migration

    db_path = tmp_path / f"clients-{int(reverse)}.db"
    init_clients_db(db_path)
    phone, first, second = "p:+79001112233", "c:77", "c:88"
    with clients_session(db_path) as conn:
        for key in (phone, second):
            _client(conn, key)
        conn.execute(
            "INSERT INTO client_events(client_key, at, kind, source_id)"
            " VALUES (?, '2026-09-01T10:00:00+00:00', 'comment', 'из-телефона')", (phone,)
        )
        conn.execute(
            "INSERT INTO client_events(client_key, at, kind, source_id)"
            " VALUES (?, '2026-09-01T11:00:00+00:00', 'comment', 'из-контакта')", (second,)
        )

    moves = [
        Migration(phone, first, "телефон ушёл к другому", (1,)),
        Migration(second, phone, "телефон пришёл", (2,)),
    ]
    with clients_session(db_path) as conn:
        apply_migrations(conn, list(reversed(moves)) if reverse else moves, at=AT)

    with clients_session(db_path, readonly=True) as conn:
        where = dict(conn.execute("SELECT source_id, client_key FROM client_events"))

    assert where["из-телефона"] == first
    assert where["из-контакта"] == phone, (
        "строка, приехавшая на освобождённый ключ, не должна уехать дальше"
    )


def test_a_key_that_is_also_a_destination_is_not_deleted(book):
    """Освобождённый и тут же занятый ключ сносить нельзя.

    Его хозяйство уже перенацелено на него же; удалить строку значит
    оставить ленту и разбор у ключа, которого нет, до ближайшей вставки
    пересборки.
    """
    from clients.merges import Migration

    phone, first, second = "p:+79001112233", "c:77", "c:88"
    with clients_session(book) as conn:
        for key in (phone, second):
            _client(conn, key)

    with clients_session(book) as conn:
        apply_migrations(conn, [
            Migration(phone, first, "", (1,)),
            Migration(second, phone, "", (2,)),
        ], at=AT)

    with clients_session(book, readonly=True) as conn:
        left = {row[0] for row in conn.execute("SELECT client_key FROM clients")}

    assert left == {phone}, "уехавший c:88 снесён, занятый заново p: остался"


def test_a_key_alias_never_points_at_itself(book):
    """Ключ, вернувшийся к себе, не заводит псевдоним на самого себя."""
    from clients.merges import Migration

    a, b = "c:77", "p:+79001112233"
    with clients_session(book) as conn:
        _client(conn, a)
    with clients_session(book) as conn:
        apply_migrations(conn, [Migration(a, b, "телефон появился", (1,))], at=AT)
    with clients_session(book) as conn:
        _client(conn, a)
        apply_migrations(conn, [Migration(b, a, "телефон оказался чужим", (1,))], at=AT)

    with clients_session(book, readonly=True) as conn:
        rows = conn.execute(
            "SELECT alias_value, client_key FROM client_aliases WHERE alias_type = 'key'"
        ).fetchall()

    assert all(row["alias_value"] != row["client_key"] for row in rows), (
        "псевдоним, ведущий сам на себя, ничего не ищет и только путает"
    )


def test_a_card_outside_the_portfolio_blocks_the_move(book):
    """Сделка, которой нет в решениях, — не повод объявлять переезд.

    Про неё ничего не известно: может, она выпала из окна витрины, а может,
    прогон её не увидел. Удалять по такому поводу строку с разбором нельзя.
    """
    links = {7: OLD, 8: OLD}
    plan = plan_migrations(links, {7: _decision(7, NEW)})

    assert plan.migrations == ()
    assert plan.split == ()


def test_cards_that_scatter_leave_the_client_alone(book):
    """Раскол — не переезд: куда девать разбор про клиента целиком, правила нет."""
    links = {7: OLD, 8: OLD}
    plan = plan_migrations(links, {7: _decision(7, NEW), 8: _decision(8, "c:88")})

    assert plan.migrations == ()
    assert plan.split == (OLD,)


def test_a_key_that_did_not_change_is_not_a_move(book):
    """Обычный прогон, где ничего не менялось, переездов не порождает."""
    plan = plan_migrations({7: OLD}, {7: _decision(7, OLD)})

    assert plan.migrations == ()


def test_a_whole_key_moving_together_is_a_move(book):
    """Все карточки прежнего ключа ушли в один новый — это и есть переезд."""
    links = {7: OLD, 8: OLD}
    decisions = {7: _decision(7, NEW), 8: _decision(8, NEW)}

    plan = plan_migrations(links, decisions)

    assert len(plan.migrations) == 1
    move = plan.migrations[0]
    assert (move.old_key, move.new_key, move.deals) == (OLD, NEW, (7, 8))
    assert move.reason == "склейка по телефону"


def test_the_links_come_from_the_book_itself(book):
    """Прежняя привязка читается из базы и только по сделкам."""
    with clients_session(book) as conn:
        conn.execute(
            "INSERT INTO client_links(client_key, entity_type, entity_id) VALUES"
            " (?, 'deal', 7), (?, 'lead', 7)", (OLD, "c:99"),
        )

    with clients_session(book, readonly=True) as conn:
        assert current_links(conn) == {7: OLD}


def test_nothing_moves_when_nothing_moved(book):
    """Пустой список переездов не трогает базу и не оставляет мусора."""
    with clients_session(book) as conn:
        _client(conn, OLD)
        assert apply_migrations(conn, [], at=AT) == 0

    with clients_session(book, readonly=True) as conn:
        assert _keys_of(conn, "clients") == [OLD]
        assert conn.execute("SELECT COUNT(*) FROM client_merges").fetchone()[0] == 0
