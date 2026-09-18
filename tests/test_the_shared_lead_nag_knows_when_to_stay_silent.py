"""Напоминание по «Общим лидам» пишет людям — и почти всё в нём про «не писать».

Это единственная задача проекта, которая шлёт человеку сообщение с пометкой
«важное» и пинг на колокольчик, и делает это не раз в день, а тиками. Цена
ошибки тут не «неверное число в отчёте», а брокер, которого дёргают в субботу
ночью, или веерная рассылка всем подряд из-за одной осечки портала.

Поэтому проверяется в первую очередь тишина: выключенная рассылка молчит, вне
рабочего окна молчит, при неответившем портале молчит и пробует в следующий
тик. И отдельно — что при открытом окне сообщение всё-таки уходит, ровно тому
брокеру, чьи это лиды, и с той пометкой, ради которой всё затевалось.

Модуль полгода жил на боевом сервере вне git: правки в нём никто не видел, а
docker build забирает src/ с диска. Тесты здесь затем, чтобы следующая правка
проходила через прогон, а не через рассылку.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import shared_lead_qualify_reminder as job  # noqa: E402
from config import Settings, get_settings  # noqa: E402

MSK = ZoneInfo("Europe/Moscow")

# 16 сентября 2026 — среда. Будний день нужен во всех тестах, где проверяется
# не календарь: иначе они начнут падать в выходные по другой причине.
WEDNESDAY_NOON = datetime(2026, 9, 16, 12, 30, tzinfo=MSK)
SATURDAY_NOON = datetime(2026, 9, 19, 12, 30, tzinfo=MSK)
BROKER = 7


@pytest.fixture
def settings(monkeypatch):
    """Настоящие Settings, а не заглушка: проверяется и проводка из .env."""
    monkeypatch.setenv("SHARED_LEAD_REMINDER_ENABLED", "true")
    monkeypatch.setenv("DRY_RUN", "false")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture
def portal(monkeypatch):
    """Портал, который отвечает: два лида одного брокера, брокер работает."""
    monkeypatch.setattr(
        job,
        "list_shared_leads",
        lambda: [
            {"id": 11, "title": "Б", "assigned_by_id": BROKER},
            {"id": 10, "title": "А", "assigned_by_id": BROKER},
        ],
    )
    monkeypatch.setattr(job, "load_active_user_ids", lambda: {BROKER})
    monkeypatch.setattr(job, "_build_crm_link", lambda _kind, lead_id: f"u/{lead_id}")


@pytest.fixture
def sent(monkeypatch) -> list[tuple]:
    """Перехват обоих каналов отправки."""
    calls: list[tuple] = []

    def _chat(user_id, message, **kwargs):
        calls.append(("chat", user_id, kwargs, message))
        return 1

    def _notify(user_id, message):
        calls.append(("notify", user_id, {}, message))
        return 44

    monkeypatch.setattr(job, "send_user_chat_message_chunked", _chat)
    monkeypatch.setattr(job, "send_personal_notify", _notify)
    return calls


# ── Выключатель ────────────────────────────────────────────────────────
def test_a_reminder_nobody_switched_on_sends_nothing(monkeypatch, portal, sent):
    """Выкатка не должна сама по себе начать писать брокерам.

    Тот же порядок, что у «Пульса»: текст сначала согласуют, потом ставят
    SHARED_LEAD_REMINDER_ENABLED=true. По умолчанию флаг выключен.
    """
    monkeypatch.setenv("SHARED_LEAD_REMINDER_ENABLED", "false")
    get_settings.cache_clear()

    result = job.run_cycle(get_settings(), deadline_hour=14, now=WEDNESDAY_NOON)

    assert result["reason"] == "disabled"
    assert sent == []


def test_the_switch_is_off_until_someone_sets_it():
    """Значение по умолчанию — часть защиты, а не деталь реализации.

    Спрашиваем модель, а не собранные настройки: get_settings() читает .env
    рабочего каталога, и на сервере, где рассылку включат, такой тест
    покраснел бы — ровно тот, который про защиту.
    """
    field = Settings.model_fields["shared_lead_reminder_enabled"]

    assert field.default is False


# ── Окно ───────────────────────────────────────────────────────────────
def test_a_saturday_lead_waits_until_monday(settings, portal, sent):
    """Все задачи по лидам в crontab.txt ограничены буднями.

    «Общие лиды» никто не разбирает в выходные, а сообщение с пометкой
    «важное» в субботу — это не напоминание, а раздражитель.
    """
    result = job.run_cycle(settings, deadline_hour=14, now=SATURDAY_NOON)

    assert result["reason"] == "weekend"
    assert sent == []


def test_nothing_goes_out_before_the_working_day_starts(settings, portal, sent):
    """Нижней границы у окна не было вовсе: ручной запуск в три ночи будил бы всех."""
    night = WEDNESDAY_NOON.replace(hour=3)

    result = job.run_cycle(settings, deadline_hour=14, start_hour=9, now=night)

    assert result["reason"] == "early"
    assert sent == []


def test_the_deadline_hour_itself_is_already_too_late(settings, portal, sent):
    """Ровно 14:00 — это «поздно»: после дедлайна напоминать не о чем."""
    result = job.run_cycle(
        settings, deadline_hour=14, now=WEDNESDAY_NOON.replace(hour=14, minute=0),
    )

    assert result["reason"] == "deadline_reached"
    assert sent == []


def test_a_minute_before_the_deadline_still_counts(settings, portal, sent):
    """Граница строгая: в 13:59 сообщение ещё имеет смысл."""
    result = job.run_cycle(
        settings, deadline_hour=14, now=WEDNESDAY_NOON.replace(hour=13, minute=59),
    )

    assert result["skipped"] is False
    assert result["sent"] == 1


def test_an_empty_window_is_refused_at_startup(monkeypatch):
    """Окно «с 14 до 9» молча не делает ничего — заметить это можно только
    по отсутствию сообщений у брокеров, поэтому падаем сразу."""
    monkeypatch.setattr(sys, "argv", ["x", "--since-hour", "14", "--until-hour", "9"])

    with pytest.raises(SystemExit):
        job.main()


def test_a_midnight_deadline_is_refused_at_startup(monkeypatch):
    """--until-hour 0 проходил валидацию и превращал задачу в вечный no-op."""
    monkeypatch.setattr(sys, "argv", ["x", "--until-hour", "0"])

    with pytest.raises(SystemExit):
        job.main()


# ── Когда портал не ответил ────────────────────────────────────────────
def test_a_failed_lead_query_costs_one_cycle_not_the_whole_day(
    settings, monkeypatch, sent,
):
    """QUERY_LIMIT_EXCEEDED у crm.lead.list — штатная ошибка портала.

    Раньше она летела насквозь через run_cycle и убивала процесс: одна осечка
    в 09:10 означала, что до 14:00 никто ничего не получит, и в cron.log об
    этом был только traceback.
    """
    def _boom():
        raise RuntimeError("QUERY_LIMIT_EXCEEDED")

    monkeypatch.setattr(job, "list_shared_leads", _boom)

    result = job.run_cycle(settings, deadline_hour=14, now=WEDNESDAY_NOON)

    assert result["reason"] == "fetch_failed"
    assert result["errors"] == 1
    assert sent == []


def test_nobody_is_nagged_when_the_portal_forgot_who_works_here(
    settings, monkeypatch, portal, sent,
):
    """Пустой список активных — это «не знаем», а не «все уволены».

    Прежний вариант в этом месте возвращал None и слал всем подряд, включая
    уволенных: одна осечка user.get превращала адресное напоминание в
    веерную рассылку — и так каждые десять минут до 14:00.
    """
    monkeypatch.setattr(job, "load_active_user_ids", lambda: set())

    result = job.run_cycle(settings, deadline_hour=14, now=WEDNESDAY_NOON)

    assert result["reason"] == "no_active_users"
    assert result["leads"] == 2
    assert sent == []


def test_one_brokers_failure_does_not_silence_the_rest(settings, monkeypatch):
    """Сообщение одному не ушло — остальные всё равно должны получить своё."""
    monkeypatch.setattr(
        job,
        "list_shared_leads",
        lambda: [
            {"id": 1, "title": "А", "assigned_by_id": 7},
            {"id": 2, "title": "Б", "assigned_by_id": 8},
            {"id": 3, "title": "В", "assigned_by_id": 9},
        ],
    )
    monkeypatch.setattr(job, "load_active_user_ids", lambda: {7, 8, 9})
    reached: list[int] = []

    def _alert(user_id, leads, **kwargs):
        if user_id == 8:
            raise RuntimeError("IM error")
        reached.append(user_id)
        return {"chat_chunks": 1, "notify_id": 1}

    monkeypatch.setattr(job, "send_broker_alert", _alert)

    result = job.run_cycle(settings, deadline_hour=14, now=WEDNESDAY_NOON)

    assert reached == [7, 9]
    assert result["sent"] == 2
    assert result["errors"] == 1


def test_the_loop_survives_a_cycle_that_blew_up(settings, monkeypatch):
    """--loop живёт до 14:00, и падение одного тика не должно уносить день."""
    calls = {"n": 0}

    def _cycle(_settings, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("503 from portal")
        return {"skipped": True, "reason": "deadline_reached"}

    monkeypatch.setattr(job, "run_cycle", _cycle)
    monkeypatch.setattr(job.time, "sleep", lambda _s: None)
    monkeypatch.setattr(job, "seconds_until_next_tick", lambda *_a, **_k: 60)

    result = job.run_loop(settings, deadline_hour=14, interval_min=10)

    assert result["cycles"] == 2
    assert result["failures"] == 1


# ── Кому уходит ────────────────────────────────────────────────────────
def test_leads_of_a_dismissed_broker_are_left_alone():
    """Уволенному сообщение уходит в никуда, а лид остаётся неразобранным."""
    leads = [
        {"id": 1, "title": "А", "assigned_by_id": 10},
        {"id": 2, "title": "Б", "assigned_by_id": 99},
    ]

    groups = job.group_leads_by_broker(leads, active_user_ids={10})

    assert set(groups) == {10}


def test_a_lead_without_an_assignee_is_nobodys_to_nag():
    """ASSIGNED_BY_ID = 0 бывает у лидов, созданных роботом."""
    leads = [{"id": 4, "title": "Г", "assigned_by_id": 0}]

    # active_user_ids=None: проверяется именно отсев по ответственному, а не
    # то, что ноль не попал в список активных.
    assert job.group_leads_by_broker(leads, active_user_ids=None) == {}


def test_each_broker_sees_their_leads_in_a_stable_order():
    """Портал отдаёт как попало; человеку список должен приходить в одном
    и том же порядке, иначе два соседних сообщения выглядят как разные."""
    leads = [
        {"id": 3, "title": "В", "assigned_by_id": 10},
        {"id": 1, "title": "А", "assigned_by_id": 10},
        {"id": 2, "title": "Б", "assigned_by_id": 11},
    ]

    groups = job.group_leads_by_broker(leads, active_user_ids={10, 11})

    assert [x["id"] for x in groups[10]] == [1, 3]
    assert [x["id"] for x in groups[11]] == [2]


def test_a_row_the_portal_mangled_is_dropped_not_guessed():
    """Лид без внятного ID — это строка, по которой нечего открывать."""
    assert job.normalize_lead({"ID": 0, "ASSIGNED_BY_ID": 5}) is None
    assert job.normalize_lead(
        {"ID": "42", "TITLE": "  Квартира  ", "ASSIGNED_BY_ID": "7"},
    ) == {"id": 42, "title": "Квартира", "assigned_by_id": 7}


def test_a_dismissed_broker_gets_nothing_even_when_leads_are_his(
    settings, monkeypatch, sent,
):
    """Сквозная проверка: фильтр активных должен стоять именно в run_cycle.

    Проверка group_leads_by_broker в изоляции этого не держит — провод из
    run_cycle можно выдернуть, и она останется зелёной. А выдернутый провод
    здесь означает важное сообщение уволенному, каждый тик.
    """
    monkeypatch.setattr(
        job,
        "list_shared_leads",
        lambda: [
            {"id": 1, "title": "А", "assigned_by_id": 7},
            {"id": 2, "title": "Б", "assigned_by_id": 99},
        ],
    )
    monkeypatch.setattr(job, "load_active_user_ids", lambda: {7})
    monkeypatch.setattr(job, "_build_crm_link", lambda *_a, **_k: "u")

    result = job.run_cycle(settings, deadline_hour=14, now=WEDNESDAY_NOON)

    assert result["brokers"] == 1
    assert [call[1] for call in sent] == [7, 7]  # чат и колокольчик, только 7


def test_a_manual_check_can_ignore_the_clock_but_not_the_switch(
    monkeypatch, settings, portal, sent,
):
    """--force нужен, чтобы посмотреть текст в неурочный час.

    Обойти им несогласованную рассылку нельзя: выключатель проверяется
    раньше и от --force не зависит.
    """
    assert job.run_cycle(
        settings, deadline_hour=14, now=SATURDAY_NOON, force=True,
    )["sent"] == 1

    monkeypatch.setenv("SHARED_LEAD_REMINDER_ENABLED", "false")
    get_settings.cache_clear()
    forced_off = job.run_cycle(
        get_settings(), deadline_hour=14, now=SATURDAY_NOON, force=True,
    )

    assert forced_off["reason"] == "disabled"


# ── Когда будить админа ────────────────────────────────────────────────
@pytest.mark.parametrize(
    "reason", ["disabled", "weekend", "early", "deadline_reached"],
)
def test_silence_by_schedule_is_not_a_failure(reason: str):
    """Иначе cron будил бы админа каждый час выходного дня."""
    assert job.job_failed({"reason": reason, "errors": 0}) is False


@pytest.mark.parametrize("reason", ["fetch_failed", "no_active_users"])
def test_a_portal_that_did_not_answer_is_worth_waking_someone_for(reason: str):
    """Прогон, где ни одно сообщение не ушло по вине портала, для cron должен
    быть неуспехом: тревогу scripts/cron_job.sh поднимает только по коду
    возврата, а в логе это иначе не отличить от обычного тихого тика."""
    assert job.job_failed({"reason": reason, "errors": 0}) is True


def test_a_send_that_failed_is_worth_waking_someone_for():
    assert job.job_failed({"reason": "", "errors": 1}) is True


def test_a_clean_run_says_nothing_to_cron():
    assert job.job_failed({"reason": "", "errors": 0, "sent": 3}) is False


# ── Что именно уходит ──────────────────────────────────────────────────
def test_the_message_names_the_leads_and_links_to_them(monkeypatch):
    """Без ссылки человек идёт искать карточку руками и не идёт вовсе."""
    monkeypatch.setattr(job, "_build_crm_link", lambda _k, i: f"https://p/lead/{i}/")

    chat = job.format_chat_message(
        [{"id": 15, "title": "ЖК Река", "assigned_by_id": 8}], deadline_hour=14,
    )

    assert "ВАЖНО" in chat
    assert "квалифицировать" in chat
    assert "14:00" in chat
    assert "Лид #15" in chat
    assert "ЖК Река" in chat
    assert "https://p/lead/15/" in chat


def test_the_bell_ping_is_short_and_sends_the_reader_to_the_chat(monkeypatch):
    """В уведомлении нет места списку: его задача — поднять человека в чат."""
    monkeypatch.setattr(job, "_build_crm_link", lambda *_a, **_k: "u")

    notify = job.format_notify_message(
        [{"id": 15, "title": "ЖК Река", "assigned_by_id": 8}], deadline_hour=14,
    )

    assert "1 лид" in notify
    assert "14:00" in notify
    assert "личных сообщениях" in notify
    assert "https://" not in notify


@pytest.mark.parametrize(
    ("count", "expected"),
    [(1, "1 лид "), (2, "2 лида "), (5, "5 лидов "), (11, "11 лидов "),
     (21, "21 лид "), (22, "22 лида "), (25, "25 лидов "), (101, "101 лид ")],
)
def test_the_count_is_spelled_the_way_a_person_would_read_it(
    monkeypatch, count, expected,
):
    """«21 лидов» человек читает как ошибку и меньше верит остальному тексту."""
    monkeypatch.setattr(job, "_build_crm_link", lambda *_a, **_k: "u")
    leads = [{"id": i, "title": "т", "assigned_by_id": 1} for i in range(count)]

    assert expected in job.format_notify_message(leads, deadline_hour=14)


def test_a_dry_run_says_what_it_would_do_and_does_nothing(
    monkeypatch, settings, portal, sent,
):
    """DRY_RUN — общий тумблер проекта, и эта задача обязана его уважать."""
    monkeypatch.setenv("DRY_RUN", "true")
    get_settings.cache_clear()

    result = job.run_cycle(get_settings(), deadline_hour=14, now=WEDNESDAY_NOON)

    assert result["sent"] == 1
    assert result["leads"] == 2
    assert result["dry_run"] is True
    assert sent == []


def test_a_live_run_marks_the_chat_message_important_and_pings_the_bell(
    settings, portal, sent,
):
    """Оба канала, и оба — с пометкой.

    Чат человек может не открыть неделю: «важное» поднимает сообщение наверх,
    уведомление висит на колокольчике, пока его не прочитают. Параметр
    important однажды уже пропадал из образа, и вызов ломался с TypeError.
    """
    result = job.run_cycle(settings, deadline_hour=14, now=WEDNESDAY_NOON)

    assert result["sent"] == 1
    assert result["errors"] == 0
    chat, notify = sent
    assert chat[0] == "chat" and chat[1] == BROKER
    assert chat[2]["important"] is True
    assert chat[2]["system"] is False
    assert "квалифицировать" in chat[3]
    assert notify[0] == "notify" and notify[1] == BROKER
    assert "14:00" in notify[3]


# ── Сон между тиками ───────────────────────────────────────────────────
def test_the_loop_never_sleeps_past_the_deadline():
    """Последний сон подрезается дедлайном, иначе процесс висит после 14:00."""
    at_1355 = WEDNESDAY_NOON.replace(hour=13, minute=55)

    assert job.seconds_until_next_tick(
        at_1355, interval_min=10, deadline_hour=14,
    ) == 5 * 60


def test_a_full_interval_is_kept_while_there_is_time_for_it():
    assert job.seconds_until_next_tick(
        WEDNESDAY_NOON, interval_min=10, deadline_hour=14,
    ) == 10 * 60


def test_after_the_deadline_there_is_nothing_left_to_wait_for():
    assert job.seconds_until_next_tick(
        WEDNESDAY_NOON.replace(hour=14, minute=1), interval_min=10, deadline_hour=14,
    ) == 0


# ── Окно как функция ───────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (WEDNESDAY_NOON.replace(hour=8, minute=59), "early"),
        (WEDNESDAY_NOON.replace(hour=9, minute=0), "open"),
        (WEDNESDAY_NOON.replace(hour=13, minute=59), "open"),
        (WEDNESDAY_NOON.replace(hour=14, minute=0), "deadline"),
        (SATURDAY_NOON, "weekend"),
        (SATURDAY_NOON + timedelta(days=1), "weekend"),
    ],
)
def test_the_window_opens_and_closes_where_it_says(moment: datetime, expected: str):
    assert job.window_state(moment, start_hour=9, deadline_hour=14) == expected


def test_the_window_is_read_in_moscow_time_not_the_containers():
    """Контейнер живёт в UTC: без пересчёта окно 9-14 открывалось бы в полдень."""
    utc_six = datetime(2026, 9, 16, 6, 30, tzinfo=ZoneInfo("UTC"))  # 09:30 МСК

    assert job.window_state(utc_six, start_hour=9, deadline_hour=14) == "open"


def test_a_cycle_reports_where_it_was_when_it_decided(settings, portal, sent):
    """В логе должно быть видно местное время решения, иначе разбор сводится
    к пересчёту UTC в уме."""
    result: dict[str, Any] = job.run_cycle(
        settings, deadline_hour=14, now=SATURDAY_NOON,
    )

    assert result["local_time"].startswith("2026-09-19T12:30")
    assert sent == []
