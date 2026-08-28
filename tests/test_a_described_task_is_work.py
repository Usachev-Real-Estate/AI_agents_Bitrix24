"""Запланированное дело засчитывается работой, если в нём описан план.

Решение агентства от 28.08: «запланированное дело можно засчитать как
работу, если в нём описаны шаги брокера и почему они такие».

Граница правила — единственное, что стоит между решением и его
вырождением. Дело-заглушка «Позвонить» работой не становится: план,
который нельзя проверить, ничем не отличается от его отсутствия. Дело без
срока не держит ничего. Дело, написанное сорок дней назад, в окно этапа не
попадает и бессрочной индульгенцией не станет. А как только срок дела
наступил, спрашивают уже за результат, а не за намерение.
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
    GAP_DUE_TASK_NO_RESULT,
    GAP_ONLY_PLANS,
    PROVEN,
    PROVEN_BY_PAUSE,
    PROVEN_BY_TASK_PLAN,
    PROVEN_BY_WAITING,
    REASON_RU,
    RECORD_FIXES,
    TASK_PLAN_MIN_LEN,
    assess_broker_work,
)
from funnel_profiles import BUYER_PROFILE  # noqa: E402

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
STAGE = "C18:UC_DVW1P9"   # Повторный показ, окно 3 дня

PLAN = (
    "Показать на Ленина 5 и Мира 12: клиент отказался от первой подборки "
    "из-за первого этажа, ищем выше пятого"
)


def _task(
    *,
    days_ago: float = 1.0,
    days_ahead: float | None = 2.0,
    subject: str = "Позвонить",
    description: str = "",
    completed: str = "N",
) -> dict[str, Any]:
    deadline = (
        (NOW + timedelta(days=days_ahead)).isoformat()
        if days_ahead is not None else ""
    )
    return {
        "kind": "activity", "type_id": 2,
        "created": (NOW - timedelta(days=days_ago)).isoformat(),
        "completed": completed, "deadline": deadline,
        "subject": subject, "description": description,
        "text": " — ".join(p for p in (subject, description) if p),
    }


def _assess(events: list[dict], **over: Any) -> dict:
    params: dict[str, Any] = {
        "profile": BUYER_PROFILE, "stage_id": STAGE,
        "hours_on_stage": 24 * 365, "claims_messaged": False,
        "comment_informative": True, "abandoned_days": 30.0, "now": NOW,
    }
    params.update(over)
    return assess_broker_work(events, **params)


# ── Само правило ───────────────────────────────────────────────────────
def test_a_described_task_counts_as_work():
    result = _assess([_task(subject="Показ", description=PLAN)])
    assert result["proven"] is True
    assert result["reason"] == PROVEN_BY_TASK_PLAN


def test_a_bare_task_still_does_not():
    """«Позвонить» не говорит ни что брокер сделает, ни почему."""
    result = _assess([_task(subject="Позвонить")])
    assert result["proven"] is False
    assert result["reason"] == GAP_ONLY_PLANS


def test_a_long_string_of_template_words_is_not_a_plan():
    """Порог длины один обмануть можно — список шаблонов закрывает это."""
    padded = "Позвонить клиенту связаться по клиенту и написать клиенту дело"
    assert len(padded) >= TASK_PLAN_MIN_LEN
    assert _assess([_task(subject=padded)])["reason"] == GAP_ONLY_PLANS


def test_mask_placeholders_do_not_count_as_the_brokers_words():
    """«Звонок ТЕЛЕФОН_1 КЛИЕНТ_1 ПОЧТА_1» — 33 символа чистой пустоты.

    Тексты доезжают сюда замаскированными. Считать плейсхолдеры за
    написанное брокером значит мерить длину нашей же подстановки.
    """
    masked = (
        "Позвонить КЛИЕНТ_1 ТЕЛЕФОН_1 ПОЧТА_1 КЛИЕНТ_2 ТЕЛЕФОН_2 ПОЧТА_2"
    )
    assert len(masked) >= TASK_PLAN_MIN_LEN
    assert _assess([_task(subject=masked)])["reason"] == GAP_ONLY_PLANS


def test_our_own_substituted_text_is_not_a_plan():
    """У дела без темы поле text заполняет аудитор, а не брокер.

    build_evidence_events подставляет «Дело без описания» тем делам, где
    брокер не написал ничего. Засчитать эту строку за план значит выдать
    отсутствие данных за результат.
    """
    ghost = _task(subject="", description="")
    ghost["text"] = "Дело без описания, срок 2026-08-30, ответственный брокер"
    assert _assess([ghost])["reason"] == GAP_ONLY_PLANS


def test_a_task_without_a_deadline_holds_nothing():
    """Битрикс метит дела без срока 9999-12-31. Это не план на будущее."""
    forever = _task(subject="Показ", description=PLAN, days_ahead=None)
    forever["deadline"] = "9999-12-31T00:00:00+03:00"
    assert _assess([forever])["reason"] == GAP_ONLY_PLANS


def test_a_closed_task_is_not_a_plan_any_more():
    done = _task(subject="Показ", description=PLAN, completed="Y")
    assert _assess([done])["reason"] != PROVEN_BY_TASK_PLAN


def test_a_task_written_before_the_window_buys_nothing():
    """Одно подробное дело не покупает брокеру месяц тишины.

    Считаем по окну этапа: дело, написанное сорок дней назад, в него не
    попадает — иначе правило вырождается ровно так же, как выродилась бы
    пауза без проверки, что срок дела в неё укладывается.
    """
    old = _task(days_ago=40.0, days_ahead=20.0, subject="Показ", description=PLAN)
    assert _assess([old])["reason"] != PROVEN_BY_TASK_PLAN


def test_a_due_task_outranks_the_plan_in_it():
    """Срок наступил — спрашивают за результат, а не за намерение."""
    overdue = _task(days_ago=1.0, days_ahead=-0.5, subject="Показ", description=PLAN)
    assert _assess([overdue])["reason"] == GAP_DUE_TASK_NO_RESULT


# ── Границы, которые правка не должна была сдвинуть ─────────────────────
def test_the_pause_branch_is_untouched():
    """Там дело засчитывается как «не забыли», а не как работа."""
    events = [
        {"kind": "comment", "created": (NOW - timedelta(days=2)).isoformat(),
         "text": "клиент в отпуске до 5 сентября"},
        _task(days_ahead=6.0, subject="Показ", description=PLAN),
    ]
    result = _assess(
        events, pause_explained=True,
        pause_until=(NOW + timedelta(days=5)).date().isoformat(),
    )
    assert result["reason"] == PROVEN_BY_PAUSE


def test_the_waiting_branch_is_untouched():
    events = [_task(days_ahead=4.0, subject="Показ", description=PLAN)]
    result = _assess(
        events, next_step_who="client",
        next_step_when=(NOW + timedelta(days=4)).date().isoformat(),
    )
    assert result["reason"] == PROVEN_BY_WAITING


def test_a_described_task_does_not_become_a_call():
    """Отчёт не должен заявлять о разговоре, которого не было."""
    result = _assess([_task(subject="Показ", description=PLAN)])
    assert result["reason"] != "call"


# ── Тексты ─────────────────────────────────────────────────────────────
def test_the_new_reason_is_in_the_tables():
    """Код без перевода уезжает РОПу как «task_plan»."""
    assert PROVEN_BY_TASK_PLAN in PROVEN
    assert PROVEN_BY_TASK_PLAN in REASON_RU
    assert "почему" in REASON_RU[PROVEN_BY_TASK_PLAN]


def test_the_bare_task_gets_an_answerable_advice():
    """Дело стоит — советовать «поставить дело» нечего."""
    fix = RECORD_FIXES[GAP_ONLY_PLANS]
    assert "Запланировать" not in fix
    assert "почему" in fix
