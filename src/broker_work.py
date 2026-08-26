"""Доказательства работы брокера по клиенту.

Правило агентства: пока клиент выбирает и смотрит объекты, работа брокера
должна быть видна в карточке. Видна она может быть тремя способами, по
убыванию надёжности:

1. Звонок с клиентом — первоисточник, спорить не о чем.
2. Звонков нет → нужен развёрнутый комментарий, по которому понятно, что
   происходит с клиентом.
3. Комментарий вида «написал клиенту» → нужен скриншот переписки. Иначе
   правило вырождается: «написал» пишется за две секунды и ничего не
   доказывает, а именно такие формулировки и появляются, когда за наличие
   комментария начинают спрашивать.

Всё, что можно посчитать, считается здесь, а не спрашивается у модели:
вывод «брокер не работал N дней» — это претензия к человеку, и она должна
опираться на факты из CRM, а не на впечатление языковой модели. У модели
спрашивается только то, что без чтения текста не узнать: утверждает ли
брокер, что писал клиенту, и понятна ли из его комментария картина.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from funnel_profiles import FunnelProfile

CALL_ACTIVITY_TYPE_ID = 2
# Битрикс отдаёт даты со смещением портала; «сегодня» для отчёта — это
# московские сутки, а не UTC: иначе вечернее дело уезжает во вчера.
PORTAL_TZ = timezone(timedelta(hours=3))

# Чем подтверждена работа.
PROVEN_BY_CALL = "call"
PROVEN_BY_SCREENSHOT = "screenshot"
PROVEN_BY_COMMENT = "comment"
# Ход за контрагентом: он сам назвал, когда вернётся. Тишина брокера тут
# не бездействие, а ожидание, и предъявлять за неё нельзя. Но ждать можно
# только с делом на контроле — иначе ожидание ничем не отличается от
# забытья, и именно так теряются агенты, обещавшие приехать «в сентябре».
PROVEN_BY_WAITING = "waiting_on_client"
# Пауза с названной причиной: клиент в отпуске, ждём документы, ждём
# продажи его квартиры. #14776: клиент в отпуске до сентября, шаг назначен
# на 2 сентября, дело стоит — а карточка восемь дней числилась в
# недоработке. Формально ход был за брокером, по сути — за клиентом.
PROVEN_BY_PAUSE = "pause_explained"
# Чем не подтверждена.
# GAP_EMPTY_COMMENT — это и есть «неотработанная карточка» в терминах
# агентства: контакт передали, звонка нет, а в карточке одна отметка
# «в работе». Такой контакт стоил денег и не отработан.
GAP_CLAIMED_MESSAGE = "claimed_message_no_proof"
GAP_CLAIMED_NO_ANSWER = "claimed_no_answer_no_calls"
GAP_EMPTY_COMMENT = "comment_says_nothing"
GAP_NO_TRACE = "no_trace"
# Работа была, но раньше нормы этапа. Отдельно от GAP_NO_TRACE: карточка,
# где брокер звонил четыре дня назад при норме два, и карточка, где не
# было ничего никогда, — разные разговоры с брокером.
GAP_NO_TRACE_IN_WINDOW = "no_trace_in_window"
# Не то же самое, что GAP_NO_TRACE. Незакрытое дело «позвонить клиенту» —
# план брокера, а не работа с клиентом, и засчитывать его как работу
# нельзя. Но и говорить «следов работы нет» про карточку, где дело
# поставлено вчера, тоже нельзя: строка «следов работы нет (последний
# след 1 дн. назад)» противоречит сама себе в тех же скобках.
GAP_ONLY_PLANS = "only_plans"
# Не обвинение, а напоминание: ход за контрагентом, но вернуться к
# разговору нечем.
GAP_WAITING_NO_TASK = "waiting_without_task"
GAP_OUT_OF_WINDOW = "window_not_started"
# Дело, срок которого настал, а отписки о результате нет. Правило
# агентства: запланировано дело на сегодня — сегодня в карточке должен
# появиться комментарий о результате связи с клиентом. Дело без
# результата — напоминание брокера самому себе, а не работа с клиентом.
GAP_DUE_TASK_NO_RESULT = "due_task_no_result"

PROVEN = frozenset({
    PROVEN_BY_CALL, PROVEN_BY_SCREENSHOT, PROVEN_BY_COMMENT, PROVEN_BY_WAITING,
    PROVEN_BY_PAUSE,
})
# Разрывы, за которые не предъявляют, а напоминают.
REMINDERS = frozenset({GAP_WAITING_NO_TASK})

REASON_RU: dict[str, str] = {
    PROVEN_BY_CALL: "есть звонок с клиентом",
    PROVEN_BY_SCREENSHOT: "написал клиенту, приложен скриншот переписки",
    PROVEN_BY_COMMENT: "есть развёрнутый комментарий",
    PROVEN_BY_WAITING: "ход за клиентом, дело на контроле стоит",
    PROVEN_BY_PAUSE: "пауза на стороне клиента объяснена, дело на контроле стоит",
    GAP_WAITING_NO_TASK: (
        "ход за клиентом, но дела на возврат к разговору нет — так теряют контакт"
    ),
    GAP_CLAIMED_MESSAGE: "брокер пишет, что написал клиенту, но скриншота переписки нет",
    GAP_CLAIMED_NO_ANSWER: (
        "брокер пишет, что клиент не отвечает, но попыток звонка в таймлайне нет"
    ),
    GAP_EMPTY_COMMENT: (
        "карточка не отработана: звонка нет, а из комментария не понять, "
        "что с клиентом"
    ),
    # Две разные вещи, которые до сих пор назывались одним словом. #16218:
    # «следов работы нет (последний след 4 дн. назад)» — брокер звонил и
    # слал СМС, просто раньше нормы. Это опоздание, а не бездействие, и
    # обвинение должно звучать по факту.
    GAP_NO_TRACE: "следов работы нет вовсе",
    GAP_NO_TRACE_IN_WINDOW: "за норму этапа ни звонка, ни комментария",
    GAP_ONLY_PLANS: (
        "в карточке только запланированное дело — ни звонка, ни комментария"
    ),
    GAP_DUE_TASK_NO_RESULT: (
        "срок дела наступил, а комментария о результате связи с клиентом нет"
    ),
    GAP_OUT_OF_WINDOW: "срок ещё не наступил",
}


def work_window_days(profile: FunnelProfile, stage_id: str) -> int:
    """Сколько дней у брокера есть на след работы по этому этапу."""
    table = profile.work_window_days
    if stage_id in table:
        return int(table[stage_id])
    return int(table.get("_default", 7))


def judgement_starts_after(profile: FunnelProfile, stage_id: str) -> float:
    """С какого возраста карточки вообще можно судить о работе, в часах.

    Двум часам на одной карточке расходиться нельзя. На «Подборе» отсрочка
    полноты — 72 часа, окно работы — 2 дня, и карточка возрастом 53 часа
    получала обе строки разом: «рано судить» и «работа не подтверждена».
    Отсрочка — это решение агентства о том, когда с брокера вообще начинают
    спрашивать, и она старше окна: берём наибольшее из двух.
    """
    grace = profile.grace_hours
    hours = int(grace.get(stage_id, grace.get("_default", 24)))
    return max(float(hours), work_window_days(profile, stage_id) * 24.0)


def _parse(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def events_in_window(
    events: list[dict[str, Any]],
    now: datetime,
    days: int,
) -> list[dict[str, Any]]:
    """События за последние ``days`` дней.

    Событие с неразбираемой датой считается свежим: пропустить работу брокера
    хуже, чем лишний раз её засчитать.
    """
    edge = now - timedelta(days=days)
    fresh: list[dict[str, Any]] = []
    for event in events:
        created = _parse(event.get("created"))
        if created is None or created >= edge:
            fresh.append(event)
    return fresh


def _has_call(events: list[dict[str, Any]]) -> bool:
    """Состоявшийся звонок либо расшифровка разговора."""
    for event in events:
        if event.get("kind") == "transcript":
            return True
        if event.get("kind") != "activity":
            continue
        if int(event.get("type_id") or 0) != CALL_ACTIVITY_TYPE_ID:
            continue
        # Незакрытое дело «позвонить» — это план, а не звонок.
        if str(event.get("completed") or "").upper() == "Y":
            return True
    return False


def _has_call_attempt(events: list[dict[str, Any]]) -> bool:
    """Хотя бы попытка дозвона, снятая трубка или нет.

    Отдельно от _has_call: «клиент не отвечает» — самое удобное объяснение
    бездействия, и проверять его надо не по факту разговора, а по факту
    попытки. Несостоявшийся звонок тоже оставляет активность в таймлайне.
    """
    for event in events:
        if event.get("kind") == "transcript":
            return True
        if event.get("kind") != "activity":
            continue
        if int(event.get("type_id") or 0) == CALL_ACTIVITY_TYPE_ID:
            return True
    return False


def _day_start(moment: datetime) -> datetime:
    """Начало суток по времени портала."""
    return moment.astimezone(PORTAL_TZ).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )


def due_task_without_result(
    events: list[dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Дело, срок которого настал, а результата в карточке нет.

    Правило агентства: если дело запланировано на сегодня, то сегодня должен
    быть комментарий о результате связи с клиентом. Само дело результатом не
    считается — это план; за результат идут комментарий, расшифровка или
    закрытое дело (звонок состоялся и его отметили).

    Срок считается наступившим, когда он уже прошёл по часам: обвинять
    брокера в 10 утра за дело со сроком в 18:00 — то же самое, что судить о
    несделанном до того, как настал срок делать. Результат ищется с начала
    суток срока, а не с самой минуты: брокер, позвонивший в 11 и поставивший
    дело на 12, работу сделал.

    Возвращает {deadline, subject, days_overdue} или None.
    """
    now = now or datetime.now(timezone.utc)
    due: datetime | None = None
    subject = ""
    for event in events:
        if event.get("kind") != "activity":
            continue
        if str(event.get("completed") or "").upper() == "Y":
            continue
        deadline = _parse(event.get("deadline"))
        if deadline is None or deadline > now:
            continue
        # Из нескольких просроченных берём самое старое: долг считается от
        # первого несданного дела, а не от последнего.
        if due is None or deadline < due:
            due = deadline
            subject = str(event.get("subject") or "").strip()
    if due is None:
        return None

    since = _day_start(due)
    for event in events:
        if event.get("kind") == "activity":
            # Незакрытое дело — это план, а не отчёт о результате.
            if str(event.get("completed") or "").upper() != "Y":
                continue
        created = _parse(event.get("created"))
        if created is not None and created >= since:
            return None

    overdue = int((_day_start(now) - since).total_seconds() // 86400)
    return {
        "deadline": since.date().isoformat(),
        "subject": subject,
        "days_overdue": max(0, overdue),
    }


def _pause_covers_the_task(
    pause_until: str,
    deadline: datetime,
    now: datetime,
    window_days: int,
) -> bool:
    """Названная причина объясняет именно этот срок, а не любой.

    Без этой проверки правило вырождается: достаточно поставить дело на три
    месяца вперёд, приписать «клиент в отпуске» — и брокер чист навсегда.
    Поэтому срок дела должен укладываться в названную паузу плюс норму этапа
    на то, чтобы после неё выйти на связь.

    Срок паузы не назван («до сентября», «когда вернётся») — проверять нечем;
    тогда правило держится на остальном: причина названа дословно в карточке,
    и дело по карточке стоит.
    """
    ends = _parse(pause_until)
    if ends is None:
        return True
    if ends <= now:
        # Пауза кончилась — ожидание больше не оправдание.
        return False
    return deadline <= ends + timedelta(days=window_days)


def _ball_is_theirs(who: str, when: str, now: datetime) -> bool:
    """Ход за контрагентом, и названный им срок ещё не прошёл.

    Неразбираемый срок («в начале сентября») считается ненаступившим: не
    сумели прочитать дату — это наша слепота, а не просрочка брокера. Как
    только срок прошёл по календарю, ожидание кончилось и карточка снова
    судится обычными правилами.
    """
    if str(who or "").strip().lower() != "client":
        return False
    return _date_state(str(when or ""), now) != "past"


def assess_broker_work(
    events: list[dict[str, Any]],
    *,
    profile: FunnelProfile,
    stage_id: str,
    hours_on_stage: float | None,
    claims_messaged: bool,
    comment_informative: bool,
    claims_no_answer: bool = False,
    next_step_who: str = "",
    next_step_when: str = "",
    pause_explained: bool = False,
    pause_until: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Подтверждена ли работа брокера за последнее окно по этапу.

    Возвращает {proven, reason, window_days, days_quiet}. days_quiet — дней с
    последнего любого следа в карточке; None, если следов нет вовсе.
    """
    now = now or datetime.now(timezone.utc)
    days = work_window_days(profile, stage_id)
    window = events_in_window(events, now, days)

    last_seen: datetime | None = None
    for event in events:
        created = _parse(event.get("created"))
        if created is not None and (last_seen is None or created > last_seen):
            last_seen = created
    # Округляем: «12.070261341574074 дня» — не точность, а шум. Он уходил в
    # state_json, менялся каждую секунду и случайно совпадал с цифрами
    # телефона, из-за чего тест маскировки падал примерно раз на сотню
    # прогонов. Отчёту хватает одного знака.
    days_quiet = (
        round((now - last_seen).total_seconds() / 86400.0, 1)
        if last_seen else None
    )

    # Наступивший срок дела проверяется раньше окна этапа и раньше звонка:
    # обязательство брокер назначил себе сам, и оно не отменяется ни тем,
    # что карточка молодая, ни звонком трёхдневной давности.
    due = due_task_without_result(events, now)
    if due is not None:
        return {
            "proven": False,
            "reason": GAP_DUE_TASK_NO_RESULT,
            "window_days": days,
            "days_quiet": days_quiet,
            "due_task": due,
        }

    # Карточка младше отсрочки этапа: спрашивать не с чего.
    if hours_on_stage is not None and hours_on_stage < judgement_starts_after(
        profile, stage_id,
    ):
        return {
            "proven": True,
            "reason": GAP_OUT_OF_WINDOW,
            "window_days": days,
            "days_quiet": days_quiet,
        }

    # Пауза с названной причиной: клиент в отпуске, ждёт документы, продаёт
    # свою квартиру. Ход при этом может числиться за брокером — выйти на связь
    # после паузы всё равно ему, — но спрашивать с него за тишину, причину
    # которой он же и записал в карточку, значит наказывать за аккуратность.
    # Ждать можно только с делом: без него это не пауза, а забытьё.
    if pause_explained:
        deadline = open_future_deadline(events, now)
        if deadline is None:
            return {
                "proven": False,
                "reason": GAP_WAITING_NO_TASK,
                "window_days": days,
                "days_quiet": days_quiet,
            }
        if _pause_covers_the_task(pause_until, deadline, now, days):
            return {
                "proven": True,
                "reason": PROVEN_BY_PAUSE,
                "window_days": days,
                "days_quiet": days_quiet,
                "pause_until": pause_until or "unknown",
            }

    # Ход за контрагентом: он сам назвал, когда вернётся к разговору. Молчание
    # брокера тут не бездействие. #16798: агент сказал, что наберёт в начале
    # сентября и приедет с покупателем, — а карточка ушла в «недоработку» за
    # пять дней тишины. Ждать можно, но только с делом на контроле: без него
    # ожидание ничем не отличается от забытья, и это не обвинение, а повод
    # напомнить.
    if _ball_is_theirs(next_step_who, next_step_when, now):
        return {
            "proven": has_open_future_task(events, now),
            "reason": (
                PROVEN_BY_WAITING
                if has_open_future_task(events, now)
                else GAP_WAITING_NO_TASK
            ),
            "window_days": days,
            "days_quiet": days_quiet,
        }

    # Ветка «комментарий» смотрит только на комментарии. Незакрытое дело
    # «позвонить клиенту» — это план брокера, а не работа с клиентом, и
    # засчитывать его как след нельзя.
    comments = [e for e in window if e.get("kind") == "comment"]
    if _has_call(window):
        reason = PROVEN_BY_CALL
    elif not comments:
        # Три разных упрёка, которые раньше звучали одинаково: пусто вовсе,
        # одни планы, и работа была — но раньше нормы этапа.
        if window:
            reason = GAP_ONLY_PLANS
        elif last_seen is None:
            reason = GAP_NO_TRACE
        else:
            reason = GAP_NO_TRACE_IN_WINDOW
    elif claims_no_answer and not _has_call_attempt(window):
        # «Не дозвонился» проверяется первым: это объяснение бездействия, и
        # оно должно стоить дороже остальных. Есть попытки — брокер работал,
        # даже если трубку не взяли; нет попыток — работы не было.
        reason = GAP_CLAIMED_NO_ANSWER
    elif claims_messaged:
        # Утверждение «написал клиенту» засчитывается только со скриншотом.
        has_screenshot = any(e.get("has_files") for e in comments)
        reason = PROVEN_BY_SCREENSHOT if has_screenshot else GAP_CLAIMED_MESSAGE
    elif not comment_informative:
        reason = GAP_EMPTY_COMMENT
    else:
        reason = PROVEN_BY_COMMENT

    return {
        "proven": reason in PROVEN,
        "reason": reason,
        "window_days": days,
        "days_quiet": days_quiet,
    }


def open_future_deadline(
    events: list[dict[str, Any]],
    now: datetime | None = None,
) -> datetime | None:
    """Ближайший срок незакрытого дела в будущем, или None."""
    now = now or datetime.now(timezone.utc)
    nearest: datetime | None = None
    for event in events:
        if event.get("kind") != "activity":
            continue
        if str(event.get("completed") or "").upper() == "Y":
            continue
        deadline = _parse(event.get("deadline"))
        if deadline is None or deadline <= now:
            continue
        if nearest is None or deadline < nearest:
            nearest = deadline
    return nearest


def has_open_future_task(
    events: list[dict[str, Any]],
    now: datetime | None = None,
) -> bool:
    """Есть ли по карточке незакрытое дело со сроком в будущем."""
    return open_future_deadline(events, now) is not None


def _date_state(text: str, now: datetime) -> str:
    """Срок: "future" / "past" / "" (не дата).

    Прошедшая дата — не то же самое, что будущая. Совет «запланировать дело
    на 19 августа», когда сегодня 26-е, читается как издёвка: срок уже
    сорван, и планировать надо не его, а разговор о новом.
    """
    parsed = _parse(text)
    if parsed is None:
        return ""
    return "future" if parsed > now else "past"


def next_action(
    state: dict[str, Any],
    events: list[dict[str, Any]],
    now: datetime | None = None,
) -> str:
    """Что брокеру сделать по карточке прямо сейчас, или "" если нечего.

    Отчёт, который сообщает «не хватает следующего шага с датой», ставит
    диагноз. РОПу нужен рецепт: дело в Битриксе, с датой. Особенно когда
    ход за клиентом — «собственник вывезет мусор, потом фотосессия» — такая
    карточка выглядит брошенной, хотя брокер просто ждёт. Ждать можно, но
    с запланированным делом, иначе ожидание ничем не отличается от забытья.
    """
    now = now or datetime.now(timezone.utc)

    due = due_task_without_result(events, now)
    if due is not None:
        subject = str(due.get("subject") or "").strip()
        tail = f" по делу «{subject}»" if subject else ""
        if int(due.get("days_overdue") or 0) >= 1:
            return (
                f"Срок дела {due['deadline']} прошёл, результата в карточке нет — "
                f"связаться с клиентом и написать результат{tail}"
            )
        return (
            f"Дело стоит на сегодня ({due['deadline']}) — "
            f"написать в карточке результат связи с клиентом{tail}"
        )

    if has_open_future_task(events, now):
        # Дело уже стоит — советовать нечего.
        return ""

    step = state.get("next_step") if isinstance(state.get("next_step"), dict) else {}
    what = str(step.get("what") or "").strip()
    when = str(step.get("when") or "").strip()
    who = str(step.get("who") or "").strip().lower()
    dated = bool(when) and when.lower() != "unknown"

    if not what or what.lower() == "unknown":
        return "Запланировать дело: согласовать с клиентом следующий шаг и срок"

    when_state = _date_state(when, now) if dated else ""

    if who == "client":
        # Агента называем агентом. «Проверить, выполнил ли клиент» про
        # регионального агента, который обещал приехать с покупателем, звучит
        # мимо, а терять такой контакт дороже всего: он приводит сделку.
        party = state.get("counterparty") if isinstance(
            state.get("counterparty"), dict
        ) else {}
        agent = str(party.get("who") or "") == "agent"
        noun = "агентом" if agent else "клиентом"
        loss = " — иначе контакт потеряется" if agent else ""
        if when_state == "future":
            return (
                f"Запланировать дело на {when}: связаться и проверить, "
                f"выполнено ли — {what}"
            )
        if when_state == "past":
            return (
                f"Срок {when} прошёл, ответа нет — связаться с {noun} "
                f"и назначить новый: {what}"
            )
        if dated:
            return (
                f"Запланировать дело: срок назван как «{when}» — поставить дело "
                f"на связь с {noun} и подтвердить точную дату ({what}){loss}"
            )
        return (
            f"Запланировать дело: связаться с {noun} и согласовать срок — "
            f"{what}{loss}"
        )

    if when_state == "future":
        return f"Запланировать дело на {when}: {what}"
    if when_state == "past":
        return f"Срок {when} прошёл, дела нет — связаться и назначить новый: {what}"
    if dated:
        return f"Запланировать дело: уточнить дату («{when}») и поставить — {what}"
    return f"Запланировать дело с датой: {what}"
