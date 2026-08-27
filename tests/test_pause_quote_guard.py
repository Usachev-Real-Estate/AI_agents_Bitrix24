"""Причина паузы засчитывается только по дословной цитате из карточки.

Флаг снимает с брокера претензию за тишину, поэтому планка та же, что у
остальных утверждений модели: не нашли фразу в карточке — считаем, что её
не было. Иначе «клиент в отпуске» становится бесплатной индульгенцией,
которую выдаёт себе языковая модель.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from broker_work import GAP_NO_TRACE_IN_WINDOW, PROVEN_BY_PAUSE  # noqa: E402
from client_state import apply_derived_verdict  # noqa: E402
from funnel_profiles import BUYER_PROFILE  # noqa: E402

NOW = datetime.now(timezone.utc)
QUOTE = "клиент в отпуске до сентября"


def _events(text: str) -> list[dict]:
    return [
        {"kind": "comment", "created": (NOW - timedelta(days=8)).isoformat(),
         "text": text},
        {"kind": "activity", "created": (NOW - timedelta(days=8)).isoformat(),
         "type_id": 2, "completed": "N", "subject": "Выйти на показ",
         "deadline": (NOW + timedelta(days=7)).isoformat()},
    ]


def _state(quote: str) -> dict:
    return {
        "next_step": {"what": "выйти на показ", "when": "2026-09-02",
                      "who": "broker"},
        "broker_work": {
            "pause_explained": True,
            "pause_reason_quote": quote,
            "pause_until": (NOW + timedelta(days=6)).date().isoformat(),
            "comment_informative": True,
        },
    }


def _record() -> dict:
    return {
        "ID": 14776, "TITLE": "ЖК «Victory Park Residences»",
        "STAGE_ID": "C18:UC_DVW1P9",
        "MOVED_TIME": (NOW - timedelta(days=40)).isoformat(),
        "contacts": [],
    }


def test_a_quote_found_in_the_card_counts():
    state = _state(QUOTE)
    apply_derived_verdict(state, _record(), BUYER_PROFILE, {}, _events(QUOTE))
    assert state["work_evidence"]["reason"] == PROVEN_BY_PAUSE


def test_an_invented_quote_does_not_count():
    """Модель сослалась на фразу, которой в карточке нет."""
    state = _state("клиент уехал в командировку на месяц")
    apply_derived_verdict(state, _record(), BUYER_PROFILE, {}, _events(QUOTE))
    assert state["work_evidence"]["reason"] == GAP_NO_TRACE_IN_WINDOW


def test_an_empty_quote_does_not_count():
    state = _state("")
    apply_derived_verdict(state, _record(), BUYER_PROFILE, {}, _events(QUOTE))
    assert state["work_evidence"]["reason"] == GAP_NO_TRACE_IN_WINDOW


def test_an_informative_situation_defends_the_broker():
    """#16192: модель описала клиента тремя предложениями и тем же ответом
    заявила, что комментарий ни о чём. Ответ спорит сам с собой."""
    from broker_work import GAP_EMPTY_COMMENT

    state = {
        "recoverable": True,
        "situation": "Собственник готов к продаже, пришлёт дизайн-проект",
        "next_step": {"what": "получить дизайн-проект", "who": "broker"},
        "broker_work": {"comment_informative": False},
    }
    events = [{"kind": "comment", "created": (NOW - timedelta(days=1)).isoformat(),
               "text": "созвонился, пришлёт дизайн-проект"}]
    apply_derived_verdict(state, _record(), BUYER_PROFILE, {}, events)
    assert state["work_evidence"]["reason"] != GAP_EMPTY_COMMENT


def test_an_unrecoverable_card_still_counts_as_unworked():
    """Модель сама признала, что картины нет, — претензия остаётся."""
    from broker_work import GAP_EMPTY_COMMENT

    state = {
        "recoverable": False,
        "situation": "не указано",
        "next_step": {"what": "unknown", "who": "unknown"},
        "broker_work": {"comment_informative": False},
    }
    events = [{"kind": "comment", "created": (NOW - timedelta(days=1)).isoformat(),
               "text": "в работе"}]
    apply_derived_verdict(state, _record(), BUYER_PROFILE, {}, events)
    assert state["work_evidence"]["reason"] == GAP_EMPTY_COMMENT
