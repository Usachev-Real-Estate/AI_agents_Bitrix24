"""Засчитываем только следы брокера или его РОПа.

Решение агентства от 28.08, в два приёма: сначала про дела, потом про
комментарии. До него `assess_broker_work` автора не смотрел вовсе. Дело,
заведённое бизнес-процессом, могло нести длинный типовой текст — и
засчитывалось брокеру как его работа, его пауза и его контроль над
карточкой; комментарий бэк-офиса закрывал норму этапа за брокера.

Основной аудит фильтровал авторов комментариев с самого начала
(`_allowed_comment_authors`), QC — нигде. Теперь правило одно.

Остаются всегда: закрытые активности (запись о состоявшемся разговоре — для
факта разговора неважно, кто её отметил) и расшифровки.

Автора не знаем — засчитываем. Незнание не должно превращаться в претензию:
это то же самое отсутствие данных, выданное за результат.
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
    GAP_NO_TRACE,
    GAP_NO_TRACE_IN_WINDOW,
    GAP_PAUSE_NO_TASK,
    GAP_WAITING_NO_TASK,
    PROVEN_BY_CALL,
    PROVEN_BY_COMMENT,
    PROVEN_BY_PAUSE,
    PROVEN_BY_TASK_PLAN,
    REASON_RU,
    assess_broker_work,
    evidence_of_the_broker,
)
from funnel_profiles import BUYER_PROFILE  # noqa: E402

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
STAGE = "C18:UC_DVW1P9"   # Повторный показ, окно 3 дня

BROKER = 11
ROP = 22
ROBOT = 99
OURS = {BROKER, ROP}

PLAN = "Показать на Ленина 5 и Мира 12: клиент отказался от подборки из-за этажа"


def _task(author: int | None, *, ago: float = 1.0, ahead: float = 2.0,
          subject: str = "Позвонить", description: str = "") -> dict[str, Any]:
    event: dict[str, Any] = {
        "kind": "activity", "type_id": 2, "completed": "N",
        "created": (NOW - timedelta(days=ago)).isoformat(),
        "deadline": (NOW + timedelta(days=ahead)).isoformat(),
        "subject": subject, "description": description,
    }
    if author is not None:
        event["author_id"] = author
    return event


def _comment(ago: float, text: str = "созвонились") -> dict[str, Any]:
    return {
        "kind": "comment", "text": text,
        "created": (NOW - timedelta(days=ago)).isoformat(),
    }


def _comment_by(
    author: int | None, ago: float, text: str = "созвонились",
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "kind": "comment", "text": text,
        "created": (NOW - timedelta(days=ago)).isoformat(),
    }
    if author is not None:
        event["author_id"] = author
    return event


def _call(author: int, ago: float = 1.0) -> dict[str, Any]:
    return {
        "kind": "activity", "type_id": 2, "completed": "Y",
        "created": (NOW - timedelta(days=ago)).isoformat(),
        "author_id": author, "subject": "Звонок",
    }


def _assess(events: list[dict], **over: Any) -> dict:
    params: dict[str, Any] = {
        "profile": BUYER_PROFILE, "stage_id": STAGE,
        "hours_on_stage": 24 * 365, "claims_messaged": False,
        "comment_informative": True, "abandoned_days": 30.0,
        "task_authors": OURS, "now": NOW,
    }
    params.update(over)
    return assess_broker_work(events, **params)


# ── Сам отбор ──────────────────────────────────────────────────────────
def test_a_robots_task_is_dropped():
    kept = evidence_of_the_broker([_task(ROBOT)], OURS)
    assert kept == []


def test_the_brokers_and_the_rops_tasks_stay():
    events = [_task(BROKER), _task(ROP)]
    assert evidence_of_the_broker(events, OURS) == events


def test_an_unknown_author_gets_the_benefit_of_the_doubt():
    """Мы не знаем, чьё дело, — и не делаем из этого претензии."""
    events = [_task(None)]
    assert evidence_of_the_broker(events, OURS) == events


def test_an_empty_author_set_switches_the_rule_off():
    """Карты не построились — правило молчит, а не работает наоборот."""
    events = [_task(ROBOT)]
    assert evidence_of_the_broker(events, set()) == events
    assert evidence_of_the_broker(events, None) == events


def test_a_closed_activity_is_kept_whoever_logged_it():
    """Разговор состоялся — для факта разговора автор отметки неважен."""
    events = [_call(ROBOT)]
    assert evidence_of_the_broker(events, OURS) == events


# ── Что это меняет в оценке ────────────────────────────────────────────
def test_a_robots_described_task_is_not_the_brokers_work():
    robot = _task(ROBOT, subject="Показ", description=PLAN)
    assert _assess([robot])["reason"] != PROVEN_BY_TASK_PLAN


def test_the_same_task_from_the_broker_counts():
    mine = _task(BROKER, subject="Показ", description=PLAN)
    assert _assess([mine])["reason"] == PROVEN_BY_TASK_PLAN


def test_a_robots_task_does_not_hold_an_explained_pause():
    """Пауза засчитывается только вместе с делом брокера на контроле."""
    events = [
        _comment(2.0, "клиент в отпуске до 5 сентября"),
        _task(ROBOT, ahead=6.0),
    ]
    result = _assess(
        events, pause_explained=True,
        pause_until=(NOW + timedelta(days=5)).date().isoformat(),
    )
    assert result["reason"] == GAP_PAUSE_NO_TASK


def test_the_brokers_task_does_hold_it():
    events = [
        _comment(2.0, "клиент в отпуске до 5 сентября"),
        _task(BROKER, ahead=6.0),
    ]
    result = _assess(
        events, pause_explained=True,
        pause_until=(NOW + timedelta(days=5)).date().isoformat(),
    )
    assert result["reason"] == PROVEN_BY_PAUSE


def test_a_robots_task_is_not_waiting_either():
    events = [_comment(1.0, "агент обещал набрать"), _task(ROBOT, ahead=4.0)]
    result = _assess(
        events, next_step_who="client",
        next_step_when=(NOW + timedelta(days=4)).date().isoformat(),
    )
    assert result["reason"] == GAP_WAITING_NO_TASK


def test_a_robots_task_does_not_hold_an_abandoned_card():
    """«Карточку держат» — это про брокера, а не про автоматику."""
    events = [_comment(200.0), _task(ROBOT, ago=200.0, ahead=30.0)]
    assert _assess(events)["reason"] == GAP_ABANDONED


def test_the_brokers_task_holds_the_abandoned_card():
    events = [_comment(200.0), _task(BROKER, ago=200.0, ahead=30.0)]
    assert _assess(events)["reason"] != GAP_ABANDONED


def test_a_robots_overdue_task_does_not_nag_the_broker():
    events = [_comment(10.0), _task(ROBOT, ago=10.0, ahead=-5.0)]
    result = _assess(events)
    assert "due_task" not in result
    assert result["reason"] == GAP_NO_TRACE_IN_WINDOW


def test_a_robots_call_still_proves_the_conversation():
    assert _assess([_call(ROBOT)])["reason"] == PROVEN_BY_CALL


# ── Комментарии: то же правило ─────────────────────────────────────────
def test_someone_elses_comment_is_dropped():
    """Бэк-офис написал в карточку — это не работа брокера с клиентом."""
    kept = evidence_of_the_broker([_comment_by(ROBOT, 1.0)], OURS)
    assert kept == []


def test_the_brokers_and_the_rops_comments_stay():
    events = [_comment_by(BROKER, 1.0), _comment_by(ROP, 1.0)]
    assert evidence_of_the_broker(events, OURS) == events


def test_a_comment_without_an_author_gets_the_benefit_of_the_doubt():
    events = [_comment_by(None, 1.0)]
    assert evidence_of_the_broker(events, OURS) == events


def test_someone_elses_comment_does_not_close_the_stage_norm():
    """Раньше комментарий бэк-офиса закрывал норму этапа за брокера."""
    result = _assess([_comment_by(ROBOT, 1.0, "клиент перезвонит")])
    assert result["reason"] != PROVEN_BY_COMMENT


def test_the_brokers_comment_does():
    result = _assess([_comment_by(BROKER, 1.0, "созвонились, показ в четверг")])
    assert result["reason"] == PROVEN_BY_COMMENT


def test_the_wording_says_whose_trace_is_missing():
    """РОП открывает карточку и видит там чужие комментарии.

    «Следов работы нет вовсе» на такой карточке читается как ошибка отчёта.
    Претензия от уточнения не слабеет: она о том, что ответственный по
    сделке ничего не написал.
    """
    assert "брокера или РОПа" in REASON_RU[GAP_NO_TRACE]
    assert "брокера или РОПа" in REASON_RU[GAP_NO_TRACE_IN_WINDOW]


def test_someone_elses_comment_is_not_a_report_on_the_due_task():
    """Отписка по делу — тоже след брокера, а не любого, кто зашёл в карточку."""
    events = [
        _task(BROKER, ago=5.0, ahead=-3.0),
        _comment_by(ROBOT, 0.1, "заявка обработана автоматически"),
    ]
    result = _assess(events)
    assert result["due_task"]["days_overdue"] >= 1


def test_the_brokers_own_report_closes_it():
    events = [
        _task(BROKER, ago=5.0, ahead=-3.0),
        _comment_by(BROKER, 0.1, "дозвонился, договорились на пятницу"),
    ]
    assert "due_task" not in _assess(events)


# ── Обвинение ищем в словах брокера, оправдание — во всей карточке ─────
def _probe(events: list[dict], claims: dict, authors: set[int] | None) -> dict:
    """Прогнать карточку через apply_derived_verdict и вернуть work_claims."""
    import client_state as cs
    from funnel_profiles import BUYER_PROFILE

    state = {
        "work_claims": {}, "next_step": {}, "signals": {},
        "recoverable": True, "broker_work": claims,
    }
    out = cs.apply_derived_verdict(
        dict(state),
        {"ID": 1, "STAGE_ID": STAGE, "ASSIGNED_BY_ID": BROKER},
        BUYER_PROFILE, {}, events, None, authors,
    )
    return out["work_claims"]


CLAIMS = {
    "claims_messaged": True,
    "claims_messaged_quote": "написал клиенту в вотсап",
    "claims_no_answer": True,
    "claims_no_answer_quote": "написал клиенту в вотсап",
    "pause_explained": True,
    "pause_reason_quote": "написал клиенту в вотсап",
}


def test_an_accusation_needs_the_brokers_own_words():
    """«Брокер пишет, что написал клиенту» — про брокера, значит и цитата его.

    Модель читает ВСЕ комментарии карточки, включая чужие. Без этого брокеру
    предъявлялась бы фраза, которую написал бэк-офис, — и он справедливо
    ответил бы «я такого не писал».
    """
    events = [
        _comment_by(ROBOT, 1.0, "написал клиенту в вотсап"),
        _comment_by(BROKER, 1.0, "жду ответа по подборке"),
    ]
    claims = _probe(events, CLAIMS, OURS)
    assert claims["claims_messaged"] is False
    assert claims["claims_no_answer"] is False


def test_the_brokers_own_words_still_count():
    events = [_comment_by(BROKER, 1.0, "написал клиенту в вотсап")]
    claims = _probe(events, CLAIMS, OURS)
    assert claims["claims_messaged"] is True


def test_an_excuse_may_rest_on_anyone_s_words():
    """Пауза брокера не обвиняет, а выгораживает.

    «Клиент в отпуске до ноября» верно независимо от того, чья рука это
    записала. Сузить корпус тут значило бы отнять у брокера оправдание за
    чужую аккуратность.
    """
    events = [_comment_by(ROBOT, 1.0, "написал клиенту в вотсап")]
    assert _probe(events, CLAIMS, OURS)["pause_explained"] is True


def test_with_the_rule_off_nothing_changes():
    events = [
        _comment_by(ROBOT, 1.0, "написал клиенту в вотсап"),
        _comment_by(BROKER, 1.0, "жду ответа"),
    ]
    assert _probe(events, CLAIMS, set())["claims_messaged"] is True
