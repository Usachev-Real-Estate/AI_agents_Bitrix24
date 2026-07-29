"""Tests for deal SOURCE_ID lock in sellers funnel."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from deal_source_lock import process_modified_deals


def _settings(**overrides):
    base = dict(
        dry_run=True,
        contact_source_lock_notify=False,
        contact_source_lock_notify_user=154,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_process_reverts_when_broker_changes_deal_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import db as dbmod

    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "violations.db")
    dbmod.init_db()
    dbmod.upsert_deal_source_snapshot(500, "CALL", "2026-07-28T10:00:00+00:00")

    bx = MagicMock()
    deals = [
        {
            "ID": "500",
            "TITLE": "Квартира на продажу",
            "SOURCE_ID": "WEB",
            "MODIFY_BY_ID": "66",
        },
    ]
    settings = _settings(dry_run=True)
    with patch("deal_source_lock.revert_deal_source") as rev:
        rev.return_value = {"dry_run_skipped": True}
        stats = process_modified_deals(
            bx,
            deals,
            restricted_ids={66},
            labels={66: "Ульяна (брокер)"},
            settings=settings,
            now_iso="2026-07-28T12:00:00+00:00",
        )
    assert stats["reverted"] == 1
    rev.assert_called_once()
    assert dbmod.get_deal_source_snapshot(500) == "CALL"


def test_process_notifies_admin_not_broker_on_deal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import db as dbmod

    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "violations.db")
    dbmod.init_db()
    dbmod.upsert_deal_source_snapshot(500, "CALL", "2026-07-28T10:00:00+00:00")

    bx = MagicMock()
    deals = [
        {
            "ID": "500",
            "TITLE": "Квартира",
            "SOURCE_ID": "WEB",
            "MODIFY_BY_ID": "66",
        },
    ]
    settings = _settings(dry_run=False, contact_source_lock_notify=True)
    with patch("deal_source_lock.revert_deal_source") as rev, patch(
        "deal_source_lock.send_user_chat_message",
    ) as notify:
        rev.return_value = {"ok": True}
        process_modified_deals(
            bx,
            deals,
            restricted_ids={66},
            labels={66: "Ульяна (брокер)"},
            settings=settings,
            now_iso="2026-07-28T12:00:00+00:00",
        )
        notify.assert_called_once()
        assert notify.call_args[0][0] == 154
        assert "Продавцы" in notify.call_args[0][1]
        assert "Ульяна" in notify.call_args[0][1]


def test_process_allows_admin_deal_source_change(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import db as dbmod

    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "violations.db")
    dbmod.init_db()
    dbmod.upsert_deal_source_snapshot(500, "CALL", "2026-07-28T10:00:00+00:00")

    bx = MagicMock()
    deals = [
        {
            "ID": "500",
            "TITLE": "Дом",
            "SOURCE_ID": "WEB",
            "MODIFY_BY_ID": "154",
        },
    ]
    settings = _settings(dry_run=False)
    with patch("deal_source_lock.revert_deal_source") as rev:
        stats = process_modified_deals(
            bx,
            deals,
            restricted_ids={66},
            labels={},
            settings=settings,
            now_iso="2026-07-28T12:00:00+00:00",
        )
    assert stats["allowed_updates"] == 1
    rev.assert_not_called()
    assert dbmod.get_deal_source_snapshot(500) == "WEB"
