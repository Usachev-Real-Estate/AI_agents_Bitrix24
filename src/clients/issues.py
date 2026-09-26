"""Проблемы клиента — справочник раздела 8 ТЗ.

Рядом с `triage_state`, но отвечает на другой вопрос. Состояние говорит
«кого смотреть первым» и у клиента одно: правила идут сверху вниз, первое
совпавшее выигрывает. Проблема говорит «что именно не так», и их бывает
несколько сразу — человек одновременно и брошен, и без единого
комментария ответственного.

Отсюда важное следствие: **проблемы считаются независимо от цепочки
правил.** У клиента с маркером отказа состояние «отказ», и правило 3 до
него не доходит, — но `missing_transcripts` у него всё равно есть, если
разговоры не расшифрованы. Считать проблемы по состоянию значило бы
потерять всё, что перекрыто более ранним правилом.

**«На активной стадии» — это хотя бы одна незакрытая карточка.** Условие
в ТЗ стоит у трёх кодов из пяти, и смысл у него один: по закрытой
карточке претензий не бывает. Здесь оно применяется ко всем пяти —
клиент, у которого всё закрыто, не проблемный, а завершённый.

Чистая функция: ни базы, ни портала. Проблема попадает на экран РОПа и в
счётчик, по которому спросят с брокера, и проверяться она должна без
поднятия базы.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping

from clients.schema import (
    ISSUE_ABANDONED,
    ISSUE_MISSING_TRANSCRIPTS,
    ISSUE_NO_ASSIGNEE_COMMENT,
    ISSUE_PROMISE_OVERDUE,
    ISSUE_REFUSAL_NOT_REFLECTED,
)
from clients.triage import ABANDONED_DAYS, Facts


def detect(
    facts: Facts,
    *,
    comments_by_assignee: int | None,
    silence_days: int | None,
    promise_overdue: bool,
) -> tuple[str, ...]:
    """Коды проблем клиента. Пустой кортеж — претензий нет.

    Порядок возврата — как в `ISSUE_ORDER`: он же порядок на экране, и
    собирать его в двух местах значило бы однажды разойтись.
    """
    if not _active(facts):
        return ()

    found: list[str] = []
    if facts.has_refusal:
        # Отказ есть, а карточка живая: слова клиента в CRM не отражены, и
        # брокер продолжит его вести. Это претензия к брокеру, а не к
        # клиенту, поэтому код первый в списке.
        found.append(ISSUE_REFUSAL_NOT_REFLECTED)
    if promise_overdue:
        found.append(ISSUE_PROMISE_OVERDUE)
    if comments_by_assignee == 0:
        # Ровно ноль, а не «пусто»: NULL значит «не считали», и объявить по
        # нему претензию — это обвинить брокера в том, что прогон не дошёл
        # до подсчёта.
        found.append(ISSUE_NO_ASSIGNEE_COMMENT)
    if silence_days is not None and silence_days > ABANDONED_DAYS:
        found.append(ISSUE_ABANDONED)
    if facts.calls_without_text:
        found.append(ISSUE_MISSING_TRANSCRIPTS)
    return tuple(found)


def overdue(promises: Iterable[Mapping[str, Any]], *, now: datetime) -> bool:
    """Есть ли у клиента просроченное обещание.

    Обещание просрочено, когда назначенный срок прошёл И не прикрыт
    законным молчанием: «созвонимся в конце осени» — это не просрочка, а
    договорённость, и ругать за неё значит ругать за правильную работу.

    Строк приходит по одной на карточку; хватает любой просроченной.
    """
    moment = now.isoformat()
    for row in promises:
        promised = _text(row.get("promised_at"))
        if not promised or promised >= moment:
            continue
        wait = _text(row.get("wait_until"))
        if wait and wait >= moment:
            continue
        return True
    return False


def _text(value: Any) -> str:
    return str(value or "").strip()


def _active(facts: Facts) -> bool:
    """Есть ли хоть одна незакрытая карточка.

    Нет карточек вовсе — клиент активен: его завели и не закрывали, и это
    как раз повод для претензии, а не причина её снять. Ноль закрытых из
    нуля — не «всё закрыто», а «закрывать было нечего».
    """
    if not facts.cards_total:
        return True
    return facts.cards_closed < facts.cards_total
