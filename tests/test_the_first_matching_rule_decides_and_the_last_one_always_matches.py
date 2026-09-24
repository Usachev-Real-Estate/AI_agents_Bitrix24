"""Восемь правил «кого смотреть первым» (раздел 6.2 ТЗ).

Порядок здесь и есть правило. Каждое следующее условие проверяется только
на тех, кого не забрало предыдущее, поэтому проверять их по отдельности
мало: важно, что отказ перебивает тишину, а закрытая карточка перебивает
всё. Перестановка двух строк ничего не ломает синтаксически и меняет
половину списка молча.

Последнее правило безусловно `[V16]`: состояние получает каждый клиент, и
«ни одно не подошло» невозможно по построению.

Текста здесь нет вовсе. Правило 2 получает готовый признак, а не
комментарии и расшифровки: модуль, который не видит разговора, не может
его напечатать.
"""

from datetime import datetime, timedelta, timezone

import pytest

from clients import triage
from clients.schema import (
    TRIAGE_ABANDONED,
    TRIAGE_CLOSED,
    TRIAGE_COOLING,
    TRIAGE_LABELS,
    TRIAGE_MOVING,
    TRIAGE_NO_DATA,
    TRIAGE_NO_PLAN,
    TRIAGE_ORDER,
    TRIAGE_REFUSED,
    TRIAGE_WAITING_US,
)
from clients.triage import Facts, decide

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def _at(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def _state(**facts) -> str:
    base = {"cards_total": 1, "cards_closed": 0, "last_touch_at": _at(1),
            "next_step_at": _at(-2)}
    base.update(facts)
    return decide(Facts(**base), now=NOW).state


# ── по одному правилу ──────────────────────────────────────────────────

def test_a_client_with_every_card_closed_is_done():
    """Правило 1: все карточки закрыты — работа кончена."""
    assert _state(cards_total=3, cards_closed=3) == TRIAGE_CLOSED


def test_one_open_card_keeps_the_client_in_work():
    """Одной живой карточки хватает, чтобы клиент остался в списке.

    Клиент бывает и собственником, и покупателем: закрытая продажа не
    означает, что подбор второй квартиры тоже кончился.
    """
    assert _state(cards_total=3, cards_closed=2) != TRIAGE_CLOSED


def test_a_refusal_marker_ends_the_work():
    """Правило 2: маркер отказа найден."""
    assert _state(has_refusal=True) == TRIAGE_REFUSED


def test_a_long_call_without_a_transcript_leaves_nothing_to_judge_by():
    """Правило 3: звонок был, а текста нет — решать не на чем."""
    assert _state(calls_without_text=1) == TRIAGE_NO_DATA


def test_a_client_who_called_and_got_no_answer_waits_for_us():
    """Правило 4: входящий звонок, после него мы не вышли на связь."""
    assert _state(last_incoming_call_at=_at(2),
                  last_outgoing_at=_at(5)) == TRIAGE_WAITING_US


def test_calling_back_takes_the_client_out_of_waiting():
    """Обратная половина: ответили — и правило 4 молчит."""
    assert _state(last_incoming_call_at=_at(5),
                  last_outgoing_at=_at(2)) == TRIAGE_MOVING


def test_a_client_nobody_ever_called_back_still_waits():
    """Исходящих не было вовсе — клиент ждёт нас тем более."""
    assert _state(last_incoming_call_at=_at(2),
                  last_outgoing_at=None) == TRIAGE_WAITING_US


def test_silence_longer_than_three_weeks_means_abandoned():
    """Правило 5: тишина больше 21 дня."""
    assert _state(last_touch_at=_at(30)) == TRIAGE_ABANDONED


def test_silence_over_a_week_means_cooling():
    """Правило 6: тишина больше недели, но меньше трёх."""
    assert _state(last_touch_at=_at(10)) == TRIAGE_COOLING


def test_a_fresh_touch_without_a_next_step_has_no_plan():
    """Правило 7: недавно касались, а что дальше — не назначено."""
    assert _state(last_touch_at=_at(3), next_step_at=None) == TRIAGE_NO_PLAN


def test_everything_else_is_simply_moving():
    """Правило 8: касались, шаг назначен — клиент в работе."""
    assert _state(last_touch_at=_at(3), next_step_at=_at(-2)) == TRIAGE_MOVING


# ── порядок: что кого перебивает ───────────────────────────────────────

def test_a_closed_client_is_not_called_abandoned():
    """Закрытая карточка перебивает тишину.

    Без этого порядка проигранная полгода назад сделка каждую ночь
    всплывала бы в «брошен» — и список «кого смотреть первым» состоял бы
    в основном из тех, с кем работать уже нечего.
    """
    assert _state(cards_total=1, cards_closed=1, last_touch_at=_at(200)) == TRIAGE_CLOSED


def test_a_refusal_beats_the_silence_that_followed_it():
    """Отказ перебивает тишину.

    После отказа тишина закономерна, и объявлять такого клиента брошенным
    значит ставить брокеру в вину то, что он перестал звонить человеку,
    который просил больше не звонить.
    """
    assert _state(has_refusal=True, last_touch_at=_at(200)) == TRIAGE_REFUSED


def test_a_refusal_beats_a_missing_transcript():
    """Отказ перебивает «нет данных».

    Данных достаточно: клиент сказал «нет». Требовать расшифровку
    следующего звонка значит отправить РОПа искать то, что уже найдено.
    """
    assert _state(has_refusal=True, calls_without_text=3) == TRIAGE_REFUSED


def test_a_missing_transcript_beats_an_incoming_call():
    """«Нет данных» перебивает «ждёт нас».

    Оба требуют действия, но разного: по первому нечего читать, по
    второму некому перезванивать. Первое — это дыра в данных, и чинить её
    надо раньше, иначе решение по клиенту всё равно принимать не на чем.
    """
    assert _state(calls_without_text=1, last_incoming_call_at=_at(2),
                  last_outgoing_at=_at(5)) == TRIAGE_NO_DATA


def test_waiting_for_us_beats_the_silence_it_caused():
    """«Ждёт нас» перебивает тишину.

    Разница не косметическая: «брошен» — это про брокера, который забыл, а
    «ждёт нас» — про человека, который звонил и не дозвонился. Второе
    срочнее и адресуется по-другому.
    """
    assert _state(last_incoming_call_at=_at(40),
                  last_outgoing_at=_at(60),
                  last_touch_at=_at(40)) == TRIAGE_WAITING_US


def test_silence_beats_a_missing_next_step():
    """Тишина перебивает «без плана».

    У брошенного клиента следующий шаг тоже не назначен — почти всегда.
    Обратный порядок сложил бы всех брошенных в «без плана», и правило 5
    не срабатывало бы никогда.
    """
    assert _state(last_touch_at=_at(30), next_step_at=None) == TRIAGE_ABANDONED


# ── границы порогов ────────────────────────────────────────────────────

@pytest.mark.parametrize("days, expected", [
    (0, TRIAGE_MOVING),
    (7, TRIAGE_MOVING),
    (8, TRIAGE_COOLING),
    (21, TRIAGE_COOLING),
    (22, TRIAGE_ABANDONED),
    (365, TRIAGE_ABANDONED),
])
def test_the_thresholds_are_strictly_greater_than(days, expected):
    """Ровно на пороге клиент ещё не перешёл.

    ТЗ говорит «больше 21 дня» и «7–21 дня». Двадцать первый день — это
    ещё «остыл»: иначе граница ползёт на сутки, и клиент, до которого
    брокер собирался дозвониться сегодня, со вчерашнего дня числится
    брошенным.
    """
    assert _state(last_touch_at=_at(days)) == expected


def test_a_card_created_and_never_touched_is_abandoned():
    """Клиента, которого завели и ни разу не коснулись, видно.

    `last_touch_at` у него пуст, и сравнивать не с чем: правила 5 и 6
    пропустили бы его насквозь, а правило 7 объявило бы «без плана» —
    будто разница только в незаполненном поле. Тишина считается от
    заведения карточки, и полгода молчания выглядят как полгода молчания.
    """
    assert _state(last_touch_at=None, oldest_card_at=_at(200)) == TRIAGE_ABANDONED


def test_a_card_created_yesterday_is_not_abandoned():
    """Обратная половина: свежую карточку в брошенные не записываем."""
    assert _state(last_touch_at=None, oldest_card_at=_at(1)) == TRIAGE_MOVING


def test_a_touch_wins_over_the_card_date():
    """Касание важнее даты заведения.

    Карточке год, коснулись вчера — клиент в работе, а не брошен.
    """
    assert _state(last_touch_at=_at(1), oldest_card_at=_at(365)) == TRIAGE_MOVING


# ── покрытие и подписи ─────────────────────────────────────────────────

def test_every_client_gets_a_state_no_matter_how_empty():
    """Состояние выдаётся всегда, даже когда не известно ничего.

    Проверяется именно ПОЛНОТА, а не конкретный ответ: «ни одно правило не
    подошло» обязано быть невозможным по построению. У пустых фактов
    срабатывает седьмое — следующий шаг и правда не назначен.
    """
    verdict = decide(Facts(), now=NOW)

    assert verdict.state in TRIAGE_LABELS
    assert verdict.reason
    assert verdict.state == TRIAGE_NO_PLAN, "шаг не назначен — это и есть ответ"


def test_a_broken_date_does_not_swallow_the_client():
    """Мусор вместо даты не роняет прогон и не прячет клиента.

    Витрина хранит время строкой, и пустое или битое значение там бывает.
    Упасть здесь значит не собрать портфель целиком из-за одной карточки.
    """
    assert _state(last_touch_at="не дата", oldest_card_at=None) == TRIAGE_MOVING


def test_every_reason_names_its_rule_number():
    """Причина начинается с номера правила из таблицы раздела 6.2.

    Номер — это то, по чему брокеру отвечают «почему я в этом списке».
    Строка без номера отсылает к чтению кода, то есть не отвечает.
    """
    reasons = [
        triage.WHY_CLOSED, triage.WHY_REFUSED, triage.WHY_NO_DATA,
        triage.WHY_WAITING_US, triage.WHY_ABANDONED, triage.WHY_COOLING,
        triage.WHY_NO_PLAN, triage.WHY_MOVING,
    ]
    assert [r.split(":")[0] for r in reasons] == [str(n) for n in range(1, 9)]


def test_the_thresholds_in_the_labels_come_from_the_constants():
    """Подпись правила выводится из порога, а не написана рядом с ним.

    Иначе поменявший `ABANDONED_DAYS` на 20 оставит на экране «21», и
    объяснение станет неверным ровно тогда, когда его начнут читать
    внимательно.
    """
    assert str(triage.ABANDONED_DAYS) in triage.WHY_ABANDONED
    assert str(triage.COOLING_DAYS) in triage.WHY_COOLING


@pytest.mark.parametrize("count, word", [
    (1, "дня"), (2, "дней"), (5, "дней"), (11, "дней"),
    (21, "дня"), (22, "дней"), (101, "дня"),
])
def test_the_day_is_declined_after_the_word_more(count, word):
    """«больше 21 дня», а не «больше 21 день» и не «больше 21 дней».

    После «больше» форма родительная, и она НЕ та, что при счёте: считают
    «21 день», а здесь «больше 21 дня». Порог меняют числом, а подпись
    собирается из него — значит склонение обязано быть правилом, а не
    удачно подобранной строкой.
    """
    assert triage._days_after_more(count) == word


def test_all_eight_states_are_known_to_the_schema():
    """Каждое выдаваемое состояние есть в словаре и в сортировке.

    Состояние, которого нет в TRIAGE_ORDER, молча уезжает в конец списка;
    состояние, которого нет в TRIAGE_LABELS, показывается кодом.
    """
    produced = {
        TRIAGE_CLOSED, TRIAGE_REFUSED, TRIAGE_NO_DATA, TRIAGE_WAITING_US,
        TRIAGE_ABANDONED, TRIAGE_COOLING, TRIAGE_NO_PLAN, TRIAGE_MOVING,
    }

    assert produced <= set(TRIAGE_ORDER)
    assert produced <= set(TRIAGE_LABELS)
