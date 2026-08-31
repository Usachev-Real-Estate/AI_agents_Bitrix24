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
    picked = pilot.pick_deals(SELLER_PROFILE, 0, 2, "judgeable")
    assert [d["ID"] for d in picked] == ["20", "25"]


def test_cards_of_unknown_age_go_last(pilot, monkeypatch):
    """По ним отсрочка не применяется — они дали бы строгость на ровном месте."""
    from funnel_profiles import SELLER_PROFILE

    monkeypatch.setattr(pilot, "_bx_get_all_sync", lambda m, p: [
        {"ID": "30"},
        {"ID": "20", "DATE_CREATE": "2026-07-01T10:00:00+00:00"},
    ])
    picked = pilot.pick_deals(SELLER_PROFILE, 0, 2, "judgeable")
    assert [d["ID"] for d in picked] == ["20", "30"]


def _unproven() -> dict[str, Any]:
    """Претензия к работе — иначе карточка в тело отчёта не попадёт."""
    return {
        "proven": False, "reason": "no_trace_in_window",
        "window_days": 3, "days_quiet": 12.0,
    }


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
    # Карточке нужен вопрос: с 31.08 в тело отчёта попадают только такие.
    buyers = _stats("Покупатели")
    buyers["results"][0]["state"]["work_evidence"] = _unproven()
    sellers = _stats("Продавцы", cost_rub=0.75)
    sellers["results"][0]["state"]["work_evidence"] = _unproven()
    report = pilot.format_report(
        [
            (buyers, {16858: "ЖК «Will Towers»"}),
            (sellers, {16858: "Собственник"}),
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


def test_a_problem_card_from_cache_is_shown_in_full(pilot):
    """Карточка с претензией печатается разбором, даже если он из кэша."""
    cached = _stats("Покупатели", analyzed=0, skipped_unchanged=1)
    cached["results"][0]["skipped"] = True
    cached["results"][0]["reason"] = "no_new_events"
    # Разрыв «за норму этапа ничего не сделал»: у «работу не начинали» с
    # 31.08 свой раздел, и проверять на нём именно недоработку больше нечем.
    cached["results"][0]["state"]["work_evidence"] = {
        "proven": False, "reason": "no_trace_in_window", "window_days": 3,
        "days_quiet": 12.0,
    }
    report = pilot.format_report(
        [(cached, {16858: "Михаил Лужники"})],
        "https://b24-po7frr.bitrix24.ru/rest/1/t/",
    )
    assert "НЕДОРАБОТКА БРОКЕРА — 1" in report
    assert "Ситуация: Обращение с Циан" in report
    assert "без изменений с прошлого разбора" in report


def test_a_healthy_card_is_left_out_of_the_report(pilot):
    """Отчёт читает РОП, чтобы контролировать работу брокеров.

    Сначала здоровую сделку сжали до однострочника — четыре экрана про них
    топили те две, ради которых отчёт открывали. С 31.08 её нет и строкой:
    к карточке нет вопроса, и места в отчёте она не занимает. Число таких
    карточек печатается — иначе «Карточек: 10» над семью напечатанными
    читалось бы как «три потерялись».
    """
    healthy = _stats("Покупатели")
    healthy["results"][0]["state"]["work_evidence"] = {
        "proven": True, "reason": "call", "window_days": 3, "days_quiet": 1.0,
    }
    report = pilot.format_report(
        [(healthy, {16858: "Михаил Лужники"})],
        "https://b24-po7frr.bitrix24.ru/rest/1/t/",
    )
    assert "В РАБОТЕ" not in report
    assert "Михаил Лужники" not in report
    assert "Ситуация:" not in report
    assert "✅ Без вопросов: 1 в работе — в отчёт не выводятся." in report


def test_judgeable_order_excludes_stages_qc_will_not_judge(pilot, monkeypatch):
    """Дольше всего стоят «Переговоры» и «Поиск клиента» — оба вне QC.

    Без фильтра выборка набивается ими целиком и прогон не разбирает
    ни одной карточки.
    """
    from funnel_profiles import SELLER_PROFILE

    monkeypatch.setattr(pilot, "_bx_get_all_sync", lambda m, p: [
        # Стоят дольше всех, но сняты с контроля качества.
        {"ID": "1", "STAGE_ID": "UC_KEOOG8", "DATE_CREATE": "2026-01-01T10:00:00+00:00"},
        {"ID": "2", "STAGE_ID": "UC_FADPBF", "DATE_CREATE": "2026-01-02T10:00:00+00:00"},
        # А эта — та, ради которой прогон и запускают.
        {"ID": "3", "STAGE_ID": "NEW", "DATE_CREATE": "2026-05-01T10:00:00+00:00"},
    ])
    picked = pilot.pick_deals(SELLER_PROFILE, 0, 10, "judgeable")
    assert [d["ID"] for d in picked] == ["3"]


def test_random_order_is_the_default_and_reproducible(pilot, monkeypatch):
    """Крайние выборки лгут по-разному; о воронке судят по представительной."""
    from funnel_profiles import BUYER_PROFILE

    pool = [{"ID": str(i), "STAGE_ID": "C18:NEW"} for i in range(1, 21)]
    monkeypatch.setattr(pilot, "_bx_get_all_sync", lambda m, p: list(pool))

    first = [d["ID"] for d in pilot.pick_deals(BUYER_PROFILE, 18, 5)]
    second = [d["ID"] for d in pilot.pick_deals(BUYER_PROFILE, 18, 5, "random")]
    assert first == second, "тот же состав воронки — та же выборка"
    assert len(first) == 5
    assert set(first) <= {d["ID"] for d in pool}
    # Не просто первые пять по порядку — иначе это не случайная выборка.
    assert first != ["1", "2", "3", "4", "5"]


@pytest.mark.parametrize("order", ["random", "judgeable", "newest", "uncached"])
def test_out_of_qc_stages_never_take_a_slot_in_the_sample(pilot, monkeypatch, order):
    """По ним вердикта не будет в любом режиме, а место в выборке они занимают."""
    from funnel_profiles import SELLER_PROFILE

    monkeypatch.setattr(pilot, "_bx_get_all_sync", lambda m, p: [
        {"ID": "1", "STAGE_ID": "UC_KEOOG8"},   # Переговоры — вне QC
        {"ID": "2", "STAGE_ID": "UC_FADPBF"},   # Поиск клиента — вне QC
        {"ID": "3", "STAGE_ID": "NEW"},
        {"ID": "4", "STAGE_ID": "FINAL_INVOICE"},
    ])
    picked = pilot.pick_deals(SELLER_PROFILE, 0, 10, order)
    assert sorted(d["ID"] for d in picked) == ["3", "4"]


def test_a_funnel_entirely_out_of_qc_yields_an_empty_sample(pilot, monkeypatch):
    from funnel_profiles import SELLER_PROFILE

    monkeypatch.setattr(pilot, "_bx_get_all_sync", lambda m, p: [
        {"ID": str(i), "STAGE_ID": "UC_KEOOG8"} for i in range(1, 6)
    ])
    assert pilot.pick_deals(SELLER_PROFILE, 0, 5) == []


def test_uncached_skips_deals_already_in_client_states(pilot, monkeypatch):
    from funnel_profiles import BUYER_PROFILE

    monkeypatch.setattr(pilot, "_bx_get_all_sync", lambda m, p: [
        {"ID": "100", "STAGE_ID": "C18:NEW"},
        {"ID": "200", "STAGE_ID": "C18:NEW"},
        {"ID": "300", "STAGE_ID": "C18:NEW"},
    ])
    picked = pilot.pick_deals(
        BUYER_PROFILE, 18, 2, "uncached", cached_deal_ids={200},
    )
    assert [d["ID"] for d in picked] == ["300", "100"]


def test_uncached_respects_the_limit(pilot, monkeypatch):
    from funnel_profiles import SELLER_PROFILE

    monkeypatch.setattr(pilot, "_bx_get_all_sync", lambda m, p: [
        {"ID": str(i), "STAGE_ID": "NEW"} for i in range(1, 21)
    ])
    picked = pilot.pick_deals(
        SELLER_PROFILE, 0, 10, "uncached", cached_deal_ids=set(),
    )
    assert len(picked) == 10
    assert [d["ID"] for d in picked] == [str(i) for i in range(20, 10, -1)]


def test_closed_sale_takes_no_slot_in_the_sample(pilot, monkeypatch):
    """Раньше две такие карточки занимали места и разбирались моделью."""
    from funnel_profiles import SELLER_PROFILE

    monkeypatch.setattr(pilot, "_bx_get_all_sync", lambda m, p: [
        {"ID": "15862", "STAGE_ID": "UC_A94BGF"},
        {"ID": "12712", "STAGE_ID": "UC_A94BGF"},
        {"ID": "16484", "STAGE_ID": "NEW"},
    ])
    picked = pilot.pick_deals(SELLER_PROFILE, 0, 10)
    assert [d["ID"] for d in picked] == ["16484"]
