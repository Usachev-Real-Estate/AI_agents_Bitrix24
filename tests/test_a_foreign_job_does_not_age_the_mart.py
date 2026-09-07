"""Свежесть витрины считается по прогонам ETL, а не по любой записи в журнале.

В etl_run пишет не только ETL. Часовая синхронизация планов из Google-таблицы
тоже будет отмечаться там — ей нужен тот же журнал прогонов и то же
уведомление админа при падении. Но витрину из Bitrix она не обновляет.

Пока «последний успешный прогон» брался как первая успешная строка журнала,
любая такая задача становилась ответом на вопрос «насколько свежие данные».
Шапка каждой страницы показывала бы «данные 3 минуты назад» при ETL,
остановившемся сутки назад, — то есть ровно ту ошибку, от которой страница
«Качество данных» и должна защищать: устаревшие данные опаснее отсутствующих,
потому что выглядят живыми.
"""

import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

import metrics
from schema import analytics_session
from scope import Scope, scoped_session


def _run(conn, kind, finished_at, status="ok"):
    conn.execute(
        "INSERT INTO etl_run(kind, entity, started_at, finished_at, status,"
        " rows_upserted, error) VALUES (?, '', ?, ?, ?, 0, '')",
        (kind, finished_at, finished_at, status),
    )


def test_a_plans_sync_is_not_the_marts_freshness(analytics_db):
    """Свежая чужая задача не выдаёт себя за свежую витрину."""
    with analytics_session() as conn:
        _run(conn, "incremental", "2026-09-05T06:00:00+00:00")
        _run(conn, "plans", "2026-09-06T09:00:00+00:00")

    with scoped_session(Scope.everything()) as conn:
        status = metrics.etl_status(conn)

    assert status["last_ok"]["kind"] == "incremental"
    assert status["last_ok"]["finished_at"] == "2026-09-05T06:00:00+00:00", (
        "свежесть обязана считаться по прогону, который действительно обновлял витрину"
    )


def test_a_long_queue_of_foreign_jobs_does_not_hide_the_etl(analytics_db):
    """Настоящий прогон находится, даже когда чужих записей больше, чем помещается на экран.

    Список для страницы обрезан по LIMIT. Если искать последний успешный
    прогон перебором этого списка, то полсотни часовых синхронизаций вытеснят
    из него ETL, и живая витрина отрапортует «данные не загружались».
    """
    with analytics_session() as conn:
        _run(conn, "full", "2026-09-01T02:30:00+00:00")
        for hour in range(60):
            _run(conn, "plans", f"2026-09-03T{hour % 24:02d}:20:00+00:00")

    with scoped_session(Scope.everything()) as conn:
        status = metrics.etl_status(conn)

    assert status["last_ok"] is not None, "живая витрина не должна выглядеть незагруженной"
    assert status["last_ok"]["kind"] == "full"


def test_a_failed_etl_is_not_rescued_by_a_successful_neighbour(analytics_db):
    """Упавший ETL остаётся упавшим: успех чужой задачи его не заменяет."""
    with analytics_session() as conn:
        _run(conn, "incremental", "2026-09-06T08:00:00+00:00", status="error")
        _run(conn, "plans", "2026-09-06T09:00:00+00:00")

    with scoped_session(Scope.everything()) as conn:
        status = metrics.etl_status(conn)

    assert status["last_ok"] is None, "витрина не обновлялась — так и надо сказать"
    assert status["lag_minutes"] is None
