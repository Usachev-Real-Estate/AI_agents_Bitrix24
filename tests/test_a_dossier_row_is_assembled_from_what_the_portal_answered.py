"""Строка досье — это пересказ ответов портала, и пересказ должен быть точен.

Читающая модель не видит ни портала, ни наших запросов: у неё есть только
эта строка. Значит, каждое поле в ней отвечает за то, что иначе узнать
нельзя, и ошибка в любом из них — это ошибка вывода о работе брокера,
которую некому будет заметить.

Главное поле здесь — author_is_assignee. По карточке пишут все: РОП,
колл-центр, коллега по просьбе. «Семь комментариев» и «семь комментариев,
ни одного от брокера» — два разных отчёта, и портал их никак не различает.
Признак считается при сборе, а не у читателя, ровно затем, чтобы его нельзя
было посчитать по-разному.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import dossier  # noqa: E402

BROKER = 7
ROP = 9
DEAL_ID = 16722
NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
USERS = {BROKER: "Галина Шарипова", ROP: "Владислав Ветров"}
STAGES = {"C18:NEW": "Подбор", "C18:UC_UFPFKK": "Первый показ"}


def _ago(**kwargs) -> str:
    return (NOW - timedelta(**kwargs)).isoformat()


def _deal(**over) -> dict:
    deal = {
        "ID": DEAL_ID,
        "TITLE": "Жизнь на Плющихе продажа",
        "CATEGORY_ID": 18,
        "STAGE_ID": "C18:UC_UFPFKK",
        "ASSIGNED_BY_ID": BROKER,
        "DATE_CREATE": _ago(days=40),
        "DATE_MODIFY": _ago(days=1),
        "OPPORTUNITY": "1500000",
        "SOURCE_ID": "CALL_CENTER_5",
        "UF_CRM_AFINA": "600749",
    }
    deal.update(over)
    return deal


def _comment(author_id: int, text: str, created: str) -> dict:
    return {"ID": 1, "AUTHOR_ID": author_id, "COMMENT": text, "CREATED": created}


def _task(subject: str, created: str, *, completed: str = "N",
          deadline: str = "", description: str = "") -> dict:
    return {
        "ID": 500, "TYPE_ID": 1, "SUBJECT": subject, "DESCRIPTION": description,
        "CREATED": created, "DEADLINE": deadline, "COMPLETED": completed,
        "AUTHOR_ID": BROKER, "RESPONSIBLE_ID": BROKER,
    }


def _call(activity_id: int, created: str, seconds: int, direction: int = 2) -> dict:
    start = dossier._parse_datetime(created)
    return {
        "ID": activity_id, "TYPE_ID": 2, "SUBJECT": "Звонок",
        "CREATED": created, "DIRECTION": direction, "COMPLETED": "Y",
        "START_TIME": created,
        "END_TIME": (start + timedelta(seconds=seconds)).isoformat(),
        "AUTHOR_ID": BROKER, "RESPONSIBLE_ID": BROKER,
    }


@pytest.fixture
def quiet_portal(monkeypatch):
    """Расшифровки не спрашиваем: сеть в тестах закрыта."""
    monkeypatch.setattr(
        dossier, "fetch_transcript_from_api", lambda _id: (None, "not_ready"),
    )


def _row(deal=None, comments=None, activities=None, history=None,
         reassignments=None, budget=10):
    return dossier.build_row(
        deal or _deal(),
        comments_raw=comments or [],
        activities_raw=activities or [],
        history_raw=history or [],
        reassignments=reassignments or [],
        users=USERS,
        stage_names=STAGES,
        afina_code="UF_CRM_AFINA",
        resolver=dossier.TranscriptResolver(budget),
        launches={},
        now=NOW,
    )


# ── Кто написал ────────────────────────────────────────────────────────
def test_a_comment_by_the_assignee_is_marked_as_his(quiet_portal):
    row = _row(comments=[_comment(BROKER, "Созвонились", _ago(days=2))])

    comment = row["comments"][0]
    assert comment["author_is_assignee"] is True
    assert comment["author_name"] == "Галина Шарипова"


def test_a_comment_by_somebody_else_is_not_the_brokers_work(quiet_portal):
    """Тот самый случай, ради которого автора и вытаскивали из портала."""
    row = _row(comments=[
        _comment(ROP, "Позвонил клиенту сам", _ago(days=2)),
        _comment(ROP, "Клиент ждёт подборку", _ago(days=1)),
    ])

    assert all(c["author_is_assignee"] is False for c in row["comments"])
    assert row["counters"]["comments_total"] == 2
    assert row["counters"]["comments_by_assignee"] == 0


def test_a_comment_without_an_author_is_nobodys(quiet_portal):
    """Ноль — не идентификатор. Совпадение с пустым полем не делает автора."""
    row = _row(deal=_deal(ASSIGNED_BY_ID=0),
               comments=[_comment(0, "Системная запись", _ago(days=1))])

    assert row["comments"][0]["author_is_assignee"] is False


# ── Счётчики ───────────────────────────────────────────────────────────
def test_the_counters_say_what_the_lists_hold(quiet_portal):
    row = _row(
        comments=[
            _comment(BROKER, "Написал сам", _ago(days=3)),
            _comment(ROP, "А это РОП", _ago(days=2)),
        ],
        activities=[
            _task("Связаться с клиентом", _ago(days=3)),
            _task("Отчёт", _ago(days=4), completed="Y"),
            _call(101, _ago(days=2), seconds=154),
        ],
    )

    assert row["counters"] == {
        "comments_total": 2,
        "comments_by_assignee": 1,
        "calls_total": 1,
        "calls_with_transcript": 0,
        "activities_open": 1,
    }


def test_a_call_is_not_counted_as_a_task(quiet_portal):
    """Звонок и поставленное дело — разная работа, и списки разные."""
    row = _row(activities=[
        _call(101, _ago(days=2), seconds=120),
        _task("Перезвонить", _ago(days=2)),
    ])

    assert [a["subject"] for a in row["activities"]] == ["Перезвонить"]
    assert [c["activity_id"] for c in row["calls"]] == [101]


def test_the_text_under_a_task_reaches_the_row(quiet_portal):
    """«Перезвонить» без описания не отличить от «Перезвонить после 18:00»."""
    row = _row(activities=[
        _task("Перезвонить", _ago(days=1), description="После 18:00, другой номер"),
    ])

    assert row["activities"][0]["description"] == "После 18:00, другой номер"


# ── История стадий ─────────────────────────────────────────────────────
def test_the_stage_history_says_where_the_card_came_from(quiet_portal):
    """crm.stagehistory отдаёт только «куда». «Откуда» — это соседняя строка."""
    row = _row(history=[
        {"OWNER_ID": DEAL_ID, "STAGE_ID": "C18:NEW", "CREATED_TIME": _ago(days=20)},
        {"OWNER_ID": DEAL_ID, "STAGE_ID": "C18:UC_UFPFKK",
         "CREATED_TIME": _ago(days=5)},
    ])

    moves = row["stage_history"]
    assert moves[0]["stage_from"] == ""
    assert moves[1]["stage_from"] == "C18:NEW"
    assert moves[1]["stage_to"] == "C18:UC_UFPFKK"
    assert moves[1]["stage_from_name"] == "Подбор"
    assert moves[1]["stage_to_name"] == "Первый показ"


def test_stage_ids_keep_the_funnel_prefix(quiet_portal):
    """Без префикса стадии двух воронок неразличимы — уже ломались об это."""
    row = _row(history=[
        {"OWNER_ID": DEAL_ID, "STAGE_ID": "C18:UC_UFPFKK",
         "CREATED_TIME": _ago(days=5)},
    ])

    assert row["stage_history"][0]["stage_to"].startswith("C18:")
    assert row["stage_id"].startswith("C18:")


def test_days_in_stage_counts_from_the_last_entry(quiet_portal):
    row = _row(history=[
        {"OWNER_ID": DEAL_ID, "STAGE_ID": "C18:UC_UFPFKK",
         "CREATED_TIME": _ago(days=17)},
    ])

    assert round(row["days_in_stage"]) == 17


def test_days_in_stage_falls_back_to_the_card_age(quiet_portal):
    """Истории может не быть вовсе — карточку завели и не двигали."""
    assert round(_row()["days_in_stage"]) == 40


# ── Что считается работой ──────────────────────────────────────────────
def test_moving_a_stage_does_not_reset_the_silence(quiet_portal):
    """Перевод стадии — один клик.

    Если засчитать его работой, у брокера появится способ обнулять счётчик
    молчания, ничего не сделав, — а на этом счётчике держится весь отбор в
    ежедневную выгрузку.
    """
    row = _row(
        comments=[_comment(BROKER, "Последняя запись", _ago(days=30))],
        history=[{"OWNER_ID": DEAL_ID, "STAGE_ID": "C18:UC_UFPFKK",
                  "CREATED_TIME": _ago(hours=1)}],
    )

    assert round(row["days_since_last_activity"]) == 30


def test_a_call_counts_as_work_even_without_a_comment(quiet_portal):
    row = _row(
        comments=[_comment(BROKER, "Давняя запись", _ago(days=30))],
        activities=[_call(101, _ago(days=2), seconds=200)],
    )

    assert round(row["days_since_last_activity"]) == 2


def test_a_card_nobody_touched_has_no_last_work_moment(quiet_portal):
    assert _row()["days_since_last_activity"] is None


# ── Поля карточки ──────────────────────────────────────────────────────
def test_the_afina_id_is_read_by_the_discovered_field_code(quiet_portal):
    row = _row()
    assert row["afina_id"] == "600749"
    assert row["afina_object"] is None, "резолв в объект — отдельный этап"


def test_the_row_carries_the_names_not_only_the_numbers(quiet_portal):
    row = _row()
    assert row["assigned_by_id"] == BROKER
    assert row["assigned_by_name"] == "Галина Шарипова"
    assert row["stage_name"] == "Первый показ"
    assert row["opportunity"] == 1500000.0
