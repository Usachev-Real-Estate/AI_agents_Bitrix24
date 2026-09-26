"""Схема клиентского слоя (data/clients.db).

Отдельный файл, а не таблицы в витрине: у витрины один писатель — ETL, и
сентябрьские поломки стоили ночного бюджета ровно потому, что к нему
подсаживались соседи. Четвёртый писатель в analytics.db дороже, чем третья
база на чтение у дашборда.

Слой читает витрину `analytics_session(readonly=True)` и портал, а пишет
только сюда. Отсюда правило, унаследованное от ETL и обязательное к
соблюдению сборщиком: транзакция записи не переживает обращение к порталу,
и загрузчик не возвращает управление с открытой транзакцией.

Что здесь НЕ заводится, хотя ТЗ перечисляет это в одном блоке с остальными
таблицами (раздел 3). Сверено по коду, а не по памяти:

* `assignee_log` — не таблица. Это JSONL рядом с выгрузкой досье,
  `dossier_dir/assignee_log.jsonl` (dossier.py:787). Читается файлом.
* `transcript_queue` — таблицы с таким именем в продукте нет вовсе.
  Очередь расшифровок живёт двумя частями: список на день в
  `queue_<дата>.json` (dossier.py, write_json) и журнал постановок
  `transcript_launches` в data/violations.db (db.py:369) — там же и
  `attempts`, на который ссылается раздел 5.1 ТЗ. Заводить копию здесь
  значит завести второй счётчик на тот же ресурс; ТЗ само запрещает это
  в разделе 10 про DOSSIER_TRANSCRIPT_BUDGET.

Все временные метки — UTC ISO-8601, как в витрине (analytics.client.to_utc_iso).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("data/clients.db")

# Тот же запас, что у витрины и у основной базы. Профиль записи здесь мягче
# (один прогон в сутки против тика раз в 15 минут), но читатель тот же — веб,
# и цена ожидания та же: страница, которая не открылась.
BUSY_TIMEOUT_MS = 10_000

# 2: колонка key_reason в clients — почему ключ получился таким.
# 3: колонка assignee_count — сколько брокеров ведёт клиента на самом деле.
SCHEMA_VERSION = 5

# --------------------------------------------------------------------------
# словарь значений
# --------------------------------------------------------------------------

# triage_state — «кого смотреть первым» (раздел 6 ТЗ). Хранится КОДОМ, а
# русская подпись живёт в TRIAGE_LABELS. В ТЗ таблица правил написана
# по-русски, и это правильно для читателя ТЗ, но не для колонки: значение
# уходит в параметр запроса `?triage_state=...`, в фильтр UI и в JSONL
# наружу, а кириллица там превращается в проценты и \uXXXX. Продукт уже
# живёт этим разделением — semantic 'won'/'lost' в витрине, статусы
# 'queued'/'failed' в расшифровках, 'agent'/'client' в counterparty.
TRIAGE_CLOSED = "closed"          # закрыт
TRIAGE_REFUSED = "refused"        # отказ
TRIAGE_NO_DATA = "no_data"        # нет данных
TRIAGE_WAITING_US = "waiting_us"  # ждёт нас
TRIAGE_ABANDONED = "abandoned"    # брошен
TRIAGE_COOLING = "cooling"        # остыл
TRIAGE_NO_PLAN = "no_plan"        # без плана
TRIAGE_MOVING = "moving"          # движется

# Девятое значение, которого нет в разделе 6, и без которого схема лжёт.
# Правило «неполный прогон не перезаписывает triage_state» (раздел 4, шаг 2)
# на ПЕРВОМ прогоне, если он неполный, оставляет клиента вовсе без
# состояния. NOT NULL требует что-то записать, и любое из восьми было бы
# утверждением, которого никто не делал. Имя взято из tools.UNKNOWN_VALUE:
# в продукте это уже принятое слово для «не установлено».
TRIAGE_UNKNOWN = "unknown"

# Порядок сортировки по умолчанию из раздела 9 ТЗ. Лежит рядом с кодами
# нарочно: список, оторванный от набора значений, расходится с ним молча —
# новое состояние просто не попадает в сортировку и уезжает в конец списка.
TRIAGE_ORDER: tuple[str, ...] = (
    TRIAGE_ABANDONED,
    TRIAGE_COOLING,
    TRIAGE_WAITING_US,
    TRIAGE_NO_DATA,
    TRIAGE_NO_PLAN,
    TRIAGE_REFUSED,
    TRIAGE_MOVING,
    TRIAGE_CLOSED,
    TRIAGE_UNKNOWN,
)

TRIAGE_LABELS: dict[str, str] = {
    TRIAGE_ABANDONED: "брошен",
    TRIAGE_COOLING: "остыл",
    TRIAGE_WAITING_US: "ждёт нас",
    TRIAGE_NO_DATA: "нет данных",
    TRIAGE_NO_PLAN: "без плана",
    TRIAGE_REFUSED: "отказ",
    TRIAGE_MOVING: "движется",
    TRIAGE_CLOSED: "закрыт",
    TRIAGE_UNKNOWN: "не посчитан",
}

# Вид события ленты. source_id разный у каждого вида — см. раздел 3.1 ТЗ.
EVENT_CALL = "call"
EVENT_COMMENT = "comment"
EVENT_ACTIVITY = "activity"
EVENT_STAGE = "stage"

# Кому принадлежит карточка-источник события. Дела висят и на сделке, и на
# контакте (раздел 4.1), и различать их обязательно: id 42 у сделки и id 42
# у контакта — разные сущности.
OWNER_DEAL = "deal"
OWNER_CONTACT = "contact"
# Лид, из которого выросла сделка. Событие у него есть, КАРТОЧКИ нет — см.
# ENTITY_LEAD ниже: карточкой лид не становится, потому что комментариев
# лидов витрина не хранит, и клиент-лид получил бы честный ноль
# комментариев ответственного. Разговор же его — часть истории человека, и
# прятать её незачем.
OWNER_LEAD = "lead"

# Тип связанной карточки. `lead` заведён в словаре и не создаётся в этапе 1:
# витрина не хранит комментариев лидов, и клиент-лид получил бы честный ноль
# комментариев ответственного — то есть ложный no_assignee_comment. Значение
# оставлено, чтобы этап 2 не потребовал миграции.
ENTITY_DEAL = "deal"
ENTITY_LEAD = "lead"

# Коды проблем раздела 8. Кодом, как и triage_state, и по той же причине:
# значение уходит в `?issue=...`, в фильтр и в выгрузку, а кириллица там
# превращается в проценты.
#
# В отличие от состояния, проблем у клиента может быть несколько сразу:
# состояние отвечает «кого смотреть первым», а это — «что именно не так».
# Один человек бывает и брошен, и без комментария ответственного.
ISSUE_NO_ASSIGNEE_COMMENT = "no_assignee_comment"
ISSUE_PROMISE_OVERDUE = "promise_overdue"
ISSUE_REFUSAL_NOT_REFLECTED = "refusal_not_reflected"
ISSUE_ABANDONED = "abandoned"
ISSUE_MISSING_TRANSCRIPTS = "missing_transcripts"

# Порядок на вкладке «Исключения»: сначала то, что требует разговора с
# брокером, потом то, что требует разговора с клиентом, потом дыры в данных.
ISSUE_ORDER: tuple[str, ...] = (
    ISSUE_REFUSAL_NOT_REFLECTED,
    ISSUE_PROMISE_OVERDUE,
    ISSUE_NO_ASSIGNEE_COMMENT,
    ISSUE_ABANDONED,
    ISSUE_MISSING_TRANSCRIPTS,
)

ISSUE_LABELS: dict[str, str] = {
    ISSUE_REFUSAL_NOT_REFLECTED: "отказ не отражён в карточке",
    ISSUE_PROMISE_OVERDUE: "обещание просрочено",
    ISSUE_NO_ASSIGNEE_COMMENT: "ответственный ни разу не написал",
    ISSUE_ABANDONED: "брошен дольше порога",
    ISSUE_MISSING_TRANSCRIPTS: "разговоры без расшифровки",
}

ALIAS_PHONE = "phone"
ALIAS_CONTACT = "contact"
ALIAS_KEY = "key"

# Таблицы, которые ссылаются на клиента колонкой client_key.
#
# Это не справка, а рабочий список: при переезде ключа (раздел 2.5) строка
# прежнего клиента удаляется, и всё, что на неё ссылалось, обязано быть
# перенацелено — иначе лента, карточки и разборы клиента исчезают вместе с
# ключом. Забыть одну таблицу здесь стоит потерянной истории, поэтому
# перенос ходит по этому списку, а тест сверяет список со схемой.
CLIENT_CHILD_TABLES: tuple[str, ...] = (
    "client_links",
    "client_aliases",
    "client_events",
    "client_reviews",
)

# Таблицы, которые при переезде ключа НЕ перенацеливают, а пересобирают.
# Разница не в аккуратности, а в природе данных: эти строки выводятся из
# портфеля целиком каждым полным прогоном, и переезд им не нужен — он
# случился бы за секунды до `DELETE`.
#
# Хуже того, он опасен. Ключ у `client_issues` составной (клиент + код), и
# `UPDATE SET client_key = ...` при совпадении кода у прежнего и нового
# клиента упёрся бы в уникальность и уронил ночной прогон целиком.
CLIENT_REBUILT_TABLES: tuple[str, ...] = (
    "client_issues",
)

_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS clients_meta (
        key    TEXT PRIMARY KEY,
        value  TEXT NOT NULL
    );
    """,
    # ---------- клиент ----------
    #
    # Счётчики (silence_days, calls_*, comments_*) нарочно БЕЗ
    # `NOT NULL DEFAULT 0`. Ноль здесь означал бы «посчитали и вышло ноль»,
    # а неполный прогон агрегаты не перезаписывает (раздел 4, шаг 2) — то
    # есть до первого полного прогона их не считали вовсе. NULL говорит это
    # прямо, ноль соврал бы: «звонков нет» вместо «не знаем». Цена лжи
    # известна заранее — no_assignee_comment у клиента, с которым работали.
    """
    CREATE TABLE IF NOT EXISTS clients (
        client_key            TEXT PRIMARY KEY,
        phone_norm            TEXT,
        phone_raw             TEXT    NOT NULL DEFAULT '',
        phone_valid           INTEGER NOT NULL DEFAULT 0,
        is_agent              INTEGER NOT NULL DEFAULT 0,
        agent_reason          TEXT    NOT NULL DEFAULT '',
        key_reason            TEXT    NOT NULL DEFAULT '',
        contact_id            INTEGER,
        name                  TEXT    NOT NULL DEFAULT '',
        assignee_id           INTEGER,
        assignee_name         TEXT    NOT NULL DEFAULT '',
        assignee_count        INTEGER,
        department_id         INTEGER,
        triage_state          TEXT    NOT NULL DEFAULT 'unknown',
        triage_reason         TEXT    NOT NULL DEFAULT '',
        last_touch_at         TEXT,
        last_touch_kind       TEXT,
        last_touch_author_id  INTEGER,
        last_event_at         TEXT,
        silence_days          INTEGER,
        next_step_at          TEXT,
        next_step_overdue     INTEGER,
        calls_total           INTEGER,
        calls_with_transcript INTEGER,
        calls_pending         INTEGER,
        comments_total        INTEGER,
        comments_by_assignee  INTEGER,
        comments_by_assignee_30d INTEGER,
        afina_id              TEXT,
        aggregates_run_id     INTEGER,
        -- Когда клиент пропал из портфеля: сделку удалили или увели в
        -- чужую воронку. Строку не удаляем — на неё ссылаются разборы и
        -- журнал переездов ключа, — но из списков и очереди разбора она
        -- уходит. Без этой пометки клиент оставался бы там навсегда с
        -- состоянием, замороженным на последнем видевшем его прогоне.
        left_at               TEXT,
        updated_at            TEXT    NOT NULL DEFAULT ''
    );
    """,
    # ---------- карточки клиента ----------
    """
    CREATE TABLE IF NOT EXISTS client_links (
        client_key   TEXT    NOT NULL,
        entity_type  TEXT    NOT NULL,
        entity_id    INTEGER NOT NULL,
        category_id  INTEGER,
        stage_id     TEXT    NOT NULL DEFAULT '',
        stage_name   TEXT    NOT NULL DEFAULT '',
        title        TEXT    NOT NULL DEFAULT '',
        date_create  TEXT,
        closed       INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (entity_type, entity_id)
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_client_links_key ON client_links(client_key);
    """,
    # ---------- прежние ключи, телефоны и контакты ----------
    #
    # Ключ первичен по (alias_type, alias_value), а не по клиенту: телефон
    # обязан вести РОВНО к одному клиенту. Если он ведёт к двум, это не
    # склейка, а конфликт (раздел 2.4) — и схема обязана не дать записать
    # его молча, а не полагаться на внимательность сборщика.
    """
    CREATE TABLE IF NOT EXISTS client_aliases (
        client_key   TEXT NOT NULL,
        alias_type   TEXT NOT NULL,
        alias_value  TEXT NOT NULL,
        PRIMARY KEY (alias_type, alias_value)
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_client_aliases_key ON client_aliases(client_key);
    """,
    # ---------- лента событий ----------
    #
    # Личность события — (kind, source_id), а не id: id здесь rowid, он
    # переиспользуется после удаления. Наступать на те же грабли, что
    # fact_stage_event.id в витрине, второй раз не будем — на него снаружи
    # не ссылается никто и ссылаться не должен.
    """
    CREATE TABLE IF NOT EXISTS client_events (
        id                 INTEGER PRIMARY KEY,
        client_key         TEXT    NOT NULL,
        at                 TEXT    NOT NULL,
        kind               TEXT    NOT NULL,
        source_id          TEXT    NOT NULL,
        entity_type        TEXT    NOT NULL DEFAULT '',
        entity_id          INTEGER,
        author_id          INTEGER,
        author_name        TEXT    NOT NULL DEFAULT '',
        author_is_assignee INTEGER,
        is_system          INTEGER NOT NULL DEFAULT 0,
        payload_json       TEXT    NOT NULL DEFAULT '{}',
        UNIQUE (kind, source_id)
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_client_events_feed ON client_events(client_key, at);
    """,
    # ---------- проблемы (раздел 8) ----------
    #
    # Таблицей, а не колонками: кодов пять сегодня и шесть завтра, и каждый
    # следующий стоил бы миграции. Считать по ней тоже проще — счётчики
    # вкладки «Исключения» это один GROUP BY, а не пять SUM.
    #
    # Пересобирается целиком каждым полным прогоном: проблема выводится из
    # портфеля, а не пишется человеком, и хранить её историю незачем —
    # история лежит в самом портфеле.
    """
    CREATE TABLE IF NOT EXISTS client_issues (
        client_key  TEXT NOT NULL,
        code        TEXT NOT NULL,
        PRIMARY KEY (client_key, code)
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_client_issues_code ON client_issues(code);
    """,
    # ---------- разборы ----------
    #
    # Единственная таблица базы, которую нельзя пересобрать: всё остальное
    # выводится из витрины и портала, а это — то, что написал человек или
    # модель. Её не удаляют при переезде ключа, а перенацеливают; её не
    # чистят прогоном. Если когда-нибудь встанет вопрос «пересоздать
    # clients.db с нуля» — ответ начинается с выгрузки этой таблицы.
    """
    CREATE TABLE IF NOT EXISTS client_reviews (
        id                INTEGER PRIMARY KEY,
        client_key        TEXT    NOT NULL,
        created_at        TEXT    NOT NULL,
        reviewed_through  TEXT    NOT NULL,
        summary           TEXT    NOT NULL DEFAULT '',
        verdict           TEXT    NOT NULL DEFAULT '',
        issues_json       TEXT    NOT NULL DEFAULT '[]',
        recommendation    TEXT    NOT NULL DEFAULT '',
        enough_data       INTEGER NOT NULL DEFAULT 1,
        author            TEXT    NOT NULL DEFAULT ''
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_client_reviews_key
        ON client_reviews(client_key, created_at DESC);
    """,
    # ---------- переезды ключей ----------
    """
    CREATE TABLE IF NOT EXISTS client_merges (
        id           INTEGER PRIMARY KEY,
        old_key      TEXT NOT NULL,
        new_key      TEXT NOT NULL,
        reason       TEXT NOT NULL DEFAULT '',
        detected_at  TEXT NOT NULL
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_client_merges_new ON client_merges(new_key);
    """,
    # ---------- один телефон у двух контактов ----------
    #
    # Первичный ключ по номеру, чтобы таблица не росла на каждом прогоне.
    # last_seen_at вместо resolved_at: конфликт «рассосался» не отдельным
    # событием, а тем, что очередной прогон его больше не увидел. Строка
    # со старым last_seen_at читается как «в прошлый раз уже не
    # повторилось»; удалять её нельзя — история склейки телефона это
    # единственное место, где вообще записана.
    """
    CREATE TABLE IF NOT EXISTS merge_conflicts (
        phone_norm       TEXT PRIMARY KEY,
        contact_ids_json TEXT NOT NULL DEFAULT '[]',
        detected_at      TEXT NOT NULL,
        last_seen_at     TEXT NOT NULL DEFAULT ''
    );
    """,
    # ---------- журнал прогонов ----------
    #
    # complete со значением по умолчанию 0: прогон, упавший до того, как
    # успел объявить себя полным, обязан читаться как неполный. Обратное
    # умолчание объявляло бы полным всё, что не дожило до конца.
    #
    # degraded_rules — список правил, которые в этом прогоне не работали из-за
    # пустой настройки (раздел 10 ТЗ). Без него пустые DOSSIER_REFUSAL_MARKERS
    # выключают правило «отказ» молча, и состояния выглядят посчитанными.
    #
    # mart_full_sync_at — когда витрину в последний раз сверяли целиком.
    # Комментарии закрытых сделок обновляются только полной сверкой, и без
    # этой отметки «у клиента нет новых комментариев» неотличимо от
    # «полная сверка не доходила» (раздел 14 ТЗ).
    """
    CREATE TABLE IF NOT EXISTS client_runs (
        id                 INTEGER PRIMARY KEY,
        started_at         TEXT    NOT NULL,
        finished_at        TEXT,
        mode               TEXT    NOT NULL DEFAULT '',
        cards              INTEGER NOT NULL DEFAULT 0,
        errors             INTEGER NOT NULL DEFAULT 0,
        complete           INTEGER NOT NULL DEFAULT 0,
        degraded_rules     TEXT    NOT NULL DEFAULT '',
        mart_full_sync_at  TEXT
    );
    """,
)


# --------------------------------------------------------------------------
# соединение
# --------------------------------------------------------------------------

def resolve_db_path(db_path: str | Path | None = None) -> Path:
    """Путь к базе клиентского слоя.

    Повторяет разбор витрины намеренно, включая запасной путь через
    переменную окружения: если настройки не загрузились — не хватает
    обязательного поля, скрипт запущен без полного окружения, — путь
    всё равно берётся из ``CLIENTS_DB_PATH``, и только потом из умолчания.

    Ловушка, ради которой это написано, уже срабатывала на витрине:
    человек называет базу переменной окружения, Settings молча падает на
    нехватке ключа Битрикса, и диагностический запрос уходит в боевой файл
    вместо названного — ничем этого не показав.
    """
    if db_path is not None:
        return Path(db_path)
    try:
        from config import get_settings

        return Path(get_settings().clients_db_path)
    except Exception:  # pragma: no cover - конфиг недоступен в изолированных тестах
        return Path(os.environ.get("CLIENTS_DB_PATH") or DEFAULT_DB_PATH)


def get_connection(db_path: str | Path | None = None, *, readonly: bool = False):
    """Соединение с базой клиентов. Вызывающий обязан закрыть — лучше clients_session()."""
    path = resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if readonly and path.exists():
        # Веб не должен уметь писать: единственный писатель — прогон сборки.
        conn = sqlite3.connect(
            f"file:{path}?mode=ro",
            uri=True,
            timeout=BUSY_TIMEOUT_MS / 1000,
            check_same_thread=False,
        )
    else:
        conn = sqlite3.connect(
            str(path),
            timeout=BUSY_TIMEOUT_MS / 1000,
            check_same_thread=False,
        )
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def clients_session(
    db_path: str | Path | None = None,
    *,
    readonly: bool = False,
) -> Iterator[sqlite3.Connection]:
    """Транзакционное соединение, которое всегда закрывается.

    Голый `with sqlite3.connect(...)` коммитит, но не закрывает: в WAL
    утёкший хендл держит read-lock и блокирует checkpoint.
    """
    conn = get_connection(db_path, readonly=readonly)
    try:
        if readonly:
            yield conn
        else:
            with conn:
                yield conn
    finally:
        conn.close()


# Колонки, дописываемые в уже заведённую clients. Все наполняются ближайшей
# полной пересборкой — ключ и агрегаты считаются заново каждым прогоном, —
# поэтому значения по умолчанию здесь только «мы это ещё не считали».
_ADDED_COLUMNS = (
    ("key_reason", "TEXT NOT NULL DEFAULT ''"),
    # Без DEFAULT 0 по той же причине, что и остальные счётчики: ноль
    # брокеров невозможен, и он означал бы не «их нет», а «не считали».
    ("assignee_count", "INTEGER"),
    ("left_at", "TEXT"),
)


def _migrate_columns(conn: sqlite3.Connection) -> None:
    """Дописать колонки базам, заведённым прежними редакциями схемы."""
    for name, declaration in _ADDED_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE clients ADD COLUMN {name} {declaration}")
        except sqlite3.OperationalError:
            pass


def _schema_is_current(db_path: str | Path | None) -> bool:
    """Схема уже нужной версии — писать нечего.

    Проверка read-only по той же причине, что у витрины: DDL, даже ничего
    не меняющий, берёт блокировку записи, и задача, которая просто
    убедилась, что таблицы на месте, встала бы поперёк идущей сборки.
    """
    try:
        with clients_session(db_path, readonly=True) as conn:
            row = conn.execute(
                "SELECT value FROM clients_meta WHERE key = 'schema_version'"
            ).fetchone()
    except sqlite3.OperationalError:
        # Таблиц ещё нет (или файла): базу надо создавать целиком.
        return False
    return bool(row) and str(row[0]) == str(SCHEMA_VERSION)


def init_clients_db(db_path: str | Path | None = None) -> None:
    """Создать таблицы клиентского слоя, если их нет. Идемпотентно.

    ВАЖНО: новая миграция обязана поднимать SCHEMA_VERSION. Версия здесь не
    украшение, а условие: совпала — функция не делает НИЧЕГО, и миграцию,
    добавленную без поднятия версии, никто не позовёт. На витрине это уже
    стоило колонки, которой не было в боевой базе при «правильной» версии.
    """
    if _schema_is_current(db_path):
        return
    with clients_session(db_path) as conn:
        for statement in _DDL:
            conn.execute(statement)
        _migrate_columns(conn)
        conn.execute(
            "INSERT INTO clients_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
