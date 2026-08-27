"""Ничего личного не должно уезжать в модель и оседать в базе.

Правило проекта: ФИО, телефоны и почта маскируются ПЕРЕД отправкой в
модель; в базе лежит замаскированная копия; разворачивается только то,
что читают люди. Адреса и суммы не маскируются.

Тесты здесь стерегут три дыры, найденные аудитом 27.08: название сделки
уходило в модель как есть; сырые subject и description дела ехали рядом с
замаскированной копией; совет и тема дела оставались замаскированными у
человека.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from client_state import (  # noqa: E402
    build_evidence_events,
    build_llm_payload,
    unmask_state,
)
from funnel_profiles import BUYER_PROFILE  # noqa: E402
from masking import build_mask_map  # noqa: E402

NAME = "Лариса"
SURNAME = "Клекова"
PHONE = "+79161234567"
NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def _deal() -> dict:
    return {
        "ID": 11954,
        # В этом агентстве название сделки — это ФИО, а нередко и телефон.
        "TITLE": f"{NAME} ({SURNAME}) агент 89161234567",
        "STAGE_ID": "C18:NEW",
        "contacts": [{
            "ID": 7, "NAME": NAME, "LAST_NAME": SURNAME,
            "PHONE": [{"VALUE": PHONE}],
        }],
    }


def _mask(deal: dict):
    return build_mask_map(deal, deal["contacts"])


def _events(mask) -> list[dict]:
    timeline = [{
        "id": 1, "created": "2026-08-20",
        "comment": f"Звонил {NAME} {SURNAME} на {PHONE}",
    }]
    activities = [{
        "ID": 2, "TYPE_ID": 2, "COMPLETED": "Y", "CREATED": "2026-08-20",
        "SUBJECT": f"Звонок {NAME} {SURNAME}", "DESCRIPTION": f"телефон {PHONE}",
    }]
    return build_evidence_events(timeline, activities, [], mask)


def test_nothing_personal_reaches_the_model():
    deal = _deal()
    mask = _mask(deal)
    events = _events(mask)
    payload = build_llm_payload(deal, None, events, events, BUYER_PROFILE, mask)
    for needle in (NAME, SURNAME, "9161234567"):
        assert needle not in payload, f"«{needle}» уехало в модель"


def test_the_deal_title_is_masked_too():
    deal = _deal()
    mask = _mask(deal)
    payload = json.loads(build_llm_payload(deal, None, [], [], BUYER_PROFILE, mask))
    assert NAME not in payload["title"]
    assert "КЛИЕНТ_1" in payload["title"]


def test_raw_activity_fields_do_not_ride_along_with_the_masked_copy():
    """Поле, из которого собран текст, ничем не безопаснее самого текста."""
    mask = _mask(_deal())
    activity = [e for e in _events(mask) if e["kind"] == "activity"][0]
    for field in ("subject", "description", "text"):
        assert NAME not in activity[field]
        assert "9161234567" not in activity[field]


def test_addresses_and_sums_are_not_masked():
    """Маскируем людей, а не сделку: адрес и цена нужны в разборе."""
    deal = _deal()
    mask = _mask(deal)
    events = build_evidence_events(
        [{"id": 1, "created": "2026-08-20",
          "comment": "Гагаринский пер., 24, цена 250 000 000 руб."}],
        [], [], mask,
    )
    assert "Гагаринский" in events[0]["text"]
    assert "250 000 000" in events[0]["text"]


def test_the_report_shows_a_name_where_the_database_keeps_a_placeholder():
    """Совет читает РОП — он не должен видеть КЛИЕНТ_1."""
    deal = _deal()
    mask = _mask(deal)
    stored = {
        "next_action": "Срок прошёл — связаться по делу «Звонок КЛИЕНТ_1»",
        "work_evidence": {
            "proven": False, "reason": "due_task_no_result",
            "due_task": {"deadline": "2026-08-25", "subject": "Звонок КЛИЕНТ_1",
                         "days_overdue": 2},
        },
    }
    human = unmask_state(stored, mask, BUYER_PROFILE)
    assert NAME in human["next_action"]
    assert NAME in human["work_evidence"]["due_task"]["subject"]
    # Копия в базе остаётся замаскированной.
    assert "КЛИЕНТ_1" in stored["next_action"]
