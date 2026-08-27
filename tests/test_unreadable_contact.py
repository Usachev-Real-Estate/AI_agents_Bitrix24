"""Непрочитанный контакт — это не «контактов нет».

Аудит 27.08: crm.contact.get падал, исключение глоталось, и карточка
уходила в модель с пустой маской — то есть с именем и телефоном клиента
открытым текстом. Отсутствие данных выдавалось за результат.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import client_state  # noqa: E402
from client_state import fetch_deal_contacts, prepare_deal_record  # noqa: E402

DEAL = {"ID": 4242, "TITLE": "Сделка", "STAGE_ID": "C18:NEW"}


def _bx(monkeypatch, contact_result):
    def fake(method, params):
        if method == "crm.deal.contact.items.get":
            return [{"CONTACT_ID": 5}]
        if method == "crm.contact.get":
            return contact_result()
        return []

    monkeypatch.setattr(client_state, "_bx_get_all_sync", fake)


def test_contact_read_failure_is_reported(monkeypatch):
    def boom():
        raise RuntimeError("503")

    _bx(monkeypatch, boom)
    contacts, failed = fetch_deal_contacts(dict(DEAL))
    assert contacts == []
    assert failed is True


def test_deal_without_contacts_is_not_a_failure(monkeypatch):
    monkeypatch.setattr(
        client_state, "_bx_get_all_sync", lambda method, params: [],
    )
    contacts, failed = fetch_deal_contacts(dict(DEAL))
    assert contacts == []
    assert failed is False


def test_contact_read_is_read(monkeypatch):
    _bx(monkeypatch, lambda: {"ID": 5, "NAME": "Ирина"})
    contacts, failed = fetch_deal_contacts(dict(DEAL))
    assert [c["NAME"] for c in contacts] == ["Ирина"]
    assert failed is False


def test_deal_contacts_list_failure_is_reported(monkeypatch):
    def fake(method, params):
        if method == "crm.deal.contact.items.get":
            raise RuntimeError("503")
        return []

    monkeypatch.setattr(client_state, "_bx_get_all_sync", fake)
    contacts, failed = fetch_deal_contacts(dict(DEAL))
    assert contacts == []
    assert failed is True


def test_card_with_unreadable_contact_is_marked_incomplete(monkeypatch):
    """Карточка с непрочитанным контактом не должна дойти до модели."""

    def boom():
        raise RuntimeError("503")

    _bx(monkeypatch, boom)
    monkeypatch.setattr(
        client_state,
        "_fetch_entity_timeline",
        lambda deal_id, kind: (
            None,
            [{
                "ID": 1,
                "CREATED": "2026-08-26T10:05:00+03:00",
                "COMMENT": "Созвонился с Ириной Логутиной, ищет 2к",
            }],
            False,
        ),
    )
    monkeypatch.setattr(
        client_state, "_fetch_deal_activities", lambda deal_id: (None, [], False),
    )
    monkeypatch.setattr(
        client_state, "fetch_and_cache", lambda deal_id, settings=None: [],
    )

    record = prepare_deal_record(dict(DEAL))
    assert record["evidence_incomplete"] is True
    assert record["transcripts"] == []


@pytest.mark.parametrize("preloaded", [[], [{"ID": 5, "NAME": "Ирина"}]])
def test_preloaded_contacts_never_fail(preloaded):
    deal = dict(DEAL)
    deal["contacts"] = preloaded
    contacts, failed = fetch_deal_contacts(deal)
    assert contacts == preloaded
    assert failed is False
