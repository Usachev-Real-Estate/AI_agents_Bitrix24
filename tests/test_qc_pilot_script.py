"""Пилотный прогон QC: выборка сделок и сборка отчёта."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _load_pilot():
    path = _ROOT / "scripts" / "run_client_state_qc_pilot.py"
    spec = importlib.util.spec_from_file_location("qc_pilot", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def pilot():
    return _load_pilot()


def test_selection_requests_the_qualification_fields(pilot, monkeypatch):
    """Без UF-полей прямая проверка бюджета/района видит пустую карточку."""
    from funnel_profiles import BUYER_PROFILE

    captured: dict[str, Any] = {}

    def _fake(method, params):
        captured["method"] = method
        captured["params"] = params
        return [{"ID": "1", "TITLE": "Сделка"}]

    monkeypatch.setattr(pilot, "_bx_get_all_sync", _fake)
    pilot.pick_deals(BUYER_PROFILE, 18, 10)

    assert captured["method"] == "crm.deal.list"
    select = captured["params"]["select"]
    for code, _name in BUYER_PROFILE.qualification_fields:
        assert code in select, f"{code} не запрошен — проверка не увидит поле"
    assert "STAGE_ID" in select, "этап нужен гейту, который экономит на модели"


def test_newest_order_is_still_available_for_debugging(pilot, monkeypatch):
    from funnel_profiles import SELLER_PROFILE

    monkeypatch.setattr(pilot, "_bx_get_all_sync", lambda m, p: [
        {"ID": "10"}, {"ID": "30"}, {"ID": "20"},
    ])
    picked = pilot.pick_deals(SELLER_PROFILE, 0, 2, "newest")
    assert [d["ID"] for d in picked] == ["30", "20"]


def test_default_order_takes_the_cards_that_can_be_judged(pilot, monkeypatch):
    """«Самые новые» системно набирают карточки моложе отсрочки."""
    from funnel_profiles import SELLER_PROFILE

    monkeypatch.setattr(pilot, "_bx_get_all_sync", lambda m, p: [
        {"ID": "30", "DATE_CREATE": "2026-08-24T11:00:00+00:00"},   # час назад
        {"ID": "20", "DATE_CREATE": "2026-07-01T10:00:00+00:00"},   # давно
        {"ID": "25", "DATE_CREATE": "2026-08-01T10:00:00+00:00"},   # три недели
    ])
    picked = pilot.pick_deals(SELLER_PROFILE, 0, 2)
    assert [d["ID"] for d in picked] == ["20", "25"]


def test_cards_of_unknown_age_go_last(pilot, monkeypatch):
    """По ним отсрочка не применяется — они дали бы строгость на ровном месте."""
    from funnel_profiles import SELLER_PROFILE

    monkeypatch.setattr(pilot, "_bx_get_all_sync", lambda m, p: [
        {"ID": "30"},
        {"ID": "20", "DATE_CREATE": "2026-07-01T10:00:00+00:00"},
    ])
    picked = pilot.pick_deals(SELLER_PROFILE, 0, 2)
    assert [d["ID"] for d in picked] == ["20", "30"]


def _stats(label: str, **over: Any) -> dict[str, Any]:
    stats = {
        "funnel_label": label,
        "total": 1, "analyzed": 1,
        "temperature": {"hot": 0, "warm": 1, "cold": 0, "unknown": 0},
        "verdicts": {"good": 0, "tolerable": 0, "poor": 1, "too_early": 0,
                     "out_of_qc": 0},
        "cost_rub": 1.25, "cost_rub_per_card": 1.25,
        "results": [{
            "deal_id": 16858, "skipped": False, "reason": "",
            "state": {
                "client_goal": "Покупка", "situation": "Обращение с Циан",
                "next_step": {"what": "Позвонить", "when": "unknown",
                              "who": "broker"},
                "risk": "medium", "confidence": 0.8, "recoverable": True,
                "temperature": "warm", "temperature_reason": "не названы сроки",
                "verdict": "poor", "verdict_reason": "нет фактов",
                "missing": [], "contradictions": [],
            },
        }],
    }
    stats.update(over)
    return stats


def test_report_covers_both_funnels_and_sums_the_cost(pilot):
    report = pilot.format_report(
        [
            (_stats("Покупатели"), {16858: "ЖК «Will Towers»"}),
            (_stats("Продавцы", cost_rub=0.75), {16858: "Собственник"}),
        ],
        "https://b24-po7frr.bitrix24.ru/rest/1/t/",
    )
    assert "ПОКУПАТЕЛИ" in report
    assert "ПРОДАВЦЫ" in report
    assert "всего 2.00 ₽" in report
    assert "ЖК «Will Towers»" in report
    assert "Собственник" in report


def test_report_carries_no_service_codes(pilot):
    report = pilot.format_report(
        [(_stats("Покупатели"), {16858: "ЖК «Will Towers»"})],
        "https://b24-po7frr.bitrix24.ru/rest/1/t/",
    )
    for code in ("warm", "poor", "broker", "medium", "unknown", "no_new_events"):
        assert code not in report, f"служебный код {code!r} дошёл до РОПа"


def test_report_shows_a_cached_card_instead_of_a_skip_line(pilot):
    cached = _stats("Покупатели", analyzed=0, skipped_unchanged=1)
    cached["results"][0]["skipped"] = True
    cached["results"][0]["reason"] = "no_new_events"
    report = pilot.format_report(
        [(cached, {16858: "Михаил Лужники"})],
        "https://b24-po7frr.bitrix24.ru/rest/1/t/",
    )
    assert "Цель: Покупка" in report
    assert "без изменений с прошлого разбора" in report
