"""Tests for the card-completeness verdict."""

from __future__ import annotations

import sys

from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from client_state import (  # noqa: E402
    MATERIAL_SEVERITY,
    MINOR_SEVERITY,
    compute_completeness_verdict,
    grace_period_for,
    _normalize_state,
)
from funnel_profiles import (  # noqa: E402
    BUYER_PROFILE, SELLER_PROFILE, all_facts_for_stage,
)


def _state(**over):
    base = {
        "recoverable": True,
        "contradictions": [],
        "stage_facts": {},
        "signals": {},
    }
    base.update(over)
    return base


def _facts(*present_keys, stage="C18:NEW", profile="buyers"):
    """Все обязательные факты, из них перечисленные — present=True."""
    return {
        key: {
            "present": key in present_keys,
            "quote": "цитата" if key in present_keys else "",
        }
        for key, _n, _r in all_facts_for_stage(profile, stage)
    }


# ── Основной путь ──────────────────────────────────────────────────────
def test_good_when_all_required_present_and_no_contradictions():
    facts = _facts(
        "property_type", "budget", "district", "timeline", "next_step",
    )
    state = _state(stage_facts=facts)
    level, why = compute_completeness_verdict(
        "C18:NEW", state, BUYER_PROFILE, hours_on_stage=100,
    )
    assert level == "good", why


def test_poor_when_two_or_more_required_missing():
    state = _state(stage_facts=_facts("property_type"))  # 4 отсутствуют
    level, _ = compute_completeness_verdict(
        "C18:NEW", state, BUYER_PROFILE, hours_on_stage=100,
    )
    assert level == "poor"


def test_tolerable_when_one_required_missing():
    state = _state(stage_facts=_facts("property_type", "budget", "district", "timeline"))
    level, why = compute_completeness_verdict(
        "C18:NEW", state, BUYER_PROFILE, hours_on_stage=100,
    )
    assert level == "tolerable"
    # Причину читает РОП — в ней человеческое название факта, не ключ.
    assert "следующий шаг с датой" in why
    assert "next_step" not in why


def test_tolerable_when_only_minor_contradictions():
    state = _state(
        stage_facts=_facts("property_type", "budget", "district", "timeline", "next_step"),
        contradictions=[{"what": "дата", "in_card": "x", "in_call": "y", "severity": "low"}],
    )
    level, _ = compute_completeness_verdict(
        "C18:NEW", state, BUYER_PROFILE, hours_on_stage=100,
    )
    assert level == "tolerable"


def test_poor_when_material_contradiction():
    state = _state(
        stage_facts=_facts("property_type", "budget", "district", "timeline", "next_step"),
        contradictions=[{"what": "позиция", "in_card": "x", "in_call": "y", "severity": "high"}],
    )
    level, _ = compute_completeness_verdict(
        "C18:NEW", state, BUYER_PROFILE, hours_on_stage=100,
    )
    assert level == "poor"


def test_poor_when_unrecoverable():
    level, _ = compute_completeness_verdict(
        "C18:NEW", _state(recoverable=False), BUYER_PROFILE, hours_on_stage=100,
    )
    assert level == "poor"


# ── Отсрочка ───────────────────────────────────────────────────────────
def test_too_early_before_grace_period():
    state = _state()
    level, why = compute_completeness_verdict(
        "C18:NEW", state, BUYER_PROFILE, hours_on_stage=10,
    )
    assert level == "too_early"
    assert "72" in why  # 3 дня на Подборе


def test_buyer_selection_stage_uses_three_day_grace():
    assert grace_period_for(BUYER_PROFILE, "C18:NEW") == 72


def test_other_buyer_stages_use_default_grace():
    assert grace_period_for(BUYER_PROFILE, "C18:UC_UFPFKK") == 24


def test_seller_stages_use_default_grace():
    assert grace_period_for(SELLER_PROFILE, "NEW") == 24
    assert grace_period_for(SELLER_PROFILE, "FINAL_INVOICE") == 24


def test_grace_bypassed_when_hours_unknown():
    """Если время на этапе не удалось прочитать, отсрочку не применяем."""
    state = _state(stage_facts=_facts())  # все отсутствуют
    level, _ = compute_completeness_verdict(
        "C18:NEW", state, BUYER_PROFILE, hours_on_stage=None,
    )
    assert level == "poor"


# ── Этапы вне QC ───────────────────────────────────────────────────────
@pytest.mark.parametrize("stage", ["C18:UC_RUCRAH", "C18:UC_8X12HI", "C18:WON"])
def test_buyer_stages_out_of_qc(stage):
    level, _ = compute_completeness_verdict(
        stage, _state(), BUYER_PROFILE, hours_on_stage=100,
    )
    assert level == "out_of_qc"


@pytest.mark.parametrize("stage", ["UC_KEOOG8", "UC_FADPBF", "WON"])
def test_seller_stages_out_of_qc(stage):
    level, _ = compute_completeness_verdict(
        stage, _state(), SELLER_PROFILE, hours_on_stage=100,
    )
    assert level == "out_of_qc"


def test_stage_without_requirements_is_out_of_qc():
    """Незнакомый этап тоже вне QC — нельзя судить по несуществующим правилам."""
    level, _ = compute_completeness_verdict(
        "C18:UC_2ZBA0G", _state(), BUYER_PROFILE, hours_on_stage=100,
    )
    assert level == "out_of_qc"


# ── Разделение расхождений на minor / material ─────────────────────────
def test_severity_classification_is_consistent():
    assert MINOR_SEVERITY == {"low"}
    assert MATERIAL_SEVERITY == {"medium", "high"}


# ── Стадии продавцов ───────────────────────────────────────────────────
def test_seller_meeting_needs_address_type_timeline_step():
    facts = _facts(
        "property_address", "property_type", "selling_timeline", "next_step",
        stage="NEW", profile="sellers",
    )
    state = _state(stage_facts=facts)
    level, _ = compute_completeness_verdict(
        "NEW", state, SELLER_PROFILE, hours_on_stage=100,
    )
    assert level == "good"


def test_seller_preparation_photo_and_documents_are_optional():
    """Отсутствие фотосессии/документов не роняет вердикт."""
    facts = _facts(
        "property_address", "property_type", "selling_timeline", "next_step",
        "listing_price",
        stage="FINAL_INVOICE", profile="sellers",
    )
    state = _state(stage_facts=facts)
    level, _ = compute_completeness_verdict(
        "FINAL_INVOICE", state, SELLER_PROFILE, hours_on_stage=100,
    )
    assert level == "good"


# ── Нормализация stage_facts ───────────────────────────────────────────
def test_stage_facts_are_normalized_from_llm_output():
    raw = {
        "stage_facts": {
            "budget": {"present": True, "quote": "до 25 млн"},
            "district": {"present": False, "quote": ""},
            "garbage": "not-a-dict",
        }
    }
    state = _normalize_state(raw, BUYER_PROFILE)
    facts = state["stage_facts"]
    assert facts["budget"]["present"] is True
    assert facts["budget"]["quote"] == "до 25 млн"
    assert facts["district"]["present"] is False
    assert "garbage" not in facts


def test_stage_facts_empty_by_default():
    state = _normalize_state({}, BUYER_PROFILE)
    assert state["stage_facts"] == {}


# ── Прямая проверка UF-полей карточки ──────────────────────────────────
def _facts_all_present():
    """Все обязательные для Подбора факты присутствуют."""
    return _facts("property_type", "budget", "district", "timeline", "next_step")


def test_qualification_fields_all_filled_returns_ok():
    from client_state import check_qualification_fields
    deal = {
        "UF_CRM_1774363333518": "до 25 млн",
        "UF_CRM_1774364869184": "Хамовники",
        "UF_CRM_1747291787883": "квартира",
    }
    ok, missing = check_qualification_fields(deal, BUYER_PROFILE, "C18:NEW")
    assert ok is True and missing == []


def test_qualification_fields_missing_ones_listed_in_russian():
    from client_state import check_qualification_fields
    deal = {"UF_CRM_1774363333518": "до 25 млн"}  # район и тип пустые
    ok, missing = check_qualification_fields(deal, BUYER_PROFILE, "C18:NEW")
    assert ok is False
    assert "район/локация" in missing and "тип недвижимости" in missing


def test_qualification_treats_empty_string_and_zero_as_unfilled():
    from client_state import check_qualification_fields
    deal = {
        "UF_CRM_1774363333518": "",
        "UF_CRM_1774364869184": "0",
        "UF_CRM_1747291787883": None,
    }
    ok, missing = check_qualification_fields(deal, BUYER_PROFILE, "C18:NEW")
    assert ok is False and len(missing) == 3


def test_qualification_returns_none_when_not_configured():
    """У продавцов проверка полей не настроена — вердикт её не учитывает."""
    from client_state import check_qualification_fields
    ok, missing = check_qualification_fields({}, SELLER_PROFILE, "NEW")
    assert ok is None and missing == []


def test_verdict_uses_qualification_gap_to_downgrade_to_poor():
    """Один недостающий факт + пустые поля карточки → poor, а не tolerable."""
    state = _state(stage_facts=_facts(
        "property_type", "budget", "district", "timeline",  # next_step отсутствует
    ))
    level, why = compute_completeness_verdict(
        "C18:NEW", state, BUYER_PROFILE,
        hours_on_stage=100,
        qualification_ok=False,
    )
    assert level == "poor"
    assert "поля карточки" in why


def test_verdict_tolerable_when_facts_ok_but_qualification_missing():
    """Все LLM-факты есть, но поля Битрикса не заполнены → терпимо."""
    state = _state(stage_facts=_facts_all_present())
    level, _ = compute_completeness_verdict(
        "C18:NEW", state, BUYER_PROFILE,
        hours_on_stage=100,
        qualification_ok=False,
    )
    assert level == "tolerable"


def test_verdict_good_when_facts_and_qualification_ok():
    state = _state(stage_facts=_facts_all_present())
    level, _ = compute_completeness_verdict(
        "C18:NEW", state, BUYER_PROFILE,
        hours_on_stage=100,
        qualification_ok=True,
    )
    assert level == "good"


def test_verdict_reason_never_leaks_service_keys():
    """Причина вердикта уходит в отчёт РОПу — служебных ключей там быть не должно."""
    from funnel_profiles import BUYER_STAGE_REQUIREMENTS, SELLER_STAGE_REQUIREMENTS

    for profile, table in (
        (BUYER_PROFILE, BUYER_STAGE_REQUIREMENTS),
        (SELLER_PROFILE, SELLER_STAGE_REQUIREMENTS),
    ):
        for stage, requirements in table.items():
            _, why = compute_completeness_verdict(
                stage, _state(stage_facts={}), profile, hours_on_stage=1000,
            )
            for key, name in requirements:
                assert key not in why, f"{stage}: ключ {key!r} попал в отчёт"
            assert any(name in why for _key, name in requirements), stage


# ── Отсрочка: свежая карточка пуста не по вине брокера ─────────────────
def test_fresh_empty_card_is_too_early_not_poor():
    """Лид пришёл час назад — он пуст по определению, брокер ещё не звонил."""
    level, why = compute_completeness_verdict(
        "C18:NEW", _state(recoverable=False), BUYER_PROFILE, hours_on_stage=1,
    )
    assert level == "too_early", why


def test_stale_empty_card_is_still_poor():
    """После отсрочки пустая карточка — это уже претензия."""
    level, _ = compute_completeness_verdict(
        "C18:NEW", _state(recoverable=False), BUYER_PROFILE, hours_on_stage=100,
    )
    assert level == "poor"


def test_contradiction_outranks_the_grace_period():
    """Расхождение с разговором — про достоверность, а не про срок."""
    state = _state(contradictions=[{
        "what": "бюджет", "in_card": "до 30 млн", "in_call": "максимум 20 млн",
        "severity": "high",
    }])
    level, _ = compute_completeness_verdict(
        "C18:NEW", state, BUYER_PROFILE, hours_on_stage=1,
    )
    assert level == "poor"


def test_unknown_stage_age_does_not_grant_an_endless_grace():
    """Неизвестен возраст — судим как старую: иначе отсрочка станет лазейкой."""
    level, _ = compute_completeness_verdict(
        "C18:NEW", _state(recoverable=False), BUYER_PROFILE, hours_on_stage=None,
    )
    assert level == "poor"


# ── «Плохо» должно быть сортируемым, иначе им нельзя пользоваться ──────
def test_poor_verdict_says_how_many_facts_are_present():
    """Карточка с 5 фактами из 7 и пустая — оба «плохо», но не одно и то же."""
    almost = _state(stage_facts=_facts(
        "property_type", "budget", "district", "timeline", "next_step",
        stage="C18:UC_UFPFKK",
    ))
    level, why = compute_completeness_verdict(
        "C18:UC_UFPFKK", almost, BUYER_PROFILE, hours_on_stage=100,
    )
    assert level == "poor"
    assert "есть 5 из 7" in why

    nothing = _state(stage_facts={})
    _, why_empty = compute_completeness_verdict(
        "C18:UC_UFPFKK", nothing, BUYER_PROFILE, hours_on_stage=100,
    )
    assert "есть 0 из 7" in why_empty


def test_tolerable_verdict_also_carries_the_score():
    state = _state(stage_facts=_facts(
        "property_type", "budget", "district", "timeline",
    ))
    level, why = compute_completeness_verdict(
        "C18:NEW", state, BUYER_PROFILE, hours_on_stage=100,
    )
    assert level == "tolerable"
    assert "есть 4 из 5" in why


def test_the_score_does_not_move_the_threshold():
    """Цифра — для сортировки, а не новое правило."""
    one_missing = _state(stage_facts=_facts(
        "property_type", "budget", "district", "timeline",
    ))
    two_missing = _state(stage_facts=_facts(
        "property_type", "budget", "district",
    ))
    assert compute_completeness_verdict(
        "C18:NEW", one_missing, BUYER_PROFILE, hours_on_stage=100,
    )[0] == "tolerable"
    assert compute_completeness_verdict(
        "C18:NEW", two_missing, BUYER_PROFILE, hours_on_stage=100,
    )[0] == "poor"


def test_a_stage_without_rules_gets_its_own_verdict():
    """Раньше такой этап показывался как «вне контроля качества» —
    ненаписанные правила выглядели решением руководства."""
    level, why = compute_completeness_verdict(
        "UC_BRAND_NEW_STAGE", _state(), SELLER_PROFILE, hours_on_stage=1000,
    )
    assert level == "no_rules"
    assert "правила полноты" in why


def test_an_excluded_stage_still_says_out_of_qc():
    """Решение агентства должно остаться отличимым от нашей недоделки."""
    level, why = compute_completeness_verdict(
        "UC_KEOOG8", _state(), SELLER_PROFILE, hours_on_stage=1000,
    )
    assert level == "out_of_qc"
    assert "у руководства" in why


def test_every_known_seller_stage_is_now_covered():
    """Пробел, в который ушли 4 из 10 карточек, закрыт «Закрытой продажей»."""
    from funnel_profiles import (
        SELLER_DIRECT_FIELD_CHECKS,
        SELLER_STAGE_REQUIREMENTS,
        SELLER_STAGES_OUT_OF_QC,
    )
    from tools import SELLERS_STAGE_NAMES

    for stage in SELLERS_STAGE_NAMES:
        assert (
            stage in SELLER_STAGE_REQUIREMENTS
            or stage in SELLER_STAGES_OUT_OF_QC
            or stage in SELLER_DIRECT_FIELD_CHECKS
        ), f"{stage} не покрыт ни одним правилом"


# ── «Закрытая продажа»: только ID Афины, без квалификации клиента ──────
def test_closed_sale_is_good_when_the_afina_id_is_filled():
    level, why = compute_completeness_verdict(
        "UC_A94BGF", _state(), SELLER_PROFILE,
        hours_on_stage=1000, qualification_ok=True,
    )
    assert level == "good"
    assert "ID объекта Афины" in why


def test_closed_sale_is_poor_when_the_afina_id_is_empty():
    level, why = compute_completeness_verdict(
        "UC_A94BGF", _state(), SELLER_PROFILE,
        hours_on_stage=1000, qualification_ok=False,
    )
    assert level == "poor"
    assert "не заполнено: ID объекта Афины" in why


def test_closed_sale_needs_no_text_facts():
    """Пустая карточка на этом этапе — не «плохо», если поле заполнено."""
    level, _ = compute_completeness_verdict(
        "UC_A94BGF", _state(stage_facts={}), SELLER_PROFILE,
        hours_on_stage=1000, qualification_ok=True,
    )
    assert level == "good"


def test_an_unchecked_field_is_not_reported_as_good():
    """Ставить «хорошо» по непроверенному полю — выдать пробел за результат."""
    level, why = compute_completeness_verdict(
        "UC_A94BGF", _state(), SELLER_PROFILE,
        hours_on_stage=1000, qualification_ok=None,
    )
    assert level == "no_rules"
    assert "не проверены" in why


def test_the_afina_field_is_the_one_the_agency_named():
    from funnel_profiles import SELLER_DIRECT_FIELD_CHECKS

    assert SELLER_DIRECT_FIELD_CHECKS["UC_A94BGF"] == (
        ("UF_CRM_1780911079", "ID объекта Афины"),
    )
