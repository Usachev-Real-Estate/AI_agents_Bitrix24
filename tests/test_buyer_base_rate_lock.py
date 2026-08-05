"""Tests for buyer-deal base-rate auto-fill and lock."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from buyer_base_rate_lock import process_buyer_deals
from fill_buyer_base_rate import UF_BASE_RATE


def _settings(**overrides):
    base = dict(
        dry_run=True,
        contact_source_lock_notify=False,
        contact_source_lock_notify_user=154,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_fills_empty_rate_on_new_deal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import db as dbmod

    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "violations.db")
    dbmod.init_db()

    bx = MagicMock()
    deals = [
        {
            "ID": "700",
            "TITLE": "Новая сделка",
            "ASSIGNED_BY_ID": "92",
            UF_BASE_RATE: "",
            "MODIFY_BY_ID": "92",
        },
    ]
    settings = _settings(dry_run=False)
    with patch("buyer_base_rate_lock.set_deal_base_rate") as fill:
        fill.return_value = {"ok": True}
        stats = process_buyer_deals(
            bx,
            deals,
            rate_by_user={92: "30%"},
            restricted_ids={92},
            labels={92: "Орешникова (брокер)"},
            settings=settings,
            now_iso="2026-07-31T10:00:00+00:00",
        )
    assert stats["filled"] == 1
    assert stats["new_snapshots"] == 1
    fill.assert_called_once()
    assert dbmod.get_deal_base_rate_snapshot(700) == "30%"


def test_reverts_when_broker_changes_base_rate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import db as dbmod

    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "violations.db")
    dbmod.init_db()
    dbmod.upsert_deal_base_rate_snapshot(700, "40%", "2026-07-28T10:00:00+00:00")

    bx = MagicMock()
    deals = [
        {
            "ID": "700",
            "TITLE": "Сделка покупателя",
            "ASSIGNED_BY_ID": "66",
            UF_BASE_RATE: "99%",
            "MODIFY_BY_ID": "66",
        },
    ]
    settings = _settings(dry_run=True)
    with patch("buyer_base_rate_lock.revert_deal_base_rate") as rev:
        rev.return_value = {"dry_run_skipped": True}
        stats = process_buyer_deals(
            bx,
            deals,
            rate_by_user={66: "30%"},
            restricted_ids={66},
            labels={66: "Ульяна (брокер)"},
            settings=settings,
            now_iso="2026-07-31T12:00:00+00:00",
        )
    assert stats["reverted"] == 1
    rev.assert_called_once()
    assert dbmod.get_deal_base_rate_snapshot(700) == "40%"


def test_notifies_admin_on_revert(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import db as dbmod

    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "violations.db")
    dbmod.init_db()
    dbmod.upsert_deal_base_rate_snapshot(700, "40%", "2026-07-28T10:00:00+00:00")

    bx = MagicMock()
    deals = [
        {
            "ID": "700",
            "TITLE": "Сделка",
            "ASSIGNED_BY_ID": "66",
            UF_BASE_RATE: "10%",
            "MODIFY_BY_ID": "66",
        },
    ]
    settings = _settings(dry_run=False, contact_source_lock_notify=True)
    with patch("buyer_base_rate_lock.revert_deal_base_rate") as rev, patch(
        "buyer_base_rate_lock.send_user_chat_message",
    ) as notify:
        rev.return_value = {"ok": True}
        process_buyer_deals(
            bx,
            deals,
            rate_by_user={66: "30%"},
            restricted_ids={66},
            labels={66: "Ульяна (брокер)"},
            settings=settings,
            now_iso="2026-07-31T12:00:00+00:00",
        )
        notify.assert_called_once()
        assert notify.call_args[0][0] == 154
        assert "Покупатели" in notify.call_args[0][1]
        assert "Базовая ставка" in notify.call_args[0][1]


def test_allows_admin_base_rate_change(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import db as dbmod

    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "violations.db")
    dbmod.init_db()
    dbmod.upsert_deal_base_rate_snapshot(700, "40%", "2026-07-28T10:00:00+00:00")

    bx = MagicMock()
    deals = [
        {
            "ID": "700",
            "TITLE": "Сделка",
            "ASSIGNED_BY_ID": "66",
            UF_BASE_RATE: "35%",
            "MODIFY_BY_ID": "154",
        },
    ]
    settings = _settings(dry_run=False)
    with patch("buyer_base_rate_lock.revert_deal_base_rate") as rev:
        stats = process_buyer_deals(
            bx,
            deals,
            rate_by_user={66: "30%"},
            restricted_ids={66},
            labels={},
            settings=settings,
            now_iso="2026-07-31T12:00:00+00:00",
        )
    assert stats["allowed_updates"] == 1
    rev.assert_not_called()
    assert dbmod.get_deal_base_rate_snapshot(700) == "35%"


def test_skips_fill_when_assignee_unknown(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import db as dbmod

    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "violations.db")
    dbmod.init_db()

    bx = MagicMock()
    deals = [
        {
            "ID": "701",
            "TITLE": "Без ставки",
            "ASSIGNED_BY_ID": "16",
            UF_BASE_RATE: "",
            "MODIFY_BY_ID": "16",
        },
    ]
    settings = _settings(dry_run=False)
    with patch("buyer_base_rate_lock.set_deal_base_rate") as fill:
        stats = process_buyer_deals(
            bx,
            deals,
            rate_by_user={92: "30%"},
            restricted_ids=set(),
            labels={},
            settings=settings,
            now_iso="2026-07-31T10:00:00+00:00",
        )
    assert stats["filled"] == 0
    assert stats["new_snapshots"] == 1
    fill.assert_not_called()
    assert dbmod.get_deal_base_rate_snapshot(701) == ""
