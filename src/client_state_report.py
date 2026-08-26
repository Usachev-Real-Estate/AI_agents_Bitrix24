"""Отчёт РОПу по состоянию клиентов: русские значения, кэш вместо пропусков.

Модель отвечает служебными кодами (warm, medium, broker), а отчёт читают люди.
Перевод живёт здесь, а не в промпте: словарь правится без изменения запроса к
модели, и ни один кэш от этого не инвалидируется.
"""

from __future__ import annotations

from typing import Any

from broker_work import REASON_RU as WORK_REASON_RU
from buyer_commission_reminder import deal_url
from client_state import MATERIAL_SEVERITY
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

SEVERITY_RU: dict[str, str] = {
    "low": "мелкое",
    "medium": "существенное",
    "high": "грубое",
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
    return f"{what} ({when}, {who})"


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
        f"[B]#{deal_id}[/B] {title}".rstrip(),
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
    lines.append(f"Цель: {humanize(state.get('client_goal'))}")
    lines.append(f"Ситуация: {humanize(state.get('situation'))}")
    lines.append(f"Шаг: {format_next_step(state.get('next_step'))}")

    work = state.get("work_evidence") or {}
    if work and not work.get("proven"):
        quiet = work.get("days_quiet")
        if not isinstance(quiet, (int, float)):
            quiet_text = ", следов нет вовсе"
        elif quiet < 1:
            quiet_text = ", последний след сегодня"
        else:
            quiet_text = f", последний след {quiet:.0f} дн. назад"
        lines.append(
            f"🔧 Работа не подтверждена: "
            f"{WORK_REASON_RU.get(str(work.get('reason')), work.get('reason'))}"
            f" (норма этапа {work.get('window_days')} дн.{quiet_text})",
        )

    if state.get("recoverable") is False:
        lines.append("⚠️ Карточка неинформативна — картину клиента не восстановить")

    for item in state.get("contradictions") or []:
        if not isinstance(item, dict):
            continue
        severity = ru(item.get("severity"), SEVERITY_RU, SEVERITY_RU["medium"])
        lines.append(f"⚡ Расхождение ({severity}): {humanize(item.get('what'))}")
        lines.append(f"   в карточке: «{humanize(item.get('in_card'))}»")
        lines.append(f"   в разговоре: «{humanize(item.get('in_call'))}»")

    missing = [str(m).strip() for m in (state.get("missing") or []) if str(m).strip()]
    if missing:
        lines.append("Не хватает: " + ", ".join(missing))

    if reason in CACHED_REASONS:
        lines.append(f"↻ {REASON_RU[reason]}")

    return "\n".join(lines)


def count_material_contradictions(results: list[dict[str, Any]]) -> int:
    """Существенных расхождений по всем карточкам отчёта."""
    total = 0
    for result in results:
        for item in (result.get("state") or {}).get("contradictions") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("severity", "medium")).lower() in MATERIAL_SEVERITY:
                total += 1
    return total


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
        f"Карточек: {int(stats.get('total') or 0)} · "
        f"разобрано моделью: {int(stats.get('analyzed') or 0)}",
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

    material = int(stats.get("contradictions_material") or 0)
    minor = int(stats.get("contradictions_minor") or 0)
    parts.append(
        f"⚡ Расхождений с разговором: существенных {material}, мелких {minor}",
    )

    unrecoverable = int(stats.get("unrecoverable") or 0)
    if unrecoverable:
        empty = int(stats.get("empty_cards") or 0)
        tail = f" (из них полностью пустых: {empty})" if empty else ""
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
    return "\n".join(parts)


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
    сказать двумя строками противоположное. Остывший клиент и расхождение с
    разговором остаются: это события, а не отсутствие данных, и срок им не
    оправдание.
    """
    if str(state.get("temperature") or "") == "cold":
        return True
    if state.get("contradictions"):
        return True
    if str(state.get("verdict") or "") == "too_early":
        return False
    return state.get("recoverable") is False


def split_sections(
    results: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Разложить карточки на «теряем клиента», «недоработка», «в работе».

    Разделение по зоне ответственности, а не по строгости. «Клиент остыл» —
    забрать себе и решать; «брокер не подтвердил работу» — спросить с брокера.
    Смешивать их в один список значит заставить РОПа сортировать вручную то,
    что уже известно.

    Карточка может попасть в оба раздела: клиент остывает ИМЕННО потому, что
    с ним не работают, и прятать одну половину этой связки нельзя.
    """
    losing: list[dict[str, Any]] = []
    neglected: list[dict[str, Any]] = []
    fine: list[dict[str, Any]] = []
    for result in results:
        state = result.get("state") or {}
        if not state:
            continue
        work = state.get("work_evidence") or {}
        is_losing = _is_losing_client(state)
        is_neglected = bool(work) and not work.get("proven")
        if is_losing:
            losing.append(result)
        if is_neglected:
            neglected.append(result)
        if not is_losing and not is_neglected:
            fine.append(result)
    return losing, neglected, fine


def format_sections(
    results: list[dict[str, Any]],
    titles: dict[int, str],
    webhook_url: str,
) -> str:
    """Тело отчёта: сначала где теряем клиента, потом где не дорабатывают."""
    losing, neglected, fine = split_sections(results)
    blocks: list[str] = []
    printed: set[int] = set()

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
                blocks.append(f"#{deal_id} {title} — см. выше".strip())
                blocks.append("")
                continue
            printed.add(deal_id)
            blocks.append(format_card(row, title, webhook_url))
            blocks.append("")

    _block(
        f"🚨 ТЕРЯЕМ КЛИЕНТА — {len(losing)}", losing,
        "Ни одной карточки с признаками потери.",
    )
    _block(
        f"🔧 НЕДОРАБОТКА БРОКЕРА — {len(neglected)}", neglected,
        "Работа подтверждена по всем карточкам.",
    )
    if fine:
        # Карточки без претензий сжимаются в строку: клиент, температура и
        # следующий шаг. Полный разбор по ним у РОПа не спрашивают, а четыре
        # экрана текста про здоровые сделки топят те две, ради которых
        # отчёт открывали.
        blocks.append(f"[B]✅ В РАБОТЕ — {len(fine)}[/B]")
        for row in fine:
            deal_id = int(row.get("deal_id") or 0)
            state = row.get("state") or {}
            icon = TEMPERATURE_ICON.get(
                str(state.get("temperature") or ""), "",
            )
            step = format_next_step(state.get("next_step"))
            blocks.append(
                f"{icon} #{deal_id} {titles.get(deal_id, '')} — {step}".strip(),
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


def format_source_mix(sources: dict[str, int]) -> str:
    """Состав выборки по источникам.

    Контакт, пришедший по платному каналу и не отработанный, стоит агентству
    денег дважды. Строка показывает, из какого канала пришло то, что лежит
    в отчёте.
    """
    if not sources:
        return ""
    ranked = sorted(sources.items(), key=lambda kv: (-kv[1], kv[0]))
    listed = ", ".join(f"{source_name(code)} {count}" for code, count in ranked)
    return f"Источники выборки: {listed}"
