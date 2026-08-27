"""Неотработанная карточка: контакт передали, звонка нет, одна отметка."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from broker_work import (  # noqa: E402
    GAP_EMPTY_COMMENT,
    PROVEN_BY_CALL,
    assess_broker_work,
)
from client_state_report import (  # noqa: E402
    format_card,
    format_source_mix,
    split_sections,
)
from funnel_profiles import SELLER_PROFILE  # noqa: E402

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
WEBHOOK = "https://b24-po7frr.bitrix24.ru/rest/1/token/"


def _assess(events: list[dict], **over: Any) -> dict:
    params: dict[str, Any] = {
        "profile": SELLER_PROFILE, "stage_id": "NEW",
        "hours_on_stage": 1000.0, "claims_messaged": False,
        "claims_no_answer": False, "comment_informative": False, "now": NOW,
    }
    params.update(over)
    return assess_broker_work(events, **params)


def _mark(hours: float = 2.0) -> dict:
    """Отметка «в работе» — ровно то, чем брокеры закрывают требование."""
    return {
        "kind": "comment", "created": (NOW - timedelta(hours=hours)).isoformat(),
        "text": "в работе", "has_files": False,
    }


# ── Само правило ───────────────────────────────────────────────────────
def test_a_bare_in_progress_mark_is_an_unworked_card():
    result = _assess([_mark()])
    assert result["proven"] is False
    assert result["reason"] == GAP_EMPTY_COMMENT


def test_the_wording_matches_what_the_agency_calls_it():
    from broker_work import REASON_RU

    assert "не отработана" in REASON_RU[GAP_EMPTY_COMMENT]
    assert "звонка нет" in REASON_RU[GAP_EMPTY_COMMENT]


def test_a_call_turns_it_into_worked():
    call = {
        "kind": "activity", "created": (NOW - timedelta(hours=2)).isoformat(),
        "type_id": 2, "completed": "Y", "text": "Звонок",
    }
    assert _assess([call, _mark()])["reason"] == PROVEN_BY_CALL


def test_a_substantive_comment_turns_it_into_worked():
    detailed = dict(
        _mark(),
        text="Дозвонился, собственник продаёт за 40 млн, покажет в четверг",
    )
    assert _assess([detailed], comment_informative=True)["proven"] is True


# ── Место в отчёте ─────────────────────────────────────────────────────
def _card(deal_id: int, **over: Any) -> dict[str, Any]:
    state = {
        "client_goal": "", "situation": "Контакт от Диспозла, разговора нет",
        "next_step": {"what": "Связаться с клиентом", "when": "unknown",
                      "who": "broker"},
        "risk": "medium", "confidence": 0.2, "recoverable": False,
        "temperature": "unknown", "temperature_reason": "нельзя восстановить",
        "verdict": "poor", "verdict_reason": "нельзя восстановить",
        "missing": [], "contradictions": [], "source_id": "26",
        "work_evidence": {
            "proven": False, "reason": GAP_EMPTY_COMMENT,
            "window_days": 1, "days_quiet": 0.2,
        },
    }
    state.update(over)
    return {"deal_id": deal_id, "skipped": False, "reason": "", "state": state}


def test_an_unworked_contact_is_a_broker_failure():
    _losing, _aband, neglect, _rem, _w, _f = split_sections([_card(16306)])
    assert [r["deal_id"] for r in neglect] == [16306]


def test_an_unworked_contact_is_not_also_called_a_loss():
    """Я поставил такие карточки в оба раздела — прогон показал, что зря.

    Десять карточек продавцов попали и в «теряем», и в «недоработку»: два
    одинаковых списка, между которыми РОП ничего не выбирает. Неотработанный
    контакт — это претензия к брокеру, и называть её ещё и потерей значит
    писать один факт дважды.
    """
    losing, _aband, neglect, _rem, _w, _f = split_sections([_card(16306)])
    assert losing == []
    assert [r["deal_id"] for r in neglect] == [16306]


def test_an_uninformative_card_where_work_was_proven_is_a_loss():
    """Брокер говорил с клиентом, а в карточке этого не видно — картина уходит."""
    worked = _card(16886)
    worked["state"]["work_evidence"] = {
        "proven": True, "reason": "call", "window_days": 1, "days_quiet": 0.2,
    }
    losing, _aband, _n, _rem, _w, _f = split_sections([worked])
    assert [r["deal_id"] for r in losing] == [16886]


def test_the_card_names_the_failure_plainly():
    card = format_card(_card(16306), "диспозл excel Lucky", WEBHOOK)
    assert "карточка не отработана" in card
    assert "звонка нет" in card
    assert GAP_EMPTY_COMMENT not in card


def test_no_cold_base_label_survives():
    """Механизм снят: карточки Диспозла — обычные контакты, их отрабатывают."""
    card = format_card(_card(16306), "диспозл excel Lucky", WEBHOOK)
    assert "Холодная база" not in card
    assert "🧊" not in card


def test_the_source_mix_still_shows_where_contacts_came_from():
    line = format_source_mix({"26": 4, "1": 3})
    assert "Диспозл 5% 4" in line
    assert "холодная база" not in line
