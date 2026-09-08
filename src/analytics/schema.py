"""Схема аналитической витрины (data/analytics.db).

Отдельный файл БД, а не data/violations.db: там 14 cron-задач и busy_timeout
подобран под короткие записи аудита. У витрины другой профиль — массовая
перезапись раз в 15 минут плюс постоянный читатель-веб, — и мешать их в одном
файле значит ловить «database is locked» на обеих сторонах.

Все временные метки хранятся как UTC ISO-8601 (см. analytics.client.to_utc_iso).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("data/analytics.db")

# Витрину читает веб и одновременно пишет ETL. В WAL читатели не блокируются,
# но ночная полная сверка держит транзакцию дольше обычного — берём тот же
# запас, что и основная база проекта.
BUSY_TIMEOUT_MS = 10_000

SCHEMA_VERSION = 4

# Семантика стадии. Без неё нельзя посчитать ни конверсию, ни win rate:
# «выиграно» и «проиграно» надо отличать от «в работе», а по одному только
# STATUS_ID это делается лишь угадыванием суффикса.
SEMANTIC_IN_PROGRESS = "in_progress"
SEMANTIC_WON = "won"
SEMANTIC_LOST = "lost"

_DDL: tuple[str, ...] = (
    # ---------- измерения ----------
    """
    CREATE TABLE IF NOT EXISTS dim_pipeline (
        category_id  INTEGER PRIMARY KEY,
        name         TEXT    NOT NULL,
        is_active    INTEGER NOT NULL DEFAULT 1,
        sort         INTEGER NOT NULL DEFAULT 0,
        synced_at    TEXT    NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS dim_stage (
        stage_id     TEXT    NOT NULL,
        category_id  INTEGER NOT NULL,
        name         TEXT    NOT NULL,
        sort         INTEGER NOT NULL DEFAULT 0,
        semantic     TEXT    NOT NULL DEFAULT 'in_progress',
        synced_at    TEXT    NOT NULL,
        PRIMARY KEY (stage_id, category_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS dim_lead_status (
        status_id    TEXT PRIMARY KEY,
        name         TEXT NOT NULL,
        sort         INTEGER NOT NULL DEFAULT 0,
        semantic     TEXT NOT NULL DEFAULT 'in_progress',
        synced_at    TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS dim_user (
        user_id          INTEGER PRIMARY KEY,
        name             TEXT    NOT NULL,
        last_name        TEXT    NOT NULL DEFAULT '',
        department_id    INTEGER,
        department_name  TEXT    NOT NULL DEFAULT '',
        is_active        INTEGER NOT NULL DEFAULT 1,
        synced_at        TEXT    NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS dim_source (
        source_id  TEXT PRIMARY KEY,
        name       TEXT NOT NULL,
        synced_at  TEXT NOT NULL
    );
    """,
    # ---------- факты ----------
    """
    CREATE TABLE IF NOT EXISTS fact_lead (
        lead_id            INTEGER PRIMARY KEY,
        title              TEXT    NOT NULL DEFAULT '',
        status_id          TEXT    NOT NULL DEFAULT '',
        source_id          TEXT    NOT NULL DEFAULT '',
        assigned_by_id     INTEGER,
        date_create        TEXT,
        date_modify        TEXT,
        date_closed        TEXT,
        opportunity        REAL    NOT NULL DEFAULT 0,
        currency_id        TEXT    NOT NULL DEFAULT '',
        is_converted       INTEGER NOT NULL DEFAULT 0,
        converted_deal_id  INTEGER,
        contact_id         INTEGER,
        company_id         INTEGER,
        is_deleted         INTEGER NOT NULL DEFAULT 0,
        synced_at          TEXT    NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS fact_deal (
        deal_id         INTEGER PRIMARY KEY,
        title           TEXT    NOT NULL DEFAULT '',
        category_id     INTEGER NOT NULL DEFAULT 0,
        stage_id        TEXT    NOT NULL DEFAULT '',
        assigned_by_id  INTEGER,
        source_id       TEXT    NOT NULL DEFAULT '',
        opportunity     REAL    NOT NULL DEFAULT 0,
        currency_id     TEXT    NOT NULL DEFAULT '',
        date_create     TEXT,
        date_modify     TEXT,
        begindate       TEXT,
        closedate       TEXT,
        is_closed       INTEGER NOT NULL DEFAULT 0,
        is_won          INTEGER NOT NULL DEFAULT 0,
        is_lost         INTEGER NOT NULL DEFAULT 0,
        lead_id         INTEGER,
        contact_id      INTEGER,
        company_id      INTEGER,
        moved_time      TEXT,
        moved_by_id     INTEGER,
        is_deleted      INTEGER NOT NULL DEFAULT 0,
        synced_at       TEXT    NOT NULL
    );
    """,
    # entered_at/left_at — интервал пребывания на стадии. left_at IS NULL
    # означает «сущность на этой стадии сейчас», а не «данных нет».
    """
    CREATE TABLE IF NOT EXISTS fact_stage_event (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_type  TEXT    NOT NULL,
        entity_id    INTEGER NOT NULL,
        category_id  INTEGER NOT NULL DEFAULT 0,
        stage_id     TEXT    NOT NULL,
        entered_at   TEXT    NOT NULL,
        left_at      TEXT,
        duration_sec INTEGER,
        seq          INTEGER NOT NULL,
        UNIQUE (entity_type, entity_id, seq)
    );
    """,
    # ---------- план ----------
    # Период объявляется агентством, а не выводится из календаря. Пресет
    # 'quarter' в metrics.resolve_period даёт «квартал по сегодня», и планом
    # он быть не может: в первый день квартала выполнение вышло бы 100%.
    """
    CREATE TABLE IF NOT EXISTS fact_activity (
        activity_id        INTEGER PRIMARY KEY,
        owner_type_id      INTEGER NOT NULL,
        owner_id           INTEGER NOT NULL,
        provider_type_id   TEXT NOT NULL DEFAULT '',
        direction          INTEGER,
        subject            TEXT NOT NULL DEFAULT '',
        responsible_id     INTEGER,
        created_at         TEXT NOT NULL,
        start_time         TEXT,
        end_time           TEXT,
        completed          INTEGER NOT NULL DEFAULT 0,
        synced_at          TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS plan_period (
        period_code TEXT PRIMARY KEY,
        starts_at   TEXT NOT NULL,
        ends_at     TEXT NOT NULL,
        label       TEXT NOT NULL DEFAULT '',
        updated_at  TEXT NOT NULL
    );
    """,
    # Норма, а не готовая сумма. План агентства — «4,5 млн на брокера за
    # квартал», то есть он ВЫЧИСЛЯЕТСЯ из штата и меняется вместе с ним.
    # Хранить посчитанное число значит завести второй источник правды,
    # который разойдётся с первым в первый же наём.
    #
    # scope_id = 0 для компании, а не NULL: в SQLite несколько NULL в
    # первичном ключе считаются разными значениями, и строка компании
    # завелась бы заново при каждой синхронизации.
    """
    CREATE TABLE IF NOT EXISTS plan_norm (
        period_code TEXT    NOT NULL,
        scope_kind  TEXT    NOT NULL,
        scope_id    INTEGER NOT NULL DEFAULT 0,
        metric      TEXT    NOT NULL,
        basis       TEXT    NOT NULL DEFAULT 'per_broker',
        amount      REAL    NOT NULL,
        source      TEXT    NOT NULL DEFAULT 'sheet',
        updated_at  TEXT    NOT NULL,
        PRIMARY KEY (period_code, scope_kind, scope_id, metric)
    );
    """,
    # Ручные исключения из планового состава. Без них состав нельзя починить
    # там, где портал говорит неправду: РОП, числящийся в служебном отделе,
    # отключённая учётка РОПа при живом отделе, стажёр, которому норму ещё
    # не ставят. Правка фамилии в Битриксе ради отчёта — цена выше ошибки.
    #
    # department_id перекрывает отдел из dim_user: это и есть случай РОПа,
    # административно приписанного не к своему подразделению.
    """
    CREATE TABLE IF NOT EXISTS plan_roster (
        period_code   TEXT    NOT NULL,
        user_id       INTEGER NOT NULL,
        department_id INTEGER,
        plan_role     TEXT    NOT NULL,
        note          TEXT    NOT NULL DEFAULT '',
        updated_at    TEXT    NOT NULL,
        PRIMARY KEY (period_code, user_id)
    );
    """,
    # ---------- служебные ----------
    """
    CREATE TABLE IF NOT EXISTS etl_watermark (
        entity             TEXT PRIMARY KEY,
        last_date_modify   TEXT,
        last_full_sync_at  TEXT,
        backfill_since     TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS etl_run (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        kind          TEXT NOT NULL,
        entity        TEXT NOT NULL DEFAULT '',
        started_at    TEXT NOT NULL,
        finished_at   TEXT,
        status        TEXT NOT NULL DEFAULT 'running',
        rows_upserted INTEGER NOT NULL DEFAULT 0,
        error         TEXT NOT NULL DEFAULT ''
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS analytics_meta (
        key    TEXT PRIMARY KEY,
        value  TEXT NOT NULL
    );
    """,
    # ---------- индексы ----------
    "CREATE INDEX IF NOT EXISTS idx_deal_cat_stage ON fact_deal(category_id, stage_id);",
    "CREATE INDEX IF NOT EXISTS idx_deal_created ON fact_deal(date_create);",
    "CREATE INDEX IF NOT EXISTS idx_deal_closed ON fact_deal(closedate);",
    "CREATE INDEX IF NOT EXISTS idx_deal_assignee ON fact_deal(assigned_by_id, date_create);",
    "CREATE INDEX IF NOT EXISTS idx_deal_modify ON fact_deal(date_modify);",
    "CREATE INDEX IF NOT EXISTS idx_lead_status ON fact_lead(status_id);",
    "CREATE INDEX IF NOT EXISTS idx_lead_created ON fact_lead(date_create);",
    "CREATE INDEX IF NOT EXISTS idx_lead_source ON fact_lead(source_id, date_create);",
    "CREATE INDEX IF NOT EXISTS idx_lead_modify ON fact_lead(date_modify);",
    "CREATE INDEX IF NOT EXISTS idx_stage_event_entity "
    "ON fact_stage_event(entity_type, entity_id);",
    "CREATE INDEX IF NOT EXISTS idx_stage_event_stage "
    "ON fact_stage_event(category_id, stage_id, entered_at);",
    "CREATE INDEX IF NOT EXISTS idx_stage_event_entered ON fact_stage_event(entered_at);",
    # Действия ищут тремя способами: «что было по этой карточке»,
    # «что делал этот человек» и «что случилось за вчера».
    "CREATE INDEX IF NOT EXISTS idx_activity_owner "
    "ON fact_activity(owner_type_id, owner_id, created_at);",
    "CREATE INDEX IF NOT EXISTS idx_activity_person "
    "ON fact_activity(responsible_id, created_at);",
    "CREATE INDEX IF NOT EXISTS idx_activity_created ON fact_activity(created_at);",
)


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    """Путь к файлу витрины. None → значение из настроек.

    Если настройки не загрузились — не хватает обязательного поля, скрипт
    запущен без полного окружения, — путь всё равно берётся из
    ``ANALYTICS_DB_PATH``, если он задан, и только потом из умолчания.

    Иначе выходит ловушка: человек указывает путь переменной окружения,
    настройки молча падают на нехватке ключа Битрикса, и скрипт читает или
    ПИШЕТ в боевую витрину вместо названной. Поймано на своём же
    диагностическом запросе — он собирался ходить во временную базу, а сходил
    в data/analytics.db и ничем этого не показал.
    """
    if db_path is not None:
        return Path(db_path)
    try:
        from config import get_settings

        return Path(get_settings().analytics_db_path)
    except Exception:  # pragma: no cover - конфиг недоступен в изолированных тестах
        return Path(os.environ.get("ANALYTICS_DB_PATH") or DEFAULT_DB_PATH)


def get_connection(db_path: str | Path | None = None, *, readonly: bool = False):
    """Соединение с витриной. Вызывающий обязан закрыть — лучше analytics_session()."""
    path = resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if readonly and path.exists():
        # Веб не должен уметь писать в витрину: единственный писатель — ETL.
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
def analytics_session(
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


def _migrate_dim_user_last_name(conn: sqlite3.Connection) -> None:
    """Добавить last_name витринам, созданным до появления колонки.

    Фамилия отдельным полем нужна, чтобы сопоставить отдел с РОПом по списку
    фамилий из qc_delivery. Разбирать её из полного имени нельзя: «Шпырная
    Юлия» и «Юлия Шпырная» встречаются в портале одинаково часто.
    """
    try:
        conn.execute("ALTER TABLE dim_user ADD COLUMN last_name TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass


def init_analytics_db(db_path: str | Path | None = None) -> None:
    """Создать таблицы витрины, если их нет. Идемпотентно."""
    with analytics_session(db_path) as conn:
        for statement in _DDL:
            conn.execute(statement)
        _migrate_dim_user_last_name(conn)
        conn.execute(
            "INSERT INTO analytics_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
