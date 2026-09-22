"""Занятая витрина не должна стоить ночного бюджета.

21.09 читатель комментариев стартовал в одну секунду с тиком ETL. Тот держал
запись всю свою полутораминутную транзакцию, читателю хватило десяти секунд
busy_timeout — и первая же карточка упала с «database is locked». Дальше
случилось то, что дороже падения: исключение вышло из цикла, но не из
`with ThreadPoolExecutor(...)`, а выход из него ждёт ВСЕ отправленные задачи.
Пул десять минут дочитывал оставшиеся четыреста карточек, ответы приходили в
умирающий процесс, деньги уходили. Итог ночи: ноль записей в витрине, ~180 ₽
в никуда и очередь, выросшая на день.

Расписание развели, транзакцию ETL порезали на этапы — но ни то, ни другое не
обещает, что писатели никогда не встретятся: этап ETL всё ещё длиннее
busy_timeout, а прогон читателя идёт десять минут. Поэтому здесь проверяется
то, что от расписания не зависит: занятость пережидается, чужая ошибка не
пережидается, а упавшая запись останавливает трату.
"""

import sqlite3
import threading
from datetime import date

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

import comment_reader
from schema import BUSY_TIMEOUT_MS, analytics_session

# Значения самой записи роли не играют: проверяется не разбор ответа модели,
# а то, доходит ли он до таблицы. Поля — ровно по _READ_UPSERT.
CARD = (1, "hash-1", "Позвонить в пятницу", "2026-09-11", None,
        0, "", "", "", "2026-09-09T00:00:00+00:00", "v1")


class _Answer:
    def __init__(self, content):
        self.content = content


class _Model:
    """Модель, считающая обращения: счётчик — это счёт за ночь.

    Умеет ждать — но не по часам, а по отмашке. Разница принципиальная.

    Первая редакция изображала задержку сети через Event().wait(0.2): без
    неё очередь пула разбиралась быстрее, чем падала запись, и тест на
    отмену очереди проходил, ничего не проверяя. Но ожидание по таймеру
    означает, что исход теста решают секунды на машине, а не код. Хуже
    того, оно умеет зависнуть: под подменой системных часов такой wait не
    просыпается вовсе, и набор встаёт молча — а зависший тест хуже
    упавшего, потому что не говорит ничего.

    Теперь первая карточка проходит сразу, остальные ждут события, которое
    ставит упавшая запись. Порядок держится сам собой, без единой отмеренной
    секунды: к моменту отмены очереди модель успевает ровно столько раз,
    сколько потоков в пуле, — и это потолок потерь, который и проверяется.
    """

    def __init__(self, content='{"promised": "Позвонить в пятницу"}',
                 gate: threading.Event | None = None):
        self.content, self.calls, self.gate = content, [], gate
        self._lock = threading.Lock()

    def invoke(self, messages):
        with self._lock:
            first = not self.calls
            self.calls.append(messages[-1].content)
        if self.gate is not None and not first:
            # Таймаут — предохранитель, а не механизм: отмашка приходит от
            # упавшей записи. Без него исчезнувшая отмашка вешала бы набор.
            self.gate.wait(30)
        return _Answer(self.content)


def _deal(conn, deal_id):
    conn.execute(
        """
        INSERT INTO fact_deal(deal_id, title, category_id, stage_id, assigned_by_id,
            source_id, opportunity, currency_id, date_create, date_modify, closedate,
            is_closed, is_won, is_lost, contact_id, is_deleted, synced_at)
        VALUES (?, 'ВГ 747', 0, 'UC_FADPBF', 10, 'ADV', 0, 'RUB',
                '2026-06-01T00:00:00+00:00', '2026-06-01T00:00:00+00:00',
                NULL, 0, 0, 0, 5000, 0, 'x')
        """,
        (deal_id,),
    )


def _note(conn, comment_id, deal_id):
    conn.execute(
        "INSERT INTO fact_comment(comment_id, entity_type, entity_id, author_id,"
        " body, is_auto, created_at, synced_at) VALUES (?, 'deal', ?, 10, ?, 0,"
        " '2026-08-10T10:00:00+00:00', 'x')",
        (comment_id, deal_id, f"Карточка {deal_id}: показ был, ушли думать"),
    )


@pytest.fixture
def mart(analytics_db):
    """Двадцать карточек к прочтению — чтобы «пул дочитал остальные» было видно."""
    with analytics_session() as conn:
        for deal_id in range(1, 21):
            _deal(conn, deal_id)
            _note(conn, 100 + deal_id, deal_id)
    return analytics_db


@pytest.fixture
def no_waiting(monkeypatch):
    """Паузы повторов — в счётчик, а не в реальное ожидание.

    Иначе тест на исчерпание попыток шёл бы минуту, и его бы выключили.
    """
    slept: list[float] = []
    monkeypatch.setattr(comment_reader.time, "sleep", slept.append)
    return slept


@pytest.fixture
def rival(analytics_db):
    """Второе соединение — тот самый сосед, что держит запись.

    Настоящая транзакция настоящего SQLite, а не подделанное исключение:
    как раз взаимодействие двух соединений в WAL здесь и ломалось.
    """
    other = sqlite3.connect(analytics_db, timeout=0.05)
    other.execute("PRAGMA journal_mode=WAL")
    yield other
    other.rollback()
    other.close()


def _impatient(conn):
    """Укоротить ожидание SQLite до неразличимого.

    Проверяется поведение повторов, а не длина busy_timeout: с боевыми
    десятью секундами на попытку один тест шёл бы больше минуты, и первое,
    что с ним сделали бы, — выключили. Бюджет ожидания считается отдельно и
    по настоящим константам.
    """
    conn.execute("PRAGMA busy_timeout=50")
    return conn


def _hold_the_write_lock(rival):
    """Занять запись так, как её занимает ETL: начатой транзакцией."""
    rival.execute("BEGIN IMMEDIATE")
    rival.execute(
        "INSERT INTO dim_source(source_id, name, synced_at)"
        " VALUES ('RIVAL', 'сосед', 'x')"
    )


# ── Занятость пережидается ─────────────────────────────────────────────
def test_a_card_lands_once_the_neighbour_lets_go(mart, rival, monkeypatch):
    """Сосед отпускает запись на паузе повтора — карточка должна дойти.

    Это и есть тот случай, ради которого повторы написаны, и именно он
    ломался молча: неудачная вставка оставляет открытую транзакцию со
    старым снимком базы, и повтор внутри неё SQLite отвергает мгновенно с
    тем же текстом «database is locked». Без отката перед паузой читатель
    сжигал бы все семь попыток за доли секунды — ровно тогда, когда сосед
    уже ушёл.
    """
    _hold_the_write_lock(rival)

    def _let_go(_pause):
        rival.commit()

    monkeypatch.setattr(comment_reader.time, "sleep", _let_go)

    with analytics_session() as conn:
        comment_reader._save_card(_impatient(conn), CARD)
        saved = conn.execute(
            "SELECT promised FROM fact_comment_read WHERE entity_id = 1"
        ).fetchone()

    assert saved is not None, "карточка потеряна, хотя витрину уже отпустили"
    assert saved[0] == "Позвонить в пятницу"


def test_a_retry_is_not_poisoned_by_the_transaction_it_failed_in(
    mart, rival, monkeypatch,
):
    """Повтор внутри той же транзакции SQLite отвергает мгновенно.

    Неудачная вставка оставляет транзакцию открытой. Если в ней уже был
    прочитан снимок базы, он остаётся прежним — тем, что был ДО чужого
    коммита, — и любая следующая попытка писать отвергается сразу, не
    дожидаясь busy_timeout (SQLITE_BUSY_SNAPSHOT). Текст ошибки при этом тот
    же самый: «database is locked». Повторы выродились бы в семь мгновенных
    отказов ровно тогда, когда сосед уже отпустил запись.

    Читатель сегодня приходит сюда без открытого снимка, и этот путь у него
    недостижим — снимок здесь открывается нарочно. Ловушка возвращается от
    любого SELECT, сделанного в транзакции до записи, и распознать её в логе
    нельзя: там будут те же семь строк «витрина занята».
    """
    _hold_the_write_lock(rival)

    def _let_go(_pause):
        rival.commit()

    monkeypatch.setattr(comment_reader.time, "sleep", _let_go)

    with analytics_session() as conn:
        _impatient(conn)
        # Тот самый открытый снимок: транзакция, в которой уже читали.
        conn.execute("BEGIN")
        conn.execute("SELECT count(*) FROM fact_deal").fetchone()

        comment_reader._save_card(conn, CARD)
        saved = conn.execute(
            "SELECT promised FROM fact_comment_read WHERE entity_id = 1"
        ).fetchone()

    assert saved is not None, "повтор остался в транзакции со старым снимком"


def test_the_waiting_covers_more_than_an_etl_tick(mart, rival, no_waiting):
    """Ждать меньше тика инкремента — значит не ждать вовсе.

    Бюджет считается по паузам плюс busy_timeout на каждой попытке: тик
    занимает около полутора минут, и повторы обязаны его перекрывать.
    Иначе вся конструкция даёт только более вежливый лог.
    """
    _hold_the_write_lock(rival)

    with analytics_session() as conn:
        with pytest.raises(sqlite3.OperationalError):
            comment_reader._save_card(_impatient(conn), CARD)

    timeout_sec = BUSY_TIMEOUT_MS / 1000
    budget = sum(no_waiting) + comment_reader.WRITE_ATTEMPTS * timeout_sec

    assert budget >= 120, f"повторы перекрывают лишь {budget} с"


def test_a_hopeless_wait_ends_instead_of_hanging_till_morning(mart, rival, no_waiting):
    """Сосед не ушёл — читатель сдаётся, а не висит до утра.

    Полная сверка держит запись полчаса; дожидаться её значит попасть в
    дайджест с недочитанной витриной и всё равно упасть, только позже.
    """
    _hold_the_write_lock(rival)

    with analytics_session() as conn:
        with pytest.raises(sqlite3.OperationalError):
            comment_reader._save_card(_impatient(conn), CARD)

    assert len(no_waiting) == comment_reader.WRITE_ATTEMPTS - 1
    assert max(no_waiting) <= comment_reader.WRITE_PAUSE_CAP_SEC


# ── Чужая ошибка не пережидается ───────────────────────────────────────
def test_a_broken_table_is_not_mistaken_for_a_busy_one(mart, no_waiting):
    """Повторять любую OperationalError — значит прятать поломку за паузой.

    «no such column» от повторов не пройдёт, а прогон будет молчать две
    минуты на каждой карточке и всё равно умрёт — с тем же сообщением,
    только через час.
    """
    with analytics_session() as conn:
        conn.execute("DROP TABLE fact_comment_read")

        with pytest.raises(sqlite3.OperationalError) as failure:
            comment_reader._save_card(conn, CARD)

    assert "no such table" in str(failure.value)
    assert no_waiting == [], "поломку схемы пережидали как занятость"


# ── Упавшая запись останавливает трату ─────────────────────────────────
def test_a_failed_write_stops_the_spending(mart, monkeypatch):
    """Главное из этого файла: деньги.

    Ошибка записи — это конец прогона. Пул к этому моменту держит очередь из
    всех карточек партии, и прежний выход из него дочитывал её целиком:
    четыреста оплаченных ответов в процесс, которому они уже не нужны.
    Отменяются те, что не начались; уже работающие доедут — их не больше
    числа потоков, и это потолок потерь.

    Запись роняется сразу, без повторов: повторы проверены выше, а здесь
    важно только то, что делает пул после исключения.
    """
    gate = threading.Event()

    def _no_room(*_args, **_kwargs):
        # Отмашка модели — от самой аварии: карточки, ждавшие её, поедут
        # только теперь, когда очередь уже отменена.
        gate.set()
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(comment_reader, "_save_card", _no_room)
    model = _Model(gate=gate)

    with analytics_session() as conn:
        with pytest.raises(sqlite3.OperationalError):
            comment_reader.read_cards(
                conn, model, today=date(2026, 9, 9), workers=2,
            )

    assert len(model.calls) <= 6, (
        f"после падения записи модель позвали {len(model.calls)} раз "
        "из 20 — пул дочитывает партию в умирающий прогон"
    )


def test_the_run_says_what_it_managed_before_dying(mart, rival, no_waiting, caplog):
    """Строка «Прочитано карточек» нужна именно при падении.

    По ней в журнале видно, потерян прогон целиком или наполовину. Раньше
    она печаталась после цикла — то есть только при успехе, — и ночь 21.09
    осталась в логах одной ошибкой без единой цифры.
    """
    model = _Model()

    with caplog.at_level("INFO", logger=comment_reader.logger.name):
        with analytics_session() as conn:
            _hold_the_write_lock(rival)
            with pytest.raises(sqlite3.OperationalError):
                comment_reader.read_cards(
                    _impatient(conn), model, today=date(2026, 9, 9), workers=2,
                )

    assert "Прочитано карточек: 0" in caplog.text
