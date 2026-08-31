"""Дело на контроле снимает тревогу — даже если оно поставлено давно.

Прогон 31.08, первый с новыми правилами: девять продавцов из десяти ушли в
«🚨 ТЕРЯЕМ КЛИЕНТА», и у трёх из них дело стояло на 1 сентября. #16304,
#16310, #16322 — норма этапа 1 день, последний след брокера 5 дней назад,
и одновременно живое дело в Битриксе.

Определение агентства — это конъюнкция: «не пишутся комментарии И не
планируются дела И нет исходящих звонков». Дело на контроле рвёт её вторым
звеном. Претензия к таким карточкам верна — за норму этапа в них ничего
нет, — но это недоработка, а не потеря, и раздел обязан их различать.
Иначе «теряем клиента» снова становится общим списком, из которого РОП не
выбирает, — ровно то, ради чего раздел и переписывали.

Окно этапа отвечает на вопрос «свежая ли работа», раздел — на вопрос
«ведём ли мы клиента вообще». Это разные вопросы, и путать их нельзя.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from broker_work import (  # noqa: E402
    GAP_ABANDONED,
    GAP_NO_TRACE,
    GAP_NO_TRACE_IN_WINDOW,
)
from client_state_report import (  # noqa: E402
    describe_next_step,
    format_sections,
    split_sections,
)

WEBHOOK = "https://example.bitrix24.ru/rest/1/token/"


def _card(
    reason: str,
    *,
    scheduled: str = "",
    verdict: str = "poor",
    **work: Any,
) -> dict[str, Any]:
    evidence = {
        "proven": False, "reason": reason, "window_days": 1, "days_quiet": 5.0,
    }
    evidence.update(work)
    return {"deal_id": 16304, "skipped": False, "state": {
        "temperature": "unknown", "verdict": verdict, "recoverable": False,
        "scheduled_task_at": scheduled,
        "next_step": {"what": "Связаться с клиентом", "when": scheduled or "unknown",
                      "who": "broker"},
        "work_evidence": evidence,
    }}


def _sections(card: dict[str, Any]) -> str:
    losing, abandoned, neglected, reminders, waiting, fine = split_sections([card])
    return "+".join(
        name for name, rows in (
            ("теряем", losing), ("брошены", abandoned),
            ("недоработка", neglected), ("напомнить", reminders),
            ("рано", waiting), ("норма", fine),
        ) if rows
    )


def test_a_standing_task_turns_a_loss_into_a_shortfall():
    """Следов нет вовсе, но дело стоит на 1 сентября — это недоработка."""
    assert _sections(_card(GAP_NO_TRACE)) == "теряем+недоработка"
    assert _sections(
        _card(GAP_NO_TRACE, scheduled="2026-09-01"),
    ) == "недоработка"


def test_lagging_behind_the_norm_needs_no_task_to_leave_the_alarm():
    """#16734: с 31.08 отставание по темпу в тревогу не ведёт вовсе.

    Раньше эту карточку из «теряем» вытаскивало только стоящее дело —
    #16304 и был тот случай. Потом агентство прочитало своё определение
    строже: след есть, он просто старше нормы, и потерей это не считается
    ни с делом, ни без.
    """
    assert _sections(_card(GAP_NO_TRACE_IN_WINDOW)) == "недоработка"
    assert _sections(
        _card(GAP_NO_TRACE_IN_WINDOW, scheduled="2026-09-01"),
    ) == "недоработка"


def test_an_abandoned_card_has_no_task_by_construction():
    """Заброшенность и дело на контроле не сходятся: assess их разводит.

    Проверка на всякий случай — если однажды сойдутся, потерей останется
    заброшенность: сто дней тишины не отменяются делом, поставленным
    когда-то на будущее.
    """
    card = _card(GAP_ABANDONED, scheduled="2026-09-01", abandoned_days=107.0)
    assert "теряем" in _sections(card)


def test_the_claim_itself_does_not_soften():
    """Раздел сменился, претензия осталась: за норму этапа ничего нет."""
    body = format_sections(
        [_card(GAP_NO_TRACE_IN_WINDOW, scheduled="2026-09-01")],
        {16304: "диспозл excel Lucky"}, WEBHOOK,
    )
    assert "за норму этапа брокер или РОП ничего не сделал" in body
    assert "ТЕРЯЕМ КЛИЕНТА — 0" in body
    assert "НЕДОРАБОТКА БРОКЕРА — 1" in body


# ── Одна дата на карточке, а не две ───────────────────────────────────
def test_the_crm_date_wins_over_the_model_s_retelling():
    """#16322: «Шаг ... (2026-08-28)» и совет «Дело стоит на 2026-09-01».

    Модель назвала одну дату, Битрикс держит другую, и читателю обе
    предъявлены как одна. Дело в CRM — доказательство, пересказ — слова.
    """
    state = {
        "scheduled_task_at": "2026-09-01",
        "next_step": {"what": "Связаться с клиентом", "when": "2026-08-28",
                      "who": "broker"},
    }
    step = describe_next_step(state)
    assert "2026-09-01" in step
    assert "2026-08-28" not in step
    assert "Связаться с клиентом" in step


def test_the_model_keeps_the_words_and_the_owner():
    """Подменяем только дату: ЧТО и КТО остаются словами брокера."""
    state = {
        "scheduled_task_at": "2026-09-01",
        "next_step": {"what": "Показать Ленина 5", "when": "2026-08-28",
                      "who": "client"},
    }
    assert describe_next_step(state) == "Показать Ленина 5 (2026-09-01, клиент)"


def test_a_matching_date_is_left_alone():
    state = {
        "scheduled_task_at": "2026-09-01",
        "next_step": {"what": "Связаться", "when": "2026-09-01", "who": "broker"},
    }
    assert describe_next_step(state) == "Связаться (2026-09-01, брокер)"


def test_a_date_named_in_words_around_the_task_is_left_alone():
    """«в начале сентября (2026-09-01)» — та же дата, спорить не с чем."""
    state = {
        "scheduled_task_at": "2026-09-01",
        "next_step": {"what": "Связаться", "when": "к 2026-09-01, в начале недели",
                      "who": "broker"},
    }
    assert "в начале недели" in describe_next_step(state)


def test_without_a_task_nothing_is_substituted():
    state = {
        "scheduled_task_at": "",
        "next_step": {"what": "Связаться", "when": "2026-08-28", "who": "broker"},
    }
    assert describe_next_step(state) == "Связаться (2026-08-28, брокер)"


def test_an_unnamed_step_still_falls_back_to_the_task():
    state = {"scheduled_task_at": "2026-09-01", "next_step": {}}
    assert describe_next_step(state) == (
        "в карточке не описан, но в Битриксе стоит дело на 2026-09-01"
    )
