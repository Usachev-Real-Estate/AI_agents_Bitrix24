#!/usr/bin/env python3
"""Разведка перед запуском ИИ-агента: что реально есть в карточках.

Только чтение. Ничего не меняет в CRM, не обращается к языковой модели
и не сохраняет тексты разговоров — считает объёмы и покрытие.

Отвечает на вопросы, без которых оценка стоимости остаётся гаданием:
  * по скольким звонкам реально отдаётся расшифровка (метод возвращает
    null, если AI-обработка не завершилась);
  * сколько текста приходится на карточку — это и есть размер контекста;
  * во что обойдётся полный проход и обычный день.

Запуск:
    python scripts/probe_transcripts.py --category 18 --limit 50
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from config import get_settings, setup_logging  # noqa: E402
from notify import _bx_call_sync  # noqa: E402
from tools import _as_list, _bx_get_all_sync, _coerce_int  # noqa: E402

CALL_ACTIVITY_TYPE_ID = 2
# Грубая оценка: для русского текста ~3 символа на токен. Считаем консервативно.
CHARS_PER_TOKEN = 3.0


def list_open_deals(category_id: int, limit: int) -> list[dict[str, Any]]:
    """Открытые сделки воронки — только id и заголовок."""
    raw = _bx_get_all_sync(
        "crm.deal.list",
        {
            "filter": {"CATEGORY_ID": category_id, "CLOSED": "N"},
            "select": ["ID", "TITLE", "STAGE_ID", "ASSIGNED_BY_ID"],
            "order": {"ID": "DESC"},
        },
    )
    deals = [d for d in _as_list(raw) if isinstance(d, dict)]
    return deals[:limit]


def list_call_activities(deal_id: int) -> list[dict[str, Any]]:
    """Дела типа «Звонок», привязанные к сделке."""
    raw = _bx_get_all_sync(
        "crm.activity.list",
        {
            "filter": {
                "OWNER_TYPE_ID": 2,
                "OWNER_ID": deal_id,
                "TYPE_ID": CALL_ACTIVITY_TYPE_ID,
            },
            "select": ["ID", "SUBJECT", "CREATED", "DIRECTION", "COMPLETED"],
        },
    )
    return [a for a in _as_list(raw) if isinstance(a, dict)]


def fetch_transcript_length(activity_id: int) -> int | None:
    """Длина расшифровки в символах; None — расшифровки нет.

    Сам текст наружу не отдаётся: скрипту нужен только объём.
    """
    try:
        result = _bx_call_sync(
            "crm.activity.call.getTranscript", {"activityId": activity_id},
        )
    except Exception as exc:  # noqa: BLE001 — разведка не должна падать на одном звонке
        print(f"    ! ошибка по звонку {activity_id}: {exc}", file=sys.stderr)
        return None
    if not result:
        return None
    if isinstance(result, dict):
        text = str(result.get("transcription") or result.get("TRANSCRIPTION") or "")
        return len(text) or None
    return len(str(result)) or None


def comment_chars(deal_id: int) -> int:
    """Суммарный объём комментариев таймлайна карточки."""
    raw = _bx_get_all_sync(
        "crm.timeline.comment.list",
        {
            "filter": {"ENTITY_ID": deal_id, "ENTITY_TYPE": "deal"},
            "select": ["ID", "COMMENT"],
        },
    )
    total = 0
    for item in _as_list(raw):
        if isinstance(item, dict):
            total += len(str(item.get("COMMENT") or ""))
    return total


def probe(category_id: int, limit: int) -> dict[str, Any]:
    deals = list_open_deals(category_id, limit)
    print(f"Воронка {category_id}: разбираем {len(deals)} открытых сделок\n")

    per_deal: list[dict[str, Any]] = []
    calls_total = 0
    calls_with_text = 0

    for index, deal in enumerate(deals, 1):
        deal_id = _coerce_int(deal.get("ID"))
        if deal_id <= 0:
            continue
        activities = list_call_activities(deal_id)
        lengths = []
        for activity in activities:
            calls_total += 1
            length = fetch_transcript_length(_coerce_int(activity.get("ID")))
            if length:
                calls_with_text += 1
                lengths.append(length)

        comments = comment_chars(deal_id)
        transcript_chars = sum(lengths)
        per_deal.append({
            "deal_id": deal_id,
            "calls": len(activities),
            "calls_with_transcript": len(lengths),
            "transcript_chars": transcript_chars,
            "comment_chars": comments,
            "context_chars": transcript_chars + comments,
        })
        print(
            f"[{index}/{len(deals)}] сделка {deal_id}: "
            f"звонков {len(activities)}, с расшифровкой {len(lengths)}, "
            f"текста {transcript_chars + comments} символов"
        )

    return summarize(per_deal, calls_total, calls_with_text)


def summarize(
    per_deal: list[dict[str, Any]],
    calls_total: int,
    calls_with_text: int,
) -> dict[str, Any]:
    contexts = [d["context_chars"] for d in per_deal] or [0]
    coverage = (calls_with_text / calls_total * 100) if calls_total else 0.0
    return {
        "deals_probed": len(per_deal),
        "calls_total": calls_total,
        "calls_with_transcript": calls_with_text,
        "transcript_coverage_pct": round(coverage, 1),
        "deals_without_any_text": sum(1 for c in contexts if c == 0),
        "context_chars_median": int(statistics.median(contexts)),
        "context_chars_max": max(contexts),
        "context_tokens_median": int(statistics.median(contexts) / CHARS_PER_TOKEN),
        "context_tokens_max": int(max(contexts) / CHARS_PER_TOKEN),
    }


def print_report(stats: dict[str, Any], fleet_size: int) -> None:
    median_tokens = stats["context_tokens_median"]
    print("\n" + "=" * 62)
    print("РАЗВЕДКА: что есть в карточках")
    print("=" * 62)
    print(f"Разобрано сделок:            {stats['deals_probed']}")
    print(f"Звонков всего:               {stats['calls_total']}")
    print(f"Из них с расшифровкой:       {stats['calls_with_transcript']} "
          f"({stats['transcript_coverage_pct']} %)")
    print(f"Сделок совсем без текста:    {stats['deals_without_any_text']}")
    print(f"Контекст на карточку, медиана: {stats['context_chars_median']} символов "
          f"(~{median_tokens} токенов)")
    print(f"Контекст на карточку, макс.:   {stats['context_chars_max']} символов "
          f"(~{stats['context_tokens_max']} токенов)")

    print("\nОценка объёма (без цены — подставьте тариф своего провайдера):")
    full = median_tokens * fleet_size
    print(f"  Полный проход по {fleet_size} карточкам: "
          f"~{full / 1_000_000:.1f} млн токенов входа")
    print(f"  Инкрементально, ~10 % карточек в день:   "
          f"~{full * 0.1 / 1_000_000:.2f} млн токенов/день")

    print("\nНа что смотреть:")
    if stats["transcript_coverage_pct"] < 60:
        print("  • Покрытие расшифровок ниже 60 % — агент будет видеть картину")
        print("    неравномерно, и брокеры без расшифровок окажутся в невыгодном")
        print("    положении не по своей вине. Это надо учесть в правилах.")
    else:
        print("  • Покрытие расшифровок достаточное: разговоры можно делать")
        print("    основным источником, а комментарии — вспомогательным.")
    if stats["context_tokens_max"] > 30_000:
        print("  • Есть карточки с очень длинным контекстом. Полная история в")
        print("    каждом запросе недопустима — нужно инкрементальное состояние.")
    if stats["deals_without_any_text"]:
        print(f"  • Карточек совсем без текста: {stats['deals_without_any_text']}.")
        print("    По ним никакой агент состояние не восстановит — это отдельная")
        print("    категория «пусто», а не «плохо описано».")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Разведка расшифровок и объёмов перед запуском ИИ-агента",
    )
    parser.add_argument("--category", type=int, required=True,
                        help="ID воронки: 18 — покупатели, 0 — продавцы")
    parser.add_argument("--limit", type=int, default=50,
                        help="Сколько сделок разобрать (по умолчанию 50)")
    parser.add_argument("--fleet-size", type=int, default=1000,
                        help="Всего открытых карточек — для оценки объёма")
    parser.add_argument("--json", action="store_true", help="Вывести результат как JSON")
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.log_level)

    stats = probe(args.category, args.limit)
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    else:
        print_report(stats, args.fleet_size)


if __name__ == "__main__":
    main()
