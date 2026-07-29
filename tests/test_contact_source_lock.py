"""Tests for contact SOURCE_ID lock (brokers / ROPs)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from contact_source_lock import (
    collect_restricted_user_ids,
    process_modified_contacts,
)


def _settings(**overrides):
    base = dict(
        owner_sales_dept_ids=[60, 66],
        contact_source_lock_exclude_user_ids=set(),
        contact_source_lock_exclude_names={"Агентство Недвижимости"},
        admin_user_id=154,
        b24_user_id=154,
        contact_source_lock_rop_position_substr=["РОП", "Руководитель отдела продаж"],
        dry_run=True,
        contact_source_lock_notify=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_collect_restricted_includes_broker_and_rop():
    users = [
        {
            "ID": "66",
            "NAME": "Ульяна",
            "LAST_NAME": "Пьянкова",
            "WORK_POSITION": "Брокер",
            "UF_DEPARTMENT": [66],
            "ACTIVE": True,
        },
        {
            "ID": "46",
            "NAME": "Антон",
            "LAST_NAME": "Кретов",
            "WORK_POSITION": "Руководитель отдела продаж (РОП)",
            "UF_DEPARTMENT": [60],
            "ACTIVE": True,
        },
        {
            "ID": "154",
            "NAME": "Даниил",
            "LAST_NAME": "Юкин",
            "WORK_POSITION": "Операционный менеджер",
            "UF_DEPARTMENT": [56],
            "ACTIVE": True,
        },
    ]
    restricted, labels = collect_restricted_user_ids(users, _settings())
    assert 66 in restricted
    assert 46 in restricted
    assert 154 not in restricted
    assert "РОП" in labels[46]
    assert "брокер" in labels[66]


def test_process_reverts_when_broker_changes_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Isolate DB under tmp
    import db as dbmod

    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "violations.db")
    dbmod.init_db()
    dbmod.upsert_contact_source_snapshot(100, "CALL", "2026-07-28T10:00:00+00:00")

    bx = MagicMock()
    contacts = [
        {
            "ID": "100",
            "NAME": "Тест",
            "LAST_NAME": "Клиент",
            "SOURCE_ID": "WEB",
            "MODIFY_BY_ID": "66",
        },
    ]
    settings = _settings(dry_run=True)
    with patch("contact_source_lock.revert_contact_source") as rev:
        rev.return_value = {"dry_run_skipped": True}
        stats = process_modified_contacts(
            bx,
            contacts,
            restricted_ids={66},
            labels={66: "Ульяна (брокер)"},
            settings=settings,
            now_iso="2026-07-28T12:00:00+00:00",
        )
    assert stats["reverted"] == 1
    rev.assert_called_once()
    # Snapshot stays CALL in dry_run path (not overwritten with WEB)
    assert dbmod.get_contact_source_snapshot(100) == "CALL"


def test_process_notifies_admin_not_broker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import db as dbmod

    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "violations.db")
    dbmod.init_db()
    dbmod.upsert_contact_source_snapshot(100, "CALL", "2026-07-28T10:00:00+00:00")

    bx = MagicMock()
    contacts = [
        {
            "ID": "100",
            "NAME": "Тест",
            "LAST_NAME": "Клиент",
            "SOURCE_ID": "WEB",
            "MODIFY_BY_ID": "66",
        },
    ]
    settings = _settings(
        dry_run=False,
        contact_source_lock_notify=True,
        contact_source_lock_notify_user=154,
    )
    with patch("contact_source_lock.revert_contact_source") as rev, patch(
        "contact_source_lock.send_user_chat_message",
    ) as notify:
        rev.return_value = {"ok": True}
        process_modified_contacts(
            bx,
            contacts,
            restricted_ids={66},
            labels={66: "Ульяна (брокер)"},
            settings=settings,
            now_iso="2026-07-28T12:00:00+00:00",
        )
        notify.assert_called_once()
        assert notify.call_args[0][0] == 154
        assert "Ульяна" in notify.call_args[0][1]


def test_process_allows_admin_source_change(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import db as dbmod

    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "violations.db")
    dbmod.init_db()
    dbmod.upsert_contact_source_snapshot(100, "CALL", "2026-07-28T10:00:00+00:00")

    bx = MagicMock()
    contacts = [
        {
            "ID": "100",
            "SOURCE_ID": "WEB",
            "MODIFY_BY_ID": "154",
            "NAME": "Тест",
            "LAST_NAME": "",
        },
    ]
    settings = _settings(dry_run=False)
    with patch("contact_source_lock.revert_contact_source") as rev:
        stats = process_modified_contacts(
            bx,
            contacts,
            restricted_ids={66},
            labels={},
            settings=settings,
            now_iso="2026-07-28T12:00:00+00:00",
        )
    assert stats["allowed_updates"] == 1
    rev.assert_not_called()
    assert dbmod.get_contact_source_snapshot(100) == "WEB"
