"""Сборка ленты стадий: именно здесь легче всего молча потерять данные."""

import analytics  # noqa: F401  — кладёт src/analytics на sys.path
from stages import build_stage_events, normalize_history_rows


def _history(*pairs):
    return [
        {"ID": idx + 1, "OWNER_ID": 7, "STAGE_ID": stage,
         "CREATED_TIME": moment, "CATEGORY_ID": 18}
        for idx, (stage, moment) in enumerate(pairs)
    ]


def test_deal_without_history_still_enters_the_funnel():
    """Сделка, ни разу не менявшая стадию, не должна исчезать из воронки.

    crm.stagehistory.list возвращает строки только для двигавшихся сущностей.
    Без синтеза первого события свежие сделки — самые интересные — выпали бы.
    """
    events = build_stage_events(
        "deal", 7, [],
        category_id=18,
        date_create="2026-08-01T10:00:00+00:00",
        current_stage_id="C18:NEW",
    )
    assert len(events) == 1
    assert events[0]["stage_id"] == "C18:NEW"
    assert events[0]["entered_at"] == "2026-08-01T10:00:00+00:00"
    assert events[0]["left_at"] is None
    assert events[0]["duration_sec"] is None


def test_consecutive_intervals_are_chained():
    events = build_stage_events(
        "deal", 7,
        _history(
            ("C18:NEW", "2026-08-01T10:00:00+03:00"),
            ("C18:UC_UFPFKK", "2026-08-03T10:00:00+03:00"),
            ("C18:WON", "2026-08-05T10:00:00+03:00"),
        ),
        category_id=18,
        date_create="2026-08-01T07:00:00+00:00",
        current_stage_id="C18:WON",
    )
    assert [e["stage_id"] for e in events] == ["C18:NEW", "C18:UC_UFPFKK", "C18:WON"]
    assert [e["seq"] for e in events] == [0, 1, 2]
    assert events[0]["left_at"] == events[1]["entered_at"]
    assert events[0]["duration_sec"] == 2 * 24 * 3600
    # Последний интервал открыт — длительность считается на момент чтения.
    assert events[-1]["left_at"] is None
    assert events[-1]["duration_sec"] is None


def test_closed_deal_closes_last_interval():
    events = build_stage_events(
        "deal", 7,
        _history(("C18:NEW", "2026-08-01T10:00:00+03:00")),
        category_id=18,
        date_create="2026-08-01T07:00:00+00:00",
        current_stage_id="C18:NEW",
        closed_at="2026-08-02T07:00:00+00:00",
    )
    assert events[-1]["left_at"] == "2026-08-02T07:00:00+00:00"
    assert events[-1]["duration_sec"] == 24 * 3600


def test_history_starting_after_creation_is_stretched_back():
    """История стадий включена не с рождения портала.

    Если первое известное событие позже создания, срок жизни сделки до него
    просто исчез бы, и цикл сделки систематически занижался.
    """
    events = build_stage_events(
        "deal", 7,
        _history(("C18:UC_UFPFKK", "2026-08-10T10:00:00+00:00")),
        category_id=18,
        date_create="2026-08-01T10:00:00+00:00",
        current_stage_id="C18:UC_UFPFKK",
    )
    assert events[0]["entered_at"] == "2026-08-01T10:00:00+00:00"


def test_repeated_stage_rows_are_collapsed():
    """Пересохранение карточки без смены стадии — не второй заход на стадию."""
    events = build_stage_events(
        "deal", 7,
        _history(
            ("C18:NEW", "2026-08-01T10:00:00+00:00"),
            ("C18:NEW", "2026-08-01T11:00:00+00:00"),
            ("C18:WON", "2026-08-02T10:00:00+00:00"),
        ),
        category_id=18,
        date_create="2026-08-01T10:00:00+00:00",
        current_stage_id="C18:WON",
    )
    assert [e["stage_id"] for e in events] == ["C18:NEW", "C18:WON"]


def test_rows_are_ordered_by_time_not_by_arrival():
    rows = _history(
        ("C18:WON", "2026-08-05T10:00:00+00:00"),
        ("C18:NEW", "2026-08-01T10:00:00+00:00"),
    )
    assert [r["stage_id"] for r in normalize_history_rows(rows)] == ["C18:NEW", "C18:WON"]


def test_rows_without_stage_or_time_are_dropped():
    rows = [
        {"ID": 1, "STAGE_ID": "", "CREATED_TIME": "2026-08-01T10:00:00+00:00"},
        {"ID": 2, "STAGE_ID": "C18:NEW", "CREATED_TIME": ""},
    ]
    assert normalize_history_rows(rows) == []
