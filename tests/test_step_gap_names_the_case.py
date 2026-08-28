"""«Шага с датой нет» звучало одинаково в двух противоположных случаях.

#11954 (прогон 28.08 11:54), две строки подряд:

    🌤 Температура: тёплый — нет согласованного шага с датой; не назван бюджет
    Оценка карточки: терпимо — нет факта: бюджет (есть 6 из 7)

Вердикт из семи фактов недосчитался одного — бюджета, то есть следующий
шаг он засчитал: дело в Битриксе стоит, а «доказательство лучше
пересказа». Температура на той же карточке говорила, что шага нет.

Спора нет — вопросы разные: вердикт спрашивает, записан ли шаг хоть
где-нибудь, температура — договорились ли о нём с клиентом. Всю разницу
несло слово «согласованный», и заметить её было нельзя.

Правило не тронуто: дело брокера клиента горячим не делает. Изменилась
формулировка пробела — теперь она называет случай.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from client_state import compute_completeness_verdict, compute_temperature  # noqa: E402
from funnel_profiles import (  # noqa: E402
    BUYER_PROFILE,
    NO_AGREED_STEP,
    ONLY_BROKERS_TASK,
    SELLER_PROFILE,
)

STAGE = "C18:UC_UFPFKK"   # семь фактов, next_step среди них

BUYER_SIGNALS = {
    "client_responsive": True, "budget_named": False, "timeline_named": True,
    "next_step_agreed": False, "next_step_date": "unknown",
}
SELLER_SIGNALS = {
    "owner_responsive": True, "price_named": False,
    "next_step_agreed": False, "next_step_date": "unknown",
}


def _facts() -> dict:
    present = ("property_type", "district", "timeline",
               "shown_objects", "show_reaction")
    facts = {k: {"present": True, "quote": "цитата"} for k in present}
    facts["budget"] = {"present": False, "quote": ""}
    facts["next_step"] = {"present": False, "quote": ""}
    return {"recoverable": True, "stage_facts": facts}


def _why(profile, signals, task_scheduled: bool) -> str:
    return compute_temperature(
        signals, profile=profile, task_scheduled=task_scheduled,
    )[1]


def test_a_standing_task_is_named_instead_of_denied():
    assert ONLY_BROKERS_TASK in _why(BUYER_PROFILE, BUYER_SIGNALS, True)
    assert NO_AGREED_STEP not in _why(BUYER_PROFILE, BUYER_SIGNALS, True)


def test_without_a_task_the_wording_is_unchanged():
    assert NO_AGREED_STEP in _why(BUYER_PROFILE, BUYER_SIGNALS, False)


def test_sellers_read_the_same_way():
    assert ONLY_BROKERS_TASK in _why(SELLER_PROFILE, SELLER_SIGNALS, True)
    assert NO_AGREED_STEP in _why(SELLER_PROFILE, SELLER_SIGNALS, False)


def test_the_broker_s_own_task_never_makes_a_client_hot():
    """Правило не тронуто: температура про клиента, дело — про брокера."""
    for profile, signals in (
        (BUYER_PROFILE, BUYER_SIGNALS), (SELLER_PROFILE, SELLER_SIGNALS),
    ):
        for scheduled in (True, False):
            level, _ = compute_temperature(
                signals, profile=profile, task_scheduled=scheduled,
            )
            assert level == "warm"


def test_an_agreed_dated_step_is_still_hot():
    signals = dict(BUYER_SIGNALS)
    signals.update({
        "next_step_agreed": True, "next_step_date": "2026-09-02",
        "budget_named": True,
    })
    level, why = compute_temperature(
        signals, profile=BUYER_PROFILE, task_scheduled=False,
    )
    assert level == "hot"
    assert "шаг с датой" in why


def test_the_two_lines_no_longer_read_as_a_contradiction():
    """Ровно пара строк из #11954."""
    _lvl, why = compute_temperature(
        BUYER_SIGNALS, profile=BUYER_PROFILE, task_scheduled=True,
    )
    verdict, reason = compute_completeness_verdict(
        STAGE, _facts(), BUYER_PROFILE, hours_on_stage=1000.0,
        task_scheduled=True,
    )
    # Вердикт засчитал шаг делом — и температура теперь говорит про то же дело.
    assert verdict == "tolerable"
    assert "есть 6 из 7" in reason
    assert "следующий шаг" not in reason
    assert "делом брокера" in why
