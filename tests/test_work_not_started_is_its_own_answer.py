"""Карточка, к которой брокер не притрагивался, — не «теряем клиента».

Прогон 31.08 12:30: все четыре продавца в 🚨 оказались строками из реестра
(«диспозл excel Набоков», «…Пожарский 5А», «…Дом на Трубецкой») и лидом
колл-центра. По #16472 в карточке прямо написано: «собраны кадастровые
номера и площади… коммуникация с клиентом не зафиксирована». Вести там ещё
некого — брокер не начинал.

Определению агентства это соответствует буквально (ни комментариев, ни дел,
ни звонков), но заголовок читают раньше определения, и «ТЕРЯЕМ КЛИЕНТА» про
импортированную строку Excel несёт не то сообщение. Решение агентства от
31.08: писать, что работу не начинали, — и в самой карточке, и отдельным
списком.

Разговор с брокером тут другой: не «верните клиента», а «начните».
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
from client_state_report import format_card, format_sections  # noqa: E402

WEBHOOK = "https://example.bitrix24.ru/rest/1/token/"
TITLES = {
    16570: "диспозл excel Набоков",
    16550: "диспозл excel Пожарский 5А",
    8870: "Клиент СС Сердце столицы",
    13370: "Жанна агент",
}


def _card(deal_id: int, reason: str, quiet: float | None = None,
          **work: Any) -> dict[str, Any]:
    evidence = {
        "proven": False, "reason": reason, "window_days": 1,
        "days_quiet": quiet,
    }
    evidence.update(work)
    return {"deal_id": deal_id, "skipped": False, "state": {
        "temperature": "unknown", "verdict": "poor", "recoverable": False,
        "next_step": {}, "work_evidence": evidence,
    }}


def test_the_card_says_the_work_never_started():
    """Шапка своя: спора о доказательствах нет, работы не было."""
    card = format_card(_card(16570, GAP_NO_TRACE), TITLES[16570], WEBHOOK)
    assert "🆕 Работу по карточке не начинали" in card
    assert "Работа не подтверждена" not in card


def test_it_still_says_whose_traces_are_missing():
    """#17128: колл-центр в карточке писал, брокер — нет.

    Без уточнения «от брокера или РОПа» строка спорит с комментарием,
    который РОП видит своими глазами.
    """
    card = format_card(_card(16550, GAP_NO_TRACE), TITLES[16550], WEBHOOK)
    assert "от брокера или РОПа" in card


def test_they_get_their_own_section_with_the_full_write_up():
    """С 31.08 разбор печатается здесь: в тревоге этих карточек больше нет.

    Раньше 🆕 был свёрнутой строкой под тревогой, и прогон 14:01 показал
    предел: у продавцов 🚨 и 🆕 совпали шестью карточками из шести —
    тревога печатала разбор, а список ниже повторял её состав слово в
    слово. «Не дублировать, оставить что работу не начинали».
    """
    body = format_sections(
        [_card(16570, GAP_NO_TRACE), _card(16550, GAP_NO_TRACE)],
        TITLES,
        WEBHOOK,
    )
    assert "🆕 РАБОТУ НЕ НАЧИНАЛИ — 2" in body
    assert "разбор выше" not in body
    assert body.count("Работу по карточке не начинали") == 2
    # И из тревоги они ушли целиком.
    assert "🚨 ТЕРЯЕМ КЛИЕНТА — 0" in body


def test_the_empty_alarm_does_not_claim_more_than_it_saw():
    """«Ни одной с признаками потери» над шестью пустыми карточками — ложь."""
    body = format_sections(
        [_card(16570, GAP_NO_TRACE), _card(16550, GAP_NO_TRACE)],
        TITLES,
        WEBHOOK,
    )
    assert (
        "Ни одной карточки с признаками потери; "
        "по 2 карточкам работу не начинали — ниже."
    ) in body


def test_the_alarm_counts_only_what_it_still_holds():
    """Не начатые из тревоги вышли — и из её цифры тоже."""
    body = format_sections(
        [
            _card(8870, GAP_ABANDONED, 31.0, abandoned_days=31.0),
            _card(16570, GAP_NO_TRACE),
            _card(16550, GAP_NO_TRACE),
        ],
        TITLES,
        WEBHOOK,
    )
    assert "🚨 ТЕРЯЕМ КЛИЕНТА — 1" in body
    assert "Из них 1 брошенная — отдельным списком ниже." in body
    assert "без начатой работы" not in body
    assert "🆕 РАБОТУ НЕ НАЧИНАЛИ — 2" in body


def test_one_overlap_reads_in_the_singular():
    body = format_sections(
        [_card(8870, GAP_ABANDONED, 31.0, abandoned_days=31.0)],
        TITLES,
        WEBHOOK,
    )
    assert "Из них 1 брошенная — отдельным списком ниже." in body


def test_lagging_behind_the_norm_is_not_in_the_list():
    """След есть — значит начинали. Раздел про другое."""
    body = format_sections(
        [_card(13370, GAP_NO_TRACE_IN_WINDOW, 17.0)],
        TITLES,
        WEBHOOK,
    )
    assert "РАБОТУ НЕ НАЧИНАЛИ" not in body
