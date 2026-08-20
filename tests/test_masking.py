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
