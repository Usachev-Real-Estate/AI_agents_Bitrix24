"""Теряем клиента — это когда с ним не ведётся работа.

Определение агентства от 28.08 дословно: «Теряем клиента — это когда с ним
не ведётся работа от брокера: не пишутся комментарии, не планируются дела,
нет исходящих звонков, или брошен на этапе долгое время».

До него правило держалось на температуре и обросло за один день тремя
исключениями: холод у собственников не считается, холод внутри отсрочки не
считается, дело на сегодня не считается. Каждое было верным, а вместе они
спорили друг с другом, и объяснить РОПу, почему карточка в тревоге, стало
нечем.

Заодно решение агентства ставит напоминание раньше квалификации: «надо
чтобы ему было напоминание, а только потом квалификация температуры».
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import broker_work  # noqa: E402
from client_state_report import LOSING_GAPS, split_sections  # noqa: E402

TEMPERATURES = ("hot", "warm", "cold", "unknown")


def _card(reason: str, *, temperature: str = "warm", **work: Any) -> dict:
    evidence = {
        "proven": reason in broker_work.PROVEN, "reason": reason,
        "window_days": 3, "days_quiet": 9.0,
    }
    evidence.update(work)
    return {
        "deal_id": 1, "skipped": False,
        "state": {
            "temperature": temperature, "temperature_reason": "—",
            "verdict": "poor", "next_step": {},
            "work_evidence": evidence,
        },
    }


def _section(reason: str, **over: Any) -> str:
    losing, abandoned, neglected, reminders, waiting, fine = split_sections(
        [_card(reason, **over)],
    )
    return "+".join(
        name for name, rows in (
            ("теряем", losing), ("брошены", abandoned),
            ("недоработка", neglected), ("напомнить", reminders),
            ("рано", waiting), ("норма", fine),
        ) if rows
    )


# ── Определение по буквам ──────────────────────────────────────────────
def test_no_trace_at_all_is_a_loss():
    """Ни комментария, ни дела, ни звонка — все три условия сразу."""
    assert _section(broker_work.GAP_NO_TRACE) == "теряем+недоработка"


def test_nothing_within_the_stage_norm_is_a_loss():
    assert _section(broker_work.GAP_NO_TRACE_IN_WINDOW) == "теряем+недоработка"


def test_a_card_abandoned_for_long_is_a_loss():
    """Вторая половина определения — дословно."""
    assert _section(
        broker_work.GAP_ABANDONED, abandoned_days=107.0,
    ) == "теряем+брошены"


def test_a_planned_task_means_we_are_not_losing_them():
    """«Не планируются дела» про карточку с делом — неправда.

    Претензия к тексту дела остаётся недоработкой: план есть, проверить его
    нельзя. Это разговор о записи, а не о том, что клиента бросили.
    """
    assert _section(broker_work.GAP_ONLY_PLANS) == "недоработка"


def test_a_written_comment_means_we_are_not_losing_them():
    """«Не пишутся комментарии» про написанный комментарий — неправда."""
    assert _section(broker_work.GAP_EMPTY_COMMENT) == "недоработка"
    assert _section(broker_work.GAP_CLAIMED_MESSAGE) == "недоработка"
    assert _section(broker_work.GAP_CLAIMED_NO_ANSWER) == "недоработка"


def test_proven_work_is_never_a_loss():
    for reason in sorted(broker_work.PROVEN):
        assert _section(reason) in {"норма", "напомнить"}, reason


# ── Температура ────────────────────────────────────────────────────────
def test_the_temperature_never_decides_the_alarm():
    """Ни один ярлык не заводит карточку в тревогу и не выводит из неё."""
    for temperature in TEMPERATURES:
        assert _section(
            broker_work.GAP_NO_TRACE, temperature=temperature,
        ) == "теряем+недоработка", temperature
        assert _section(
            broker_work.GAP_EMPTY_COMMENT, temperature=temperature,
        ) == "недоработка", temperature


def test_the_grace_period_still_silences_everything():
    """Карточка суток от роду пуста потому, что брокер ещё не работал."""
    card = _card(broker_work.GAP_NO_TRACE)
    card["state"]["verdict"] = "too_early"
    losing, _ab, _n, _rem, _w, _f = split_sections([card])
    assert losing == []


# ── Лестница ───────────────────────────────────────────────────────────
def test_a_reminder_is_never_anything_else():
    """Решение агентства: сначала напоминание, потом квалификация."""
    for reason in sorted(broker_work.REMINDERS):
        for temperature in TEMPERATURES:
            assert _section(
                reason, temperature=temperature,
            ) == "напомнить", f"{reason}/{temperature}"


def test_no_gap_is_both_a_reminder_and_a_loss():
    """Инвариант: множества не пересекаются, иначе лестница неоднозначна."""
    assert LOSING_GAPS & broker_work.REMINDERS == frozenset()


def test_every_losing_gap_has_a_russian_wording():
    for reason in sorted(LOSING_GAPS):
        assert reason in broker_work.REASON_RU, reason
