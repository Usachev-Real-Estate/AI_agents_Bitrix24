"""Живое дело брокера снимает претензию к темпу — решение агентства 01.09.

Прогон 01.09, #13636: второй показ прошёл, брокер поставил дело «получить
обратную связь» на 3 сентября — и карточка ушла в «🔧 Работа не
подтверждена: за норму этапа брокер или РОП ничего не сделал (норма 3 дн.,
последний след брокера 5 дн. назад)». Формулировка агентства: «если брокер
запланировал дело для получения обратной связи, значит клиент в работе и
всё хорошо».

Это отмена решения от 28.08 (#16032), где незакрытое дело претензию не
снимало. Отмена не сплошная, и границы здесь важнее самого правила: срок
дела обязан укладываться в норму этапа, а пустое «Позвонить» не снимает
ничего. Без первой границы правило вырождается в «поставил дело подальше —
и чист», без второй возвращается «Позвонить» вместо работы.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from broker_work import (  # noqa: E402
    GAP_NO_TRACE_IN_WINDOW,
    GAP_ONLY_PLANS,
    PROVEN,
    PROVEN_BY_OPEN_TASK,
    PROVEN_BY_TASK_PLAN,
    REASON_RU,
    assess_broker_work,
    work_window_days,
)
from funnel_profiles import SELLER_PROFILE  # noqa: E402

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
# «Переговоры» у продавцов: норма этапа — трое суток.
STAGE = "UC_KEOOG8"
FEEDBACK = "Получить обратную связь от клиента по второму показу"


def _task(days_ahead: float, text: str = FEEDBACK, done: str = "N",
          created_days_ago: float = 5) -> dict:
    return {
        "kind": "activity",
        "author_is_broker": True,
        "completed": done,
        "created": (NOW - timedelta(days=created_days_ago)).isoformat(),
        "deadline": (NOW + timedelta(days=days_ahead)).isoformat(),
        "subject": text,
        "description": text,
    }


def _comment(days_ago: float) -> dict:
    return {
        "kind": "comment",
        "author_is_broker": True,
        "text": "Показ прошёл, клиент думает, ждём обратную связь",
        "created": (NOW - timedelta(days=days_ago)).isoformat(),
    }


def _assess(events: list[dict]) -> dict:
    return assess_broker_work(
        events=events,
        profile=SELLER_PROFILE,
        stage_id=STAGE,
        hours_on_stage=24 * 10,
        claims_messaged=False,
        comment_informative=True,
        now=NOW,
    )


def test_the_stage_norm_is_three_days():
    """Норма этапа — довод правила, и она должна стоять там, где её ждут."""
    assert work_window_days(SELLER_PROFILE, STAGE) == 3


def test_13636_a_task_on_control_clears_the_pace_reproach():
    work = _assess([_comment(5), _task(2)])
    assert work["reason"] == PROVEN_BY_OPEN_TASK
    assert work["proven"] is True
    assert PROVEN_BY_OPEN_TASK in PROVEN


def test_the_same_card_without_a_task_is_still_a_reproach():
    """Без дела претензия остаётся: тишина ничем не объяснена."""
    work = _assess([_comment(5)])
    assert work["reason"] == GAP_NO_TRACE_IN_WINDOW
    assert work["proven"] is False


def test_a_task_beyond_the_stage_norm_clears_nothing():
    """Дело на три месяца вперёд — не ведение клиента, а откладывание.

    Ради этого случая проверку и пришлось писать отдельно: исключить
    GAP_PLAN_TOO_FAR из списка снимаемых претензий мало. Тот код рождается
    только в ветке «в окне есть события, но нет комментариев», а карточка
    со старым комментарием и делом на три месяца вперёд приходит сюда как
    GAP_NO_TRACE_IN_WINDOW — и через него правило обходилось.
    """
    work = _assess([_comment(5), _task(90)])
    assert work["reason"] == GAP_NO_TRACE_IN_WINDOW
    assert work["proven"] is False


def test_the_horizon_is_the_stage_norm_itself():
    """Граница проходит по норме этапа, а не рядом с ней."""
    assert _assess([_comment(5), _task(3)])["reason"] == PROVEN_BY_OPEN_TASK
    assert _assess([_comment(5), _task(4)])["reason"] == GAP_NO_TRACE_IN_WINDOW


def test_a_closed_task_holds_nothing():
    """Закрытое дело — сделанное, а не стоящее на контроле."""
    work = _assess([_comment(5), _task(2, done="Y")])
    assert work["reason"] == GAP_NO_TRACE_IN_WINDOW


def test_an_empty_task_is_still_only_a_plan():
    """«Позвонить» на завтра — по-прежнему упрёк (решение от 28.08).

    Новое решение говорит о темпе: клиента ведут. Претензию к тому, ЧТО
    записано, оно не трогало, и пример агентства под неё не подпадал.
    """
    work = _assess([_comment(5), _task(2, text="Позвонить", created_days_ago=1)])
    assert work["reason"] == GAP_ONLY_PLANS
    assert work["proven"] is False


def test_real_work_is_named_as_real_work():
    """Дело — последний довод, а не первый.

    Содержательное дело внутри окна засчитывается своим кодом: отчёт должен
    называть работу работой, а не прятать её за «дело на контроле».
    """
    work = _assess([_comment(5), _task(2, created_days_ago=1)])
    assert work["reason"] == PROVEN_BY_TASK_PLAN


def test_the_reason_has_words_for_the_report():
    assert REASON_RU[PROVEN_BY_OPEN_TASK]
