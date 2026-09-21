"""Пока портал отвечает, витрина обязана быть свободна.

Коммиты между этапами ETL — половина дела, и меньшая. Замер на боевом
сервере 21.09 это показал: читатель комментариев ждал освобождения витрины
81 секунду, то есть почти весь прогон инкремента, хотя коммиты между этапами
уже стояли. Из семи попыток записи он израсходовал пять.

Причина не в границах этапов, а внутри них. Запись берётся на ПЕРВОЙ
строке этапа, а обращения к порталу идут дальше: этап комментариев
опрашивает его по карточке за раз, полтораста раз по полсекунды, и всё это
время держит замок, ничего не записывая. То же у выгрузки сделок, лидов и
дел — там запись берётся на первой пачке, а генератор идёт за следующей
страницей.

Отсюда правило, которое здесь и проверяется: транзакция записи не имеет
права переживать обращение к порталу. Проверяется оно единственным честным
способом — на настоящих функциях загрузки, а не на заглушках: портал
подменён, и на КАЖДОМ запросе к нему приходит сосед с коротким терпением и
пробует записать свою строку. Не смог — значит замок держат через сеть.

Сосед здесь не выдумка: это читатель комментариев, который в ту ночь не
смог и потерял четыреста оплаченных ответов модели.
"""

import sqlite3

import analytics  # noqa: F401  — кладёт src/analytics на sys.path
import etl
import pytest
from config import get_settings
from schema import analytics_session

CARDS = (101, 102, 103)


class _Portal:
    """Портал, у которого на каждом ответе проверяют, свободна ли витрина.

    Пробу ставим ДО возврата данных: в этот миг вызывающий код находится
    ровно там, где в бою он ждёт сеть.
    """

    def __init__(self, db_path, pages=None, comments=None):
        self.db_path = db_path
        self.pages = pages or []
        self.comments = comments or {}
        self.request_count = 0
        self.busy: list[str] = []
        self.probes = 0

    def _probe(self, where: str) -> None:
        self.request_count += 1
        self.probes += 1
        other = sqlite3.connect(self.db_path, timeout=0.05)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute(
                "INSERT INTO dim_source(source_id, name, synced_at)"
                " VALUES (?, 'сосед', 'x')",
                (f"N{self.probes}",),
            )
            other.commit()
        except sqlite3.OperationalError:
            self.busy.append(where)
        finally:
            other.close()

    def call(self, method, params=None):
        self._probe(method)
        if method == "crm.timeline.comment.list":
            return self.comments.get(
                (params or {}).get("filter", {}).get("ENTITY_ID"), [])
        return []

    def list_by_id(self, method, params=None):
        for page in self.pages:
            self._probe(method)
            yield from page

    def list_paged(self, method, params=None):
        self._probe(method)
        return iter(())

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


def _deals(conn):
    for deal_id in CARDS:
        conn.execute(
            "INSERT INTO fact_deal(deal_id, title, category_id, stage_id,"
            " assigned_by_id, source_id, opportunity, currency_id, date_create,"
            " is_closed, is_won, is_lost, is_deleted, synced_at)"
            " VALUES (?, 'ВГ', 18, 'C18:NEW', 32, 'CALL', 0, 'RUB',"
            " '2026-08-01T10:00:00+00:00', 0, 0, 0, 0, 'x')",
            (deal_id,),
        )


def _raw_deal(deal_id):
    return {"ID": str(deal_id), "TITLE": "ВГ", "CATEGORY_ID": "18",
            "STAGE_ID": "C18:NEW", "ASSIGNED_BY_ID": "32", "SOURCE_ID": "CALL",
            "OPPORTUNITY": "0", "CURRENCY_ID": "RUB",
            "DATE_CREATE": "2026-08-01T10:00:00+03:00"}


def _raw_comment(comment_id, deal_id):
    return {"ID": str(comment_id), "CREATED": "2026-08-10T10:00:00+03:00",
            "AUTHOR_ID": "32", "COMMENT": f"Показ по карточке {deal_id}"}


# ── Комментарии: тот самый этап, что держал замок 81 секунду ───────────
@pytest.fixture
def comment_walk(analytics_db):
    with analytics_session() as conn:
        _deals(conn)

    portal = _Portal(analytics_db, comments={
        deal_id: [_raw_comment(deal_id * 10, deal_id)] for deal_id in CARDS
    })
    with analytics_session() as conn:
        etl.sync_comments(portal, conn, batch=None)
    return portal


def test_the_comment_walk_frees_the_shopfront_between_cards(comment_walk):
    """Главная проверка файла: между карточками витрина свободна.

    Портал опрашивается по карточке за раз — полтораста раз за ночной
    прогон. Держать на этом замок значит держать его минуту с лишним.
    """
    assert comment_walk.probes == len(CARDS), "опрошены не все карточки"
    assert comment_walk.busy == [], (
        f"витрина занята, пока портал отвечает: {comment_walk.busy}"
    )


def test_a_card_is_marked_asked_in_the_same_commit_as_its_comments(comment_walk):
    """Коммит внутри цикла безопасен ровно потому, что отметка едет с данными.

    Отметка «карточку спросили» — это граница для следующего прогона.
    Уехав раньше комментариев, она увела бы карточку из очереди, не записав
    её записей: карточку больше не спросят, и разбор её не увидит никогда.
    """
    with analytics_session(readonly=True) as conn:
        asked = {row[0] for row in conn.execute(
            "SELECT entity_id FROM comment_sync WHERE entity_type = 'deal'")}
        with_rows = {row[0] for row in conn.execute(
            "SELECT DISTINCT entity_id FROM fact_comment WHERE entity_type = 'deal'")}

    assert asked == set(CARDS)
    assert with_rows == set(CARDS), "карточка отмечена спрошенной, а записей нет"


# ── Постраничные выгрузки ──────────────────────────────────────────────
def _pages_portal(analytics_db):
    # Три страницы: одна пачка пишется, генератор идёт за следующей — вот
    # на этом переходе замок и оставался.
    return _Portal(analytics_db, pages=[[_raw_deal(d)] for d in CARDS])


def test_the_deal_pages_free_the_shopfront_between_pages(analytics_db, monkeypatch):
    """Сделки: запись берётся на первой пачке, а страниц впереди ещё много."""
    monkeypatch.setattr(etl, "HISTORY_BATCH", 1)
    portal = _pages_portal(analytics_db)

    with analytics_session() as conn:
        etl.sync_deals(portal, conn, get_settings(),
                       since="2026-01-01", modified_since=None)

    assert portal.probes == len(CARDS)
    assert portal.busy == [], f"витрина занята между страницами: {portal.busy}"


def test_the_activity_pages_free_the_shopfront_between_pages(analytics_db,
                                                             monkeypatch):
    """Дела грузятся тем же способом — и тем же способом держали замок."""
    monkeypatch.setattr(etl, "HISTORY_BATCH", 1)
    raw = [{"ID": str(i), "OWNER_TYPE_ID": "2", "OWNER_ID": "101",
            "PROVIDER_TYPE_ID": "CALL", "SUBJECT": "Звонок",
            "RESPONSIBLE_ID": "32", "COMPLETED": "Y",
            "CREATED": "2026-08-01T10:00:00+03:00"} for i in (1, 2, 3)]
    portal = _Portal(analytics_db, pages=[[row] for row in raw])

    with analytics_session() as conn:
        etl.sync_activities(portal, conn, since="2026-01-01", modified_since=None)

    assert portal.probes == 3
    assert portal.busy == [], f"витрина занята между страницами: {portal.busy}"


# ── Справочники ────────────────────────────────────────────────────────
def test_the_dictionaries_free_the_shopfront_between_requests(analytics_db):
    """Пять справочников — это два десятка запросов к порталу подряд.

    Одна транзакция на все пять держала бы замок все десять секунд их
    ожидания: ровно столько, сколько сосед готов ждать, и ни секундой
    меньше.
    """
    portal = _Portal(analytics_db)

    with analytics_session() as conn:
        etl.sync_dimensions(portal, conn, get_settings())

    assert portal.probes >= 5, "справочники опрошены не все"
    assert portal.busy == [], f"витрина занята между справочниками: {portal.busy}"
