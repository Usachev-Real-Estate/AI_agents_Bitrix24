"""Tests for KC owner Excel import mapping."""

from __future__ import annotations

import pytest

from kc_owner_import import (
    BROKER_USER_IDS,
    KcRow,
    build_contact_fields,
    build_deal_fields,
    normalize_phone,
    parse_created_at,
    resolve_broker_id,
)


def _sample_row(**overrides: object) -> KcRow:
    base = {
        "row_num": 2,
        "external_id": "507",
        "row_hash": "lead_de1c00",
        "name": "ЛОБОВ ДМИТРИЙ ВЛАДИМИРОВИЧ 07.03.1975",
        "phone": "79173906037",
        "zk": "hight life",
        "comment": "готов рассмотреть продажу",
        "created_raw": "10.07.2026 12:36",
        "responsible": "Пьянкова Ульяна",
    }
    base.update(overrides)
    return KcRow(**base)  # type: ignore[arg-type]


def test_normalize_phone() -> None:
    assert normalize_phone("79173906037") == "+79173906037"
    assert normalize_phone("89173906037") == "+79173906037"


def test_normalize_phone_invalid() -> None:
    with pytest.raises(ValueError):
        normalize_phone("123")


def test_parse_created_at() -> None:
    assert parse_created_at("10.07.2026 12:36").startswith("2026-07-10T12:36:00")


def test_resolve_broker_id() -> None:
    assert resolve_broker_id("Ветров Владислав") == BROKER_USER_IDS["Ветров Владислав"]


def test_resolve_broker_unknown() -> None:
    with pytest.raises(ValueError):
        resolve_broker_id("Unknown Broker")


def test_build_contact_fields() -> None:
    row = _sample_row()
    fields = build_contact_fields(row)
    assert fields["NAME"] == row.name
    assert fields["LAST_NAME"] == "507"
    assert fields["PHONE"][0]["VALUE"] == "+79173906037"
    assert fields["TYPE_ID"] == "UC_2G0TD3"
    assert fields["SOURCE_ID"] == "24"
    assert fields["ASSIGNED_BY_ID"] == 66
    assert fields["COMMENTS"] == (
        "Дата создания в КЦ: 10.07.2026 12:36\n"
        "hight life\n"
        "готов рассмотреть продажу"
    )


def test_build_deal_fields() -> None:
    row = _sample_row(zk="hight life", external_id="300")
    fields = build_deal_fields(row, contact_id=12345)
    assert fields["TITLE"] == "hight life 300"
    assert fields["CONTACT_ID"] == 12345
    assert fields["CATEGORY_ID"] == 0
    assert fields["STAGE_ID"] == "NEW"
    assert fields["SOURCE_ID"] == "24"
    assert fields["OPPORTUNITY"] == 0
    assert fields["UF_CRM_1774521607469"] == "0|RUB"
    assert fields["UF_CRM_1747291787883"] == "356"
    assert fields["UF_CRM_1774364892961"] == ["hight life"]
