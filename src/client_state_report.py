"""Отчёт РОПу по состоянию клиентов: русские значения, кэш вместо пропусков.

Модель отвечает служебными кодами (warm, medium, broker), а отчёт читают люди.
Перевод живёт здесь, а не в промпте: словарь правится без изменения запроса к
модели, и ни один кэш от этого не инвалидируется.
"""

from __future__ import annotations

from typing import Any

from broker_work import REASON_RU as WORK_REASON_RU
from broker_work import GAP_ABANDONED
from broker_work import GAP_PAUSE_TASK_TOO_LATE
from broker_work import PROVEN
from broker_work import PROVEN_BY_PAUSE
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
    """
    step = format_next_step(state.get("next_step"))
    if step != NO_STEP_RU:
        return step
    scheduled = str(state.get("scheduled_task_at") or "").strip()
    if not scheduled:
        return step
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
        elif quiet < 1:
            quiet_text = ", последний след сегодня"
        elif quiet < float(work.get("window_days") or 0) + 1:
            # «Норма 7 дн., последний след 7 дн. назад» — читатель вычитает и
            # получает ноль, а мы при этом обвиняем. У самой границы округление
            # до целого превращает верную претензию в арифметическую ошибку,
            # поэтому у границы показываем десятую долю.
            quiet_text = f", последний след {quiet:.1f} дн. назад"
        else:
            quiet_text = f", последний след {quiet:.0f} дн. назад"
        due = work.get("due_task") if isinstance(work.get("due_task"), dict) else None
        if due:
            # У наступившего срока своя арифметика: норма этапа тут ни при
            # чём, спрашивают за конкретное дело и конкретную дату.
            overdue = int(due.get("days_overdue") or 0)
            tail = (
                f"срок {due.get('deadline')}, "
                + (f"просрочено на {overdue} дн." if overdue else "срок сегодня")
            )
        else:
            tail = f"норма {work.get('window_days')} дн.{quiet_text}"
        reason_code = str(work.get("reason") or "")
        if reason_code in TIMELESS_GAPS:
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
        elif reason_code == GAP_ABANDONED:
            quiet = work.get("abandoned_days")
            tail = (
                f"ни звонка, ни комментария {float(quiet):.0f} дн."
                if isinstance(quiet, (int, float)) else "месяцы без следов"
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
        else:
            head = "🔧 Работа не подтверждена"
        # Напоминание печатается без скобок: норма этапа там не довод.
        # Но у разрыва со своим доводом скобки и есть вся проверяемость —
        # «вернуться собрался позже» без двух дат оспорить нечем.
        drop_tail = reason_code in WORK_REMINDERS and reason_code not in SELF_ARGUED_GAPS
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
        # proven=True бывает и там, где судить ещё рано: отсрочка этапа
        # возвращает «подтверждено», хотя работы не видели вовсе. Спрашиваем
        # не про флаг, а про причину: работу видно тогда, когда её нашли.
        if str((state.get("work_evidence") or {}).get("reason") or "") in PROVEN:
            # #16886 стояла в «теряем клиента» без единой строки о том, почему.
            # Температура «неизвестно», претензий к работе нет — и раздел
            # выглядит ошибкой. Причина есть, и её надо назвать: клиент теряется
            # не потому, что с ним не работают, а потому, что работа нигде не
            # записана и подхватить сделку не сможет никто.
            lines.append(
                "🚨 Работу видно, а клиента — нет: по такой карточке "
                "сделку не подхватить, так и теряют молча",
            )

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
        line = (
            f"📞 Звонки есть у {with_calls} из {read} карточек"
            if read == total
            else f"📞 Звонки есть у {with_calls} из {read} прочитанных "
                 f"(всего {total})"
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
            tails.append(f"{young} моложе отсрочки — по ним судить рано")
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
def _is_losing_client(state: dict[str, Any]) -> bool:
    """Признаки, что клиента теряем: остыл, замолчал, картины нет.

    Внутри отсрочки пустая карточка в этот список не попадает. Лид, заведённый
    сутки назад, пуст потому, что брокер ещё не работал — мы это уже признали
    вердиктом «рано судить», и тащить ту же карточку в тревожный раздел значит
    сказать двумя строками противоположное. Остывший клиент остаётся: это
    событие, а не отсутствие данных, и срок ему не оправдание.

    Но только там, где остывание вообще событие. У продавцов «собственник не
    выходит на связь» — обычное начало работы по холодной базе, а не потеря
    (#15594, решение агентства). Решает воронка, и она кладёт ответ в
    state["cold_is_a_loss"].
    """
    if (
        str(state.get("temperature") or "") == "cold"
        and state.get("cold_is_a_loss", True)
    ):
        return True
    work = state.get("work_evidence") or {}
    if str(state.get("temperature") or "") == "hot" and not work.get("proven", True):
        # Горячий клиент, которым не занимаются, — самое дорогое в отчёте.
        # #16066: бюджет 130 млн, согласован шаг, восемь дней тишины при норме
        # три. Такая карточка не должна лежать вторым пунктом среди восьми
        # недоработок.
        return True
    if str(state.get("verdict") or "") == "too_early":
        return False
    if state.get("recoverable") is not False:
        return False
    # Пустая карточка — потеря только тогда, когда брокер работу подтвердил:
    # значит с клиентом говорили, а в карточке этого не видно, и картина
    # клиента уходит. Если работа НЕ подтверждена, это недоработка, и звать её
    # ещё и потерей — писать один факт дважды. Прошлый прогон дал два
    # одинаковых списка по десять карточек, и разделение перестало разделять.
    return bool(work.get("proven", True))


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

    «Напомнить» — тоже не «в работе» и не «недоработка». Ход за контрагентом,
    он сам назвал срок, а дела на возврат к разговору нет. Предъявлять тут
    не за что, но именно так теряются агенты, обещавшие приехать в сентябре.
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
        is_losing = _is_losing_client(state)
        is_reminder = bool(work) and reason in WORK_REMINDERS
        # Брошенная карточка — не отставание от каденса, а вопрос, ведём ли
        # мы эту сделку. В общем списке недоработок она теряется.
        is_abandoned = reason == GAP_ABANDONED
        is_neglected = (
            bool(work) and not work.get("proven")
            and not is_reminder and not is_abandoned
        )
        if is_losing:
            losing.append(result)
        if is_abandoned:
            abandoned.append(result)
        if is_neglected:
            neglected.append(result)
        if is_losing or is_abandoned or is_neglected:
            continue
        if is_reminder:
            reminders.append(result)
        elif str(state.get("verdict") or "") == "too_early":
            waiting.append(result)
        else:
            fine.append(result)
    # Худшее — первым: список читают сверху, и сделка, брошенная сто дней
    # назад, должна стоять раньше брошенной месяц.
    abandoned.sort(
        key=lambda r: float(
            ((r.get("state") or {}).get("work_evidence") or {}).get(
                "abandoned_days",
            ) or 0.0,
        ),
        reverse=True,
    )
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

    _block(
        f"🚨 ТЕРЯЕМ КЛИЕНТА — {len(losing)}", losing,
        "Ни одной карточки с признаками потери." + caveat,
    )
    if abandoned:
        # Раньше недоработок: месяц тишины срочнее, чем отставание на три дня.
        _block(f"🕸 БРОШЕНЫ — {len(abandoned)}", abandoned, "")
    # «Работа подтверждена по всем прочитанным карточкам» на выборке, где по
    # всем карточкам судить ещё рано, — то же самое отсутствие вердикта,
    # выданное за вердикт. Прогон 28.08 12:08: двадцать свежих лидов, ни
    # одного разбора работы, и обе воронки отрапортовали «подтверждена».
    if waiting:
        no_shortfall = (
            f"Недоработок нет; по {len(waiting)} "
            f"{'карточке' if len(waiting) == 1 else 'карточкам'} судить ещё рано."
        )
    else:
        no_shortfall = "Работа подтверждена по всем прочитанным карточкам."
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
            silent = " · 📵 без звонка" if state.get("no_call") else ""
            blocks.append(
                f"{icon} #{deal_id} {card_title(titles.get(deal_id))} — "
                f"{step}{pause}{silent}{poor}".strip(),
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
