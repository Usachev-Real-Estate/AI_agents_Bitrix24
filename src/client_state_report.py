"""Отчёт РОПу по состоянию клиентов: русские значения, кэш вместо пропусков.

Модель отвечает служебными кодами (warm, medium, broker), а отчёт читают люди.
Перевод живёт здесь, а не в промпте: словарь правится без изменения запроса к
модели, и ни один кэш от этого не инвалидируется.
"""

from __future__ import annotations

from typing import Any

from broker_work import REASON_RU as WORK_REASON_RU
from broker_work import GAP_ABANDONED
from broker_work import GAP_NO_TRACE
from broker_work import GAP_PAUSE_TASK_TOO_LATE
from broker_work import DUE_TASK_GAPS
from broker_work import GAP_ONLY_PLANS
from broker_work import GAP_PLAN_TOO_FAR
from broker_work import PROVEN
from broker_work import PROVEN_BY_PAUSE
from broker_work import PROVEN_BY_TASK_PLAN
from broker_work import REMINDERS as WORK_REMINDERS
from broker_work import SELF_ARGUED_GAPS
from broker_work import TIMELESS_GAPS
from buyer_commission_reminder import deal_url
from funnel_profiles import fact_name_table
from tools import (
    BUYERS_STAGE_NAMES,
    SELLERS_PAID_SOURCE_NAMES,
    SELLERS_STAGE_NAMES,
)

# Значение, которым модель отвечает «не знаю». В отчёте показываем словами.
UNKNOWN = "unknown"
UNKNOWN_RU = "не указано"

TEMPERATURE_RU: dict[str, str] = {
    "hot": "горячий",
    "warm": "тёплый",
    "cold": "холодный",
    "unknown": "неизвестно",
}

TEMPERATURE_ICON: dict[str, str] = {
    "hot": "🔥",
    "warm": "🌤",
    "cold": "❄️",
    "unknown": "❓",
}

RISK_RU: dict[str, str] = {
    "low": "низкий",
    "medium": "средний",
    "high": "высокий",
}

WHO_RU: dict[str, str] = {
    "broker": "брокер",
    "client": "клиент",
    "unknown": "не определён",
}

VERDICT_RU: dict[str, str] = {
    "good": "хорошо",
    "tolerable": "терпимо",
    "poor": "плохо",
    "too_early": "рано судить",
    "out_of_qc": "вне контроля качества",
    # Не то же самое, что «вне контроля качества»: там решение агентства,
    # здесь — ненаписанные правила. Формулировка не должна их смешивать.
    "no_rules": "полнота не оценивалась",
}


# Причины, по которым карточка не пошла в модель. Те, что означают «данные не
# изменились», отчёт показывает прошлым разбором, а не строкой о пропуске.
CACHED_REASONS = frozenset({"unchanged", "no_new_events"})

REASON_RU: dict[str, str] = {
    "unchanged": "без изменений с прошлого разбора",
    "no_new_events": "без изменений с прошлого разбора",
    "stage_out_of_qc": "этап вне контроля качества",
    "evidence_incomplete": "карточку не удалось прочитать целиком",
    "collect_error": "не удалось собрать карточку",
    "llm_error": "модель не ответила",
    "parse_error": "ответ модели не разобран",
    "invalid_deal_id": "некорректный ID сделки",
    "unexpected_error": "непредвиденная ошибка",
}


# Причины, по которым карточка не прочитана и потому не судится ни в одном
# разделе. «Этап вне контроля качества» сюда не входит: это решение агентства,
# а не сбой чтения.
UNREAD_REASONS = frozenset({
    "evidence_incomplete",
    "collect_error",
    "llm_error",
    "parse_error",
    "invalid_deal_id",
    "unexpected_error",
})


def unread_cards(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Карточки, которые прогон не смог прочитать или разобрать.

    Они выпадали из тела отчёта целиком: состояния нет — раздел их
    пропускает. Пустая «НЕДОРАБОТКА БРОКЕРА — 0» при этом заявляла «работа
    подтверждена по всем карточкам», хотя часть карточек никто не открывал.
    """
    return [
        r for r in results
        if r.get("skipped") and str(r.get("reason") or "") in UNREAD_REASONS
    ]


def ru(value: Any, table: dict[str, str], default: str = UNKNOWN_RU) -> str:
    """Перевод кода в русское слово; неизвестный код возвращаем как есть."""
    key = str(value or "").strip().lower()
    if not key:
        return default
    return table.get(key, key)


def humanize(value: Any) -> str:
    """Текстовое поле от модели: «unknown» показываем словами, а не кодом."""
    text = str(value or "").strip()
    if not text or text.lower() == UNKNOWN:
        return UNKNOWN_RU
    return text


def format_next_step(step: Any) -> str:
    """«Что (когда, кто)» — с русскими значениями и без голого unknown."""
    if not isinstance(step, dict):
        return UNKNOWN_RU
    what = humanize(step.get("what"))
    when = humanize(step.get("when"))
    who = ru(step.get("who"), WHO_RU, WHO_RU["unknown"])
    if what == UNKNOWN_RU:
        # «не указано (не указано, не определён)» — три пустоты подряд там,
        # где нечего сказать одной. Срок и исполнитель имеют смысл только
        # при названном шаге: без него они не уточняют, а повторяют.
        return NO_STEP_RU
    return f"{what} ({when}, {who})"


NO_STEP_RU = "шаг не назначен"


def cards_noun(count: int) -> str:
    """«карточке» / «карточкам» — одно правило на все счётные строки отчёта.

    Строк, где отчёт называет число карточек, стало больше одной, и каждая
    новая склоняла по-своему: «по 21 карточкам», «21 брошенных». Ошибка
    мелкая, но она стоит ровно там, где отчёт просят перепроверить.
    """
    return "карточке" if count % 10 == 1 and count % 100 != 11 else "карточкам"


def abandoned_noun(count: int) -> str:
    """«брошенная» / «брошенные» / «брошенных» — по числу."""
    if count % 100 in range(11, 15):
        return "брошенных"
    tail = count % 10
    if tail == 1:
        return "брошенная"
    if tail in (2, 3, 4):
        return "брошенные"
    return "брошенных"


def too_early_tail(count: int) -> str:
    """«по N карточкам судить ещё рано» — с правильным числом.

    Оговорка нужна обоим пустым разделам, а не одному: после того как холод
    внутри отсрочки перестал поднимать тревогу (#17080), «ни одной карточки
    с признаками потери» стало неверным ровно так же, как «работа
    подтверждена». Признак был — мы решили пока не считать его потерей.
    """
    if count <= 0:
        return ""
    return f"по {count} {cards_noun(count)} судить ещё рано"


def describe_next_step(state: dict[str, Any]) -> str:
    """Следующий шаг карточки — словами брокера или делом из Битрикса.

    Правило проекта уже гласит, что дело с датой и есть следующий шаг, и
    доказательство это лучше пересказа: его видно в CRM, а не только на
    словах (см. NEXT_STEP_FACT — вердикт так и считает). Отчёт про это не
    знал: #13520 печатала «шаг не назначен», а строкой ниже — «дело стоит
    на 2026-09-01». Две строки одной карточки о разном.

    Пересказ брокера, если он есть, остаётся главным: он говорит, ЧТО
    будет сделано, а дело — только когда. Подменять одно другим нельзя,
    поэтому дело подставляется лишь там, где шага не назвали вовсе.

    А вот ДАТУ берём из Битрикса, если дело там стоит. Прогон 31.08,
    #16322: «Шаг: Связаться с клиентом (2026-08-28)» и через две строки
    совет «Дело стоит на 2026-09-01». Модель назвала одну дату, CRM
    держит другую, и читателю предъявлены обе как одна. Спорить тут не о
    чем: дело в Битриксе — доказательство, пересказ — слова, и это
    правило в проекте уже записано (см. NEXT_STEP_FACT в вердикте).
    """
    scheduled = str(state.get("scheduled_task_at") or "").strip()
    if not scheduled:
        # Дело со сроком сегодня или уже прошедшим будущим не считается, и
        # подстановка молчала ровно там, где дата важнее всего. #14094:
        # «Шаг: Повторный созвон (2026-08-20)» и через две строки
        # «Напоминание: дело стоит на сегодня (2026-08-31)». Тот же дефект,
        # что чинили на #16322, только через другую дверь: доказательство из
        # CRM снова уступило пересказу. Берём срок из наступившего дела.
        due = ((state.get("work_evidence") or {}).get("due_task") or {})
        scheduled = str(due.get("deadline") or "").strip()
    step = state.get("next_step")
    if isinstance(step, dict) and scheduled:
        told = humanize(step.get("when"))
        if told != UNKNOWN_RU and scheduled not in told:
            step = {**step, "when": scheduled}
    text = format_next_step(step)
    if text != NO_STEP_RU:
        return text
    if not scheduled:
        return text
    return f"в карточке не описан, но в Битриксе стоит дело на {scheduled}"


# Сколько символов названия сделки помещается в строку отчёта. Дальше —
# рекламный текст, а не название.
TITLE_LIMIT = 90


def card_title(title: Any) -> str:
    """Название сделки в одну строку.

    #16422 называется целым объявлением с Циан: три строки текста, пустая
    строка внутри и цена. Отчёт печатал его как есть, ссылка на сделку
    уезжала на четвёртую строку, и карточка переставала читаться. Битрикс
    переносы в названии разрешает — значит их убирать нам.
    """
    text = " ".join(str(title or "").split())
    if len(text) <= TITLE_LIMIT:
        return text
    return text[:TITLE_LIMIT].rstrip() + "…"


def format_card(
    result: dict[str, Any],
    title: str,
    webhook_url: str,
) -> str:
    """Блок одной сделки.

    Карточка без новых данных печатается прошлым разбором — с пометкой, что он
    прошлый. Пустая строка вместо анализа скрывала бы от РОПа половину
    портфеля: «пропущено» и «ничего не происходит» — разные вещи, а выглядели
    одинаково.
    """
    deal_id = int(result.get("deal_id") or 0)
    reason = str(result.get("reason") or "")
    state = result.get("state") or {}

    lines = [
        f"[B]#{deal_id}[/B] {card_title(title)}".rstrip(),
        f"[URL]{deal_url(webhook_url, deal_id)}[/URL]",
    ]

    if not state:
        lines.append(f"⏭ Не разбиралась: {REASON_RU.get(reason, reason)}")
        return "\n".join(lines)

    level = str(state.get("temperature") or "unknown")
    icon = TEMPERATURE_ICON.get(level, TEMPERATURE_ICON["unknown"])
    reason_text = str(state.get("temperature_reason") or "").strip()
    temperature = f"{icon} Температура: [B]{ru(level, TEMPERATURE_RU)}[/B]"
    if reason_text:
        temperature += f" — {reason_text}"
    lines.append(temperature)

    verdict = str(state.get("verdict") or "")
    if verdict:
        verdict_line = f"Оценка карточки: {ru(verdict, VERDICT_RU)}"
        verdict_reason = str(state.get("verdict_reason") or "").strip()
        if verdict_reason:
            verdict_line += f" — {verdict_reason}"
        lines.append(verdict_line)

    lines.append(
        f"Риск: {ru(state.get('risk'), RISK_RU)} | "
        f"уверенность: {float(state.get('confidence') or 0.0):.2f}",
    )
    party = state.get("counterparty") if isinstance(state.get("counterparty"), dict) else {}
    if str(party.get("who") or "") == "agent":
        why = str(party.get("why") or "").strip()
        lines.append("👤 Контрагент: агент" + (f" — {why}" if why else ""))

    lines.append(f"Цель: {humanize(state.get('client_goal'))}")
    lines.append(f"Ситуация: {humanize(state.get('situation'))}")
    lines.append(f"Шаг: {describe_next_step(state)}")

    work = state.get("work_evidence") or {}
    if work and not work.get("proven"):
        quiet = work.get("days_quiet")
        if not isinstance(quiet, (int, float)):
            # Причина уже сказала «следов работы нет вовсе» — повторять
            # в скобках нечего.
            quiet_text = ""
        # Слово «брокера» здесь обязательно: days_quiet с 28.08 считается
        # только по следам брокера и его РОПа. На карточке, где бэк-офис
        # написал вчера, «последний след 9 дн. назад» РОП прочитает как
        # ошибку отчёта — он эти комментарии видит.
        elif quiet < 1:
            quiet_text = ", последний след брокера сегодня"
        elif quiet < float(work.get("window_days") or 0) + 1:
            # «Норма 7 дн., последний след 7 дн. назад» — читатель вычитает и
            # получает ноль, а мы при этом обвиняем. У самой границы округление
            # до целого превращает верную претензию в арифметическую ошибку,
            # поэтому у границы показываем десятую долю.
            quiet_text = f", последний след брокера {quiet:.1f} дн. назад"
        else:
            quiet_text = f", последний след брокера {quiet:.0f} дн. назад"
        due = work.get("due_task") if isinstance(work.get("due_task"), dict) else None
        reason_code = str(work.get("reason") or "")
        due_tail = ""
        if due:
            # У наступившего срока своя арифметика: норма этапа тут ни при
            # чём, спрашивают за конкретное дело и конкретную дату.
            overdue = int(due.get("days_overdue") or 0)
            due_tail = (
                f"срок {due.get('deadline')}, "
                + (f"просрочено на {overdue} дн." if overdue else "срок сегодня")
            )
        if due and reason_code in DUE_TASK_GAPS:
            # Претензия и есть про это дело — довод в скобках только его.
            tail = due_tail
        else:
            # А тут дело идёт довеском: с 28.08 просрочка претензию не гасит,
            # и в скобках должны стоять оба довода. Показать только срок дела
            # значило бы подпереть претензию про норму этапа цифрой, которая
            # к ней не относится, — читатель вычтет и не сойдётся.
            tail = f"норма {work.get('window_days')} дн.{quiet_text}"
        if reason_code == GAP_ONLY_PLANS and work.get("task_text"):
            # Порог, по которому дело признаётся планом, решает судьбу
            # карточки — а проверить его РОПу нечем: «в деле не сказано, что
            # и почему» звучит одинаково и про «Позвонить», и про четыре
            # строки разбора. Показываем, что в деле написано на самом деле.
            tail = f"в деле только «{work.get('task_text')}»"
        elif reason_code in TIMELESS_GAPS:
            # Цифры приводим только там, где они и есть довод.
            tail = ""
        elif reason_code == GAP_PAUSE_TASK_TOO_LATE:
            # Норма этапа и «последний след» тут не довод: брокер молчал
            # ровно потому, что клиент в отпуске, и это он же и записал.
            # Довод — две даты, которые не сходятся.
            until = str(work.get("pause_until") or "").strip()
            stands = str(work.get("task_deadline") or "").strip()
            tail = (
                f"клиент возвращается {until}, дело на {stands}"
                if until and until != "unknown" and stands
                else f"норма {work.get('window_days')} дн."
            )
        elif reason_code == GAP_PLAN_TOO_FAR:
            # Довод — норма этапа против срока дела. «Последний след» тут не
            # при чём: след как раз есть, вопрос к его дате.
            window = work.get("window_days")
            tail = (
                f"норма этапа {window} дн."
                if isinstance(window, (int, float)) else "срок дальше нормы этапа"
            )
        elif reason_code == GAP_ABANDONED:
            quiet = work.get("abandoned_days")
            if not isinstance(quiet, (int, float)):
                tail = "месяцы без действий брокера"
            elif work.get("no_trace_at_all"):
                # Следов нет вовсе: число — возраст карточки на этапе, а не
                # длина паузы после последнего следа. Говорим ровно это,
                # иначе «31 дн.» рядом с «просрочено на 116 дн.» читается
                # как ошибка отчёта.
                tail = (
                    "работу по карточке не начинали, "
                    f"она на этапе {float(quiet):.0f} дн."
                )
            else:
                # Срок считается от последнего действия брокера или РОПа —
                # неважно, дело это, звонок или комментарий (решение
                # агентства от 31.08). Раньше строка обещала срок «без
                # звонка и комментария», а число приходило от любого следа:
                # #8870 — единственный след дело, звонка и комментария не
                # было никогда, и рядом стояло «просрочено на 116 дн.».
                tail = (
                    f"последнее действие брокера {float(quiet):.0f} дн. назад"
                )

        # Напоминание и претензия не должны выглядеть одинаково: «работа не
        # подтверждена» про карточку, где клиент сам уехал до сентября, —
        # выговор за чужой отпуск.
        if reason_code in WORK_REMINDERS:
            head = "🔔 Напоминание"
        elif reason_code == GAP_ABANDONED:
            # «Работа не подтверждена» про карточку столетней давности —
            # слишком мягко и не о том: тут не подтверждать нечего.
            head = "🕸 Карточка брошена"
        elif reason_code == GAP_NO_TRACE:
            # Своя шапка — решение агентства от 31.08. «Работа не
            # подтверждена» про карточку, к которой брокер не притрагивался,
            # звучит как спор о доказательствах, а спора нет: работы не
            # было. Довод в скобках остаётся тот же — чьих следов нет.
            head = "🆕 Работу по карточке не начинали"
        else:
            head = "🔧 Работа не подтверждена"
        # Напоминание печатается без скобок: норма этапа там не довод.
        # Но у разрыва со своим доводом скобки и есть вся проверяемость —
        # «вернуться собрался позже» без двух дат оспорить нечем.
        drop_tail = reason_code in WORK_REMINDERS and reason_code not in SELF_ARGUED_GAPS
        if due_tail and reason_code not in DUE_TASK_GAPS:
            # Просрочку РОП должен видеть в любом случае: она конкретнее
            # нормы этапа и по ней сразу видно, за что зацепиться.
            tail = f"{tail}; {due_tail}" if tail and not drop_tail else due_tail
            drop_tail = False
        tail = "" if drop_tail or not tail else f" ({tail})"
        lines.append(
            (
                f"{head}{tail}" if reason_code == GAP_ABANDONED
                else f"{head}: {WORK_REASON_RU.get(reason_code, reason_code)}{tail}"
            ),
        )

    if state.get("no_call"):
        # Пометка, а не претензия: подтвердить слова брокера нечем, и это
        # видно. Направление звонка агентство решило не различать — важен
        # сам факт разговора.
        #
        # Срок называем: пометка считается по окну этапа, а не по всей
        # истории карточки. «Звонка в таймлайне нет» на сделке, где звонили
        # полгода назад, — неправда, и брокер вправе её оспорить.
        window = work.get("window_days")
        lines.append(
            f"📵 Работа описана комментарием, звонка за {window} дн. нет"
            if isinstance(window, (int, float))
            else "📵 Работа описана комментарием, звонка за это время нет",
        )

    if state.get("recoverable") is False:
        lines.append("⚠️ Карточка неинформативна — картину клиента не восстановить")
        # Строки «работу видно, а клиента — нет» здесь больше нет. Полный
        # разбор печатается только для проблемных разделов, а карточка, где
        # работа подтверждена, с 28.08 в них не попадает: по определению
        # агентства работа ведётся, значит клиента мы не теряем. Строка стала
        # бы мёртвым кодом, который выглядит работающим правилом.
        #
        # Сам пробел никуда не делся, и говорить о нём мы не перестали: он
        # печатается маркером «⚠️ сделку по карточке не подхватить» в
        # однострочнике (см. _one_liners), там, где такая карточка теперь и
        # стоит. #16886 — карточка, ради которой правило написано.

    facts = fact_name_table()
    missing = [
        # Модель иногда возвращает ключ факта вместо имени: «budget, district,
        # timeline». Имя у нас есть — подставляем, а незнакомое оставляем как
        # есть: своя догадка хуже чужого текста.
        facts.get(str(m).strip(), str(m).strip())
        for m in (state.get("missing") or []) if str(m).strip()
    ]
    if missing:
        lines.append("Не хватает: " + ", ".join(missing))

    action = str(state.get("next_action") or "").strip()
    if action:
        lines.append(f"➡️ {action}")

    if reason in CACHED_REASONS:
        lines.append(f"↻ {REASON_RU[reason]}")

    return "\n".join(lines)


def _analyzed_line(stats: dict[str, Any]) -> str:
    """Первая строка шапки: сколько карточек и сколько из них читала модель.

    Пустая карточка разбирается без модели — читать в ней нечего. Считать её
    «разобранной моделью» значит показывать РОПу работу, которой не было, и
    завышать знаменатель, по которому судят о качестве разбора.
    """
    total = int(stats.get("total") or 0)
    analyzed = int(stats.get("analyzed") or 0)
    empty = int(stats.get("empty_cards") or 0)
    by_model = max(0, analyzed - empty)
    line = f"Карточек: {total} · разобрано моделью: {by_model}"
    if empty:
        line += f" · пустых, без модели: {empty}"
    return line


def format_summary(stats: dict[str, Any]) -> str:
    """Шапка отчёта по одной воронке.

    Показывает всё, что прогон посчитал: температуру, вердикты, расхождения,
    сколько карточек не пошло в модель и почему, и во что обошёлся прогон.
    Цифры, оставшиеся только в логе, РОП не увидит.
    """
    temperature = stats.get("temperature") or {}
    verdicts = stats.get("verdicts") or {}
    label = stats.get("funnel_label") or stats.get("funnel") or "Воронка"
    parts = [
        f"━━━ [B]{label.upper()}[/B] ━━━",
        _analyzed_line(stats),
        " | ".join(
            f"{TEMPERATURE_ICON[key]} {TEMPERATURE_RU[key]} "
            f"{int(temperature.get(key) or 0)}"
            for key in ("hot", "warm", "cold", "unknown")
        ),
        "Оценка карточек: " + " | ".join(
            f"{VERDICT_RU[key]} {int(verdicts.get(key) or 0)}"
            for key in (
                "good", "tolerable", "poor", "too_early", "out_of_qc", "no_rules",
            )
        ),
    ]

    # Сверку пересказа с разговором сняли: на портале расшифровка есть у
    # одной карточки из семи, и проверка работала вхолостую. Разговоры
    # по-прежнему читаются моделью как первоисточник, и сколько их читается —
    # видно здесь: карточка без разговора разобрана по одному пересказу.
    with_calls = int(stats.get("cards_with_call") or 0)
    readable = int(stats.get("cards_with_transcript") or 0)
    total = int(stats.get("total") or 0)
    # Знаменатель — карточки, таймлайн которых мы прочитали. Считали же
    # числитель только по ним: непрочитанная карточка не может дать звонок,
    # и делить одно на другое значит занижать долю тем сильнее, чем хуже
    # отвечал портал. Старые прогоны ключа не знают — там остаётся total.
    read = int(stats.get("cards_read") or 0) or total
    if read:
        # Сначала звонки, потом расшифровки: после снятия сверки главное
        # доказательство работы — сам факт разговора, а не его текст. Одна
        # строка «разговор читается у 0 из 10» читалась как «звонков не
        # было», хотя звонки были и ни один не расшифрован.
        # «За всё время» — не украшение. Счётчик считает по всей истории
        # карточки, а строка внутри карточки — по норме этапа, и без этих
        # трёх слов они читаются как спор. Прогон 31.08: шапка «звонки есть
        # у 1 из 10», #10994 — «ни звонка, ни комментария брокера 106 дн.».
        # Оба верны: разговор был, но раньше, чем сто шесть дней назад.
        line = (
            f"📞 Звонки есть у {with_calls} из {read} карточек за всё время"
            if read == total
            else f"📞 Звонки есть у {with_calls} из {read} прочитанных "
                 f"за всё время (всего {total})"
        )
        if readable != with_calls:
            line += f", разговор читается у {readable}"
        pending = int(stats.get("transcripts_pending") or 0)
        tails = []
        if pending:
            # Считаем разговоры, а не карточки: «ещё 7 расшифровок не готово»
            # рядом с «звонки есть у 1 из 10» читалось как «ещё у семи
            # карточек звонки есть», хотя все семь записей — с той же одной.
            tails.append(f"не расшифровано разговоров: {pending}")
        failed = int(stats.get("transcripts_failed") or 0)
        if failed:
            # Наша ошибка не должна выглядеть как задержка Битрикса.
            tails.append(f"не загрузилось разговоров: {failed}")
        if tails:
            line += " (" + ", ".join(tails) + ")"
        parts.append(line)

    silent = int(stats.get("cards_without_a_call") or 0)
    if silent:
        parts.append(
            "📵 Работа только на словах брокера "
            f"(звонка за окно этапа нет): {silent}",
        )

    agents = int(stats.get("agent_cards") or 0)
    if agents:
        # Агент — не клиент: он не остывает, и мерить его тем же, чем живого
        # покупателя, нельзя. Строка нужна, чтобы видеть, сколько таких в
        # выборке, и поправить разметку, если агентов узнали неверно.
        parts.append(f"👤 Карточек с агентом, а не клиентом: {agents}")

    unrecoverable = int(stats.get("unrecoverable") or 0)
    if unrecoverable:
        empty = int(stats.get("empty_cards") or 0)
        tails = []
        # Лид, заведённый два часа назад, пуст не по вине брокера — мы это
        # уже признали вердиктом «рано судить». Без оговорки «неинформативных
        # 8 из 10» на выборке свежих лидов читается как претензия к людям.
        young = int(stats.get("unrecoverable_too_early") or 0)
        if young >= unrecoverable:
            tails.append("все моложе отсрочки — судить рано")
        elif young:
            tails.append(f"{young} моложе отсрочки — {too_early_tail(young)}")
        if empty:
            tails.append(f"полностью пустых: {empty}")
        tail = f" ({', '.join(tails)})" if tails else ""
        parts.append(f"⚠️ Неинформативных карточек: {unrecoverable}{tail}")

    skipped = []
    if stats.get("skipped_unchanged"):
        skipped.append(f"без изменений {int(stats['skipped_unchanged'])}")
    if stats.get("skipped_out_of_qc"):
        skipped.append(f"этап вне контроля {int(stats['skipped_out_of_qc'])}")
    if stats.get("skipped_incomplete"):
        skipped.append(f"карточка не прочитана {int(stats['skipped_incomplete'])}")
    if stats.get("errors"):
        skipped.append(f"ошибок {int(stats['errors'])}")
    if skipped:
        parts.append("Не разбиралось моделью: " + ", ".join(skipped))

    stage_line = format_stage_mix(stats.get("stages") or {})
    if stage_line:
        parts.append(stage_line)

    source_line = format_source_mix(stats.get("sources") or {})
    if source_line:
        parts.append(source_line)

    # Если возраст этапа неизвестен, отсрочка не применяется и вердикты
    # смещены в сторону «плохо». Молчать об этом нельзя: РОП примет завышенную
    # строгость за реальное качество работы брокеров.
    # Пробел в правилах — это наша недоделка, и молчать о ней нельзя:
    # иначе карточки годами лежат «неоценёнными» и это выглядит нормой.
    gaps = stats.get("stages_without_rules") or {}
    if gaps:
        listed = ", ".join(
            f"{stage_name(code)} {count}"
            for code, count in sorted(gaps.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        parts.append(f"📋 Правила полноты не заданы для этапов: {listed}")

    unknown_age = int(stats.get("stage_age_unknown") or 0)
    if unknown_age:
        parts.append(
            f"❗ У {unknown_age} карточек неизвестен возраст этапа — "
            "отсрочка не применялась, оценка строже реальной",
        )

    parts.append(
        f"💰 Стоимость: {float(stats.get('cost_rub') or 0.0):.2f} ₽ "
        f"({float(stats.get('cost_rub_per_card') or 0.0):.3f} ₽ за карточку)",
    )
    breakdown = cost_breakdown(stats)
    if breakdown:
        parts.append(breakdown)
    return "\n".join(parts)


def cost_breakdown(stats: dict[str, Any]) -> str:
    """Из чего сложился счёт: размышления и кэш входа.

    Обе цифры лежали только в JSON прогона, и увидеть их можно было, лишь
    открыв файл. А решают они многое: размышления тарифицируются по цене
    выхода — впятеро дороже входа, — и на разборе 26.08 составили 45 %
    счёта. Кэш входа вдесятеро дешевле обычного; ноль в этой графе значит,
    что постоянная часть запроса каждый раз оплачивается заново.
    """
    usage = stats.get("usage") or {}
    output = int(usage.get("output_tokens") or 0)
    total_input = int(usage.get("input_tokens") or 0)
    if not output and not total_input:
        return ""
    reasoning = int(usage.get("reasoning_tokens") or 0)
    cached = int(usage.get("cached_tokens") or 0)
    bits = []
    if output:
        # Размышления уже внутри output_tokens и стоят столько же.
        share = reasoning / output * 100.0
        bits.append(f"размышления {reasoning} из {output} ток. ответа ({share:.0f} %)")
    if total_input:
        bits.append(f"кэш входа {cached / total_input * 100.0:.0f} %")
    return "🧠 " + " · ".join(bits)


def stage_name(code: str) -> str:
    """Человеческое имя этапа; неизвестный код показываем как есть."""
    return BUYERS_STAGE_NAMES.get(code) or SELLERS_STAGE_NAMES.get(code) or code


def format_stage_mix(stages: dict[str, int]) -> str:
    """Состав выборки по этапам.

    Без этой строки перекос выборки невидим: прогон по самым свежим карточкам
    и прогон по самым залежавшимся дают одинаково бессодержательный итог
    («всё рано судить» / «всё плохо»), и отличить их можно только по этапам.
    """
    if not stages:
        return ""
    ranked = sorted(stages.items(), key=lambda kv: (-kv[1], kv[0]))
    return "Этапы выборки: " + ", ".join(
        f"{stage_name(code)} {count}" for code, count in ranked
    )


# ── Два раздела: клиент уходит / брокер не дорабатывает ────────────────
# Разрывы, которые и означают «с клиентом не ведётся работа». Определение
# агентства от 28.08 дословно: «Теряем клиента — это когда с ним не ведётся
# работа от брокера: не пишутся комментарии, не планируются дела, нет
# исходящих звонков, или брошен на этапе долгое время».
#
# Отсюда ровно два кода, и каждый проверяется по буквам определения:
#   GAP_NO_TRACE   — следов нет вовсе: ни комментария, ни дела, ни звонка;
#   GAP_ABANDONED  — «брошен на этапе долгое время».
#
# Чего здесь намеренно нет:
#   GAP_NO_TRACE_IN_WINDOW — след ЕСТЬ, он просто старше нормы этапа. Решение
#                         агентства от 31.08: это отставание по темпу, а не
#                         потеря. #16734 — брокер писал 4 дня назад при норме
#                         2 и дело поставил (пусть и просроченное): два звена
#                         конъюнкции целы, а карточка стояла в 🚨. Норма этапа
#                         отвечает на вопрос «свежая ли работа», раздел — на
#                         вопрос «ведём ли мы клиента вообще», и подменять
#                         второй первым значит снова свалить всё в один
#                         список, из которого РОП не выбирает.
#   GAP_ONLY_PLANS      — дело ПОСТАВЛЕНО, значит «не планируются дела» про эту
#                         карточку неправда. Претензия к тексту дела, не потеря.
#   GAP_EMPTY_COMMENT,
#   GAP_CLAIMED_*       — комментарий написан, пусть и слабый. Это недоработка.
#   разрывы-напоминания — до этого правила они вообще не доходят: решение
#                         агентства ставит напоминание раньше квалификации.
LOSING_GAPS = frozenset({GAP_NO_TRACE, GAP_ABANDONED})


def _is_losing_client(state: dict[str, Any]) -> bool:
    """Ведётся ли по карточке работа с клиентом — или мы его теряем.

    Правило держится на том, что в карточке есть, а не на ярлыке
    температуры. Так было не всегда: до 28.08 в тревогу вели «клиент остыл»,
    «клиент горячий, а работа не подтверждена» и «картину не восстановить» —
    три ветки с тремя исключениями, накопленными за один день. Каждое
    исключение по отдельности было верным, а вместе они спорили друг с
    другом, и объяснить РОПу, почему карточка в тревоге, стало нельзя.
    Температура осталась в отчёте ярлыком: она говорит о клиенте, а тревога —
    о работе брокера, и это разные вопросы.

    Внутри отсрочки этапа тревоги нет. Лид, заведённый сутки назад, пуст
    потому, что брокер ещё не работал — мы это уже признали вердиктом «рано
    судить», и тащить ту же карточку в тревожный раздел значит сказать
    двумя строками противоположное.

    Кроме брошенных. Отсрочка считается от входа НА ЭТАП, а заброшенность —
    от последнего следа в карточке, и сделка, переставленная на новый этап
    час назад после ста дней тишины, получает «рано судить» и «брошена»
    разом. Про сто дней рано не бывает: иначе отчёт печатает «ни одной
    карточки с признаками потери» прямо над списком брошенных.

    Стоящее дело тревогу снимает. Определение агентства — это конъюнкция:
    не пишутся комментарии И не планируются дела И нет звонков. Дело на
    контроле рвёт её вторым звеном, и неважно, когда его поставили: окно
    этапа отвечает на вопрос «свежая ли работа», а раздел — на вопрос
    «ведём ли мы клиента вообще».

    Прогон 31.08 показал цену этой разницы: девять продавцов из десяти
    ушли в «теряем», и у трёх из них дело стояло на 1 сентября. Претензия
    к ним верна — за норму этапа в карточке ничего нет, — но это
    недоработка, а не потеря, и раздел должен их различать. Иначе «теряем
    клиента» снова становится общим списком, из которого РОП не выбирает.

    Тем же вечером выяснилось, что и стоящего дела для этого мало.
    Отставание за норму этапа (GAP_NO_TRACE_IN_WINDOW) вело в тревогу само
    по себе — то есть карточка, где брокер писал четыре дня назад при норме
    два, объявлялась потерей клиента. Конъюнкция там цела дважды:
    комментарии пишутся, дело поставлено. Решение агентства от 31.08:
    потеря — это «нет вообще ничего» или «брошен», а отставание по темпу
    остаётся недоработкой (см. LOSING_GAPS).
    """
    work = state.get("work_evidence") or {}
    reason = str(work.get("reason") or "")
    if reason == GAP_ABANDONED:
        # У брошенной дела на контроле нет по построению (см. assess_broker_work),
        # так что проверка ниже её и не тронула бы.
        return True
    if str(state.get("verdict") or "") == "too_early":
        return False
    if str(state.get("scheduled_task_at") or "").strip():
        return False
    return reason in LOSING_GAPS


def split_sections(
    results: list[dict[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Теряем / брошены / недоработка / напомнить / рано судить / в работе.

    Разделение по зоне ответственности, а не по строгости. «Клиент остыл» —
    забрать себе и решать; «брокер не подтвердил работу» — спросить с брокера.
    Смешивать их в один список значит заставить РОПа сортировать вручную то,
    что уже известно.

    Карточка может попасть и в теряем, и в недоработку: клиент остывает ИМЕННО
    потому, что с ним не работают, и прятать одну половину этой связки нельзя.

    «Рано судить» — отдельный список, а не «в работе». Карточка, заведённая
    сутки назад и ещё пустая, — не повод для тревоги, но и галочку ✅ ей
    ставить нельзя: работа по ней не началась.

    «Напомнить» — тоже не «в работе» и не «недоработка», и с 28.08 не
    «теряем» тоже: карточка-напоминание уходит в свой раздел раньше, чем её
    начинают квалифицировать, и в другие разделы не попадает. Это
    единственный исключающий раздел. Остальные три — 🚨, 🕸 и 🔧 —
    намеренно пересекаются: горячая брошенная карточка должна стоять и в
    тревоге, и среди брошенных, а печатается всё равно один раз (см.
    printed/«см. выше» в format_sections).
    """
    losing: list[dict[str, Any]] = []
    abandoned: list[dict[str, Any]] = []
    neglected: list[dict[str, Any]] = []
    reminders: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    fine: list[dict[str, Any]] = []
    for result in results:
        state = result.get("state") or {}
        if not state:
            continue
        work = state.get("work_evidence") or {}
        reason = str(work.get("reason") or "")
        # Напоминание — первым и до конца. Решение агентства от 28.08:
        # «надо чтобы ему было напоминание, а только потом квалификация».
        # Раньше порядок был обратный, и холодный клиент с записанной паузой
        # уезжал в 🚨 ТЕРЯЕМ, хотя по лестнице ему полагалось 🔔: карточка не
        # доходила до этой ветки, её забирала тревога.
        if bool(work) and reason in WORK_REMINDERS:
            reminders.append(result)
            continue
        is_losing = _is_losing_client(state)
        # Брошенная карточка — не отставание от каденса, а вопрос, ведём ли
        # мы эту сделку. В общем списке недоработок она теряется.
        is_abandoned = reason == GAP_ABANDONED
        # `not is_reminder` тут больше не нужен — напоминание до этой строки
        # не доходит, — но условие оставлено страховкой: ослабнет ранний
        # выход, и 🔔 бесшумно вольётся в 🔧, то есть напоминание снова
        # станет претензией.
        # «Работу не начинали» из недоработки тоже исключаем — решение
        # агентства от 31.08, тем же правилом, что и брошенные. Прогон 12:55:
        # #17100 стоял разом в 🚨, 🆕 и 🔧, то есть одна карточка под тремя
        # заголовками. Претензия при этом никуда не делась — она названа
        # своим разделом, и повторять её нечем.
        #
        # Условие связано с тревогой, а не с одним кодом разрыва: раздел 🆕
        # собирается из `losing`, и без этой связки карточка со следов-нет
        # плюс стоящим делом не попадала бы никуда — ни в 🆕, ни в 🔧, —
        # то есть тихо уезжала бы в «в работе». В живых данных такой пары
        # быть не может (дело брокера само по себе след), но правило,
        # держащееся на «этого не бывает», ломается первым.
        is_not_started = reason == GAP_NO_TRACE and is_losing
        is_neglected = (
            bool(work) and not work.get("proven")
            and reason not in WORK_REMINDERS
            and not is_abandoned and not is_not_started
        )
        if is_losing:
            losing.append(result)
        if is_abandoned:
            abandoned.append(result)
        if is_neglected:
            neglected.append(result)
        if is_losing or is_abandoned or is_neglected:
            continue
        if str(state.get("verdict") or "") == "too_early":
            waiting.append(result)
        else:
            fine.append(result)
    # Худшее — первым: список читают сверху, и сделка, брошенная сто дней
    # назад, должна стоять раньше брошенной месяц.

    def _quiet(row: dict[str, Any]) -> float:
        return float(
            ((row.get("state") or {}).get("work_evidence") or {}).get(
                "abandoned_days",
            ) or 0.0,
        )

    abandoned.sort(key=_quiet, reverse=True)
    # 🚨 печатает полный разбор первым, а 🕸 после него — строками «см. выше».
    # Значит сортировка брошенных видна читателю только здесь: без неё
    # карточка, брошенная сто дней, оказывалась ниже брошенной месяц просто
    # потому, что пришла позже во входном списке.
    losing.sort(key=_quiet, reverse=True)
    return losing, abandoned, neglected, reminders, waiting, fine


def format_sections(
    results: list[dict[str, Any]],
    titles: dict[int, str],
    webhook_url: str,
) -> str:
    """Тело отчёта: сначала где теряем клиента, потом где не дорабатывают."""
    (
        losing, abandoned, neglected, reminders, waiting, fine,
    ) = split_sections(results)
    blocks: list[str] = []
    printed: set[int] = set()
    unread = unread_cards(results)
    # Оговорка для пустых разделов: «ни одной» и «по всем» верны только про
    # то, что мы прочитали. Молчать об остальном — значит выдать непрочитанное
    # за проверенное.
    caveat = (
        f" Не прочитано карточек: {len(unread)}." if unread else ""
    )

    def _block(header: str, rows: list[dict[str, Any]], empty: str) -> None:
        if rows and all(int(r.get("deal_id") or 0) in printed for r in rows):
            # Весь раздел уже напечатан выше. Раньше он выходил столбиком
            # строк «— см. выше»: заголовок с числом, а под ним ни одного
            # довода. Прогон 31.08 довёл это до предела — у покупателей и
            # 🕸, и 🔧 состояли из одних отсылок, то есть пять карточек
            # стояли под тремя заголовками, два из которых пустые.
            # Решение агентства от 31.08: свернуть в строку. Счёт и состав
            # остаются проверяемыми, разбор читается один раз.
            ids = ", ".join(
                f"#{int(r.get('deal_id') or 0)}" for r in rows
            )
            blocks.append(f"[B]{header}[/B]: {ids} (разбор выше)")
            blocks.append("")
            return
        blocks.append(f"[B]{header}[/B]")
        if not rows:
            blocks.append(empty)
            blocks.append("")
            return
        for row in rows:
            deal_id = int(row.get("deal_id") or 0)
            title = titles.get(deal_id, "")
            if deal_id in printed:
                # Карточка уже напечатана разбором выше. Повторять её целиком
                # значит удвоить отчёт ради строки, которую читатель только
                # что прочёл.
                blocks.append(f"#{deal_id} {card_title(title)} — см. выше".strip())
                blocks.append("")
                continue
            printed.add(deal_id)
            blocks.append(format_card(row, title, webhook_url))
            blocks.append("")

    # Та же оговорка, что и у недоработок: холод внутри отсрочки тревогу
    # больше не поднимает (#17080), значит «ни одной с признаками» верно
    # только про карточки, которые мы взялись судить.
    #
    # Считаем по вердикту, а не по разделу «рано судить». Прогон 28.08
    # 13:37: у продавцов таких карточек две, но #15682 попала в
    # «недоработку» — у неё просрочено дело, — и оговорка сказала «по 1
    # карточке» при «рано судить 2» в шапке. Два ответа на один вопрос в
    # одном отчёте.
    #
    # Единственное исключение — брошенные: про сто дней тишины «рано» не
    # бывает, и по ним мы как раз высказались. Считать их «отложенными»
    # значит написать «судить ещё рано» и «брошена 200 дн.» об одной
    # карточке в одном отчёте.
    young = too_early_tail(
        sum(
            1 for r in results
            if str(((r.get("state") or {}).get("verdict")) or "") == "too_early"
            and str((((r.get("state") or {}).get("work_evidence")) or {}).get(
                "reason",
            ) or "") != GAP_ABANDONED
        ),
    )
    # Оговорка про 🆕 считается здесь, а применяется ниже: с 31.08 такие
    # карточки из тревоги уходят, и пустое «ни одной с признаками потери»
    # над списком из шести карточек, где брокер не сделал ничего, было бы
    # тем же отсутствием вердикта, выданным за вердикт.
    fresh_count = sum(
        1 for r in results
        if str(((((r.get("state") or {}) or {}).get("work_evidence")) or {}).get(
            "reason",
        ) or "") == GAP_NO_TRACE
    )
    no_loss_tails = [tail for tail in (
        young,
        (
            f"по {fresh_count} {cards_noun(fresh_count)} работу не начинали "
            "— ниже"
            if fresh_count else ""
        ),
    ) if tail]
    no_loss = (
        "Ни одной карточки с признаками потери; " + "; ".join(no_loss_tails) + "."
        if no_loss_tails else "Ни одной карточки с признаками потери."
    )
    # «Брошен на этапе долгое время» — половина определения потери, поэтому
    # брошенные карточки считаются и здесь, и в своём разделе. Печатаются они
    # один раз (см. printed ниже), но цифра в двух заголовках без объяснения
    # выглядит как двойной счёт. Называем пересечение прямо: заголовок,
    # который нельзя сверить с телом, ничем не лучше отсутствующего.
    overlap = ""
    # Считаем пересечение, а не длину списка брошенных: брошенная карточка
    # внутри отсрочки этапа в тревогу не идёт, и «из них N» про неё было бы
    # цифрой, которую в разделе не найти.
    losing_ids = {id(r) for r in losing}
    shared = sum(1 for r in abandoned if id(r) in losing_ids)
    # Карточки, к которым брокер не притрагивался. Тревогу они поднимают по
    # букве определения (ни комментариев, ни дел, ни звонков), но вести там
    # ещё некого: прогон 12:30 — все четыре продавца в 🚨 оказались строками
    # из реестра и лидом колл-центра. Решение агентства от 31.08: сказать
    # прямо, что работу не начинали, и дать им свой список.
    #
    # Решение агентства от 31.08: «не дублировать, оставить что работу не
    # начинали». Прогон 14:01 показал предел прежнего вида — у продавцов 🚨
    # и 🆕 совпали шестью карточками из шести: тревога печатала разбор, а
    # список ниже повторял её состав слово в слово. Теперь такие карточки
    # уходят из тревоги целиком и печатаются разбором в своём разделе.
    # Каждая — под одним заголовком, и «теряем клиента» снова означает
    # ровно то, что говорит.
    not_started = [
        r for r in losing
        if str((((r.get("state") or {}).get("work_evidence")) or {}).get(
            "reason",
        ) or "") == GAP_NO_TRACE
    ]
    if not_started:
        fresh = {id(r) for r in not_started}
        losing = [r for r in losing if id(r) not in fresh]
        # Пересечение с брошенными считалось по прежнему списку: карточка,
        # ушедшая в 🆕, из тревоги вышла, и цифру «из них N» по ней в
        # разделе было бы не найти.
        losing_ids = {id(r) for r in losing}
        shared = sum(1 for r in abandoned if id(r) in losing_ids)
    if shared:
        overlap = (
            f"Из них {shared} {abandoned_noun(shared)} — отдельным списком ниже."
        )

    _block(
        f"🚨 ТЕРЯЕМ КЛИЕНТА — {len(losing)}", losing, no_loss + caveat,
    )
    if overlap:
        blocks.append(overlap)
        blocks.append("")
    if abandoned:
        # Раньше недоработок: месяц тишины срочнее, чем отставание на три дня.
        _block(f"🕸 БРОШЕНЫ — {len(abandoned)}", abandoned, "")
    if not_started:
        # Свой раздел, и с 31.08 — с полным разбором: в тревоге этих карточек
        # больше нет, печатать их больше негде. Разговор с брокером тут
        # другой: не «верните клиента», а «начните работать».
        _block(
            f"🆕 РАБОТУ НЕ НАЧИНАЛИ — {len(not_started)}", not_started, "",
        )
    # «Работа подтверждена по всем прочитанным карточкам» на выборке, где по
    # всем карточкам судить ещё рано, — то же самое отсутствие вердикта,
    # выданное за вердикт. Прогон 28.08 12:08: двадцать свежих лидов, ни
    # одного разбора работы, и обе воронки отрапортовали «подтверждена».
    # И вторая оговорка — про напоминания. У всех разрывов-напоминаний
    # work_evidence.proven = False: незакрытый вопрос по карточке есть, просто
    # предъявлять за него не за что. «Работа подтверждена по всем карточкам»
    # рядом с разделом 🔔 на десять сделок — то же самое отсутствие вердикта,
    # выданное за вердикт.
    tails = [tail for tail in (
        young,
        (
            f"по {len(reminders)} {cards_noun(len(reminders))} — напоминание ниже"
            if reminders else ""
        ),
        # Раздел 🕸 стоит выше и виден читателю. «Работа подтверждена по всем
        # карточкам» прямо под списком брошенных — самоопровержение.
        (
            f"{len(abandoned)} {abandoned_noun(len(abandoned))} — выше"
            if abandoned else ""
        ),
        # То же и с 🆕: с 31.08 такие карточки в недоработку не идут, и
        # пустой раздел под их списком снова заявлял бы «работа подтверждена
        # по всем карточкам». Оговорка обязана появляться вместе с
        # исключением — иначе исключение превращается в неправду.
        (
            f"по {len(not_started)} {cards_noun(len(not_started))} "
            "работу не начинали — выше"
            if not_started else ""
        ),
    ) if tail]
    no_shortfall = (
        "Недоработок нет; " + "; ".join(tails) + "."
        if tails else "Работа подтверждена по всем прочитанным карточкам."
    )
    _block(
        f"🔧 НЕДОРАБОТКА БРОКЕРА — {len(neglected)}", neglected,
        no_shortfall + caveat,
    )
    if reminders:
        # Не претензия, а напоминание: ход за контрагентом, и вернуться к
        # разговору нечем. Отдельно от недоработки — иначе брокер получает
        # выговор за то, что клиент уехал до сентября.
        _block(f"🔔 НАПОМНИТЬ БРОКЕРУ — {len(reminders)}", reminders, "")

    def _one_liners(header: str, rows: list[dict[str, Any]]) -> None:
        """Карточки без претензий — строкой: клиент, температура, шаг.

        Полный разбор по ним у РОПа не спрашивают, а четыре экрана текста
        про здоровые сделки топят те две, ради которых отчёт открывали.
        """
        if not rows:
            return
        blocks.append(f"[B]{header} — {len(rows)}[/B]")
        for row in rows:
            deal_id = int(row.get("deal_id") or 0)
            state = row.get("state") or {}
            icon = TEMPERATURE_ICON.get(
                str(state.get("temperature") or ""), "",
            )
            step = describe_next_step(state)
            work = state.get("work_evidence") or {}
            # Карточка молчит восемь дней и стоит с галочкой ✅ — без
            # объяснения это выглядит как просмотренная недоработка. Пауза
            # названа в карточке, значит её надо показать.
            pause = (
                f" · ⏸ пауза до {work.get('pause_until')}"
                if str(work.get("reason") or "") == PROVEN_BY_PAUSE
                and str(work.get("pause_until") or "unknown") != "unknown"
                else (
                    " · ⏸ пауза объяснена"
                    if str(work.get("reason") or "") == PROVEN_BY_PAUSE
                    else ""
                )
            )
            # ✅ на карточке, которую тот же отчёт двумя разделами выше
            # назвал «плохо», читается как одобрение. Раздел говорит о работе
            # брокера, вердикт — о заполнении карточки; смешивать их в одну
            # галочку нельзя. #15342: шага нет, цели нет, оценка «плохо».
            poor = (
                " · карточка заполнена плохо"
                if str(state.get("verdict") or "") == "poor" else ""
            )
            # Карточка без единого разговора и без комментария не должна
            # стоять с безмолвной зелёной галочкой: засчитали её по тексту
            # дела, и это надо сказать — иначе ✅ читается как «звонили».
            planned = (
                " · 🗓 план описан в деле"
                if str(work.get("reason") or "") == PROVEN_BY_TASK_PLAN else ""
            )
            # Полный разбор печатается только для проблемных разделов, а
            # «⚠️ Карточка неинформативна» и «🚨 Работу видно, а клиента — нет»
            # живут именно там. С 28.08 такая карточка уходит в ✅ (работа
            # ведётся — значит не теряем) и уносила обе строки с собой: шапка
            # считала неинформативные карточки, которых в теле было не найти,
            # а сам пробел исчезал из отчёта. Пробел остался — он просто не
            # тревога: сделку по такой карточке не подхватит никто.
            blind = ""
            if state.get("recoverable") is False:
                blind = (
                    " · ⚠️ сделку по карточке не подхватить"
                    if str(work.get("reason") or "") in PROVEN
                    else " · ⚠️ картину клиента не восстановить"
                )
            silent = " · 📵 без звонка" if state.get("no_call") else ""
            # Шапка считает агентские карточки, а найти их в теле было
            # нельзя: полный разбор метку печатает, однострочник — нет.
            # Прогон 28.08 12:58: «👤 Карточек с агентом: 6», в теле видна
            # одна, остальные пять — в «рано судить». Цифра, которую нечем
            # проверить, ничем не лучше отсутствующей; к тому же агент
            # судится другим правилом температуры, и знать, что перед
            # тобой агент, нужно до чтения оценки.
            party = state.get("counterparty")
            agent = (
                " · 👤 агент"
                if isinstance(party, dict) and str(party.get("who") or "") == "agent"
                else ""
            )
            blocks.append(
                f"{icon} #{deal_id} {card_title(titles.get(deal_id))} — "
                f"{step}{agent}{pause}{planned}{silent}{blind}{poor}".strip(),
            )
        blocks.append("")

    _one_liners("⏳ РАНО СУДИТЬ", waiting)
    _one_liners("✅ В РАБОТЕ", fine)

    if unread:
        # Отдельный список, а не строка в шапке: РОПу нужно знать, какие
        # именно сделки прогон не видел. Иначе «всё хорошо» относится и к ним.
        blocks.append(f"[B]👁 НЕ ПРОЧИТАНЫ — {len(unread)}[/B]")
        for row in unread:
            deal_id = int(row.get("deal_id") or 0)
            reason = str(row.get("reason") or "")
            blocks.append(
                f"#{deal_id} {card_title(titles.get(deal_id))} — "
                f"{REASON_RU.get(reason, reason)}".strip(),
            )
        blocks.append("")
    return "\n".join(blocks).strip()


# Известные коды источников из основного аудита. Живые имена подтягивает
# fetch_source_names(); этот словарь — запасной вариант, когда Битрикс не
# ответил, и заодно документация на коды, вокруг которых настроена холодная база.
SOURCE_NAMES: dict[str, str] = dict(SELLERS_PAID_SOURCE_NAMES)


def set_source_names(names: dict[str, str]) -> None:
    """Подставить живые имена источников, прочитанные из Битрикса."""
    SOURCE_NAMES.update({str(k): str(v) for k, v in names.items() if k and v})


def source_name(code: str) -> str:
    """Человеческое имя источника; неизвестный код показываем как есть."""
    if not code:
        return "без источника"
    return SOURCE_NAMES.get(code, code)


def merge_source_names(sources: dict[str, int]) -> dict[str, int]:
    """Свести источники, различающиеся только регистром, в один.

    На портале «усачев» и «Усачев» — два разных кода с одинаковым по сути
    именем. Отчёт показывал их порознь, и один канал делился на два: шесть
    карточек и одна вместо семи. Побеждает то написание, которым источник
    заводили чаще; при равенстве — с заглавной буквы, чтобы строка не
    менялась от прогона к прогону.
    """
    groups: dict[str, dict[str, int]] = {}
    for code, count in sources.items():
        name = source_name(code)
        groups.setdefault(name.casefold(), {})[name] = (
            groups.setdefault(name.casefold(), {}).get(name, 0) + int(count)
        )
    merged: dict[str, int] = {}
    for variants in groups.values():
        winner = sorted(variants.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        merged[winner] = sum(variants.values())
    return merged


def format_source_mix(sources: dict[str, int]) -> str:
    """Состав выборки по источникам.

    Контакт, пришедший по платному каналу и не отработанный, стоит агентству
    денег дважды. Строка показывает, из какого канала пришло то, что лежит
    в отчёте.
    """
    if not sources:
        return ""
    merged = merge_source_names(sources)
    ranked = sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))
    listed = ", ".join(f"{name} {count}" for name, count in ranked)
    return f"Источники выборки: {listed}"
