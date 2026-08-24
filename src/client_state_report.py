"""Отчёт РОПу по состоянию клиентов: русские значения, кэш вместо пропусков.

Модель отвечает служебными кодами (warm, medium, broker), а отчёт читают люди.
Перевод живёт здесь, а не в промпте: словарь правится без изменения запроса к
модели, и ни один кэш от этого не инвалидируется.
"""

from __future__ import annotations

from typing import Any

from buyer_commission_reminder import deal_url
from client_state import MATERIAL_SEVERITY
from tools import BUYERS_STAGE_NAMES, SELLERS_STAGE_NAMES

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
            for key in ("good", "tolerable", "poor", "too_early", "out_of_qc")
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

    # Если возраст этапа неизвестен, отсрочка не применяется и вердикты
    # смещены в сторону «плохо». Молчать об этом нельзя: РОП примет завышенную
    # строгость за реальное качество работы брокеров.
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
