"""Температура говорит о клиенте, тревога — о работе брокера.

#15594 (прогон 28.08 10:41): карточка суток от роду, брокер написал
собственнику и ждёт ответ, дело стоит на сегодня — и она в разделе
«🚨 ТЕРЯЕМ КЛИЕНТА» с вердиктом «рано судить». Причина: модель поставила
«холодный — собственник не выходит на связь», а холод заводил в тревожный
раздел безусловно.

Сначала исключение сделали для собственников: у покупателя «остыл» —
событие (клиент был в разговоре и вышел из него), а у продавца из холодной
базы «не выходит на связь» — обычное начало работы. Потом второе — для
карточек внутри отсрочки (#17080). Потом третье. Три исключения за один
день к правилу, которое всё это время отвечало не на тот вопрос.

Решение агентства от 28.08 сняло вопрос целиком: «теряем клиента — это
когда с ним не ведётся работа от брокера». Ярлык температуры остался в
отчёте и никуда не заводит — ни у покупателей, ни у продавцов.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from client_state_report import split_sections  # noqa: E402


def _card(
    deal_id: int,
    *,
    temperature: str = "cold",
    verdict: str = "too_early",
    work: dict | None = None,
) -> dict:
    return {
        "deal_id": deal_id,
        "skipped": False,
        "state": {
            "temperature": temperature,
            "temperature_reason": "собственник не выходит на связь",
            "verdict": verdict,
            "work_evidence": work or {
                "proven": True, "reason": "window_not_started", "window_days": 1,
            },
        },
    }


def test_a_cold_owner_does_not_raise_the_alarm():
    """#15594: холодный собственник, по которому брокер работает."""
    losing, _ab, _n, _rem, waiting, _fine = split_sections([_card(15594)])
    assert losing == []
    assert [r["deal_id"] for r in waiting] == [15594]


def test_a_cold_buyer_does_not_either():
    """И покупатель тоже: воронка на тревогу больше не влияет.

    Исключение для собственников было верным по сути и неверным по месту —
    оно чинило одну воронку в правиле, которое ошибалось в обеих.
    """
    losing, _ab, _n, _rem, _w, fine = split_sections(
        [_card(13512, verdict="poor", work={
            "proven": True, "reason": "call", "window_days": 1,
        })],
    )
    assert losing == []
    assert [r["deal_id"] for r in fine] == [13512]


def test_a_cold_buyer_inside_the_grace_waits():
    """#17080: карточке 22 часа при отсрочке 72 — судить рано."""
    losing, _ab, _n, _rem, waiting, _fine = split_sections(
        [_card(17080, verdict="too_early")],
    )
    assert losing == []
    assert [r["deal_id"] for r in waiting] == [17080]


def test_a_hot_client_is_not_spared_either():
    """Симметрия: горячий ярлык сам по себе тоже ничего не решает."""
    losing, _ab, _n, _rem, _w, fine = split_sections(
        [_card(1, temperature="hot", verdict="poor", work={
            "proven": True, "reason": "call", "window_days": 1,
        })],
    )
    assert losing == []
    assert [r["deal_id"] for r in fine] == [1]


def test_a_cold_owner_nobody_works_is_a_loss():
    """Решает не ярлык, а то, что в карточке нет ничего вовсе.

    Разрыв здесь именно no_trace, а не no_trace_in_window: с 31.08
    отставание за норму этапа потерей не считается — след есть, он просто
    старше нормы. Ярлык «холодный» при этом по-прежнему ничего не решает,
    и проверяем мы это.
    """
    card = _card(15594, verdict="poor", work={
        "proven": False, "reason": "no_trace", "window_days": 1,
    })
    losing, _ab, neglected, _rem, _w, _fine = split_sections([card])
    assert [r["deal_id"] for r in losing] == [15594]
    # В недоработку такая карточка с 31.08 не идёт: у неё свой раздел
    # «работу не начинали».
    assert neglected == []


def test_a_cold_owner_lagging_the_norm_is_only_a_shortfall():
    """Тот же холодный собственник, но след за норму этапа был."""
    card = _card(15594, verdict="poor", work={
        "proven": False, "reason": "no_trace_in_window", "window_days": 1,
    })
    losing, _ab, neglected, _rem, _w, _fine = split_sections([card])
    assert losing == []
    assert [r["deal_id"] for r in neglected] == [15594]


def test_a_cold_owner_with_a_weak_record_is_only_a_shortfall():
    """Комментарий написан — работа ведётся, пусть и записана плохо."""
    card = _card(15594, verdict="poor", work={
        "proven": False, "reason": "comment_says_nothing", "window_days": 1,
    })
    losing, _ab, neglected, _rem, _w, _fine = split_sections([card])
    assert losing == []
    assert [r["deal_id"] for r in neglected] == [15594]
