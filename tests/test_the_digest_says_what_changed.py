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

def _pulse_only(deliveries):
    """Только сообщения-сводки.

    Рядом с ними директору уходит второе сообщение — «что делать сегодня».
    У него другие обязательства: сводка обязана назвать воронку в первой
    строке и поставить новости раньше итога, совет — назвать человека и
    число. Проверять одно правилами другого значит однажды сломать оба.
    """
    return [item for item in deliveries
            if item.get("kind", pulse_digest.KIND_PULSE) == pulse_digest.KIND_PULSE]


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

    deliveries = _pulse_only(pulse_digest.build(Q3, URL))
    boss = next(d for d in deliveries if d["user_id"] == 7)

    assert "Кретов" in boss["text"] and "Волкова" in boss["text"]
    assert "10,0 млн ₽ из 9,0 млн ₽" in boss["text"]


def test_the_message_names_the_funnel_it_counted(agency):
    """Сводка обязана сказать, по какой воронке посчитана.

    В день, когда список воронок меняется, число в сообщении меняется вместе
    с ним. Объяснение должно стоять в заголовке, рядом с числом, — в сноске
    его прочитают уже после того, как решат, что отчёт сломался.
    """
    with analytics_session() as conn:
        conn.execute(
            "INSERT INTO dim_pipeline(category_id, name, is_active, sort, synced_at)"
            " VALUES (18, 'Покупатели', 1, 10, 'x')"
        )
        _won(conn, 1, 2, 1_000_000, "2026-08-10T09:00:00+00:00")

    deliveries = _pulse_only(pulse_digest.build(Q3, URL))

    assert deliveries, "сообщений нет — проверять нечего"
    for delivery in deliveries:
        first = delivery["text"].splitlines()[0]
        assert "«" in first and "»" in first, first
        assert "Покупатели" in first, first


def test_a_department_without_a_rop_is_named_not_dropped(agency):
    """Отчёт, не дошедший ни до кого, выглядит как отчёт без замечаний."""
    deliveries = _pulse_only(pulse_digest.build(Q3, URL))
    boss = next(d for d in deliveries if d["user_id"] == 7)

    assert [d["user_id"] for d in deliveries] == [7, 1], "у Волковой РОПа нет"
    assert "Без опознанного РОПа: Волкова" in boss["text"]


def test_the_link_is_omitted_rather_than_wrong(agency):
    """Пустой адрес — нет ссылки. Неверная ссылка хуже: она выглядит рабочей."""
    deliveries = _pulse_only(pulse_digest.build(Q3, ""))
    assert all("http" not in d["text"] for d in deliveries)


# --------------------------------------------------------------------------
# что попадает в текст
# --------------------------------------------------------------------------

def test_full_coverage_is_not_mentioned(agency):
    """«Заполнено 100%» каждый день — шум. Строка появляется, когда мешает."""
    with analytics_session() as conn:
        _won(conn, 1, 2, 1_000_000, "2026-08-10T09:00:00+00:00")

    boss = _pulse_only(pulse_digest.build(Q3, URL))[0]
    assert "заполнена" not in boss["text"]


def test_poor_coverage_is_a_warning(agency):
    """При заполненности ниже 90% факт занижен, и об этом надо сказать."""
    with analytics_session() as conn:
        _won(conn, 1, 2, 1_000_000, "2026-08-10T09:00:00+00:00")
        for deal_id in range(2, 6):
            _won(conn, deal_id, 2, 0, "2026-08-10T09:00:00+00:00")

    boss = _pulse_only(pulse_digest.build(Q3, URL))[0]
    assert "Сумма заполнена у 20%" in boss["text"]


def test_an_empty_department_stays_out_of_the_summary(agency):
    """Отдел без плана и без факта строкой «0,0 из —» ничего не сообщает."""
    with analytics_session() as conn:
        conn.execute("DELETE FROM plan_norm WHERE scope_id = 3")

    boss = _pulse_only(pulse_digest.build(Q3, URL))[0]
    assert "Волкова" not in boss["text"].split("По отделам:")[1].split("\n\n")[0]


def test_the_first_line_is_the_news_not_the_total(agency):
    """Порядок строк — не косметика: итог первым читается как «то же самое»."""
    with analytics_session() as conn:
        _won(conn, 1, 2, 1_000_000, "2026-08-10T09:00:00+00:00")

    boss = _pulse_only(pulse_digest.build(Q3, URL))[0]
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


def test_the_digest_recipient_is_set_apart_from_the_alert_admin(agency, monkeypatch):
    """PULSE_DIGEST_TO перекрывает ADMIN_USER_ID и ничего больше не задевает.

    На ADMIN_USER_ID висят уведомления о падении задач, и он же исключается
    из рейтинга брокеров и из-под замка «Источника». Живой человек,
    назначенный туда ради одной рассылки, тихо выпал бы из двух проверок.
    """
    monkeypatch.setenv("PULSE_DIGEST_TO", "154")
    get_settings.cache_clear()
    assert get_settings().admin_user_id == 7, "ADMIN_USER_ID остаётся прежним"

    deliveries = pulse_digest.build(Q3, URL)

    assert deliveries[0]["user_id"] == 154
    assert all(d["user_id"] != 7 for d in deliveries), "прежний адресат не задет"


def test_without_its_own_setting_the_digest_falls_back_to_the_admin(agency):
    """Пустой PULSE_DIGEST_TO — прежнее поведение, а не отсутствие адресата."""
    assert pulse_digest.build(Q3, URL)[0]["user_id"] == 7


# --------------------------------------------------------------------------
# разбор воронки в общий чат
# --------------------------------------------------------------------------

def _stalled_deal(conn):
    """Норма стадии два дня и одна карточка, перешагнувшая её вчера."""
    from datetime import timedelta as _delta, timezone as _tz

    now = datetime.now(_tz.utc)
    ago = lambda days: (now - _delta(days=days)).isoformat()  # noqa: E731
    conn.execute("INSERT INTO dim_pipeline(category_id, name, is_active, sort,"
                 " synced_at) VALUES (18, 'Покупатели', 1, 10, 'x')")
    conn.execute("INSERT INTO dim_stage(stage_id, category_id, name, sort,"
                 " semantic, synced_at)"
                 " VALUES ('C18:NEW', 18, 'Подбор', 10, 'in_progress', 'x')")
    for deal_id in (9101, 9102, 9103):
        conn.execute(
            "INSERT INTO fact_deal(deal_id, title, category_id, stage_id,"
            " assigned_by_id, source_id, opportunity, currency_id, date_create,"
            " date_modify, closedate, is_closed, is_won, is_lost, is_deleted,"
            " synced_at) VALUES (?, 'Норма', 18, 'C18:NEW', 2, 'CALL', 0, 'RUB',"
            " ?, ?, NULL, 1, 0, 0, 0, 'x')", (deal_id, ago(40), ago(38)))
        conn.execute(
            "INSERT INTO fact_stage_event(entity_type, entity_id, category_id,"
            " stage_id, entered_at, left_at, duration_sec, seq)"
            " VALUES ('deal', ?, 18, 'C18:NEW', ?, ?, 172800, 0)",
            (deal_id, ago(40), ago(38)))
    conn.execute(
        "INSERT INTO fact_deal(deal_id, title, category_id, stage_id,"
        " assigned_by_id, source_id, opportunity, currency_id, date_create,"
        " date_modify, closedate, is_closed, is_won, is_lost, is_deleted,"
        " synced_at) VALUES (9200, 'Пентхаус на Поклонной', 18, 'C18:NEW', 2,"
        " 'CALL', 5000000, 'RUB', ?, ?, NULL, 0, 0, 0, 0, 'x')",
        (ago(40), ago(0)))
    conn.execute(
        "INSERT INTO fact_stage_event(entity_type, entity_id, category_id,"
        " stage_id, entered_at, left_at, duration_sec, seq)"
        " VALUES ('deal', 9200, 18, 'C18:NEW', ?, NULL, NULL, 0)", (ago(2.5),))


def test_the_funnel_report_goes_to_the_shared_chat(agency, monkeypatch):
    """План-факт — разговор с РОПом лично, движение сделок — общее.

    Обсуждать «встала сделка на 5 млн» удобнее там, где это видят все, кого
    оно касается, а не пересылая из личной переписки.
    """
    monkeypatch.setenv("PULSE_EVENTS_CHAT_ID", "22358")
    get_settings.cache_clear()
    with analytics_session() as conn:
        _stalled_deal(conn)

    deliveries = pulse_digest.build(Q3, URL)
    chat = [d for d in deliveries if d.get("chat_id")]
    boss = next(d for d in deliveries if d.get("user_id") == 7)

    assert len(chat) == 1 and chat[0]["chat_id"] == 22358
    assert "Пентхаус на Поклонной" in chat[0]["text"]
    assert "Воронка за" in chat[0]["text"]
    assert "Встала" not in boss["text"], "в личной сводке разбор не дублируется"


def test_without_a_chat_the_report_stays_in_the_personal_digest(agency):
    """Выкатка без настройки не должна молча потерять разбор."""
    with analytics_session() as conn:
        _stalled_deal(conn)

    deliveries = pulse_digest.build(Q3, URL)
    boss = next(d for d in _pulse_only(deliveries) if d.get("user_id") == 7)

    assert not [d for d in deliveries if d.get("chat_id")]
    assert "Пентхаус на Поклонной" in boss["text"]


def test_a_quiet_day_sends_nothing_to_the_chat(agency, monkeypatch):
    """Ежедневное «событий нет» приучает не открывать рассылку."""
    monkeypatch.setenv("PULSE_EVENTS_CHAT_ID", "22358")
    get_settings.cache_clear()

    assert not [d for d in pulse_digest.build(Q3, URL) if d.get("chat_id")]
