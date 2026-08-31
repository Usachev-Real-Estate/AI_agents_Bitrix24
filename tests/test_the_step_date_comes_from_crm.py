"""Дата шага берётся из Битрикса, даже когда срок дела уже настал.

Прогон 31.08 14:01, #14094: «Шаг: Повторный созвон (2026-08-20, брокер)» и
через две строки «🔔 Напоминание: дело стоит на сегодня (срок 2026-08-31)».
Модель назвала одну дату, CRM держит другую, и читателю предъявлены обе.

Дефект тот же, что чинили на #16322, только через другую дверь: подстановка
брала дату только у дела в БУДУЩЕМ (`open_future_deadline`), а дело со
сроком сегодня утром будущим уже не считается. То есть правило «дело в
Битриксе — доказательство, пересказ — слова» молчало ровно там, где дата
важнее всего: в напоминании.

Заодно про чужое дело. #15810: «работу не начинали: ни дела», строкой выше
«Шаг: Связаться с клиентом (2026-08-19)», и совет «срок прошёл, дела нет».
Дело в карточке есть — его завёл импорт, а не брокер. Претензия верна, а
«дела нет» читается как ошибка отчёта: РОП видит дело своими глазами.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from broker_work import next_action  # noqa: E402
from client_state_report import describe_next_step  # noqa: E402

NOW = datetime(2026, 8, 31, 14, 0, tzinfo=timezone.utc)
BROKER = 11
ROBOT = 99


def _state(**over: Any) -> dict[str, Any]:
    state = {
        "next_step": {"what": "Повторный созвон", "when": "2026-08-20",
                      "who": "broker"},
        "scheduled_task_at": "",
        "work_evidence": {"reason": "task_due_today", "due_task": {
            "deadline": "2026-08-31", "subject": "Перезвонить",
            "days_overdue": 0, "due_today": True,
        }},
    }
    state.update(over)
    return state


# --- дата шага -----------------------------------------------------------


def test_a_task_due_today_gives_the_step_its_date():
    """#14094: дело на сегодня — не будущее, но дата у него настоящая."""
    assert describe_next_step(_state()) == "Повторный созвон (2026-08-31, брокер)"


def test_an_overdue_task_gives_its_date_too():
    state = _state(work_evidence={"reason": "due_task_no_result", "due_task": {
        "deadline": "2026-08-25", "subject": "Связаться",
        "days_overdue": 6, "due_today": False,
    }})
    assert describe_next_step(state) == "Повторный созвон (2026-08-25, брокер)"


def test_a_future_task_still_wins():
    """Прежнее правило не сломалось: живое дело важнее наступившего."""
    state = _state(scheduled_task_at="2026-09-02")
    assert describe_next_step(state) == "Повторный созвон (2026-09-02, брокер)"


def test_without_any_task_the_model_keeps_its_word():
    state = _state(work_evidence={"reason": "no_trace_in_window"})
    assert describe_next_step(state) == "Повторный созвон (2026-08-20, брокер)"


# --- чужое дело ----------------------------------------------------------


def _foreign_task() -> dict[str, Any]:
    return {
        "kind": "activity", "id": 1, "completed": "N",
        "created": (NOW - timedelta(days=20)).isoformat(),
        "deadline": (NOW - timedelta(days=12)).isoformat(),
        "subject": "Связаться с клиентом", "author_id": ROBOT,
    }


def _past_step() -> dict[str, Any]:
    return {
        "work_evidence": {"reason": "no_trace", "window_days": 1},
        "next_step": {"what": "Связаться с клиентом", "when": "2026-08-19",
                      "who": "broker"},
    }


def test_a_foreign_task_is_named_not_denied():
    """#15810: дело в карточке есть, просто не брокера."""
    advice = next_action(
        _past_step(), [_foreign_task()], NOW, task_authors={BROKER},
    )
    assert "дело в карточке заведено не брокером" in advice
    assert "дела нет" not in advice


def test_with_no_task_at_all_the_wording_stays():
    advice = next_action(_past_step(), [], NOW, task_authors={BROKER})
    assert "дела нет" in advice


def test_the_brokers_own_task_is_not_called_foreign():
    own = dict(_foreign_task(), author_id=BROKER)
    advice = next_action(_past_step(), [own], NOW, task_authors={BROKER})
    assert "заведено не брокером" not in advice
