"""Tests for buyer OPPORTUNITY (commission) reminders."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from buyer_commission_reminder import (  # noqa: E402
    ROLE_BROKER,
    ROLE_ROP,
    collect_due_notifications,
    format_reminder_message,
    is_notify_due,
    is_opportunity_empty,
    resolve_rop_for_broker,
    select_empty_commission_deals,
)


def test_is_opportunity_empty_variants() -> None:
    assert is_opportunity_empty(None) is True
    assert is_opportunity_empty("") is True
    assert is_opportunity_empty(0) is True
    assert is_opportunity_empty("0") is True
    assert is_opportunity_empty("0.00000000") is True
    assert is_opportunity_empty("0|RUB") is True
    assert is_opportunity_empty("150000") is False
    assert is_opportunity_empty("150000.50|RUB") is False


def test_is_notify_due_never_sent() -> None:
    now = datetime(2026, 8, 6, 10, 0, tzinfo=timezone.utc)
    assert is_notify_due(None, 2.0, now) is True


def test_is_notify_due_interval() -> None:
    now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
    recent = (now - timedelta(hours=1)).isoformat()
    old = (now - timedelta(hours=2)).isoformat()
    assert is_notify_due(recent, 2.0, now) is False
    assert is_notify_due(old, 2.0, now) is True
    assert is_notify_due(recent, 1.0, now) is True


def test_select_empty_commission_deals_filters_pool_and_filled() -> None:
    deals = [
        {
            "ID": 1,
            "TITLE": "A",
            "STAGE_ID": "C18:NEW",
            "CATEGORY_ID": 18,
            "ASSIGNED_BY_ID": 10,
            "OPPORTUNITY": "0",
        },
        {
            "ID": 2,
            "TITLE": "B",
            "STAGE_ID": "C18:NEW",
            "CATEGORY_ID": 18,
            "ASSIGNED_BY_ID": 10,
            "OPPORTUNITY": "500000",
        },
        {
            "ID": 3,
            "TITLE": "C",
            "STAGE_ID": "C18:NEW",
            "CATEGORY_ID": 18,
            "ASSIGNED_BY_ID": 1,
            "OPPORTUNITY": None,
        },
        {
            "ID": 4,
            "TITLE": "Общая база",
            "STAGE_ID": "C26:NEW",
            "CATEGORY_ID": 26,
            "ASSIGNED_BY_ID": 10,
            "OPPORTUNITY": None,
        },
        {
            "ID": 5,
            "TITLE": "C26 stage leak",
            "STAGE_ID": "C26:PREPARATION",
            "CATEGORY_ID": 18,
            "ASSIGNED_BY_ID": 10,
            "OPPORTUNITY": "0",
        },
    ]
    selected = select_empty_commission_deals(deals, pool_user_id=1)
    assert [d["id"] for d in selected] == [1]


def test_resolve_rop_for_broker() -> None:
    assert resolve_rop_for_broker(10, {10: 44}, {44: 99}) == 99
    assert resolve_rop_for_broker(10, {10: 44}, {}) == 0
    assert resolve_rop_for_broker(10, {}, {44: 99}) == 0


def test_format_reminder_contains_warning() -> None:
    msg = format_reminder_message(
        [
            {
                "id": 14900,
                "title": "Тест",
                "stage_id": "C18:NEW",
                "assigned_by_id": 10,
            }
        ],
        deadline_hour=19,
        role=ROLE_BROKER,
        webhook_url="https://b24-po7frr.bitrix24.ru/rest/154/token/",
    )
    assert "19:00" in msg
    assert "Заполните сумму" in msg
    assert "уйдет в общую базу" not in msg
    assert "Сделка #14900" in msg
    assert "Подбор" in msg
    assert "/crm/deal/details/14900/" in msg


def test_collect_due_notifications_respects_intervals(monkeypatch) -> None:
    now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
    deals = [
        {
            "id": 100,
            "title": "A",
            "stage_id": "C18:NEW",
            "assigned_by_id": 10,
        },
        {
            "id": 101,
            "title": "B",
            "stage_id": "C18:NEW",
            "assigned_by_id": 10,
        },
    ]

    def fake_last(deal_id: int, role: str) -> str | None:
        if deal_id == 100 and role == ROLE_BROKER:
            return (now - timedelta(hours=1)).isoformat()
        if deal_id == 100 and role == ROLE_ROP:
            return (now - timedelta(minutes=30)).isoformat()
        return None

    monkeypatch.setattr(
        "buyer_commission_reminder.get_buyer_commission_notified_at",
        fake_last,
    )

    broker_groups, rop_groups = collect_due_notifications(
        deals,
        broker_dept_map={10: 44},
        rop_map={44: 99},
        broker_interval_hours=2.0,
        rop_interval_hours=1.0,
        now=now,
    )
    # deal 100 broker not due; deal 101 broker due
    assert [d["id"] for d in broker_groups[10]] == [101]
    # deal 100 rop not due; deal 101 rop due
    assert [d["id"] for d in rop_groups[99]] == [101]


def test_collect_due_skips_inactive_broker(monkeypatch) -> None:
    now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
    deals = [
        {
            "id": 200,
            "title": "Fired broker deal",
            "stage_id": "C18:NEW",
            "assigned_by_id": 222,
        }
    ]
    monkeypatch.setattr(
        "buyer_commission_reminder.get_buyer_commission_notified_at",
        lambda *_a, **_k: None,
    )
    broker_groups, rop_groups = collect_due_notifications(
        deals,
        broker_dept_map={222: 44},
        rop_map={44: 99},
        broker_interval_hours=2.0,
        rop_interval_hours=1.0,
        now=now,
        active_user_ids={99},  # broker 222 inactive
    )
    assert broker_groups == {}
    assert [d["id"] for d in rop_groups[99]] == [200]
