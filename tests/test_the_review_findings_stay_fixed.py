"""Дефекты, найденные противником в правке правил 28.08, — и их границы.

Каждый из них подтверждался запуском до правки. Тесты стоят здесь вместе,
потому что все они об одном: правило, введённое ради одного случая, ломает
соседний, и поймать это можно только перебором соседей.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from broker_work import (  # noqa: E402
    GAP_ABANDONED,
    GAP_NO_TRACE_IN_WINDOW,
    GAP_ONLY_PLANS,
    GAP_PLAN_TOO_FAR,
    GAP_TASK_DUE_TODAY,
    PROVEN_BY_PAUSE,
    PROVEN_BY_TASK_PLAN,
    PROVEN_BY_WAITING,
    REASON_RU,
    REMINDERS,
    assess_broker_work,
    due_task_without_result,
    next_action,
)
from client_state_report import (  # noqa: E402
    abandoned_noun,
    cards_noun,
    format_sections,
    split_sections,
)
from funnel_profiles import BUYER_PROFILE  # noqa: E402

NOW = datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc)
STAGE = "C18:UC_DVW1P9"   # Повторный показ, окно 3 дня
WEBHOOK = "https://example.bitrix24.ru/rest/1/token/"

PLAN = "Показать на Ленина 5 и Мира 12: клиент отказался от подборки из-за этажа"


def _task(*, ago: float = 1.0, ahead: float = 2.0, subject: str = "Позвонить",
          description: str = "") -> dict[str, Any]:
    return {
        "kind": "activity", "type_id": 2, "completed": "N",
        "created": (NOW - timedelta(days=ago)).isoformat(),
        "deadline": (NOW + timedelta(days=ahead)).isoformat(),
        "subject": subject, "description": description,
    }


def _comment(ago: float, text: str = "созвонились") -> dict[str, Any]:
    return {
        "kind": "comment", "text": text,
        "created": (NOW - timedelta(days=ago)).isoformat(),
    }


def _assess(events: list[dict], **over: Any) -> dict:
    params: dict[str, Any] = {
        "profile": BUYER_PROFILE, "stage_id": STAGE,
        "hours_on_stage": 24 * 365, "claims_messaged": False,
        "comment_informative": True, "abandoned_days": 30.0, "now": NOW,
    }
    params.update(over)
    return assess_broker_work(events, **params)


# ── Заброшенность встала первой и начала съедать соседей ────────────────
def test_a_pause_with_a_task_on_control_is_not_abandonment():
    """Назвать причину не должно быть хуже, чем промолчать."""
    events = [_comment(40.0, "клиент за границей до ноября"), _task(ago=40.0, ahead=65.0)]
    result = _assess(
        events, pause_explained=True,
        pause_until=(NOW + timedelta(days=64)).date().isoformat(),
    )
    assert result["reason"] == PROVEN_BY_PAUSE


def test_waiting_with_a_task_on_control_is_not_abandonment():
    events = [_comment(40.0, "агент наберёт"), _task(ago=40.0, ahead=10.0)]
    result = _assess(
        events, next_step_who="client",
        next_step_when=(NOW + timedelta(days=10)).date().isoformat(),
    )
    assert result["reason"] == PROVEN_BY_WAITING


def test_without_a_task_the_silence_still_ends():
    """Граница не должна была отменить саму починку."""
    assert _assess(
        [_comment(200.0, "клиент на паузе")],
        pause_explained=True, pause_until="unknown",
    )["reason"] == GAP_ABANDONED


def test_the_advice_for_an_abandoned_card_is_a_decision():
    """Строка карточки говорит «брошена» — совет не должен звать закрыть дело."""
    state = {"next_step": {}, "work_evidence": {
        "proven": False, "reason": GAP_ABANDONED, "abandoned_days": 107.0,
    }}
    advice = next_action(state, [_task(ago=107.0, ahead=-90.0)], NOW)
    assert "брошена 107 дн." in advice
    assert "закрывать сделку" in advice


# ── У плана появился горизонт ──────────────────────────────────────────
def test_a_plan_dated_beyond_the_stage_norm_is_not_this_weeks_work():
    """Иначе одно дело на два года вперёд закрывает каждое окно навсегда."""
    far = _task(ahead=365.0, subject="Показ", description=PLAN)
    assert _assess([far])["reason"] == GAP_PLAN_TOO_FAR


def test_a_plan_inside_the_norm_still_counts():
    near = _task(ahead=2.0, subject="Показ", description=PLAN)
    assert _assess([near])["reason"] == PROVEN_BY_TASK_PLAN


def test_the_far_plan_is_not_accused_of_being_empty():
    """«В деле не сказано, что и почему» про расписанное дело — неправда."""
    assert "не сказано" not in REASON_RU[GAP_PLAN_TOO_FAR]
    assert "позже" in REASON_RU[GAP_PLAN_TOO_FAR]
    assert GAP_PLAN_TOO_FAR in REMINDERS


def test_the_far_plan_advice_argues_with_the_date_not_the_record():
    state = {"next_step": {}, "work_evidence": {
        "proven": False, "reason": GAP_PLAN_TOO_FAR, "window_days": 3,
    }}
    advice = next_action(state, [_task(ahead=365.0, subject="Показ",
                                       description=PLAN)], NOW)
    assert "норма этапа — 3 дн." in advice
    assert "почему" in advice


def test_a_template_in_an_oblique_case_is_still_a_template():
    """«Звонка клиенту по клиентам созвона» — падежи те же шаблоны."""
    padded = _task(
        subject="Звонка клиенту по клиентам созвона с клиентами по звонкам делам",
    )
    assert _assess([padded])["reason"] == GAP_ONLY_PLANS


# ── Отписка ищется по каждому делу отдельно ────────────────────────────
def test_one_forgotten_task_does_not_silence_the_others():
    """Комментарий восьмидесятидневной давности не закрывает дело на сегодня."""
    events = [
        _task(ago=100.0, ahead=-90.0, subject="старое"),
        _task(ago=1.0, ahead=0.2, subject="сегодня"),
        _comment(80.0),
    ]
    due = due_task_without_result(events, NOW)
    assert due is not None
    assert due["subject"] == "сегодня"
    assert due["due_today"] is True


def test_a_result_written_today_still_closes_todays_task():
    events = [_task(ago=1.0, ahead=0.2), _comment(0.01, "дозвонился")]
    assert due_task_without_result(events, NOW) is None


# ── Отчёт не спорит сам с собой ────────────────────────────────────────
def _abandoned_card(verdict: str) -> dict[str, Any]:
    return {"deal_id": 1, "skipped": False, "state": {
        "temperature": "warm", "verdict": verdict, "next_step": {},
        "work_evidence": {
            "proven": False, "reason": GAP_ABANDONED, "window_days": 3,
            "days_quiet": 107.0, "abandoned_days": 107.0,
        }}}


def test_a_hundred_days_of_silence_is_never_too_early_to_judge():
    """Отсрочка считается от входа на этап, тишина — от последнего следа.

    Сделка, переставленная на новый этап час назад после ста дней тишины,
    получала «рано судить» и «брошена» разом — и отчёт печатал «ни одной
    карточки с признаками потери» прямо над списком брошенных.
    """
    losing, abandoned, _n, _r, _w, _f = split_sections(
        [_abandoned_card("too_early")],
    )
    assert [r["deal_id"] for r in losing] == [1]
    assert [r["deal_id"] for r in abandoned] == [1]


def test_the_empty_shortfall_section_does_not_ignore_the_lists_above():
    body = format_sections([_abandoned_card("poor")], {1: "Сделка"}, WEBHOOK)
    assert "Работа подтверждена по всем прочитанным карточкам" not in body
    assert "1 брошенная — выше" in body


def test_the_overlap_line_counts_the_overlap_not_the_list():
    body = format_sections([_abandoned_card("poor")], {1: "Сделка"}, WEBHOOK)
    assert "Из них 1 брошенная — отдельным списком ниже." in body


def test_a_reminder_is_named_in_the_empty_shortfall_section():
    card = {"deal_id": 2, "skipped": False, "state": {
        "temperature": "warm", "verdict": "poor", "next_step": {},
        "work_evidence": {
            "proven": False, "reason": GAP_TASK_DUE_TODAY, "window_days": 3,
            "due_task": {"deadline": "2026-08-28", "days_overdue": 0,
                         "subject": "Позвонить", "due_today": True},
        }}}
    body = format_sections([card], {2: "Сделка"}, WEBHOOK)
    assert "Работа подтверждена по всем прочитанным карточкам" not in body
    assert "по 1 карточке — напоминание ниже" in body


def test_the_bare_task_shows_what_is_written_in_it():
    """Порог в 60 символов решает ✅ против 🔧 — РОП должен его проверить."""
    result = _assess([_task(subject="Позвонить")])
    assert result["reason"] == GAP_ONLY_PLANS
    assert result["task_text"] == "Позвонить"

    card = {"deal_id": 4, "skipped": False, "state": {
        "temperature": "warm", "verdict": "poor", "next_step": {},
        "work_evidence": result}}
    body = format_sections([card], {4: "Сделка"}, WEBHOOK)
    assert "в деле только «Позвонить»" in body


def test_a_hundred_days_are_never_counted_as_too_early_to_judge():
    """«Судить ещё рано» и «брошена 107 дн.» об одной карточке в одном отчёте."""
    body = format_sections([_abandoned_card("too_early")], {1: "Сделка"}, WEBHOOK)
    assert "судить ещё рано" not in body
    assert "Карточка брошена" in body


def test_the_alarm_puts_the_worst_abandoned_card_first():
    """Полный разбор достаётся 🚨 — значит сортировка нужна и там."""
    def card(deal_id: int, quiet: float) -> dict[str, Any]:
        return {"deal_id": deal_id, "skipped": False, "state": {
            "temperature": "warm", "verdict": "poor", "next_step": {},
            "work_evidence": {
                "proven": False, "reason": GAP_ABANDONED, "window_days": 3,
                "days_quiet": quiet, "abandoned_days": quiet,
            }}}

    losing, abandoned, _n, _r, _w, _f = split_sections([card(1, 41.0), card(2, 107.0)])
    assert [r["deal_id"] for r in losing] == [2, 1]
    assert [r["deal_id"] for r in abandoned] == [2, 1]


def test_an_overdue_task_does_not_silence_the_claim():
    """Решение агентства: «претензия может быть только если дело просрочено».

    Значит просрочка претензию не гасит, а стоит рядом с ней. Раньше самый
    мягкий вердикт перебивал самый тяжёлый: карточка без единого следа
    работы получала «напоминание: срок дела прошёл».
    """
    events = [_comment(10.0), _task(ago=10.0, ahead=-5.0)]
    result = _assess(events)
    assert result["reason"] == GAP_NO_TRACE_IN_WINDOW
    assert result["due_task"]["days_overdue"] >= 1


def test_the_card_shows_both_arguments_at_once():
    """Скобки должны подпирать ту претензию, которая напечатана.

    Показать только срок дела под строкой про норму этапа значит дать
    читателю цифру, которая к претензии не относится: он вычтет и не сойдётся.
    """
    card = {"deal_id": 5, "skipped": False, "state": {
        "temperature": "warm", "verdict": "poor", "next_step": {},
        "work_evidence": {
            "proven": False, "reason": GAP_NO_TRACE_IN_WINDOW,
            "window_days": 3, "days_quiet": 9.0,
            "due_task": {"deadline": "2026-08-20", "days_overdue": 8,
                         "subject": "Позвонить", "due_today": False},
        }}}
    body = format_sections([card], {5: "Сделка"}, WEBHOOK)
    assert "норма 3 дн., последний след 9 дн. назад" in body
    assert "просрочено на 8 дн." in body


def test_the_numerals_decline():
    """«по 21 карточкам» и «21 брошенных» — там, где отчёт просят перепроверить."""
    assert cards_noun(1) == "карточке"
    assert cards_noun(11) == "карточкам"
    assert cards_noun(21) == "карточке"
    assert abandoned_noun(1) == "брошенная"
    assert abandoned_noun(3) == "брошенные"
    assert abandoned_noun(5) == "брошенных"
    assert abandoned_noun(11) == "брошенных"
    assert abandoned_noun(21) == "брошенная"


def test_an_unreadable_card_keeps_its_gap_visible_in_one_line():
    """Карточка ушла в ✅ и унесла обе строки о пробеле с собой."""
    card = {"deal_id": 3, "skipped": False, "state": {
        "temperature": "warm", "verdict": "good", "recoverable": False,
        "next_step": {}, "work_evidence": {
            "proven": True, "reason": "call", "window_days": 3,
        }}}
    body = format_sections([card], {3: "Сделка"}, WEBHOOK)
    assert "В РАБОТЕ" in body
    assert "сделку по карточке не подхватить" in body
