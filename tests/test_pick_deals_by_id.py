"""Разобрать ровно названные сделки, не дожидаясь случайной выборки.

Правку паузы (#16756) проверить было не на чем: карточка не попала в
выборку, а флага, чтобы спросить про неё по номеру, в скрипте не было.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
for path in (str(_SRC), str(_SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

import run_client_state_qc_pilot as pilot  # noqa: E402
from funnel_profiles import BUYER_PROFILE  # noqa: E402

OUT_OF_QC = next(iter(BUYER_PROFILE.stages_out_of_qc), "C18:PREPARATION")

DEALS = [
    {"ID": "16756", "TITLE": "Доминион", "STAGE_ID": "C18:UC_DVW1P9"},
    {"ID": "14362", "TITLE": "Ирина студия", "STAGE_ID": "C18:NEW"},
    {"ID": "10984", "TITLE": "Рыбалкина", "STAGE_ID": OUT_OF_QC},
]


def _pick(monkeypatch, **over):
    monkeypatch.setattr(pilot, "_bx_get_all_sync", lambda method, params: DEALS)
    params = {"profile": BUYER_PROFILE, "category_id": 18, "limit": 10}
    params.update(over)
    return pilot.pick_deals(**params)


def test_named_deals_are_returned_in_order(monkeypatch):
    got = _pick(monkeypatch, deal_ids=(14362, 16756))
    assert [d["ID"] for d in got] == ["14362", "16756"]


def test_the_limit_does_not_cut_named_deals(monkeypatch):
    got = _pick(monkeypatch, limit=1, deal_ids=(14362, 16756))
    assert len(got) == 2


def test_a_stage_out_of_qc_is_still_answered_when_asked_by_number(monkeypatch):
    """Спросили про карточку по номеру — отвечаем про неё, а не молчим."""
    got = _pick(monkeypatch, deal_ids=(10984,))
    assert [d["ID"] for d in got] == ["10984"]


def test_the_same_stage_is_filtered_out_of_a_random_sample(monkeypatch):
    got = _pick(monkeypatch, order="random")
    assert "10984" not in [d["ID"] for d in got]


def test_a_deal_that_is_not_there_is_named_out_loud(monkeypatch, caplog):
    """Молча вернуть меньше — выдать «карточки нет» за «мы её не искали»."""
    with caplog.at_level(logging.WARNING):
        got = _pick(monkeypatch, deal_ids=(16756, 99999))
    assert [d["ID"] for d in got] == ["16756"]
    assert "#99999" in caplog.text


def test_without_the_flag_nothing_changes(monkeypatch):
    got = _pick(monkeypatch, order="newest")
    assert [d["ID"] for d in got] == ["16756", "14362"]
