"""Tests for exclusive smart-process expiry reminders."""

from datetime import date
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from exclusive_expiry import (  # noqa: E402
    days_until,
    format_reminder_message,
    parse_end_date,
    resolve_due_milestone,
    resolve_notify_recipients,
    select_due_reminders,
)


class _FakeSettings:
    exclusive_end_date_field = "ufCrm20_1784712129125"
    exclusive_address_field = "ufCrm20_1784712031409"
    exclusive_skip_stage_ids = {"DT1080_26:SUCCESS", "DT1080_26:FAIL"}
    exclusive_expiry_days = 7
    exclusive_expiry_milestones = [7, 3, 1]
    exclusive_expiry_catch_up = True
    exclusive_entity_type_id = 1080
    exclusive_notify_user_ids = [32, 154, 378]
    b24_webhook_url = "https://example.bitrix24.ru/rest/1/token/"


def test_parse_end_date_from_datetime():
    assert parse_end_date("2026-07-25T03:00:00+03:00") == date(2026, 7, 25)


def test_parse_end_date_empty():
    assert parse_end_date("") is None
    assert parse_end_date(None) is None
    assert parse_end_date("0000-00-00") is None


def test_resolve_due_milestone_exact_days():
    assert resolve_due_milestone(7, [7, 3, 1], catch_up=False) == 7
    assert resolve_due_milestone(3, [7, 3, 1], catch_up=False) == 3
    assert resolve_due_milestone(1, [7, 3, 1], catch_up=False) == 1
    assert resolve_due_milestone(5, [7, 3, 1], catch_up=False) is None
    assert resolve_due_milestone(2, [7, 3, 1], catch_up=False) is None


def test_resolve_due_milestone_catch_up_windows():
    assert resolve_due_milestone(5, [7, 3, 1], catch_up=True) == 7
    assert resolve_due_milestone(2, [7, 3, 1], catch_up=True) == 3
    assert resolve_due_milestone(0, [7, 3, 1], catch_up=True) == 1
    assert resolve_due_milestone(8, [7, 3, 1], catch_up=True) is None
    assert resolve_due_milestone(-1, [7, 3, 1], catch_up=True) is None


def test_days_until():
    assert days_until(date(2026, 7, 30), date(2026, 7, 23)) == 7


def test_resolve_notify_recipients_includes_assigned_and_watchers():
    assert resolve_notify_recipients(10, [32, 154, 378]) == [10, 32, 154, 378]


def test_resolve_notify_recipients_dedupes_assigned_in_watchers():
    assert resolve_notify_recipients(378, [32, 154, 378]) == [378, 32, 154]


def test_format_reminder_message_contains_important():
    msg = format_reminder_message(
        title="Тест",
        end=date(2026, 7, 30),
        days_left=7,
        item_url="https://example/crm/type/1080/details/1/",
        address="Москва",
    )
    assert "ВАЖНО" in msg
    assert "через 7 дн." in msg
    assert "30.07.2026" in msg
    assert "Москва" in msg


def test_select_due_reminders_filters(monkeypatch):
    monkeypatch.setattr(
        "exclusive_expiry.was_exclusive_expiry_notified",
        lambda item_id, end_date, days_before: False,
    )
    settings = _FakeSettings()
    items = [
        {
            "id": 1,
            "title": "due in 7",
            "assignedById": 10,
            "stageId": "DT1080_26:NEW",
            "ufCrm20_1784712129125": "2026-07-30T03:00:00+03:00",
            "ufCrm20_1784712031409": "Адрес 1",
        },
        {
            "id": 2,
            "title": "too early",
            "assignedById": 10,
            "stageId": "DT1080_26:NEW",
            "ufCrm20_1784712129125": "2026-08-30T03:00:00+03:00",
        },
        {
            "id": 3,
            "title": "closed",
            "assignedById": 10,
            "stageId": "DT1080_26:SUCCESS",
            "ufCrm20_1784712129125": "2026-07-30T03:00:00+03:00",
        },
        {
            "id": 5,
            "title": "due in 2 -> milestone 3 catch-up",
            "assignedById": 10,
            "stageId": "DT1080_26:NEW",
            "ufCrm20_1784712129125": "2026-07-25T03:00:00+03:00",
        },
    ]
    due = select_due_reminders(items, settings, today=date(2026, 7, 23))
    assert {(d["item_id"], d["milestone"]) for d in due} == {(1, 7), (5, 3)}


def test_select_due_reminders_skips_already_notified(monkeypatch):
    monkeypatch.setattr(
        "exclusive_expiry.was_exclusive_expiry_notified",
        lambda item_id, end_date, days_before: True,
    )
    settings = _FakeSettings()
    items = [
        {
            "id": 1,
            "title": "already",
            "assignedById": 10,
            "stageId": "DT1080_26:NEW",
            "ufCrm20_1784712129125": "2026-07-30",
        },
    ]
    due = select_due_reminders(items, settings, today=date(2026, 7, 23))
    assert due == []
