"""Прогон, который не всё увидел, не трогает портфель вовсе.

ТЗ формулирует мягче — «агрегаты не перезаписываются» (раздел 4, находка
V13), — и для клиентского слоя этого мало. Контакт, за которым не сходили,
не имеет телефона; его сделки уезжают с ключа `p:` на `c:`; а это переезд
ключа, при котором прежняя строка клиента УДАЛЯЕТСЯ, а разбор
перенацеливается. Совершить это из-за таймаута портала и «откатить
завтра» нельзя: откатывать уже нечего.

Поэтому правило строже: не добрали контакты — портфель остаётся вчерашним,
а неудача записывается в журнал прогонов. Здесь же закреплено всё
остальное, что делает прогон целиком.
"""

from datetime import datetime, timedelta, timezone

import pytest

from analytics.schema import analytics_session
from clients import build as build_mod
from clients.schema import clients_session, init_clients_db

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
SYNCED = NOW.isoformat()


def _at(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


class _Portal:
    """Портал, который отвечает по списку ID и умеет не ответить."""

    def __init__(self, contacts, *, fail=()):
        self.contacts = contacts
        self.fail = set(fail)
        self.calls = 0
        self.probe = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def call(self, method, params=None):
        assert method == "crm.contact.list"
        self.calls += 1
        if self.probe:
            self.probe()
        ids = [int(value) for value in (params or {})["filter"]["@ID"]]
        if self.fail & set(ids):
            raise RuntimeError("портал не ответил")
        return [row for cid, row in self.contacts.items() if cid in ids]


CONTACTS = {
    77: {"ID": "77", "NAME": "Пётр", "LAST_NAME": "Сидоров",
         "PHONE": [{"VALUE": "+79001112233"}]},
    88: {"ID": "88", "NAME": "Анна", "LAST_NAME": "Круглова",
         "PHONE": [{"VALUE": "+79004445566"}]},
}


@pytest.fixture
def book(tmp_path, monkeypatch):
    from config import get_settings

    path = tmp_path / "clients.db"
    monkeypatch.setenv("CLIENTS_DB_PATH", str(path))
    get_settings.cache_clear()
    init_clients_db(path)
    return path


@pytest.fixture
def portfolio(analytics_db):
    with analytics_session() as conn:
        conn.execute(
            "INSERT INTO dim_user(user_id, name, last_name, department_id,"
            " synced_at) VALUES (10, 'Пётр', 'Брокеров', 5, ?)", (SYNCED,),
        )
        conn.execute(
            "INSERT INTO dim_stage(stage_id, category_id, name, synced_at)"
            " VALUES ('C18:NEW', 18, 'Подбор', ?)", (SYNCED,),
        )
        for deal_id, contact_id in ((7, 77), (8, 88)):
            conn.execute(
                "INSERT INTO fact_deal(deal_id, title, category_id, stage_id,"
                " assigned_by_id, contact_id, date_create, is_deleted, synced_at)"
                " VALUES (?, ?, 18, 'C18:NEW', 10, ?, ?, 0, ?)",
                (deal_id, f"сделка {deal_id}", contact_id, _at(40), SYNCED),
            )
        conn.execute(
            "INSERT INTO fact_comment(comment_id, entity_type, entity_id,"
            " author_id, body, is_auto, created_at, synced_at)"
            " VALUES (1, 'deal', 7, 10, 'созвонились', 0, ?, ?)", (_at(2), SYNCED),
        )
        conn.execute(
            "INSERT INTO fact_activity(activity_id, owner_type_id, owner_id,"
            " provider_type_id, author_id, created_at, completed, synced_at)"
            " VALUES (900, 3, 77, 'CALL', 10, ?, 1, ?)", (_at(3), SYNCED),
        )
    return analytics_db


@pytest.fixture
def portal(monkeypatch):
    holder = {}

    def _make(contacts=None, *, fail=()):
        made = _Portal(contacts if contacts is not None else CONTACTS, fail=fail)
        holder["portal"] = made
        monkeypatch.setattr(build_mod, "_portal", lambda settings: made)
        monkeypatch.setattr(build_mod, "_contact_type_names", lambda degraded: {})
        return made

    _make()
    return _make


def _rows(path, sql, params=()):
    with clients_session(path, readonly=True) as conn:
        return [dict(row) for row in conn.execute(sql, params)]


def test_a_whole_run_fills_the_book(book, portfolio, portal):
    """Полный прогон заводит клиентов, карточки, псевдонимы, ленту и журнал."""
    summary = build_mod.build(now=NOW)

    assert summary["written"] is True and summary["complete"] is True
    clients = _rows(book, "SELECT * FROM clients ORDER BY client_key")
    assert [row["client_key"] for row in clients] == [
        "p:+79001112233", "p:+79004445566",
    ]
    first = clients[0]
    assert first["name"] == "Сидоров Пётр", "имя берётся из карточки контакта"
    assert first["assignee_id"] == 10 and first["department_id"] == 5
    assert first["silence_days"] == 2, "касание — комментарий двухдневной давности"
    assert first["comments_by_assignee"] == 1
    assert first["calls_total"] == 1, "звонок на контакте обязан дойти до клиента"
    assert first["calls_with_transcript"] is None, "расшифровки — следующий шаг"

    run = _rows(book, "SELECT * FROM client_runs")[0]
    assert run["complete"] == 1 and run["finished_at"] is not None
    assert run["cards"] == 2
    assert first["aggregates_run_id"] == run["id"]


def test_an_incomplete_run_does_not_touch_the_portfolio(book, portfolio, portal):
    """Прогон, не добравший контакты, не пишет вообще ничего.

    ТЗ говорит мягче — «агрегаты не перезаписываются», — и этого мало.
    Контакт, за которым не сходили, не имеет телефона, значит его сделки
    уезжают с ключа `p:` на `c:`. А это переезд ключа: прежняя строка
    клиента УДАЛЯЕТСЯ, разбор перенацеливается. Совершить это из-за
    таймаута портала и «откатить завтра» нельзя — откатывать уже нечего.
    """
    build_mod.build(now=NOW)
    before = _rows(book, "SELECT * FROM clients ORDER BY client_key")

    portal(fail=(88,))
    summary = build_mod.build(now=NOW + timedelta(days=1))

    assert summary["complete"] is False and summary["written"] is False
    after = _rows(book, "SELECT * FROM clients ORDER BY client_key")
    assert after == before, "вчерашний, но верный портфель лучше выдуманного"
    assert _rows(book, "SELECT COUNT(*) AS n FROM client_merges")[0]["n"] == 0, (
        "переезда ключа из-за таймаута портала быть не должно"
    )

    runs = _rows(book, "SELECT complete, errors, finished_at FROM client_runs ORDER BY id")
    assert [row["complete"] for row in runs] == [1, 0]
    assert runs[1]["errors"] == 2
    assert runs[1]["finished_at"] is not None, "неудача обязана быть записана, а не пропасть"


def test_a_late_transcript_survives_the_nightly_rebuild(book, portfolio, portal):
    """Расшифровка, дописанная в событие, переживает пересборку ленты.

    Тело события пересобирается из витрины каждую ночь. Замена payload
    стирала бы самую дорогую часть системы молча, каждым прогоном `[V15]`.
    """
    build_mod.build(now=NOW)
    with clients_session(book) as conn:
        conn.execute(
            "UPDATE client_events SET payload_json ="
            " json_set(payload_json, '$.transcript', 'текст разговора')"
            " WHERE kind = 'call' AND source_id = '900'"
        )

    build_mod.build(now=NOW + timedelta(days=1))

    event = _rows(book, "SELECT payload_json FROM client_events"
                        " WHERE kind = 'call' AND source_id = '900'")
    assert len(event) == 1, "звонок обязан остаться одним событием"
    assert "текст разговора" in event[0]["payload_json"]


def test_a_deal_that_left_the_portfolio_stops_counting(book, portfolio, portal):
    """Сделка, выпавшая из воронок, перестаёт числиться за клиентом."""
    build_mod.build(now=NOW)
    with analytics_session() as conn:
        conn.execute("UPDATE fact_deal SET is_deleted = 1 WHERE deal_id = 8")

    build_mod.build(now=NOW + timedelta(days=1))

    links = _rows(book, "SELECT entity_id FROM client_links")
    assert [row["entity_id"] for row in links] == [7]


def test_a_phone_erased_in_the_portal_stops_gluing(book, portfolio, portal):
    """Стёртый телефон перестаёт вести к клиенту.

    Псевдонимы выводятся из портфеля без остатка, поэтому переписываются
    целиком. Дозапись сохранила бы номер навсегда и продолжала бы склеивать
    людей, которых уже ничего не связывает.
    """
    two_numbers = {
        77: {"ID": "77", "NAME": "Пётр",
             "PHONE": [{"VALUE": "+79001112233"}, {"VALUE": "+79007778899"}]},
        88: CONTACTS[88],
    }
    portal(two_numbers)
    build_mod.build(now=NOW)
    assert ("phone", "+79007778899") in {
        (row["alias_type"], row["alias_value"])
        for row in _rows(book, "SELECT alias_type, alias_value FROM client_aliases")
    }

    portal(CONTACTS)
    build_mod.build(now=NOW + timedelta(days=1))

    aliases = {
        (row["alias_type"], row["alias_value"])
        for row in _rows(book, "SELECT alias_type, alias_value FROM client_aliases")
    }
    assert ("phone", "+79007778899") not in aliases


def test_a_limited_run_never_writes(book, portfolio, portal):
    """Прогон на части портфеля ключи не пишет.

    Признак агента копится по контакту, поэтому над половиной карточек
    ключи получаются другими. Записать их значит развести одного человека
    по двум клиентам и отцепить разбор.
    """
    summary = build_mod.build(now=NOW, limit=1)

    assert summary["written"] is False
    assert _rows(book, "SELECT * FROM clients") == []
    assert _rows(book, "SELECT * FROM client_runs") == []


def test_a_dry_run_looks_but_does_not_touch(book, portfolio, portal):
    """Сухой прогон считает всё и не пишет ничего."""
    summary = build_mod.build(now=NOW, dry_run=True)

    assert summary["clients"] == 2 and summary["events"] == 2
    assert summary["written"] is False
    assert _rows(book, "SELECT * FROM clients") == []


def test_the_run_is_opened_before_the_work_not_after(book, portfolio, portal):
    """Упавший посередине прогон остаётся в журнале неполным, а не пропадает.

    Журнал открывается первой же транзакцией. Прогон, который не дожил до
    конца, обязан читаться как неполный: иначе вопрос «почему числа
    вчерашние» останется без ответа.
    """
    made = portal()

    def _boom():
        raise RuntimeError("портал упал целиком")

    made.probe = _boom
    with pytest.raises(RuntimeError):
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(build_mod, "fetch_contacts",
                          lambda *a, **k: (_ for _ in ()).throw(RuntimeError("упал")))
            build_mod.build(now=NOW)

    runs = _rows(book, "SELECT complete, finished_at FROM client_runs")
    assert len(runs) == 1
    assert runs[0]["complete"] == 0 and runs[0]["finished_at"] is None


def test_the_portal_is_not_called_inside_a_write_transaction(book, portfolio, portal):
    """Пока идёт разговор с порталом, книга клиентов свободна.

    То же правило, которым живёт ETL после сентябрьских поломок: транзакция
    записи не переживает обращение к порталу. Проверяется соседом, который
    в момент вызова портала берёт запись на себя, — с терпением в 50 мс,
    чтобы ожидание не сошло за успех.
    """
    import sqlite3

    made = portal()
    seen = []

    def _probe():
        rival = sqlite3.connect(str(book), timeout=0.05)
        try:
            rival.execute("BEGIN IMMEDIATE")
            seen.append(True)
        finally:
            rival.rollback()
            rival.close()

    made.probe = _probe
    build_mod.build(now=NOW)

    assert seen and made.calls == len(seen), (
        "на каждый вызов портала книга клиентов обязана быть свободна"
    )


def test_the_client_belongs_to_the_broker_of_his_newest_deal(book, portfolio, portal):
    """Ответственный клиента — тот, кто ведёт его самую свежую сделку.

    У клиента их может быть несколько и с разными брокерами. «Свежая»
    отвечает на вопрос «кто ведёт его сейчас»; «первая» отвечает на вопрос,
    который никто не задавал, и оставила бы клиента за уволившимся.
    """
    with analytics_session() as conn:
        conn.execute(
            "INSERT INTO dim_user(user_id, name, last_name, department_id,"
            " synced_at) VALUES (20, 'Анна', 'Новикова', 9, ?)", (SYNCED,),
        )
        conn.execute(
            "INSERT INTO fact_deal(deal_id, title, category_id, stage_id,"
            " assigned_by_id, contact_id, date_create, is_deleted, synced_at)"
            " VALUES (12, 'вторая сделка', 18, 'C18:NEW', 20, 77, ?, 0, ?)",
            (_at(2), SYNCED),
        )

    build_mod.build(now=NOW)

    row = _rows(book, "SELECT * FROM clients WHERE client_key = 'p:+79001112233'")[0]
    assert row["assignee_id"] == 20, "сделка двухдневной давности свежее сорокадневной"
    assert row["assignee_name"] == "Новикова Анна"
    assert row["department_id"] == 9, "отдел берётся у того же человека"
    links = _rows(book, "SELECT entity_id FROM client_links"
                        " WHERE client_key = 'p:+79001112233' ORDER BY entity_id")
    assert [row["entity_id"] for row in links] == [7, 12], "обе сделки у одного клиента"


def test_a_dry_run_leaves_no_file_behind(tmp_path, portfolio, portal, monkeypatch):
    """Сухой прогон не оставляет за собой даже пустой базы.

    Соседний тест этого не видел: он пользуется фикстурой, которая заводит
    базу заранее, и проверяет лишь пустоту таблиц. «Ничего не пишет»
    означает в том числе «не создаёт файла» — иначе на боевом сервере
    после диагностического прогона появляется `data/clients.db`, которого
    никто не просил, и следующий человек гадает, откуда он взялся.
    """
    from config import get_settings

    path = tmp_path / "ни-разу-не-открытая.db"
    monkeypatch.setenv("CLIENTS_DB_PATH", str(path))
    get_settings.cache_clear()

    summary = build_mod.build(now=NOW, dry_run=True)

    assert summary["written"] is False
    assert not path.exists(), "сухой прогон оставил за собой файл базы"
