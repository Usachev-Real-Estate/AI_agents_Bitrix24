"""Цифры шапки и пустые разделы не должны обещать больше, чем прогон видел.

Аудит 27.08, один класс дефектов: отсутствие данных подаётся как результат.
Непрочитанная карточка выпадала из тела отчёта, а пустой раздел заявлял «по
всем карточкам». «Звонки есть у N из M» делило число по прочитанным на
число по всем. «Разобрано моделью» считало пустые карточки, которых модель
не видела. Пометка «звонка в таймлайне нет» звучала абсолютно, а считалась
по окну этапа. «Работу видно, а клиента — нет» срабатывало там, где судить
ещё рано.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from broker_work import (  # noqa: E402
    GAP_OUT_OF_WINDOW,
    PROVEN_BY_COMMENT,
)
from client_state_report import (  # noqa: E402
    format_card,
    format_sections,
    format_summary,
    unread_cards,
)

WEBHOOK = "https://example.bitrix24.ru/rest/1/token/"


def _stats(**over) -> dict:
    base = {
        "funnel": "buyers", "funnel_label": "Покупатели",
        "total": 10, "analyzed": 8,
        "cost_rub": 1.0, "cost_rub_per_card": 0.1,
    }
    base.update(over)
    return base


def _result(deal_id: int, **over) -> dict:
    result = {"deal_id": deal_id, "skipped": False, "state": {}}
    result.update(over)
    return result


# --- «звонки есть у N из M» ---------------------------------------------


def test_calls_are_counted_over_the_cards_we_read():
    text = format_summary(_stats(cards_with_call=3, cards_read=4))
    assert "у 3 из 4 прочитанных за всё время (всего 10)" in text


def test_the_counter_names_its_time_frame():
    """Шапка считает по всей истории, карточка — по норме этапа.

    Прогон 31.08: «📞 Звонки есть у 1 из 10» и в теле #10994 «ни звонка, ни
    комментария брокера 106 дн.». Оба утверждения верны — разговор был
    раньше, чем сто шесть дней назад, — но без названного срока читаются
    как спор отчёта с самим собой.
    """
    text = format_summary(_stats(cards_with_call=1, cards_read=10))
    assert "за всё время" in text


def test_all_cards_read_reads_plainly():
    text = format_summary(_stats(cards_with_call=3, cards_read=10))
    assert "📞 Звонки есть у 3 из 10 карточек" in text
    assert "прочитанных" not in text


def test_old_runs_without_the_key_fall_back_to_total():
    text = format_summary(_stats(cards_with_call=3))
    assert "у 3 из 10 карточек" in text


# --- «разобрано моделью» -------------------------------------------------


def test_empty_cards_are_not_counted_as_analyzed_by_the_model():
    text = format_summary(_stats(analyzed=8, empty_cards=3))
    assert "разобрано моделью: 5" in text
    assert "пустых, без модели: 3" in text


def test_without_empty_cards_the_line_stays_short():
    text = format_summary(_stats(analyzed=8))
    assert "Карточек: 10 · разобрано моделью: 8" in text
    assert "без модели" not in text


# --- пометка «звонка нет» ------------------------------------------------


def test_the_no_call_marker_names_its_window():
    card = format_card(
        _result(101, state={
            "temperature": "warm", "verdict": "good", "no_call": True,
            "work_evidence": {
                "proven": True, "reason": PROVEN_BY_COMMENT, "window_days": 7,
            },
        }),
        "Сделка",
        WEBHOOK,
    )
    assert "звонка за 7 дн. нет" in card
    assert "звонка в таймлайне нет" not in card


# --- «работу видно, а клиента — нет» -------------------------------------


def _unrecoverable(reason: str, verdict: str = "good") -> str:
    """Отчёт целиком по карточке, у которой работа подтверждена.

    Раньше такая карточка стояла в ⏳ или ✅ однострочником, и он различал
    два пробела словами: «сделку по карточке не подхватить» (работу видели)
    против «картину клиента не восстановить» (её не искали). С 31.08 обоих
    разделов нет — карточка без вопросов в отчёт не идёт, — и различение
    ушло вместе с ними. Осталось то, что и должно: она учтена.
    """
    return format_sections(
        [_result(102, state={
            "temperature": "unknown", "verdict": verdict,
            "recoverable": False, "next_step": {},
            "work_evidence": {"proven": True, "reason": reason, "window_days": 7},
        })],
        {102: "Сделка"},
        WEBHOOK,
    )


def test_a_card_with_proven_work_leaves_the_report_counted():
    body = _unrecoverable(PROVEN_BY_COMMENT)
    assert "Сделка" not in body
    assert "✅ Без вопросов: 1 в работе — в отчёт не выводятся." in body


def test_too_early_is_counted_separately_from_work_in_progress():
    """«Рано судить» и «в работе» — разные ответы, и учёт их различает.

    proven=True из-за отсрочки этапа — это «не смотрели», а не «видно», и
    сваливать их в одно число значит снова выдать отсутствие за результат.
    """
    body = _unrecoverable(GAP_OUT_OF_WINDOW, verdict="too_early")
    assert "✅ Без вопросов: 1 рано судить — в отчёт не выводятся." in body
    assert "в работе" not in body


# --- непрочитанные карточки ----------------------------------------------


def test_unread_cards_are_selected_by_reason():
    results = [
        _result(1, skipped=True, reason="evidence_incomplete", state=None),
        _result(2, skipped=True, reason="stage_out_of_qc", state=None),
        _result(3, skipped=True, reason="unchanged", state={"temperature": "warm"}),
        _result(4, skipped=True, reason="llm_error", state=None),
    ]
    assert [r["deal_id"] for r in unread_cards(results)] == [1, 4]


def test_unread_cards_are_listed_in_the_body():
    results = [
        _result(1, skipped=True, reason="evidence_incomplete", state=None),
    ]
    text = format_sections(results, {1: "Сделка про Ирину"}, WEBHOOK)
    assert "👁 НЕ ПРОЧИТАНЫ — 1" in text
    assert "#1 Сделка про Ирину — карточку не удалось прочитать целиком" in text


def test_empty_sections_do_not_claim_to_cover_unread_cards():
    results = [
        _result(1, skipped=True, reason="evidence_incomplete", state=None),
    ]
    text = format_sections(results, {}, WEBHOOK)
    assert "Работа подтверждена по всем прочитанным карточкам." in text
    assert "Не прочитано карточек: 1." in text


def test_without_unread_cards_the_caveat_is_absent():
    text = format_sections([], {}, WEBHOOK)
    assert "Не прочитано карточек" not in text
    assert "Работа подтверждена по всем прочитанным карточкам." in text
