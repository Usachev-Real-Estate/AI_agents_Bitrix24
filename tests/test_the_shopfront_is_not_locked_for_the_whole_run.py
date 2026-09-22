"""Прогон ETL не имеет права держать витрину занятой от начала до конца.

Писать в витрину может кто-то один: SQLite в WAL второго писателя не пускает,
а busy_timeout у всех соединений — десять секунд. Пока прогон держал одну
транзакцию от первой записи до последней, окно занятости равнялось длине
прогона: полторы минуты у инкремента, полчаса у полной сверки. Любой сосед,
попавший в это окно, умирал — 21.09 так пропала целая ночь чтения
комментариев: ноль записей, четыреста оплаченных ответов модели впустую.

Поэтому прогон коммитит по этапам. Цена названа честно: он перестал быть
одной транзакцией и, упав на середине, оставляет обновлённым то, что успел.
Безопасно это ровно потому, что водяной знак сущности ставится ПОСЛЕ её
данных: упавший прогон не сдвинет границу окна, и следующий возьмёт то же
окно заново, а записи идут upsert'ом.

Проверяется КАЖДАЯ граница этапа, а не одна.

Первая редакция этого файла ставила наблюдателя в одну точку — на входе в
этап комментариев — и на том успокаивалась. Разбор показал, чего она стоила:
из шести вызовов release() можно было удалить любые пять, и все пять тестов
оставались зелёными. Наблюдатель видел только последний коммит перед собой,
а тот вбирал в себя все предыдущие, так что отличить шесть коммитов от
одного файл не мог в принципе. Удаление release() между сделками и историей
стадий — то есть слияние двух самых долгих этапов загрузки в одну
транзакцию — прошло бы мимо тестов молча и вернуло бы ровно ту беду, ради
которой всё и писалось.

Теперь наблюдатель приходит на вход КАЖДОГО этапа и спрашивает две вещи
сразу: свободна ли запись и видно ли уже то, что записал предыдущий этап.
Второй вопрос важнее первого: отпустить замок, не закоммитив, — это не
починка, а её видимость.

Без наблюдателя остаётся один вызов, последний: после release() «водяные
знаки» прогон больше ничего не пишет, и наблюдать там нечего — этот коммит
всё равно сделает выход из etl_run. Он стоит в коде не ради окна, а ради
того, кто завтра допишет туда восьмой этап.
"""

import sqlite3

import analytics  # noqa: F401  — кладёт src/analytics на sys.path
import etl
import pytest
from schema import analytics_session
from stages import ENTITY_ACTIVITY, ENTITY_DEAL, ENTITY_LEAD

DEAL = 101

# Этап -> счётчик, доказывающий, что этап закоммичен. Имя этапа здесь то же,
# что и в release(): тест называет границу теми же словами, что и код.
MARKS = {
    "справочники": "SELECT COUNT(*) FROM dim_pipeline",
    "сделки и лиды": "SELECT COUNT(*) FROM fact_deal",
    "история стадий": "SELECT COUNT(*) FROM fact_stage_event",
    "дела": "SELECT COUNT(*) FROM fact_activity",
    "комментарии": "SELECT COUNT(*) FROM fact_comment",
    "удалённые карточки": "SELECT COUNT(*) FROM fact_deal WHERE is_deleted = 1",
}

# Границы, у которых есть наблюдаемое окно в обычном прогоне. «Удалённые
# карточки» сюда не входят: этот этап бывает только у полной сверки.
WATCHED = ("справочники", "сделки и лиды", "история стадий", "дела", "комментарии")


class _NoPortal:
    """Соединения с порталом нет: проверяется устройство прогона, не загрузка."""

    request_count = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


def _watch(db_path, seen, stage):
    """Что видит и может сосед на входе в очередной этап.

    Сосед — это читатель комментариев: отдельное соединение, короткое
    терпение, единственное желание записать одну строку. Спрашиваем у него
    и про замок, и про видимость: в WAL читатель видит только закоммиченное,
    так что нулевой счётчик означал бы «замок отпущен, а данных ещё нет».
    """
    other = sqlite3.connect(db_path, timeout=0.05)
    other.execute("PRAGMA journal_mode=WAL")
    record = {"visible": {}}
    try:
        for name, query in MARKS.items():
            record["visible"][name] = other.execute(query).fetchone()[0]
        other.execute("BEGIN IMMEDIATE")
        other.execute(
            "INSERT INTO dim_source(source_id, name, synced_at) VALUES (?, ?, 'x')",
            (f"N{len(seen)}", "сосед"),
        )
        other.commit()
        record["wrote"] = True
    except sqlite3.OperationalError as error:
        record["wrote"] = False
        record["error"] = str(error)
    finally:
        other.close()
    seen[stage] = record


def _run_watched(db_path, monkeypatch, kind):
    """Прогон из заглушек, где каждый этап пишет метку, а следующий смотрит.

    Заглушки, а не настоящий клиент портала: проверяется не то, что
    загружается, а то, когда отпускается замок. Каждая заглушка сперва
    зовёт наблюдателя — то есть спрашивает про ПРЕДЫДУЩИЙ этап, — и только
    потом пишет свою метку.
    """
    seen: dict = {}

    def probe(stage):
        _watch(db_path, seen, stage)

    def _dimensions(_client, conn, _settings):
        conn.execute(
            "INSERT INTO dim_pipeline(category_id, name, is_active, sort, synced_at)"
            " VALUES (18, 'Покупатели', 1, 10, 'x')"
        )
        return [18]

    def _deals(_client, conn, _settings, *, since, modified_since=None):
        probe("справочники")
        conn.execute(
            """
            INSERT INTO fact_deal(deal_id, title, category_id, stage_id,
                assigned_by_id, source_id, opportunity, currency_id, date_create,
                date_modify, closedate, is_closed, is_won, is_lost, contact_id,
                is_deleted, synced_at)
            VALUES (?, 'ВГ 747', 18, 'C18:NEW', 32, 'CALL', 0, 'RUB',
                    '2026-08-01T10:00:00+00:00', '2026-08-01T10:00:00+00:00',
                    NULL, 0, 0, 0, 5000, 0, 'x')
            """,
            (DEAL,),
        )
        return [DEAL]

    def _leads(_client, _conn, *, since, modified_since=None):
        return []

    def _history(_client, conn, _entity, _ids):
        probe("сделки и лиды")
        conn.execute(
            "INSERT INTO fact_stage_event(entity_type, entity_id, category_id,"
            " stage_id, entered_at, left_at, duration_sec, seq)"
            " VALUES ('deal', ?, 18, 'C18:NEW', '2026-08-01T10:00:00+00:00',"
            " NULL, NULL, 1)",
            (DEAL,),
        )

    def _activities(_client, conn, *, since, modified_since=None):
        probe("история стадий")
        conn.execute(
            "INSERT INTO fact_activity(activity_id, owner_type_id, owner_id,"
            " provider_type_id, subject, description, responsible_id, created_at,"
            " completed, synced_at) VALUES (1, 2, ?, 'CALL', 'Звонок', '', 32,"
            " '2026-08-01T10:00:00+00:00', 1, 'x')",
            (DEAL,),
        )
        return 1

    def _comments(_client, conn, *, batch=None):
        probe("дела")
        conn.execute(
            "INSERT INTO fact_comment(comment_id, entity_type, entity_id,"
            " author_id, body, is_auto, created_at, synced_at)"
            " VALUES (1, 'deal', ?, 32, 'Показ был', 0,"
            " '2026-08-01T10:00:00+00:00', 'x')",
            (DEAL,),
        )
        return 1

    def _reconcile(_client, conn, entity, _since):
        if entity == ENTITY_DEAL:
            probe("комментарии")
            conn.execute("UPDATE fact_deal SET is_deleted = 1 WHERE deal_id = ?",
                         (DEAL,))
        return 1

    real_watermark = etl.set_watermark

    def _watermark(conn, entity, *, full_sync=False):
        # Последняя наблюдаемая граница. В обычном прогоне перед знаками идут
        # комментарии, в полной сверке — удалённые карточки.
        if entity == ENTITY_DEAL:
            probe("удалённые карточки" if kind == "full" else "комментарии")
        real_watermark(conn, entity, full_sync=full_sync)

    monkeypatch.setattr(etl, "BitrixClient", lambda *a, **kw: _NoPortal())
    monkeypatch.setattr(etl, "sync_dimensions", _dimensions)
    monkeypatch.setattr(etl, "sync_users", lambda *a, **kw: None)
    monkeypatch.setattr(etl, "sync_deals", _deals)
    monkeypatch.setattr(etl, "sync_leads", _leads)
    monkeypatch.setattr(etl, "sync_stage_history", _history)
    monkeypatch.setattr(etl, "sync_activities", _activities)
    monkeypatch.setattr(etl, "sync_comments", _comments)
    monkeypatch.setattr(etl, "reconcile_deleted", _reconcile)
    monkeypatch.setattr(etl, "set_watermark", _watermark)

    etl.run_sync(kind, since_override="2026-01-01")
    return seen


@pytest.fixture
def backfill(analytics_db, monkeypatch):
    return _run_watched(analytics_db, monkeypatch, "backfill")


@pytest.fixture
def full(analytics_db, monkeypatch):
    return _run_watched(analytics_db, monkeypatch, "full")


# ── Каждая граница этапа: замок свободен и записанное видно ────────────
@pytest.mark.parametrize("stage", WATCHED)
def test_the_write_lock_is_free_at_every_stage_boundary(backfill, stage):
    """Сосед приходит с терпением в 50 миллисекунд на вход каждого этапа.

    Одна точка наблюдения не годилась: последний коммит перед ней вбирал
    все предыдущие, и удаление любого из них проходило незамеченным.
    """
    record = backfill[stage]

    assert record["wrote"], (
        f"витрина занята после этапа «{stage}»: {record.get('error')}"
    )


@pytest.mark.parametrize("stage", WATCHED)
def test_every_stage_is_committed_before_the_next_one_starts(backfill, stage):
    """Отпустить замок мало — записанное должно быть видно, то есть закоммичено.

    Читают витрину отдельным соединением: веб и все прочие. В WAL читатель
    видит только закоммиченное, так что ноль здесь означал бы «замок
    отпущен, а данных ещё нет» — то есть коммита не было.
    """
    assert backfill[stage]["visible"][stage] == 1, (
        f"этап «{stage}» не закоммичен к началу следующего"
    )


def test_the_full_sync_commits_its_deletions_too(full):
    """У полной сверки есть свой этап, которого нет у остальных.

    Пометка удалённых — единственное, что делает только она, и держать её
    в общей транзакции значило бы вернуть получасовое окно занятости
    именно тому прогону, у которого оно и было самым длинным.
    """
    record = full["удалённые карточки"]

    assert record["wrote"], f"витрина занята: {record.get('error')}"
    assert record["visible"]["удалённые карточки"] == 1


def test_a_stage_does_not_leak_the_next_ones_data(backfill):
    """Обратная проверка: наблюдатель смотрит туда, куда думает.

    Если бы он приходил позже, чем заявлено, счётчики следующих этапов были
    бы уже ненулевыми — и оба теста выше зеленели бы, ничего не проверяя.
    """
    assert backfill["справочники"]["visible"]["сделки и лиды"] == 0
    assert backfill["сделки и лиды"]["visible"]["история стадий"] == 0
    assert backfill["история стадий"]["visible"]["дела"] == 0
    assert backfill["дела"]["visible"]["комментарии"] == 0


# ── Цена размена: прогон больше не одна транзакция ─────────────────────
@pytest.fixture
def died_halfway(analytics_db, monkeypatch):
    """Прогон, упавший на этапе комментариев."""
    def _boom(*_a, **_kw):
        raise RuntimeError("портал ответил пятисоткой")

    def _one_deal(_client, conn, _settings, *, since, modified_since=None):
        conn.execute(
            """
            INSERT INTO fact_deal(deal_id, title, category_id, stage_id,
                assigned_by_id, source_id, opportunity, currency_id, date_create,
                date_modify, closedate, is_closed, is_won, is_lost, contact_id,
                is_deleted, synced_at)
            VALUES (?, 'ВГ 747', 18, 'C18:NEW', 32, 'CALL', 0, 'RUB',
                    '2026-08-01T10:00:00+00:00', '2026-08-01T10:00:00+00:00',
                    NULL, 0, 0, 0, 5000, 0, 'x')
            """,
            (DEAL,),
        )
        return [DEAL]

    monkeypatch.setattr(etl, "BitrixClient", lambda *a, **kw: _NoPortal())
    monkeypatch.setattr(etl, "sync_dimensions", lambda *a, **kw: [])
    monkeypatch.setattr(etl, "sync_users", lambda *a, **kw: None)
    monkeypatch.setattr(etl, "sync_deals", _one_deal)
    monkeypatch.setattr(etl, "sync_leads", lambda *a, **kw: [])
    monkeypatch.setattr(etl, "sync_stage_history", lambda *a, **kw: None)
    monkeypatch.setattr(etl, "sync_activities", lambda *a, **kw: 0)
    monkeypatch.setattr(etl, "sync_comments", _boom)

    with pytest.raises(RuntimeError):
        etl.run_sync("backfill", since_override="2026-01-01")
    return analytics_db


def test_a_run_that_dies_halfway_keeps_what_it_wrote(died_halfway):
    """Это и есть цена: витрина остаётся обновлённой наполовину.

    Проверяется не потому, что так лучше, а потому, что так теперь есть:
    правило должно быть записано, иначе следующий читающий код будет
    рассчитывать на прежнюю атомарность прогона.
    """
    with analytics_session(readonly=True) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM fact_deal WHERE deal_id = ?", (DEAL,)
        ).fetchone()[0] == 1


def test_a_dead_run_does_not_move_the_deal_window(died_halfway):
    """А это — то, что делает цену приемлемой.

    Знак сделок ставится последним этапом. Прогон, не дошедший до него,
    оставляет границу окна на месте, и следующий инкремент заберёт те же
    сделки заново — upsert'ом, без потерь. Переставь знак раньше данных, и
    карточки, не доехавшие до витрины, не доедут уже никогда: их никто
    больше не запросит.
    """
    with analytics_session(readonly=True) as conn:
        assert etl.get_watermark(conn, ENTITY_DEAL, 0) is None
        assert etl.get_watermark(conn, ENTITY_LEAD, 0) is None


def test_the_activity_window_moves_only_with_the_activities(died_halfway):
    """У дел свой знак, и он уезжает ТЕМ ЖЕ коммитом, что и они.

    Этап дел прогон прошёл, значит и знак их обязан стоять. Порознь они
    разошлись бы при падении между записью и знаком — и разошлись бы в
    худшую сторону: знак без данных.
    """
    with analytics_session(readonly=True) as conn:
        assert etl.get_watermark(conn, ENTITY_ACTIVITY, 0) is not None
