"""Раздел, целиком напечатанный выше, сворачивается в строку.

Прогон 31.08 11:57: у покупателей 🚨 напечатал пять карточек, а следом
🕸 БРОШЕНЫ — 2 и 🔧 НЕДОРАБОТКА — 3 вышли столбиками строк «— см. выше».
Пять карточек под тремя заголовками, два из которых не несут ни одного
довода: заголовок с числом и отсылка назад.

Решение агентства от 31.08: свернуть такой раздел в одну строку. Счёт и
состав остаются проверяемыми — РОП видит, какие именно карточки сюда
входят, — а разбор читается один раз. Если в разделе есть карточка, ещё
не напечатанная, он печатается как раньше: отсылка там стоит рядом с
настоящим разбором, а не вместо него.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from broker_work import GAP_ABANDONED, GAP_ONLY_PLANS  # noqa: E402
from client_state_report import format_sections  # noqa: E402

WEBHOOK = "https://example.bitrix24.ru/rest/1/token/"


def _card(deal_id: int, reason: str, **work: Any) -> dict[str, Any]:
    evidence = {
        "proven": False, "reason": reason, "window_days": 2, "days_quiet": 40.0,
    }
    evidence.update(work)
    return {"deal_id": deal_id, "skipped": False, "state": {
        "temperature": "warm", "verdict": "poor", "recoverable": True,
        "next_step": {}, "work_evidence": evidence,
    }}


TITLES = {10994: "Агент НДВ", 12956: "Артем агент", 15778: "Звонок"}


def test_a_fully_repeated_section_becomes_one_line():
    """Обе брошенные уже напечатаны в 🚨 — раздел сворачивается."""
    body = format_sections(
        [
            _card(10994, GAP_ABANDONED, abandoned_days=106.0),
            _card(12956, GAP_ABANDONED, abandoned_days=67.0),
        ],
        TITLES,
        WEBHOOK,
    )
    assert "🕸 БРОШЕНЫ — 2[/B]: #10994, #12956 (разбор выше)" in body
    assert "см. выше\n" not in body
    # Разбор при этом никуда не делся — он выше, в тревоге.
    assert body.count("Карточка брошена") == 2


def test_the_count_and_the_membership_survive_the_collapse():
    body = format_sections(
        [_card(10994, GAP_ABANDONED, abandoned_days=106.0)],
        TITLES,
        WEBHOOK,
    )
    assert "🕸 БРОШЕНЫ — 1" in body
    assert "#10994" in body.split("🕸 БРОШЕНЫ")[1]


def test_a_section_with_a_new_card_still_prints_it_in_full():
    """Дело поставлено, но пустое: потерей это не считается (#15778 в 🚨 нет).

    Значит полный разбор такой карточки достаётся 🔧 — и раздел печатается
    целиком, а не сворачивается.
    """
    body = format_sections(
        [
            _card(10994, GAP_ABANDONED, abandoned_days=106.0),
            _card(15778, GAP_ONLY_PLANS),
        ],
        TITLES,
        WEBHOOK,
    )
    neglected = body.split("🔧 НЕДОРАБОТКА")[1]
    assert "разбор выше" not in neglected
    assert "Работа не подтверждена" in neglected
