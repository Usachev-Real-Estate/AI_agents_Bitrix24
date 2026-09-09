"""Невыполненное обещание не должно проигрывать рублям на карточке.

Место в сводке — это ещё и общая мера: внутри места советы соревнуются по
весу, и сравнивать их можно, только пока вес у всех в одних единицах.

Просроченные обещания стояли на одном месте с событиями по сделкам, где
вес — сумма на карточке. Двадцать девять обещаний дают вес 2929, одна
ушедшая из работы сделка на 2,6 млн — вес 2 600 000. Рубли побеждали штуки
на три порядка, а сделки уходят из работы почти каждый день, — и самый
сильный сигнал витрины не мог прозвучать ни разу.

Заметить это по коду было нельзя: оба правила выглядели правильно каждое
само по себе. Видно было только по живой сводке, где девяносто два
невыполненных обещания по агентству молчали, а первым советом дня стоял
разбор одной проигранной карточки.

Отсюда тест: не «правило работает», а «правило слышно рядом с деньгами».
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import advice  # noqa: E402
import advice_rules  # noqa: E402


def _promises(count: int, *, user_id: int = 7, name: str = "Марат Абзалилов"):
    return [
        {
            "deal_id": 7842 + i, "title": f"ВГ {747 + i}",
            "stage_id": "UC_X", "stage_name": "Подбор",
            "broker": name, "user_id": user_id,
            "promised": "Позвонить, запросить обратную связь",
            "promised_at": "2026-08-14", "terms": "", "ready": "",
            "overdue_days": 27.0 + i,
        }
        for i in range(count)
    ]


def _lost_deal(amount: float = 2_600_000.0):
    return {
        "left_work": {"top": [{
            "deal_id": 9001, "title": "ЖК «Прайм Парк»",
            "from_name": "Первый показ", "to_name": "Сделка проиграна",
            "opportunity": amount, "assignee": "Вадим Берестов",
        }]},
    }


def _chosen(promises, events):
    candidates = advice_rules.collect(promises=promises, events=events)
    return advice.select(candidates, {})


def test_the_promise_is_heard_next_to_the_lost_deal():
    """Оба совета в сводке. Раньше обещания вытеснялись подчистую."""
    selection = _chosen(_promises(29), _lost_deal())

    rules = [item.rule for item in selection.advices]
    assert "promise_overdue" in rules, "обещания снова заглушены рублями"
    assert "deal_left_work" in rules, "событие по сделке тоже должно звучать"


def test_the_promise_comes_before_the_lost_deal():
    """Порядок мест: сделать сегодня важнее, чем разобрать вчерашнее.

    Ушедшую сделку уже потеряли, и совет по ней — разбор задним числом.
    Обещание можно выполнить сегодня.
    """
    selection = _chosen(_promises(29), _lost_deal())

    rules = [item.rule for item in selection.advices]
    assert rules.index("promise_overdue") < rules.index("deal_left_work")


def test_one_broken_promise_still_speaks():
    """Даже одно обещание не должно теряться за сделкой в миллионы."""
    selection = _chosen(_promises(1), _lost_deal(90_000_000.0))

    assert "promise_overdue" in [item.rule for item in selection.advices]


def test_the_loudest_broker_is_the_one_named():
    """На месте один совет, и достаётся оно тому, у кого обещаний больше."""
    rows = _promises(29) + _promises(6, user_id=8, name="Ирина Логутина")
    selection = _chosen(rows, None)

    promise = [a for a in selection.advices if a.rule == "promise_overdue"]
    assert len(promise) == 1
    assert promise[0].who == "Марат Абзалилов"
    assert promise[0].value == 29


def test_the_deal_events_still_compete_by_money():
    """Внутри своего места события по-прежнему меряются суммой.

    Разведение по местам не должно сломать то, ради чего эти два правила
    рядом и стояли: уход с поздней стадии дороже отката с ранней, и решает
    это сумма.
    """
    events = _lost_deal(2_600_000.0)
    events["returned"] = {"top": [{
        "deal_id": 9002, "title": "ЖК «Дешёвый»",
        "from_name": "Повторный показ", "to_name": "Первый показ",
        "opportunity": 100_000.0, "assignee": "Ирина Тарасова",
    }]}

    selection = _chosen(None, events)

    acute = [a for a in selection.advices if a.slot == advice.SLOT_ACUTE]
    assert [a.rule for a in acute] == ["deal_left_work"]
