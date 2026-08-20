"""Tests for contact PII masking."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from masking import apply_mask, build_mask_map, unmask  # noqa: E402


def _deal_with_irina() -> dict:
    return {
        "contacts": [
            {
                "NAME": "Ирина",
                "LAST_NAME": "Петрова",
                "PHONE": [{"VALUE": "+7 (916) 123-45-67"}],
                "EMAIL": [{"VALUE": "Irina.Petrova@example.com"}],
            },
        ],
    }


def test_phone_masked_in_three_formats():
    mask_map = build_mask_map(_deal_with_irina())
    assert apply_mask("перезвонил +79161234567", mask_map) == "перезвонил ТЕЛЕФОН_1"
    assert apply_mask("номер 8-916-123-45-67", mask_map) == "номер ТЕЛЕФОН_1"
    assert apply_mask("писал +7 (916) 123-45-67", mask_map) == "писал ТЕЛЕФОН_1"


def test_name_inflected_forms_masked():
    mask_map = build_mask_map(_deal_with_irina())
    assert apply_mask("передал Ирине документы", mask_map) == "передал КЛИЕНТ_1 документы"
    assert apply_mask("согласовали с Ириной", mask_map) == "согласовали с КЛИЕНТ_1"
    assert apply_mask("ждём Ирину на показе", mask_map) == "ждём КЛИЕНТ_1 на показе"


def test_email_masked():
    mask_map = build_mask_map(_deal_with_irina())
    assert apply_mask("пишите на Irina.Petrova@example.com", mask_map) == (
        "пишите на ПОЧТА_1"
    )


def test_no_contacts_returns_empty_map():
    mask_map = build_mask_map({})
    assert mask_map.placeholder_to_value == {}
    assert apply_mask("любой текст", mask_map) == "любой текст"


def test_text_without_matches_unchanged():
    mask_map = build_mask_map(_deal_with_irina())
    assert apply_mask("созвонился, договорились", mask_map) == "созвонился, договорились"


def test_unmask_restores_placeholders():
    mask_map = build_mask_map(_deal_with_irina())
    masked = apply_mask("Ирина звонила с +79161234567", mask_map)
    restored = unmask(masked, mask_map)
    assert "Ирина" in restored or "Петрова" in restored
    assert "916" in restored


# ── Границы слова ──────────────────────────────────────────────────────
def _deal_with(name: str, last: str = "") -> dict:
    return {"contacts": [{"ID": 1, "NAME": name, "LAST_NAME": last,
                          "PHONE": [], "EMAIL": []}]}


def test_short_name_does_not_match_inside_words():
    """«Ира» встречается внутри «квартира» — для базы недвижимости это фатально."""
    text = "смотрел квартиру, ждём анализа, романтика ни при чём"
    for name in ("Ира", "Лиза", "Роман"):
        assert apply_mask(text, build_mask_map(_deal_with(name))) == text, name


def test_real_name_is_still_masked():
    mask_map = build_mask_map(_deal_with("Ира"))
    assert apply_mask("Ира смотрела квартиру", mask_map) == "КЛИЕНТ_1 смотрела квартиру"
    assert apply_mask("звонил Ире вчера", mask_map) == "звонил КЛИЕНТ_1 вчера"


def test_phone_not_matched_inside_longer_number():
    mask_map = build_mask_map({
        "contacts": [{"ID": 1, "NAME": "Пётр",
                      "PHONE": [{"VALUE": "9161234567"}], "EMAIL": []}],
    })
    assert apply_mask("код 99161234567890", mask_map) == "код 99161234567890"


# ── Круговой обход ─────────────────────────────────────────────────────
def test_full_name_collapses_to_one_placeholder():
    """Один человек — один участник, иначе модель посчитает их двумя."""
    mask_map = build_mask_map(_deal_with("Ирина", "Логутина"))
    masked = apply_mask("Созвонился с Ириной Логутиной сегодня", mask_map)
    assert masked == "Созвонился с КЛИЕНТ_1 сегодня"


def test_unmask_does_not_duplicate_the_name():
    mask_map = build_mask_map(_deal_with("Ирина", "Логутина"))
    restored = unmask(apply_mask("Звонил Ириной Логутиной", mask_map), mask_map)
    assert restored.count("Ирина") == 1
    assert restored.count("Логутина") == 1


# ── Устойчивость плейсхолдеров ─────────────────────────────────────────
def test_placeholder_numbering_follows_contact_id():
    """Замаскированное состояние хранится между прогонами: КЛИЕНТ_1 обязан
    указывать на того же человека независимо от порядка выдачи API."""
    contacts = [
        {"ID": 20, "NAME": "Борис", "PHONE": [], "EMAIL": []},
        {"ID": 10, "NAME": "Анна", "PHONE": [], "EMAIL": []},
    ]
    direct = build_mask_map({}, contacts)
    reversed_order = build_mask_map({}, list(reversed(contacts)))
    assert direct.placeholder_to_value == reversed_order.placeholder_to_value
    assert direct.placeholder_to_value["КЛИЕНТ_1"] == "Анна"
