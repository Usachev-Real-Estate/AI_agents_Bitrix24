"""Отсутствие вердикта — не вердикт «всё хорошо».

Прогон 28.08 12:08: двадцать свежих лидов, все до единого «рано судить» —
работу по ним никто не разбирал, потому что разбирать ещё нечего. Обе
воронки при этом отрапортовали:

    🔧 НЕДОРАБОТКА БРОКЕРА — 0
    Работа подтверждена по всем прочитанным карточкам.

    ⚠️ Неинформативных карточек: 8

Первое — подтверждение, которого никто не давал. Второе — претензия к
брокерам за карточки, работать по которым ещё не начинали: лид, заведённый
два часа назад, пуст не по их вине, и мы это уже признали вердиктом «рано
судить».
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from client_state_report import format_sections, format_summary  # noqa: E402

WEBHOOK = "https://example.bitrix24.ru/rest/1/token/"


def _too_early(deal_id: int) -> dict:
    return {
        "deal_id": deal_id, "skipped": False,
        "state": {
            "temperature": "warm", "verdict": "too_early",
            "next_step": {"what": "Связаться", "when": "2026-08-31",
                          "who": "broker"},
            "work_evidence": {
                "proven": True, "reason": "window_not_started", "window_days": 3,
            },
        },
    }


def _worked(deal_id: int) -> dict:
    card = _too_early(deal_id)
    card["state"]["verdict"] = "good"
    card["state"]["work_evidence"] = {
        "proven": True, "reason": "call", "window_days": 3,
    }
    return card


# ── пустой раздел недоработок ──────────────────────────────────────────
def test_a_sample_nobody_judged_is_not_confirmed_work():
    text = format_sections([_too_early(i) for i in (17048, 17058)], {}, WEBHOOK)
    assert "Работа подтверждена" not in text
    assert "Недоработок нет; по 2 карточкам судить ещё рано." in text


def test_one_young_card_reads_in_singular():
    text = format_sections([_too_early(17048)], {}, WEBHOOK)
    assert "по 1 карточке судить ещё рано" in text


def test_when_the_work_really_was_judged_the_claim_stands():
    text = format_sections([_worked(16204)], {}, WEBHOOK)
    assert "Работа подтверждена по всем прочитанным карточкам." in text
    assert "судить ещё рано" not in text


def test_the_unread_caveat_survives_alongside():
    results = [
        _too_early(17048),
        {"deal_id": 1, "skipped": True, "reason": "evidence_incomplete",
         "state": None},
    ]
    text = format_sections(results, {}, WEBHOOK)
    assert "судить ещё рано" in text
    assert "Не прочитано карточек: 1." in text


# ── «неинформативных» в шапке ──────────────────────────────────────────
def _summary(**over) -> str:
    stats = {
        "funnel_label": "Продавцы", "total": 10, "analyzed": 10,
        "temperature": {"hot": 0, "warm": 2, "cold": 0, "unknown": 8},
        "verdicts": {"good": 0, "tolerable": 0, "poor": 0, "too_early": 10,
                     "out_of_qc": 0},
        "cost_rub": 3.43, "cost_rub_per_card": 0.343,
    }
    stats.update(over)
    return format_summary(stats)


def test_fresh_leads_are_not_a_complaint_about_brokers():
    text = _summary(unrecoverable=8, unrecoverable_too_early=8)
    assert "Неинформативных карточек: 8 (все моложе отсрочки — судить рано)" in text


def test_a_mixed_sample_names_how_many_are_young():
    text = _summary(unrecoverable=8, unrecoverable_too_early=3)
    assert (
        "Неинформативных карточек: 8 (3 моложе отсрочки — по 3 карточкам "
        "судить ещё рано)"
    ) in text


def test_a_single_young_card_is_not_called_them():
    """«1 моложе отсрочки — по ним судить рано» — число не сходится."""
    text = _summary(unrecoverable=8, unrecoverable_too_early=1)
    assert "по 1 карточке судить ещё рано" in text
    assert "по ним" not in text


def test_a_judged_sample_reads_as_before():
    text = _summary(unrecoverable=8, unrecoverable_too_early=0)
    assert "Неинформативных карточек: 8" in text
    assert "отсрочки" not in text


def test_the_empty_card_note_survives():
    text = _summary(unrecoverable=8, unrecoverable_too_early=8, empty_cards=5)
    assert "все моложе отсрочки — судить рано, полностью пустых: 5" in text


def test_an_old_run_without_the_key_reads_as_before():
    text = _summary(unrecoverable=8)
    assert "Неинформативных карточек: 8" in text
    assert "отсрочки" not in text


# ── пустой раздел потерь ───────────────────────────────────────────────
def test_no_loss_also_admits_the_cards_it_did_not_judge():
    """После #17080 холод внутри отсрочки тревогу не поднимает.

    Значит «ни одной карточки с признаками потери» стало неверным ровно
    так же, как «работа подтверждена»: признак был, мы решили пока не
    считать его потерей.
    """
    text = format_sections([_too_early(i) for i in (17048, 17058)], {}, WEBHOOK)
    assert (
        "Ни одной карточки с признаками потери; по 2 карточкам судить ещё рано."
    ) in text


def test_no_loss_stays_plain_when_everything_was_judged():
    text = format_sections([_worked(16204)], {}, WEBHOOK)
    assert "Ни одной карточки с признаками потери." in text
    assert "судить ещё рано" not in text


def test_the_plural_of_one_card_is_singular_everywhere():
    text = format_sections([_too_early(17048)], {}, WEBHOOK)
    assert "по 1 карточке судить ещё рано" in text
    assert "карточкам" not in text


def test_eleven_is_not_singular():
    """21 — «карточке», 11 — «карточкам». Правило не «последняя цифра 1»."""
    from client_state_report import too_early_tail

    assert too_early_tail(1) == "по 1 карточке судить ещё рано"
    assert too_early_tail(11) == "по 11 карточкам судить ещё рано"
    assert too_early_tail(21) == "по 21 карточке судить ещё рано"
    assert too_early_tail(0) == ""
