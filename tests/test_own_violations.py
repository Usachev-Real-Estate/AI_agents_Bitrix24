"""Tests for the broker self-check digest (/мои)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chat_poller import (  # noqa: E402
    COMMAND_MINE,
    format_own_violations,
    message_author_id,
)

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


def test_command_matches_expected_spellings():
    for text in ("/мои", "/МОИ", "!мои", "/my", "мои нарушения"):
        assert COMMAND_MINE.search(text), text


def test_command_ignores_unrelated_text():
    for text in ("мои дела", "/moi", "проверь 42", "рейтинг"):
        assert not COMMAND_MINE.search(text), text


def test_author_id_read_from_any_casing():
    assert message_author_id({"author_id": "17"}) == 17
    assert message_author_id({"AUTHOR_ID": 18}) == 18
    assert message_author_id({"authorId": 19}) == 19
    assert message_author_id({}) == 0
    assert message_author_id({"author_id": "не число"}) == 0


def test_clean_broker_gets_a_clear_answer():
    assert "нет" in format_own_violations([], NOW).lower()


def test_digest_lists_age_and_next_action():
    rows = [
        {
            "entity_type": "deal",
            "entity_id": 501,
            "rule": "seller_afina_id_missing",
            "first_detected_at": "2026-08-08T12:00:00+00:00",
        },
        {
            "entity_type": "lead",
            "entity_id": 77,
            "rule": "lead_rule_2",
            "first_detected_at": "2026-08-10T09:00:00+00:00",
        },
    ]
    text = format_own_violations(
        rows, NOW, portal_url="https://portal.bitrix24.ru",
    )
    assert "Открытых нарушений: 2" in text
    assert "Сделка #501" in text and "открыто 2 дн." in text
    assert "Лид #77" in text and "открыто 3 ч" in text
    # Подсказка «что сделать» берётся из тех же текстов, что и отчёт отдела.
    assert "Заполнить поле «ID Афины»" in text
    assert "https://portal.bitrix24.ru/crm/deal/details/501/" in text
    assert "https://portal.bitrix24.ru/crm/lead/details/77/" in text


def test_digest_without_portal_url_has_no_broken_links():
    rows = [{
        "entity_type": "deal",
        "entity_id": 1,
        "rule": "buyer_stage_2",
        "first_detected_at": "2026-08-10T11:00:00+00:00",
    }]
    text = format_own_violations(rows, NOW)
    assert "http" not in text
