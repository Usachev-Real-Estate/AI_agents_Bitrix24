"""Модель перестала отвечать — отчёт по такому прогону не отправляется.

Прогон 01.09, боевой. RouterAI вернул 402 (баланс кончился) на первой
минуте, и следующие ~265 карточек получили `llm_error` одна за другой.
Отчёт не ушёл четверым РОПам только потому, что за логом смотрел человек.
В 12:00 по крону смотреть будет некому.

Что получил бы РОП: отчёт, где почти всё в разделе «👁 НЕ ПРОЧИТАНЫ», а
претензий — горстка, та, что успела разобраться до отказа. Короткий список
претензий читается как «в отделе порядок». Это ровно тот класс ошибок,
который проект чинит с самого начала: отсутствие данных, выданное за
результат, — только теперь сразу четверым людям.

Одна сбойная карточка прогон не роняет: сеть моргает, ответ не приходит,
это невезение. Пять отказов подряд — уже обстоятельство: кончился баланс,
отозван ключ, лёг провайдер. Разница не в количестве, а в том, что во
втором случае продолжать бессмысленно.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest  # noqa: E402

import client_state as cs  # noqa: E402
from client_state import LLM_FAILURE_STREAK, run_client_state  # noqa: E402
from funnel_profiles import SELLER_PROFILE  # noqa: E402


def _deals(count: int) -> list[dict[str, Any]]:
    return [
        {"ID": str(1000 + i), "TITLE": f"Сделка {i}", "STAGE_ID": "NEW"}
        for i in range(count)
    ]


@pytest.fixture()
def analyzed(monkeypatch):
    """Подменяем разбор карточки: важно только, чем он кончился."""
    calls: list[int] = []

    def _install(outcome) -> None:
        def _fake(deal, **_kw):
            deal_id = int(deal["ID"])
            calls.append(deal_id)
            return outcome(len(calls), deal_id)

        monkeypatch.setattr(cs, "analyze_deal", _fake)

    return _install, calls


def _failure(deal_id: int) -> dict[str, Any]:
    return {
        "deal_id": deal_id, "skipped": True, "reason": "llm_error",
        "state": None, "content_hash": "",
    }


def _success(deal_id: int) -> dict[str, Any]:
    return {
        "deal_id": deal_id, "skipped": False, "reason": "", "content_hash": "h",
        "state": {"temperature": "warm", "verdict": "poor"},
    }


def test_a_streak_of_failures_stops_the_run(analyzed):
    """Пять подряд — дальше не идём: следующие 600 обречены так же."""
    install, calls = analyzed
    install(lambda n, deal_id: _failure(deal_id))
    stats = run_client_state(SELLER_PROFILE, _deals(200))
    assert stats["aborted"] == "llm_unavailable"
    assert len(calls) == LLM_FAILURE_STREAK


def test_a_single_flaky_card_does_not_stop_anything(analyzed):
    """Одна карточка не разобралась — это невезение, а не обстоятельство."""
    install, calls = analyzed
    install(
        lambda n, deal_id: _failure(deal_id) if n == 3 else _success(deal_id),
    )
    stats = run_client_state(SELLER_PROFILE, _deals(20))
    assert not stats["aborted"]
    assert len(calls) == 20


def test_the_streak_resets_on_a_good_card(analyzed):
    """Четыре отказа, успех, четыре отказа — провайдер жив, просто шумит."""
    install, calls = analyzed
    bad = set(range(1, 5)) | set(range(6, 10))
    install(
        lambda n, deal_id: _failure(deal_id) if n in bad else _success(deal_id),
    )
    stats = run_client_state(SELLER_PROFILE, _deals(12))
    assert not stats["aborted"]
    assert len(calls) == 12


def test_a_finished_run_says_so(analyzed):
    install, _calls = analyzed
    install(lambda n, deal_id: _success(deal_id))
    assert run_client_state(SELLER_PROFILE, _deals(5))["aborted"] == ""


def test_parse_failures_are_not_a_streak(analyzed):
    """«Ответ не разобрался» — про карточку, а не про доступность модели."""
    install, calls = analyzed
    install(lambda n, deal_id: {
        "deal_id": deal_id, "skipped": True, "reason": "parse_error",
        "state": None, "content_hash": "",
    })
    stats = run_client_state(SELLER_PROFILE, _deals(20))
    assert not stats["aborted"]
    assert len(calls) == 20
