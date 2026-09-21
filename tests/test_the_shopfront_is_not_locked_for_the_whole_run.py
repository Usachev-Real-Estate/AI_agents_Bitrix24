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
окно заново, а записи идут upsert'ом. Здесь проверяются обе стороны размена —
и что замок отпускается, и что порядок «данные, потом знак» соблюдён. Второе
важнее: нарушив его, прогон терял бы карточки молча.
"""

import sqlite3

import analytics  # noqa: F401  — кладёт src/analytics на sys.path
import etl
import pytest
from schema import analytics_session
from stages import ENTITY_ACTIVITY, ENTITY_DEAL, ENTITY_LEAD

DEAL = 101


class _NoPortal:
    """Соединения с порталом нет: проверяется устройство прогона, не загрузка."""

    request_count = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


def _one_deal(_client, conn, _settings, *, since, modified_since=None):
    """Заменяет sync_deals: одна строка в витрину — та, что ищет сосед."""
    conn.execute(
        """
        INSERT INTO fact_deal(deal_id, title, category_id, stage_id, assigned_by_id,
            source_id, opportunity, currency_id, date_create, date_modify, closedate,
            is_closed, is_won, is_lost, contact_id, is_deleted, synced_at)
        VALUES (?, 'ВГ 747', 18, 'C18:NEW', 32, 'CALL', 0, 'RUB',
                '2026-08-01T10:00:00+00:00', '2026-08-01T10:00:00+00:00',
                NULL, 0, 0, 0, 5000, 0, 'x')
        """,
        (DEAL,),
    )
    return [DEAL]


@pytest.fixture
def staged(monkeypatch):
    """Прогон из одних заглушек: остаются только этапы и коммиты между ними.

    Настоящий FakeClient здесь не нужен и мешал бы: проверяется не то, что
    загружается, а то, когда отпускается замок.
    """
    monkeypatch.setattr(etl, "BitrixClient", lambda *a, **kw: _NoPortal())
    monkeypatch.setattr(etl, "sync_dimensions", lambda *a, **kw: [])
    monkeypatch.setattr(etl, "sync_users", lambda *a, **kw: None)
    monkeypatch.setattr(etl, "sync_deals", _one_deal)
    monkeypatch.setattr(etl, "sync_leads", lambda *a, **kw: [])
    monkeypatch.setattr(etl, "sync_stage_history", lambda *a, **kw: None)
    monkeypatch.setattr(etl, "sync_activities", lambda *a, **kw: 0)
    return monkeypatch


def _neighbour(db_path):
    """Второе соединение с коротким терпением — как читатель комментариев."""
    other = sqlite3.connect(db_path, timeout=0.05)
    other.execute("PRAGMA journal_mode=WAL")
    return other


# ── Замок отпускается посреди прогона ──────────────────────────────────
@pytest.fixture
def midway(analytics_db, staged):
    """Что видит и может сосед, пришедший к середине прогона.

    Точка входа — этап комментариев: до него прогон успел записать
    справочники, сделки, историю стадий и дела, то есть четыре коммита.
    """
    seen: dict = {}

    def _probe(_client, _conn, **_kwargs):
        other = _neighbour(analytics_db)
        try:
            seen["deals_visible"] = other.execute(
                "SELECT COUNT(*) FROM fact_deal"
            ).fetchone()[0]
            other.execute("BEGIN IMMEDIATE")
            other.execute(
                "INSERT INTO dim_source(source_id, name, synced_at)"
                " VALUES ('NEIGHBOUR', 'сосед', 'x')"
            )
            other.commit()
            seen["wrote"] = True
        except sqlite3.OperationalError as error:
            seen["error"] = str(error)
        finally:
            other.close()
        return 0

    staged.setattr(etl, "sync_comments", _probe)
    etl.run_sync("backfill", since_override="2026-01-01")
    return seen


def test_a_neighbour_can_write_midway_through_the_run(midway):
    """Главная проверка: прогон не держит запись всё своё время.

    Сосед приходит с терпением в 50 миллисекунд — прогон обязан уже отпустить
    замок, а не дожидаться конца.
    """
    assert midway.get("wrote"), (
        f"витрина занята посреди прогона: {midway.get('error')}"
    )


def test_what_the_run_has_written_is_already_visible(midway):
    """Отпустить замок мало: записанное должно быть видно, то есть закоммичено.

    Читают витрину отдельным соединением — веб и все прочие. В WAL читатель
    видит только закоммиченное, так что нулевой счётчик здесь означал бы
    «замок отпущен, а данных ещё нет».
    """
    assert midway.get("deals_visible") == 1


# ── Цена размена: прогон больше не одна транзакция ─────────────────────
@pytest.fixture
def died_halfway(analytics_db, staged):
    """Прогон, упавший на этапе комментариев."""
    def _boom(*_a, **_kw):
        raise RuntimeError("портал ответил пятисоткой")

    staged.setattr(etl, "sync_comments", _boom)
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
