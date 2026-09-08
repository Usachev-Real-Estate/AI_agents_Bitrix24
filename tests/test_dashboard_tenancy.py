"""Разграничение доступа: РОП видит свой отдел, админ — всё.

Проверка построена как ПЕРЕБОР всех маршрутов приложения, а не как список
известных мест. Смысл в том, что новая страница или новый JSON-эндпоинт,
забывший про область видимости, обязан провалить этот тест — иначе проверка
устаревает в тот же день, когда её написали.

Данные размечены так, что чужое видно невооружённым глазом: у каждого отдела
свой маркер в названиях сделок, лидов и в имени ответственного. Маркер ищется
в HTML, в островках JSON для графиков и в выгрузке CSV — то есть во всём, что
уходит наружу.
"""

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path
from fastapi.testclient import TestClient

import store
from app import create_app
from config import get_settings
from schema import analytics_session

BASE = "/dashboard"
PASSWORD = "correct-horse-battery"

DEPT_A, DEPT_B = 44, 50
MARK_A, MARK_B = "АЛЬФАМЕТКА", "БЕТАМЕТКА"
USER_A, USER_B = 32, 77
DEAL_A, DEAL_B = 101, 202
LEAD_A, LEAD_B = 901, 902

PERIOD = "?start=2026-08-01&end=2026-08-31&category=18"


@pytest.fixture
def tenancy_app(analytics_db, monkeypatch):
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "t" * 48)
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    get_settings.cache_clear()

    with analytics_session() as conn:
        conn.execute(
            "INSERT INTO dim_pipeline(category_id, name, is_active, sort, synced_at) "
            "VALUES (18, 'Покупатели', 1, 10, 'x')"
        )
        for stage_id, name, sort, semantic in (
            ("C18:NEW", "Подбор", 10, "in_progress"),
            ("C18:SHOW", "Показ", 20, "in_progress"),
            ("C18:WON", "Договор закрыт", 90, "won"),
        ):
            conn.execute(
                "INSERT INTO dim_stage(stage_id, category_id, name, sort, semantic, "
                "synced_at) VALUES (?, 18, ?, ?, ?, 'x')", (stage_id, name, sort, semantic))
        conn.execute(
            "INSERT INTO dim_source(source_id, name, synced_at) VALUES ('CALL', 'Звонок', 'x')")
        conn.execute(
            "INSERT INTO dim_lead_status(status_id, name, sort, semantic, synced_at) "
            "VALUES ('NEW', 'Не обработан', 10, 'in_progress', 'x')")

        for user_id, dept, mark in ((USER_A, DEPT_A, MARK_A), (USER_B, DEPT_B, MARK_B)):
            conn.execute(
                "INSERT INTO dim_user(user_id, name, department_id, department_name, "
                "is_active, synced_at) VALUES (?, ?, ?, ?, 1, 'x')",
                (user_id, f"Брокер {mark}", dept, f"Отдел {mark}"))

        for deal_id, user_id, mark in ((DEAL_A, USER_A, MARK_A), (DEAL_B, USER_B, MARK_B)):
            conn.execute(
                "INSERT INTO fact_deal(deal_id, title, category_id, stage_id, "
                "assigned_by_id, source_id, opportunity, date_create, closedate, "
                "is_closed, is_won, is_lost, is_deleted, synced_at) "
                "VALUES (?, ?, 18, 'C18:SHOW', ?, 'CALL', 500000, "
                "'2026-08-01T00:00:00+00:00', NULL, 0, 0, 0, 0, 'x')",
                (deal_id, f"Квартира {mark}", user_id))
            for seq, (stage, entered, left) in enumerate((
                ("C18:NEW", "2026-08-01T00:00:00+00:00", "2026-08-04T00:00:00+00:00"),
                ("C18:SHOW", "2026-08-04T00:00:00+00:00", None),
            )):
                conn.execute(
                    "INSERT INTO fact_stage_event(entity_type, entity_id, category_id, "
                    "stage_id, entered_at, left_at, duration_sec, seq) "
                    "VALUES ('deal', ?, 18, ?, ?, ?, ?, ?)",
                    (deal_id, stage, entered, left, 259200 if left else None, seq))

        for lead_id, user_id, mark in ((LEAD_A, USER_A, MARK_A), (LEAD_B, USER_B, MARK_B)):
            conn.execute(
                "INSERT INTO fact_lead(lead_id, title, status_id, source_id, "
                "assigned_by_id, date_create, is_converted, is_deleted, synced_at) "
                "VALUES (?, ?, 'NEW', 'CALL', ?, '2026-08-01T00:00:00+00:00', 0, 0, 'x')",
                (lead_id, f"Заявка {mark}", user_id))

        conn.execute(
            "INSERT INTO etl_run(kind, entity, started_at, finished_at, status, "
            "rows_upserted) VALUES ('backfill', '', '2026-08-20T10:00:00+00:00', "
            "'2026-08-20T10:01:00+00:00', 'ok', 4)")

    application = create_app()
    store.create_user("boss", PASSWORD, "Директор", role="admin")
    store.create_user("rop_a", PASSWORD, "РОП А", role="rop", department_ids=[DEPT_A])
    store.create_user("rop_b", PASSWORD, "РОП Б", role="rop", department_ids=[DEPT_B])
    store.create_user("rop_none", PASSWORD, "РОП без отдела", role="rop")
    return application


def _login(app, username):
    session = TestClient(app, follow_redirects=False)
    session.get(f"{BASE}/login")
    response = session.post(f"{BASE}/login", data={
        "username": username, "password": PASSWORD,
        "csrf_token": session.cookies.get("dash_csrf"), "next": "",
    })
    assert response.status_code == 303, f"{username}: {response.text[:300]}"
    return session


@pytest.fixture
def admin(tenancy_app):
    return _login(tenancy_app, "boss")


@pytest.fixture
def rop_a(tenancy_app):
    return _login(tenancy_app, "rop_a")


@pytest.fixture
def rop_none(tenancy_app):
    return _login(tenancy_app, "rop_none")


def _data_paths(app) -> list[str]:
    """Все маршруты, отдающие данные: страницы, JSON и выгрузка."""
    paths = []
    for route in app.routes:
        included = getattr(route, "original_router", None)
        if included is None:
            continue
        for sub in included.routes:
            path = getattr(sub, "path", "")
            if path and path not in ("/login", "/logout"):
                paths.append(f"{BASE}{path}")
    return sorted(set(paths))


# --------------------------------------------------------------------------
# главная проверка: перебор всех маршрутов
# --------------------------------------------------------------------------

def test_route_enumeration_actually_finds_the_data_routes(tenancy_app):
    """Страховка на саму проверку: список маршрутов не должен схлопнуться."""
    paths = _data_paths(tenancy_app)
    assert f"{BASE}/" in paths and f"{BASE}/table" in paths
    assert f"{BASE}/api/export.csv" in paths
    assert len(paths) >= 12


def test_no_route_leaks_another_department(rop_a, tenancy_app):
    """Ни одна страница, ни один JSON, ни выгрузка не отдают чужой отдел."""
    leaked = []
    for path in _data_paths(tenancy_app):
        body = rop_a.get(f"{path}{PERIOD}").content
        if MARK_B.encode() in body:
            leaked.append(path)
    assert leaked == [], f"чужой отдел просочился в: {leaked}"


def test_own_department_is_actually_visible(rop_a, tenancy_app):
    """Обратная сторона: ограничение не должно прятать и своё."""
    body = rop_a.get(f"{BASE}/table{PERIOD}").text
    assert MARK_A in body, "РОП не видит собственные сделки — ограничение слишком широкое"


def test_admin_sees_both_departments(admin):
    body = admin.get(f"{BASE}/table{PERIOD}").text
    assert MARK_A in body and MARK_B in body


def test_user_without_departments_sees_nothing(rop_none, tenancy_app):
    """Недонастроенная учётка обязана закрываться, а не открываться.

    Если признаком «видеть всё» был бы пустой список отделов, забытая
    настройка отдавала бы РОПу всю компанию. Признак — отдельная роль.
    """
    for path in _data_paths(tenancy_app):
        body = rop_none.get(f"{path}{PERIOD}").content
        assert MARK_A.encode() not in body, path
        assert MARK_B.encode() not in body, path


# --------------------------------------------------------------------------
# подмена параметров в адресе
# --------------------------------------------------------------------------

# Признак того, что чужая СТРОКА действительно отрисована: ссылка на карточку.
# Проверять по маркеру нельзя — строку поиска страница возвращает в поле формы,
# и РОП, набравший чужое название, увидел бы собственный ввод, а не утечку.
FOREIGN_ROW_MARKERS = (
    f"/crm/deal/details/{DEAL_B}/".encode(),
    f"/crm/lead/details/{LEAD_B}/".encode(),
)


@pytest.mark.parametrize("query", [
    f"&department={DEPT_B}",
    f"&assignee={USER_B}",
    f"&q={DEAL_B}",
    f"&q=Квартира {MARK_B}",
    f"&entity=lead&q={LEAD_B}",
    f"&department={DEPT_B}&all_time=1&size=500",
    "&department=all&all_time=1",
    f"&stage=C18:SHOW&assignee={USER_B}&all_time=1",
])
def test_crafted_query_cannot_reach_another_department(rop_a, query):
    """Авторизованный РОП правит адрес руками — это первое, что он попробует."""
    for path in (f"{BASE}/table", f"{BASE}/movement", f"{BASE}/api/table",
                 f"{BASE}/api/export.csv", f"{BASE}/api/funnel"):
        body = rop_a.get(f"{path}{PERIOD}{query}").content
        for marker in FOREIGN_ROW_MARKERS:
            assert marker not in body, f"{path}{query}: отрисована чужая карточка"
        assert f"Брокер {MARK_B}".encode() not in body, f"{path}{query}"


def test_search_by_foreign_deal_id_returns_nothing(rop_a, admin):
    """Точечный запрос по ID чужой сделки — самый прямой способ проверить."""
    theirs = rop_a.get(f"{BASE}/api/table{PERIOD}&all_time=1&q={DEAL_B}").json()
    everything = admin.get(f"{BASE}/api/table{PERIOD}&all_time=1&q={DEAL_B}").json()
    assert theirs["total"] == 0
    assert everything["total"] == 1


def test_csv_export_is_scoped(rop_a):
    text = rop_a.get(f"{BASE}/api/export.csv{PERIOD}&all_time=1&size=5000").content.decode(
        "utf-8-sig")
    assert MARK_A in text
    assert MARK_B not in text


def test_api_numbers_are_scoped(rop_a, admin):
    """Не только строки: агрегаты тоже должны считаться по своему отделу."""
    theirs = rop_a.get(f"{BASE}/api/funnel{PERIOD}").json()
    everything = admin.get(f"{BASE}/api/funnel{PERIOD}").json()
    assert theirs["funnel"]["cohort_size"] == 1
    assert everything["funnel"]["cohort_size"] == 2


def test_money_is_scoped(rop_a, admin):
    theirs = rop_a.get(f"{BASE}/api/overview{PERIOD}").json()
    everything = admin.get(f"{BASE}/api/overview{PERIOD}").json()
    assert theirs["overview"]["money"]["open_amount"] == 500000
    assert everything["overview"]["money"]["open_amount"] == 1000000


def test_people_list_does_not_expose_other_departments(rop_a):
    body = rop_a.get(f"{BASE}/people{PERIOD}").text
    assert f"Брокер {MARK_A}" in body
    assert f"Брокер {MARK_B}" not in body
    assert f"Отдел {MARK_B}" not in body


def test_department_dropdown_offers_only_own_departments(rop_a, admin):
    """Иначе РОП выберет чужой отдел, получит пустую страницу и решит, что там ноль."""
    theirs = rop_a.get(f"{BASE}/movement{PERIOD}").text
    everything = admin.get(f"{BASE}/movement{PERIOD}").text
    assert f"Отдел {MARK_B}" not in theirs
    assert f"Отдел {MARK_A}" in everything and f"Отдел {MARK_B}" in everything


# --------------------------------------------------------------------------
# смена прав
# --------------------------------------------------------------------------

def test_role_change_takes_effect_without_relogin(rop_a):
    """Права читаются на каждый запрос, а не кладутся в куку при входе.

    Иначе закрытие доступа вступало бы в силу только через 12 часов, когда
    истечёт сессия — а закрывают его обычно ровно тогда, когда нужно сейчас.
    """
    assert MARK_A in rop_a.get(f"{BASE}/table{PERIOD}").text

    store.set_user_role("rop_a", "rop", [])
    assert MARK_A not in rop_a.get(f"{BASE}/table{PERIOD}").text

    store.set_user_role("rop_a", "admin")
    body = rop_a.get(f"{BASE}/table{PERIOD}").text
    assert MARK_A in body and MARK_B in body


def test_disabled_user_loses_access_immediately(rop_a):
    store.set_user_active("rop_a", False)
    assert rop_a.get(f"{BASE}/table{PERIOD}").status_code == 303


def test_forgotten_role_flag_creates_the_least_privileged_user():
    """Ошибка в сторону меньшего доступа исправляется командой, обратная — утечкой."""
    import inspect

    signature = inspect.signature(store.create_user)
    assert signature.parameters["role"].default == store.ROLE_ROP


# --------------------------------------------------------------------------
# запрет обхода представлений
# --------------------------------------------------------------------------

def test_metrics_never_touch_raw_tables_directly():
    """Запрос мимо представления обошёл бы ограничение целиком.

    Область видимости живёт в представлениях v_deal / v_lead / v_stage_event /
    v_user, которые создаются на соединении. Запрос, обратившийся к fact_deal
    напрямую, вернул бы данные всей компании независимо от роли — и заметить
    это по внешнему виду страницы было бы невозможно.
    """
    import re
    from pathlib import Path

    analytics = Path(__file__).resolve().parent.parent / "src" / "analytics"
    # Список файлов и список таблиц растут вместе: новая адресная таблица без
    # строки здесь защищена только памятью следующего разработчика.
    forbidden = (
        "fact_deal", "fact_lead", "fact_stage_event", "fact_activity",
        "dim_user", "plan_norm", "plan_roster",
    )
    for name in ("metrics.py", "plans.py", "pulse.py", "events.py", "work.py"):
        source = (analytics / name).read_text(encoding="utf-8")
        found = {table for table in forbidden if re.search(rf"\b{table}\b", source)}
        assert not found, (
            f"{name} обращается к таблицам мимо представлений: {sorted(found)}. "
            "Используйте v_deal / v_lead / v_stage_event / v_activity / "
            "v_user / v_plan_norm / v_plan_roster."
        )


def test_scoped_views_exist_only_on_a_scoped_connection(analytics_db):
    """Метрику нельзя вызвать на неограниченном соединении — она упадёт.

    Это не косметика: без такого поведения забытая область видимости давала бы
    полный доступ вместо ошибки.
    """
    import sqlite3

    import metrics

    with analytics_session(readonly=True) as conn:
        with pytest.raises(sqlite3.OperationalError, match="v_deal"):
            metrics.counts_by_pipeline(conn)


def test_drilldown_from_movement_keeps_the_department(admin):
    """Разложение обязано сходиться с числом, по которому кликнули.

    Администратор фильтрует «Движение» по отделу и жмёт «карточки →». Если
    таблица фильтр не применит, он увидит всю компанию вместо отдела и
    решит, что цифра на предыдущей странице врала.
    """
    scoped = admin.get(f"{BASE}/api/table{PERIOD}&all_time=1&department={DEPT_A}").json()
    everything = admin.get(f"{BASE}/api/table{PERIOD}&all_time=1").json()
    assert everything["total"] == 2
    assert scoped["total"] == 1
    assert scoped["rows"][0]["id"] == DEAL_A


def test_movement_drilldown_link_carries_the_department(admin):
    body = admin.get(f"{BASE}/movement{PERIOD}&department={DEPT_A}").text
    link_start = body.index(f"{BASE}/table?")
    assert f"department={DEPT_A}" in body[link_start:link_start + 300]


def test_rop_cannot_widen_the_table_by_department(rop_a):
    """РОП подставляет чужой отдел в фильтр таблицы — должно остаться своё."""
    theirs = rop_a.get(f"{BASE}/api/table{PERIOD}&all_time=1&department={DEPT_B}").json()
    ids = {row["id"] for row in theirs["rows"]}
    assert DEAL_B not in ids


def test_stuck_threshold_is_the_same_for_rop_and_admin(tenancy_app, rop_a, admin):
    """Одна карточка не может быть «зависшей» для директора и нормальной для РОПа.

    Порог — 75-й перцентиль времени на стадии по всей воронке. Считай его
    внутри отдела, и медленный отдел сравнивался бы сам с собой; хуже того,
    два человека, глядя на одну сделку, расходились бы в оценке.
    """
    import metrics
    from scope import Scope, scoped_session

    with scoped_session(Scope.everything()) as conn:
        whole = metrics.stage_norms(conn, 18)
    with scoped_session(Scope.departments([DEPT_A])) as conn:
        theirs = metrics.stage_norms(conn, 18)
    assert theirs == whole, "норма стадии разъехалась между ролями"

    # А сама выборка зависших при этом сужена по отделу.
    with scoped_session(Scope.departments([DEPT_A])) as conn:
        rows = metrics.stuck_deals(conn, 18)
    assert all(row["deal_id"] != DEAL_B for row in rows)


def test_stage_norm_view_carries_no_identifying_data():
    """Единственное несуженное представление не должно ничего опознавать.

    Если в него попадут идентификаторы, названия, суммы или ответственные,
    оно превратится из отраслевого ориентира в дыру.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent
              / "src" / "analytics" / "scope.py").read_text(encoding="utf-8")
    view = source[source.index("CREATE TEMP VIEW v_stage_norm"):]
    view = view[:view.index('"""')]
    for forbidden in ("entity_id", "title", "opportunity", "assigned_by_id", "deal_id"):
        assert forbidden not in view, f"v_stage_norm раскрывает {forbidden}"
