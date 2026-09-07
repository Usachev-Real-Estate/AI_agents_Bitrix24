"""Дайджест сообщает новость, а не повторяет итог, и не путает адресатов.

Квартальный план меняется медленно: «14,5 из 127,5, отстаём» будет одинаковым
девяносто дней подряд, и такое перестают читать на третий раз. Поэтому первая
строка — про вчера, а итог идёт опорой следом.

Вторая половина проверок про адресацию. РОПу считается своё, на суженном
соединении: не отфильтрованное из общего расчёта, а собранное из данных, где
чужого отдела нет вовсе.
"""

from datetime import datetime

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

import pulse_digest
from config import get_settings
from metrics import BUSINESS_TZ
from schema import analytics_session
from scope import Scope, scoped_session

Q3 = "2026-Q3"
URL = "https://example.invalid/dashboard/pulse"


def _user(conn, user_id, name, last_name, dept_id, dept_name):
    conn.execute(
        "INSERT INTO dim_user(user_id, name, last_name, department_id,"
        " department_name, is_active, synced_at) VALUES (?, ?, ?, ?, ?, 1, 'x')",
        (user_id, name, last_name, dept_id, dept_name),
    )


def _won(conn, deal_id, user_id, amount, closed):
    conn.execute(
        """
        INSERT INTO fact_deal(deal_id, title, category_id, stage_id, assigned_by_id,
            source_id, opportunity, currency_id, date_create, date_modify, closedate,
            is_closed, is_won, is_lost, is_deleted, synced_at)
        VALUES (?, ?, 18, 'C18:WON', ?, 'CALL', ?, 'RUB', '2026-07-01T00:00:00+00:00',
                ?, ?, 1, 1, 0, 0, 'x')
        """,
        (deal_id, f"Сделка {deal_id}", user_id, amount, closed, closed),
    )


def _norm(conn, user_id, amount):
    conn.execute(
        "INSERT INTO plan_norm(period_code, scope_kind, scope_id, metric,"
        " basis, amount, source, updated_at)"
        " VALUES (?, 'user', ?, 'commission', 'absolute', ?, 'test', 'x')",
        (Q3, user_id, amount),
    )


@pytest.fixture
def agency(analytics_db, monkeypatch):
    """Два отдела: у Кретова РОП опознан, у Волковой — нет."""
    monkeypatch.setenv("ADMIN_USER_ID", "7")
    monkeypatch.setenv("OWNER_SALES_DEPT_IDS_JSON", "[50, 60]")
    get_settings.cache_clear()
    with analytics_session() as conn:
        _user(conn, 1, "Антон Кретов", "Кретов", 60, "Кретов")
        _user(conn, 2, "Брокер Первый", "Первый", 60, "Кретов")
        _user(conn, 3, "Брокер Третий", "Третий", 50, "Волкова")
        _norm(conn, 2, 3_500_000)
        _norm(conn, 3, 5_500_000)
    yield analytics_db
    get_settings.cache_clear()


# --------------------------------------------------------------------------
# окно «вчера»
# --------------------------------------------------------------------------

def test_on_monday_yesterday_covers_the_weekend():
    """В понедельник «вчера» — это пятница, а окно накрывает выходные.

    Сообщение про воскресенье, в котором закономерно ничего не закрыто,
    обесценивает рассылку целиком.
    """
    monday = datetime(2026, 9, 7, 9, 0, tzinfo=BUSINESS_TZ)
    window = pulse_digest.yesterday_window(monday)

    assert window["label"] == "04.09–06.09"
    assert window["since"] < window["until"]
    # Границы хранятся в UTC, а календарь у агентства московский: полночь
    # пятницы по Москве — это 21:00 четверга по UTC. Сверять надо после
    # перевода в рабочую зону, иначе тест проверяет не то, что видит человек.
    start = datetime.fromisoformat(window["since"]).astimezone(BUSINESS_TZ)
    assert start.date().isoformat() == "2026-09-04"
    assert start.weekday() == 4, "начало окна — пятница"


def test_on_an_ordinary_day_yesterday_is_yesterday():
    tuesday = datetime(2026, 9, 8, 9, 0, tzinfo=BUSINESS_TZ)
    assert pulse_digest.yesterday_window(tuesday)["label"] == "вчера"


def test_a_quiet_day_says_so_instead_of_staying_silent(agency):
    """«Сделок нет» — тоже новость. Пропуск строки читался бы как сбой."""
    window = pulse_digest.yesterday_window(
        datetime(2026, 9, 8, 9, 0, tzinfo=BUSINESS_TZ))
    with scoped_session(Scope.everything()) as conn:
        closed = pulse_digest.closed_in(conn, window)

    assert closed["deals"] == 0
    assert "закрытых сделок нет" in pulse_digest._yesterday_line(closed)


def test_the_deals_word_agrees_with_the_number():
    """«1 сделка», «2 сделки», «5 сделок» — иначе сообщение выглядит машинным."""
    assert pulse_digest._deals_word(1) == "сделка"
    assert pulse_digest._deals_word(3) == "сделки"
    assert pulse_digest._deals_word(5) == "сделок"
    assert pulse_digest._deals_word(11) == "сделок"
    assert pulse_digest._deals_word(22) == "сделки"


# --------------------------------------------------------------------------
# адресация
# --------------------------------------------------------------------------

def test_a_rop_gets_only_their_own_department(agency):
    """В сообщении РОПа чужого отдела нет — не отфильтрован, а не собран."""
    with analytics_session() as conn:
        _won(conn, 1, 2, 1_000_000, "2026-08-10T09:00:00+00:00")
        _won(conn, 2, 3, 9_000_000, "2026-08-10T09:00:00+00:00")

    deliveries = pulse_digest.build(Q3, URL)
    rop = next(d for d in deliveries if d["user_id"] == 1)

    assert "Волкова" not in rop["text"], "чужой отдел просочился в личное сообщение"
    assert "Кретов" in rop["text"]
    assert "1,0 млн ₽" in rop["text"]


def test_the_director_gets_the_whole_company(agency):
    with analytics_session() as conn:
        _won(conn, 1, 2, 1_000_000, "2026-08-10T09:00:00+00:00")
        _won(conn, 2, 3, 9_000_000, "2026-08-10T09:00:00+00:00")

    deliveries = pulse_digest.build(Q3, URL)
    boss = next(d for d in deliveries if d["user_id"] == 7)

    assert "Кретов" in boss["text"] and "Волкова" in boss["text"]
    assert "10,0 млн ₽ из 9,0 млн ₽" in boss["text"]


def test_a_department_without_a_rop_is_named_not_dropped(agency):
    """Отчёт, не дошедший ни до кого, выглядит как отчёт без замечаний."""
    deliveries = pulse_digest.build(Q3, URL)
    boss = next(d for d in deliveries if d["user_id"] == 7)

    assert [d["user_id"] for d in deliveries] == [7, 1], "у Волковой РОПа нет"
    assert "Без опознанного РОПа: Волкова" in boss["text"]


def test_the_link_is_omitted_rather_than_wrong(agency):
    """Пустой адрес — нет ссылки. Неверная ссылка хуже: она выглядит рабочей."""
    deliveries = pulse_digest.build(Q3, "")
    assert all("http" not in d["text"] for d in deliveries)


# --------------------------------------------------------------------------
# что попадает в текст
# --------------------------------------------------------------------------

def test_full_coverage_is_not_mentioned(agency):
    """«Заполнено 100%» каждый день — шум. Строка появляется, когда мешает."""
    with analytics_session() as conn:
        _won(conn, 1, 2, 1_000_000, "2026-08-10T09:00:00+00:00")

    boss = pulse_digest.build(Q3, URL)[0]
    assert "заполнена" not in boss["text"]


def test_poor_coverage_is_a_warning(agency):
    """При заполненности ниже 90% факт занижен, и об этом надо сказать."""
    with analytics_session() as conn:
        _won(conn, 1, 2, 1_000_000, "2026-08-10T09:00:00+00:00")
        for deal_id in range(2, 6):
            _won(conn, deal_id, 2, 0, "2026-08-10T09:00:00+00:00")

    boss = pulse_digest.build(Q3, URL)[0]
    assert "Сумма заполнена у 20%" in boss["text"]


def test_an_empty_department_stays_out_of_the_summary(agency):
    """Отдел без плана и без факта строкой «0,0 из —» ничего не сообщает."""
    with analytics_session() as conn:
        conn.execute("DELETE FROM plan_norm WHERE scope_id = 3")

    boss = pulse_digest.build(Q3, URL)[0]
    assert "Волкова" not in boss["text"].split("По отделам:")[1].split("\n\n")[0]


def test_the_first_line_is_the_news_not_the_total(agency):
    """Порядок строк — не косметика: итог первым читается как «то же самое»."""
    with analytics_session() as conn:
        _won(conn, 1, 2, 1_000_000, "2026-08-10T09:00:00+00:00")

    boss = pulse_digest.build(Q3, URL)[0]
    lines = [line for line in boss["text"].splitlines() if line.strip()]

    assert lines[0].startswith("📊 Пульс")
    assert lines[1].startswith("За ") or lines[1].startswith("Вчера")
    assert lines[2].startswith("Квартал:")


# --------------------------------------------------------------------------
# выключатель
# --------------------------------------------------------------------------

def test_a_preview_works_while_the_digest_is_off(agency, monkeypatch, capsys):
    """--dry-run показывает текст, даже когда рассылка выключена.

    Иначе выключатель загоняет в тупик: чтобы посмотреть сообщение, надо
    включить рассылку — то есть сделать ровно то, от чего флаг и уберегает.
    """
    monkeypatch.setenv("PULSE_DIGEST_ENABLED", "false")
    monkeypatch.setenv("DRY_RUN", "false")
    get_settings.cache_clear()
    with analytics_session() as conn:
        _won(conn, 1, 2, 1_000_000, "2026-08-10T09:00:00+00:00")

    assert pulse_digest.main(["--period", Q3, "--dry-run"]) == 0
    printed = capsys.readouterr().out
    assert "📊 Пульс" in printed
    assert "НЕ отправлено" in printed


def test_a_disabled_digest_sends_nothing(agency, monkeypatch):
    """Без флага выключенная рассылка не доходит до отправки."""
    monkeypatch.setenv("PULSE_DIGEST_ENABLED", "false")
    monkeypatch.setenv("DRY_RUN", "false")
    get_settings.cache_clear()

    def _boom(*args, **kwargs):
        raise AssertionError("выключенный дайджест не должен отправлять")

    monkeypatch.setattr(pulse_digest, "send_user_chat_message_chunked", _boom)
    assert pulse_digest.main(["--period", Q3]) == 0


# --------------------------------------------------------------------------
# отделы, которые разбирает не их руководитель
# --------------------------------------------------------------------------

def test_a_redirected_rop_gets_nothing_and_the_owner_gets_it(agency):
    """Отчёт отдела Волковой уходит владельцу отчёта, а не ей самой.

    Кто именно не получает лично — берётся из рассылки QC. Агентство решило
    это один раз, и вторая рассылка обязана адресовать так же.
    """
    with analytics_session() as conn:
        _user(conn, 9, "Вера Волкова", "Волкова", 50, "Волкова")

    deliveries = pulse_digest.build(Q3, URL)
    addressees = [(d["user_id"], d["name"]) for d in deliveries]

    assert 9 not in [user_id for user_id, _ in addressees], (
        "РОП из списка перенаправления личного сообщения не получает"
    )
    redirected = [d for d in deliveries if "→ директору" in d["name"]]
    assert len(redirected) == 1
    assert redirected[0]["user_id"] == 7, "ушло владельцу отчёта"
    assert "Пульс отдела «Волкова»" in redirected[0]["text"]


def test_an_ordinary_rop_still_gets_their_own(agency):
    """Перенаправление точечное: остальные РОПы получают лично, как и раньше."""
    with analytics_session() as conn:
        _user(conn, 9, "Вера Волкова", "Волкова", 50, "Волкова")

    deliveries = pulse_digest.build(Q3, URL)
    assert any(d["user_id"] == 1 and d["name"] == "РОП Кретов" for d in deliveries)


def test_a_redirected_department_is_not_dropped_without_an_owner(agency, monkeypatch):
    """Без ADMIN_USER_ID отчёт не уходит никому — и об этом пишется в лог.

    Тихо потерянный отдел выглядит как отдел без замечаний.
    """
    monkeypatch.setenv("ADMIN_USER_ID", "0")
    get_settings.cache_clear()
    with analytics_session() as conn:
        _user(conn, 9, "Вера Волкова", "Волкова", 50, "Волкова")

    deliveries = pulse_digest.build(Q3, URL)
    assert all("Волкова" not in d["name"] for d in deliveries)
    assert all(d["user_id"] != 9 for d in deliveries)


def test_a_rop_moved_by_the_roster_is_addressed(agency):
    """Руководитель, переставленный ростером, получает отчёт своего отдела.

    Ровно это и сломалось на боевом сервере. Рассылка спрашивала портал
    напрямую, а портал считает руководителем того, кто в отделе ЧИСЛИТСЯ.
    Волкова числится в служебном «Битриксе», ростер вернул её в отдел 50 —
    на экране состав стал верным, а отчёт отдел молча перестал получать.
    Теперь и состав, и адресация берутся из одного места.
    """
    with analytics_session() as conn:
        _user(conn, 9, "Светлана Трофимова", "Трофимова", 99, "Битрикс")
        conn.execute(
            "INSERT INTO plan_roster(period_code, user_id, department_id,"
            " plan_role, note, updated_at) VALUES ('*', 9, 50, 'rop', '', 'x')"
        )

    deliveries = pulse_digest.build(Q3, URL)

    assert any(d["user_id"] == 9 for d in deliveries), (
        "отдел остался без отчёта, хотя ростер вернул ему руководителя"
    )
    own = next(d for d in deliveries if d["user_id"] == 9)
    assert "Пульс отдела «Волкова»" in own["text"]
    boss = next(d for d in deliveries if d["user_id"] == 7)
    assert "Без опознанного РОПа" not in boss["text"]
