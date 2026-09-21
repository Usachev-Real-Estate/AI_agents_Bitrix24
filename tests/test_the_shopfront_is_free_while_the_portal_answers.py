"""Пока портал отвечает, витрина обязана быть свободна.

Коммиты между этапами ETL — половина дела, и меньшая. Замер на боевом
сервере 21.09: читатель комментариев ждал освобождения витрины 81 секунду,
то есть почти весь прогон инкремента, хотя коммиты между этапами уже стояли.
Из семи попыток записи он израсходовал пять.

Причина не на границах этапов, а внутри них. Запись берётся на ПЕРВОЙ
строке, а обращения к порталу идут дальше: этап комментариев опрашивает его
по карточке за раз, выгрузки листают страницы, справочники идут подряд.
Отсюда правило, которое здесь и проверяется: транзакция записи не имеет
права переживать обращение к порталу, а загрузчик не возвращает управление
с открытой транзакцией.

Проверяется оно единственным честным способом — на настоящих функциях
загрузки: портал подменён, и на КАЖДОМ запросе к нему приходит сосед с
коротким терпением и пробует записать свою строку. Не смог — значит замок
держат через сеть. Сосед здесь не выдумка: это читатель комментариев,
который в ту ночь не смог и потерял четыреста оплаченных ответов модели.

Первая редакция этого файла почти всё это пропускала, и стоит сказать как
именно — ошибки тут показательнее находок.

  * Размер пачки подменялся на единицу. При боевых пятистах обычный прогон
    в ветку внутри цикла не заходит ни разу: единственная запись — хвостовой
    сброс ПОСЛЕ цикла, и вот его-то транзакцию держали через всю выгрузку
    лидов. Подмена уводила проверку ровно с того пути, по которому ходит бой.
  * Заглушка портала на все методы отвечала пустотой. Четыре справочника из
    пяти при этом не писали ни строки — значит и замка не брали, и проба не
    могла оказаться занятой никогда.
  * Проверка «отметка уехала тем же коммитом» читала итог ПОСЛЕ прогона,
    когда закоммичено уже всё. Два множества, сверенные в конце, равны при
    любом порядке коммитов — в том числе при том самом, которым пугал её
    собственный докстринг.
  * Лид и история стадий не проверялись вовсе, хотя именно они держали
    замок дольше прочих.

Общее у всех четырёх одно: тест был написан под ответ, а не под вопрос.
"""

import itertools
import sqlite3

import analytics  # noqa: F401  — кладёт src/analytics на sys.path
import etl
import pytest
from config import get_settings
from schema import analytics_session

CARDS = (101, 102, 103)
LEADS = (201, 202, 203)

# Оба размера пачки: боевой и такой, при котором цикл срабатывает. Ветка
# внутри цикла и хвостовой сброс после него — разные пути записи, и держать
# замок умеет каждый.
BATCHES = (1, etl.HISTORY_BATCH)


class _Portal:
    """Портал, у которого на каждом ответе проверяют, свободна ли витрина.

    Проба ставится ДО возврата данных: в этот миг вызывающий код находится
    ровно там, где в бою он ждёт сеть.
    """

    # Сквозной на весь процесс: в одном тесте порталов бывает два, и на
    # общей витрине их строки соседа не должны сталкиваться ключом.
    _mark = itertools.count(1)

    def __init__(self, db_path, pages=None, comments=None, watch=None):
        self.db_path = db_path
        self.pages = pages or []
        self.comments = comments or {}
        self.watch = watch          # что ещё подсмотреть на каждой пробе
        self.request_count = 0
        self.busy: list[str] = []
        self.seen: list = []
        self.probes = 0

    def _probe(self, where: str) -> None:
        self.request_count += 1
        self.probes += 1
        other = sqlite3.connect(self.db_path, timeout=0.05)
        other.row_factory = sqlite3.Row
        try:
            if self.watch is not None:
                self.seen.append(self.watch(other))
            other.execute("BEGIN IMMEDIATE")
            other.execute(
                "INSERT INTO dim_source(source_id, name, synced_at)"
                " VALUES (?, 'сосед', 'x')",
                (f"N{next(_Portal._mark)}",),
            )
            other.commit()
        except sqlite3.OperationalError:
            self.busy.append(where)
        finally:
            other.close()

    # ── ответы портала ────────────────────────────────────────────────
    def call(self, method, params=None):
        self._probe(method)
        entity = (params or {}).get("filter", {}).get("ENTITY_ID")
        if method == "crm.timeline.comment.list":
            return self.comments.get(entity, [])
        if method == "crm.category.list":
            return {"categories": [{"id": 18, "name": "Покупатели", "sort": 10}]}
        if method == "crm.status.list":
            return [{"STATUS_ID": "C18:NEW", "NAME": "Подбор", "SORT": 10}]
        if method == "department.get":
            return [{"ID": "44", "NAME": "Отдел Трофимовой"}]
        return []

    def list_by_id(self, method, params=None):
        for page in self.pages:
            self._probe(method)
            yield from page

    def list_paged(self, method, params=None):
        self._probe(method)
        return iter([{"ID": "32", "NAME": "Марат", "LAST_NAME": "Абзалилов",
                      "ACTIVE": "Y", "UF_DEPARTMENT": [44]}])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


# ── данные ─────────────────────────────────────────────────────────────
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


def _raw_lead(lead_id):
    return {"ID": str(lead_id), "TITLE": "Лид", "STATUS_ID": "NEW",
            "SOURCE_ID": "CALL", "ASSIGNED_BY_ID": "32",
            "DATE_CREATE": "2026-08-01T10:00:00+03:00"}


def _raw_activity(activity_id):
    return {"ID": str(activity_id), "OWNER_TYPE_ID": "2", "OWNER_ID": "101",
            "PROVIDER_TYPE_ID": "CALL", "SUBJECT": "Звонок",
            "RESPONSIBLE_ID": "32", "COMPLETED": "Y",
            "CREATED": "2026-08-01T10:00:00+03:00"}


def _raw_comment(comment_id, deal_id):
    return {"ID": str(comment_id), "CREATED": "2026-08-10T10:00:00+03:00",
            "AUTHOR_ID": "32", "COMMENT": f"Показ по карточке {deal_id}"}


def _pages(rows):
    """Каждая строка — своя страница: так проба попадает между записями."""
    return [[row] for row in rows]


# ── Постраничные выгрузки ──────────────────────────────────────────────
@pytest.mark.parametrize("batch", BATCHES)
def test_the_deal_pages_free_the_shopfront_between_pages(analytics_db,
                                                         monkeypatch, batch):
    """Сделки: замок не должен жить дольше одной пачки.

    Прогоняется при обоих размерах пачки. При единице работает ветка внутри
    цикла, при боевых пятистах — только хвостовой сброс; держать замок
    умеет каждый путь, и проверять надо оба.
    """
    monkeypatch.setattr(etl, "HISTORY_BATCH", batch)
    portal = _Portal(analytics_db, pages=_pages(_raw_deal(d) for d in CARDS))

    with analytics_session() as conn:
        etl.sync_deals(portal, conn, get_settings(),
                       since="2026-01-01", modified_since=None)

    assert portal.probes == len(CARDS)
    assert portal.busy == [], f"витрина занята между страницами: {portal.busy}"


@pytest.mark.parametrize("batch", BATCHES)
def test_the_lead_pages_free_the_shopfront_between_pages(analytics_db,
                                                         monkeypatch, batch):
    """Лиды грузятся тем же способом — и проверялись раньше никак."""
    monkeypatch.setattr(etl, "HISTORY_BATCH", batch)
    portal = _Portal(analytics_db, pages=_pages(_raw_lead(i) for i in LEADS))

    with analytics_session() as conn:
        etl.sync_leads(portal, conn, since="2026-01-01", modified_since=None)

    assert portal.probes == len(LEADS)
    assert portal.busy == [], f"витрина занята между страницами: {portal.busy}"


@pytest.mark.parametrize("batch", BATCHES)
def test_the_activity_pages_free_the_shopfront_between_pages(analytics_db,
                                                             monkeypatch, batch):
    """Дела — третий загрузчик той же формы."""
    monkeypatch.setattr(etl, "HISTORY_BATCH", batch)
    portal = _Portal(analytics_db, pages=_pages(_raw_activity(i) for i in (1, 2, 3)))

    with analytics_session() as conn:
        etl.sync_activities(portal, conn, since="2026-01-01", modified_since=None)

    assert portal.probes == 3
    assert portal.busy == [], f"витрина занята между страницами: {portal.busy}"


def test_a_loader_does_not_hand_over_an_open_transaction(analytics_db):
    """Та самая дыра, которую первая редакция прятала подменой размера пачки.

    Размер здесь боевой и нарочно не подменяется. При нём обычный прогон в
    ветку внутри цикла не заходит ни разу — единственная запись сделок
    приходит хвостовым сбросом, и если его не отпустить, транзакцию будет
    держать уже следующий загрузчик, пока листает портал.

    Порядок вызовов тот же, что в run_sync: сделки, следом лиды, и никакого
    коммита между ними.
    """
    portal = _Portal(analytics_db, pages=_pages(_raw_deal(d) for d in CARDS))
    leads = _Portal(analytics_db, pages=_pages(_raw_lead(i) for i in LEADS))

    with analytics_session() as conn:
        etl.sync_deals(portal, conn, get_settings(),
                       since="2026-01-01", modified_since=None)
        etl.sync_leads(leads, conn, since="2026-01-01", modified_since=None)

    assert leads.busy == [], (
        f"сделки отдали управление с открытой транзакцией: {leads.busy}"
    )


def _neighbour_can_write(db_path) -> bool:
    """Может ли сосед записать прямо сейчас, не дожидаясь никого."""
    other = sqlite3.connect(db_path, timeout=0.05)
    try:
        other.execute("BEGIN IMMEDIATE")
        other.execute(
            "INSERT INTO dim_source(source_id, name, synced_at)"
            " VALUES (?, 'сосед', 'x')", (f"N{next(_Portal._mark)}",),
        )
        other.commit()
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        other.close()


def _load_deals(portal, conn):
    etl.sync_deals(portal, conn, get_settings(),
                   since="2026-01-01", modified_since=None)


def _load_leads(portal, conn):
    etl.sync_leads(portal, conn, since="2026-01-01", modified_since=None)


def _load_activities(portal, conn):
    etl.sync_activities(portal, conn, since="2026-01-01", modified_since=None)


LOADERS = {
    "сделки": (_load_deals, [_raw_deal(d) for d in CARDS]),
    "лиды": (_load_leads, [_raw_lead(i) for i in LEADS]),
    "дела": (_load_activities, [_raw_activity(i) for i in (1, 2, 3)]),
}


@pytest.mark.parametrize("name", sorted(LOADERS))
def test_a_loader_returns_with_no_transaction_open(analytics_db, name):
    """Правило напрямую: загрузчик не отдаёт управление с открытым замком.

    Проверять его через соседний вызов — значит проверять порядок в
    run_sync, а он меняется. Здесь загрузчик вызывается один, и сразу
    после возврата приходит сосед: замок обязан быть свободен, что бы ни
    шло дальше.

    Размер пачки боевой и нарочно не подменяется: при нём весь портфель
    уходит хвостовым сбросом после цикла — тем самым, который первая
    редакция отпустить забыла.
    """
    load, rows = LOADERS[name]
    portal = _Portal(analytics_db, pages=_pages(rows))

    with analytics_session() as conn:
        load(portal, conn)

        assert _neighbour_can_write(analytics_db), (
            f"загрузчик «{name}» вернулся с открытой транзакцией"
        )


# ── Комментарии: тот самый этап, что держал замок 81 секунду ───────────
def _marks(conn):
    """Кто отмечен спрошенным и у кого есть записи — на этот миг."""
    asked = {r[0] for r in conn.execute(
        "SELECT entity_id FROM comment_sync WHERE entity_type = 'deal'")}
    stored = {r[0] for r in conn.execute(
        "SELECT DISTINCT entity_id FROM fact_comment WHERE entity_type = 'deal'")}
    return asked, stored


@pytest.fixture
def comment_walk(analytics_db):
    with analytics_session() as conn:
        _deals(conn)

    portal = _Portal(
        analytics_db,
        comments={d: [_raw_comment(d * 10, d)] for d in CARDS},
        watch=_marks,
    )
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


def test_a_card_is_never_marked_asked_without_its_comments(comment_walk):
    """Отметка и записи карточки обязаны быть видны одновременно.

    Смотреть надо ВО ВРЕМЯ прогона, а не после. Первая редакция сверяла два
    множества в конце — а в конце закоммичено уже всё, и равны они при любом
    порядке коммитов, включая тот, которым её собственный докстринг и
    пугал. Здесь сосед заглядывает в витрину на каждом запросе к порталу,
    то есть в промежутках между записями карточек.

    Цена ошибки — не абстракция. Отметка «карточку спросили» это граница
    для следующего прогона: уехав раньше комментариев, она уводит карточку
    в конец очереди, не записав её записей. `_comment_targets` сортирует по
    этой отметке — и карточку не спросят уже никогда.
    """
    assert comment_walk.seen, "сосед не заглянул ни разу"
    for asked, stored in comment_walk.seen:
        assert asked == stored, (
            f"отмечены спрошенными {sorted(asked)}, а записи есть у "
            f"{sorted(stored)}"
        )


# ── Справочники ────────────────────────────────────────────────────────
def test_the_dictionaries_free_the_shopfront_between_requests(analytics_db):
    """Пять справочников — это два десятка запросов к порталу подряд.

    Заглушка отвечает НЕПУСТЫМИ данными, и это условие проверки, а не
    оформление: справочник, которому не из чего писать, не берёт замка, и
    проба рядом с ним не значит ничего. Первая редакция отвечала пустотой
    и потому стерегла один справочник из пяти.
    """
    portal = _Portal(analytics_db)

    with analytics_session() as conn:
        etl.sync_dimensions(portal, conn, get_settings())
        rows = {
            "dim_pipeline": conn.execute(
                "SELECT COUNT(*) FROM dim_pipeline").fetchone()[0],
            "dim_stage": conn.execute(
                "SELECT COUNT(*) FROM dim_stage").fetchone()[0],
            "dim_user": conn.execute(
                "SELECT COUNT(*) FROM dim_user").fetchone()[0],
        }

    assert all(rows.values()), f"справочники ничего не записали: {rows}"
    assert portal.probes >= 5, "справочники опрошены не все"
    assert portal.busy == [], f"витрина занята между справочниками: {portal.busy}"


# ── История стадий ─────────────────────────────────────────────────────
def test_the_stage_history_frees_the_shopfront_between_chunks(analytics_db,
                                                              monkeypatch):
    """Самая дорогая часть прогона — и раньше не проверялась вовсе.

    История запрашивается кусками, и между кусками идёт новый запрос к
    порталу. Кусок уменьшен до одной карточки, чтобы их стало три: при
    боевых пятистах весь портфель уходит одним куском, и промежутка между
    ними просто нет.
    """
    monkeypatch.setattr(etl, "HISTORY_BATCH", 1)
    with analytics_session() as conn:
        _deals(conn)

    portal = _Portal(analytics_db)
    asked: list[int] = []

    def _history(_client, _entity, chunk):
        # Место запроса истории — здесь и проверяем замок.
        asked.append(len(chunk))
        portal._probe("fetch_history_for_entities")
        return {}

    monkeypatch.setattr(etl, "fetch_history_for_entities", _history)

    with analytics_session() as conn:
        etl.sync_stage_history(portal, conn, "deal", list(CARDS))

    assert len(asked) == len(CARDS), "куски не разделились"
    assert portal.busy == [], f"витрина занята между кусками: {portal.busy}"
