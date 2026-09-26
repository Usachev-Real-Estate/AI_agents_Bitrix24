"""«Кого смотреть первым» — восемь правил раздела 6.2 ТЗ.

Это НЕ тот вопрос, на который отвечает `client_state.py`. Тот говорит «что
происходит с клиентом» и делает это моделью; этот говорит «кого смотреть
первым» и обязан быть дешёвым, детерминированным и объяснимым построчно.
Рядом с состоянием кладётся `triage_reason` — номер и краткое имя
сработавшего правила: иначе ответить брокеру, почему он в этом списке,
можно будет только чтением кода.

Правила идут сверху вниз, первое совпавшее выигрывает, последнее
безусловно — покрытие полное `[V16]`.

**Сюда не попадает ни строчки текста.** Правило 2 получает готовый
признак `has_refusal`, а не комментарии и не расшифровки: поиск маркеров
живёт в `clients.transcripts` и наружу отдаёт только да/нет. Модуль,
который не видит разговора, не может его напечатать.

**Активность карточки не проверяется отдельно** в правилах 2, 3 и 5, хотя
ТЗ приписывает её каждому. Правило 1 уже забрало всех, у кого закрыты ВСЕ
карточки; значит до правила 2 доходят только клиенты хотя бы с одной
живой. Дублировать проверку значило бы завести второе место, где её можно
забыть поправить.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from clients.schema import (
    TRIAGE_ABANDONED,
    TRIAGE_CLOSED,
    TRIAGE_COOLING,
    TRIAGE_MOVING,
    TRIAGE_NO_DATA,
    TRIAGE_NO_PLAN,
    TRIAGE_REFUSED,
    TRIAGE_WAITING_US,
)

# Пороги тишины из раздела 6.4 ТЗ.
#
# В продукте уже живёт третье число — SILENT_DAYS = 14 в analytics/work.py,
# по которому считают вкладки «Сегодня» и «Люди». Три порога в одном
# продукте это расхождение, и молчаливым его оставлять нельзя: раздел 6.4
# прямо требует показывать рядом с состоянием, по какому порогу оно
# посчитано. Константы лежат здесь, чтобы подпись бралась из них, а не
# переписывалась в шаблоне.
ABANDONED_DAYS = 21
COOLING_DAYS = 7

# Короче минуты — не разговор, а недозвон или «перезвоните позже»
# (раздел 5 ТЗ). Расшифровывать такое незачем, и требовать по нему текста
# тем более: правило 3 объявляло бы безданным каждого, кому не дозвонились.
MEANINGFUL_CALL_SEC = 60


def _days_after_more(count: int) -> str:
    """«дня» или «дней» — форма после слова «больше».

    Подпись правила выводится из порога, а не пишется руками: поменяв
    ABANDONED_DAYS на 20, легко оставить в тексте «21». Но подстановка без
    склонения даёт «больше 21 дней», и на экране РОПа это читается как
    небрежность ко всему остальному.

    Форма здесь родительная и потому НЕ та, что при счёте: считают «21
    день» и «22 дня», а после «больше» — «больше 21 дня» и «больше 22
    дней». Правило простое: оканчивается на единицу (кроме одиннадцати) —
    «дня», во всех остальных случаях «дней».
    """
    return "дня" if count % 10 == 1 and count % 100 != 11 else "дней"


# Причина — номер правила и его краткое имя. Номера те же, что в таблице
# раздела 6.2, и менять их нельзя: они уходят на экран и в выгрузки.
WHY_CLOSED = "1: все карточки закрыты"
WHY_REFUSED = "2: маркер отказа"
WHY_NO_DATA = "3: звонок без расшифровки"
WHY_WAITING_US = "4: звонил клиент, мы не ответили"
WHY_ABANDONED = f"5: тишина больше {ABANDONED_DAYS} {_days_after_more(ABANDONED_DAYS)}"
WHY_COOLING = f"6: тишина больше {COOLING_DAYS} {_days_after_more(COOLING_DAYS)}"
WHY_NO_PLAN = "7: следующий шаг не назначен"
WHY_MOVING = "8: в работе"


@dataclass(frozen=True)
class Facts:
    """Что известно о клиенте к моменту решения.

    Факты, а не сырьё: считает их сборщик, знающий и витрину, и кэш
    расшифровок. Правила остаются чистой функцией — их можно проверить,
    не заводя ни одной базы.
    """

    cards_total: int = 0
    cards_closed: int = 0
    # Маркер отказа нашёлся в комментарии или расшифровке. Пусто — правило
    # 2 не сработало ИЛИ было выключено: разницу знает сборщик, и он же
    # пишет её в degraded_rules.
    has_refusal: bool = False
    # Звонков длиннее минуты, по которым расшифровки нет.
    calls_without_text: int = 0
    last_incoming_call_at: str | None = None
    # Последнее, чем мы ответили: исходящий звонок или состоявшаяся встреча.
    # Комментарий сюда не входит — написать себе в карточку не значит
    # выйти на связь с человеком, который звонил.
    last_outgoing_at: str | None = None
    last_touch_at: str | None = None
    next_step_at: str | None = None
    # Когда завели самую старую карточку. Нужен ровно одному случаю —
    # клиенту, которого не касались НИ РАЗУ: тишину ему считать не от чего,
    # а объявить «в работе» значило бы соврать про заведённую и забытую
    # карточку. См. silence_days.
    oldest_card_at: str | None = None


@dataclass(frozen=True)
class Verdict:
    """Состояние и правило, которое его дало."""

    state: str
    reason: str


def decide(facts: Facts, *, now: datetime) -> Verdict:
    """Состояние клиента. Первое совпавшее правило выигрывает."""
    if facts.cards_total and facts.cards_closed >= facts.cards_total:
        return Verdict(TRIAGE_CLOSED, WHY_CLOSED)

    if facts.has_refusal:
        return Verdict(TRIAGE_REFUSED, WHY_REFUSED)

    if facts.calls_without_text:
        return Verdict(TRIAGE_NO_DATA, WHY_NO_DATA)

    if _waiting_us(facts):
        return Verdict(TRIAGE_WAITING_US, WHY_WAITING_US)

    silence = silence_days(facts, now=now)
    if silence is not None:
        if silence > ABANDONED_DAYS:
            return Verdict(TRIAGE_ABANDONED, WHY_ABANDONED)
        if silence > COOLING_DAYS:
            return Verdict(TRIAGE_COOLING, WHY_COOLING)

    if not facts.next_step_at:
        return Verdict(TRIAGE_NO_PLAN, WHY_NO_PLAN)

    return Verdict(TRIAGE_MOVING, WHY_MOVING)


def _waiting_us(facts: Facts) -> bool:
    """Клиент звонил, а мы после этого не вышли на связь.

    Непринятый вызов считается наравне с состоявшимся разговором, и это не
    послабление, а суть правила: человек пытался дозвониться и не
    дозвонился — ждёт он нас тем более.
    """
    if not facts.last_incoming_call_at:
        return False
    return not facts.last_outgoing_at or facts.last_outgoing_at < facts.last_incoming_call_at


def silence_days(facts: Facts, *, now: datetime) -> int | None:
    """Сколько дней с последнего касания. ``None`` — измерить нечем.

    Публичная намеренно: тем же числом считает претензию `abandoned`
    справочника раздела 8. Своя копия там разошлась бы с правилом ровно на
    клиенте, которого не касались ни разу, — и он оказался бы «брошен» в
    списке и без претензии в исключениях. Одна мера, одно место.

    Касания не было ни разу — тишина считается от заведения самой старой
    карточки. Без этого клиент, которого завели и забыли, проваливался бы
    сквозь правила 5 и 6 в «без плана» или «движется»: `last_touch_at` у
    него пуст, сравнивать не с чем, и молчание длиной в полгода выглядело
    бы как отсутствие поводов для тревоги. Это ровно тот клиент, ради
    которого список и заводили.
    """
    return _days_since(facts.last_touch_at or facts.oldest_card_at, now=now)


def _days_since(value: str | None, *, now: datetime) -> int | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0, (now - moment).days)
