#!/usr/bin/env python3
"""Разведка звонков перед выгрузкой досье: сколько их и у скольких есть текст.

Только чтение. Ничего не меняет в CRM, не обращается к языковой модели
и не сохраняет тексты разговоров — считает объёмы и покрытие.

ПОЧЕМУ НЕ ТЕЛЕФОНИЯ. Первым делом сюда просилась voximplant.statistic.get:
она отдаёт CALL_DURATION, TRANSCRIPT_ID и TRANSCRIPT_PENDING одним запросом
на страницу вместо запроса на звонок. На этом портале таблица мертва — 222
записи, последняя 31.07.2026, TRANSCRIPT_ID не заполнен ни у одной, тогда
как crm.activity.list знает 3555 звонков. Звонки регистрирует внешняя АТС
(megapbx) через REST-приложение, и в статистику Воксимпланта она писать
перестала. Опаснее всего, что метод дал бы не пустоту, а ЛОЖНОЕ ОТРИЦАНИЕ:
на десяти последних звонках getTranscript вернул текст у трёх, а таблица
телефонии о них не знает вовсе. Поэтому источник здесь один —
crm.activity.list, а наличие текста проверяется единственным честным
способом: запросом расшифровки.

ЗАЧЕМ ДЛИТЕЛЬНОСТЬ. Расшифровка стоит одного запроса на звонок, и это
самая дорогая часть выгрузки. Но треть звонков длится ноль секунд, а ещё
треть — меньше тридцати: в них нет разговора, и расшифровывать там нечего.
END_TIME − START_TIME приходит вместе со списком дел, бесплатно, и отсекает
эти звонки ДО первого обращения к расшифровкам. Скрипт отвечает, сколько
именно остаётся после отсечки — по всему портфелю, а не по выборке.

Запуск:
    python scripts/probe_transcripts.py --category 18 --category 0
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
from tools import (  # noqa: E402
    _as_list, _bx_get_all_sync, _coerce_int, _parse_datetime,
)

CALL_ACTIVITY_TYPE_ID = 2
DIRECTION_IN = 1
DIRECTION_OUT = 2
# Порог, ниже которого разговора не было. Взят не из головы: на этом портале
# 30 % звонков длятся ровно ноль секунд, ещё 31 % — меньше тридцати секунд.
LONG_CALL_SEC = 60
# Сколько сделок спрашивать одним фильтром. Тот же размер, что у истории
# стадий в tools.py: портал такой список принимает, а запросов выходит в
# сотню раз меньше, чем при обходе по одной карточке.
OWNER_CHUNK = 50


def list_open_deals(category_ids: list[int], limit: int) -> list[dict[str, Any]]:
    """Открытые сделки указанных воронок — только то, что нужно для отбора."""
    deals: list[dict[str, Any]] = []
    for category_id in category_ids:
        raw = _bx_get_all_sync(
            "crm.deal.list",
            {
                "filter": {"CATEGORY_ID": category_id, "CLOSED": "N"},
                "select": ["ID", "STAGE_ID", "CATEGORY_ID", "ASSIGNED_BY_ID"],
                "order": {"ID": "DESC"},
            },
        )
        found = [d for d in _as_list(raw) if isinstance(d, dict)]
        print(f"Воронка {category_id}: открытых сделок {len(found)}")
        deals.extend(found)
    return deals[:limit] if limit > 0 else deals


def _chunks(values: list[int], size: int) -> list[list[int]]:
    return [values[i:i + size] for i in range(0, len(values), size)]


def list_call_activities(deal_ids: list[int]) -> list[dict[str, Any]]:
    """Звонки по списку сделок. Пачками, с откатом на по-сделочный запрос.

    Откат обязателен: пачка падает целиком, и без него один сбойный
    идентификатор стирал бы из разведки полсотни карточек, а отчёт при этом
    выглядел бы успешным.
    """
    out: list[dict[str, Any]] = []
    for chunk in _chunks([d for d in deal_ids if d > 0], OWNER_CHUNK):
        params = {
            "filter": {
                "OWNER_TYPE_ID": 2,
                "OWNER_ID": chunk,
                "TYPE_ID": CALL_ACTIVITY_TYPE_ID,
            },
            "select": [
                "ID", "OWNER_ID", "SUBJECT", "CREATED",
                "START_TIME", "END_TIME", "DIRECTION", "COMPLETED",
            ],
        }
        try:
            out.extend(_as_list(_bx_get_all_sync("crm.activity.list", params)))
            continue
        except Exception as exc:  # noqa: BLE001 — разведка не падает на пачке
            print(f"  ! пачка из {len(chunk)} сделок не прочиталась: {exc}",
                  file=sys.stderr)
        for deal_id in chunk:
            single = dict(params)
            single["filter"] = dict(params["filter"], OWNER_ID=deal_id)
            try:
                out.extend(_as_list(_bx_get_all_sync("crm.activity.list", single)))
            except Exception as exc:  # noqa: BLE001
                print(f"  ! сделка {deal_id}: {exc}", file=sys.stderr)
    return [a for a in out if isinstance(a, dict)]


def call_duration_sec(activity: dict[str, Any]) -> int:
    """Длительность звонка из END_TIME − START_TIME.

    Единственный доступный здесь источник длительности: CALL_DURATION живёт
    в статистике телефонии, а она на этом портале не наполняется. Нет одной
    из границ — считаем нулём: неизвестную длительность нельзя записывать в
    длинные звонки, иначе очередь на расшифровку наберётся из пустых.
    """
    start = _parse_datetime(activity.get("START_TIME"))
    end = _parse_datetime(activity.get("END_TIME"))
    if start is None or end is None:
        return 0
    seconds = int((end - start).total_seconds())
    return seconds if seconds > 0 else 0


def has_transcript(activity_id: int) -> bool | None:
    """Есть ли расшифровка. None — запрос не удался (это не «текста нет»).

    Сам текст наружу не отдаётся: скрипту нужен только факт наличия.
    """
    try:
        result = _bx_call_sync(
            "crm.activity.call.getTranscript", {"activityId": activity_id},
        )
    except Exception as exc:  # noqa: BLE001 — разведка не падает на звонке
        print(f"  ! ошибка по звонку {activity_id}: {exc}", file=sys.stderr)
        return None
    if not result:
        return False
    if isinstance(result, dict):
        text = str(result.get("transcription") or result.get("TRANSCRIPTION") or "")
        return bool(text.strip())
    return bool(str(result).strip())


def _bucket(seconds: int) -> str:
    if seconds <= 0:
        return "0"
    if seconds < 30:
        return "1-29"
    if seconds < LONG_CALL_SEC:
        return "30-59"
    return ">=60"


def probe(
    category_ids: list[int],
    deal_limit: int,
    max_checks: int,
) -> dict[str, Any]:
    deals = list_open_deals(category_ids, deal_limit)
    deal_ids = [_coerce_int(d.get("ID")) for d in deals]
    print(f"Разбираем {len(deal_ids)} сделок\n")

    activities = list_call_activities(deal_ids)
    print(f"Звонков по этим сделкам: {len(activities)}\n")

    buckets: dict[str, int] = {"0": 0, "1-29": 0, "30-59": 0, ">=60": 0}
    durations: list[int] = []
    long_calls: list[dict[str, Any]] = []
    for activity in activities:
        seconds = call_duration_sec(activity)
        durations.append(seconds)
        buckets[_bucket(seconds)] += 1
        if seconds >= LONG_CALL_SEC:
            long_calls.append(activity)

    checked = long_calls if max_checks <= 0 else long_calls[:max_checks]
    print(f"Спрашиваем расшифровку по {len(checked)} длинным звонкам "
          f"(из {len(long_calls)})\n")

    with_text = {DIRECTION_IN: 0, DIRECTION_OUT: 0, 0: 0}
    total_by_dir = {DIRECTION_IN: 0, DIRECTION_OUT: 0, 0: 0}
    errors = 0
    for index, activity in enumerate(checked, 1):
        direction = _coerce_int(activity.get("DIRECTION"))
        if direction not in (DIRECTION_IN, DIRECTION_OUT):
            direction = 0
        total_by_dir[direction] += 1
        answer = has_transcript(_coerce_int(activity.get("ID")))
        if answer is None:
            errors += 1
        elif answer:
            with_text[direction] += 1
        if index % 50 == 0:
            print(f"  … проверено {index}/{len(checked)}")

    text_total = sum(with_text.values())
    checks = len(checked) - errors
    return {
        "deals_probed": len(deal_ids),
        "calls_total": len(activities),
        "duration_buckets": buckets,
        "duration_median_sec": int(statistics.median(durations)) if durations else 0,
        "long_calls": len(long_calls),
        "long_share_pct": round(len(long_calls) / len(activities) * 100, 1)
        if activities else 0.0,
        "checked": len(checked),
        "check_errors": errors,
        "with_transcript": text_total,
        "without_transcript": max(checks - text_total, 0),
        "coverage_pct": round(text_total / checks * 100, 1) if checks else 0.0,
        "incoming_checked": total_by_dir[DIRECTION_IN],
        "incoming_with_text": with_text[DIRECTION_IN],
        "outgoing_checked": total_by_dir[DIRECTION_OUT],
        "outgoing_with_text": with_text[DIRECTION_OUT],
    }


def print_report(stats: dict[str, Any]) -> None:
    print("\n" + "=" * 62)
    print("РАЗВЕДКА ЗВОНКОВ: длительности и покрытие расшифровками")
    print("=" * 62)
    print(f"Сделок разобрано:        {stats['deals_probed']}")
    print(f"Звонков всего:           {stats['calls_total']}")
    print()
    print("Распределение длительностей (END_TIME − START_TIME):")
    total = stats["calls_total"] or 1
    for label in ("0", "1-29", "30-59", ">=60"):
        count = stats["duration_buckets"][label]
        print(f"  {label:>6} сек: {count:>5}  ({count / total * 100:.0f} %)")
    print(f"  медиана: {stats['duration_median_sec']} сек")
    print()
    print(f"Длинных звонков (>= {LONG_CALL_SEC} сек): {stats['long_calls']} "
          f"({stats['long_share_pct']} % всех)")
    print(f"Отсечка экономит запросов: "
          f"{stats['calls_total'] - stats['long_calls']}")
    print()
    print(f"Проверено расшифровок:   {stats['checked']}")
    if stats["check_errors"]:
        print(f"  из них не ответили:    {stats['check_errors']} "
              f"(в покрытие не считаются)")
    print(f"  с текстом:             {stats['with_transcript']} "
          f"({stats['coverage_pct']} %)")
    print(f"  без текста:            {stats['without_transcript']} "
          f"← столько кандидатов в очередь на запуск")
    print()
    print("По направлению звонка:")
    for label, checked_key, text_key in (
        ("входящие", "incoming_checked", "incoming_with_text"),
        ("исходящие", "outgoing_checked", "outgoing_with_text"),
    ):
        checked = stats[checked_key]
        with_text = stats[text_key]
        share = f"{with_text / checked * 100:.0f} %" if checked else "—"
        print(f"  {label:<10} проверено {checked:>4}, с текстом {with_text:>4} "
              f"({share})")

    print("\nНа что смотреть:")
    print("  • «Без текста» — это ПОТОЛОК очереди на запуск, а не её размер:")
    print("    приоритетный фильтр (активная стадия, брокер молчит) отберёт")
    print("    из них меньше. Бюджет --launch-budget держит сотню за прогон.")
    if stats["outgoing_checked"] and stats["incoming_checked"]:
        out_share = stats["outgoing_with_text"] / stats["outgoing_checked"]
        in_share = stats["incoming_with_text"] / stats["incoming_checked"]
        if in_share - out_share > 0.15:
            print("  • У исходящих покрытие заметно ниже входящих. Это перекос")
            print("    не в пользу брокера: он звонил, а текста его разговора")
            print("    нет, и карточка выглядит молчащей. Ровно те звонки и")
            print("    надо ставить в очередь первыми.")
    if stats["check_errors"]:
        print("  • Часть запросов не ответила. Если их много, цифра покрытия")
        print("    занижена — повторите разведку, прежде чем верить очереди.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Разведка звонков: длительности и доля без расшифровки",
    )
    parser.add_argument("--category", type=int, action="append", required=True,
                        help="ID воронки; можно повторить: --category 18 --category 0")
    parser.add_argument("--limit", type=int, default=0,
                        help="Ограничить число сделок (0 — весь портфель)")
    parser.add_argument("--max-transcript-checks", type=int, default=0,
                        help="Ограничить число запросов расшифровки (0 — все длинные)")
    parser.add_argument("--json", action="store_true", help="Вывести результат как JSON")
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.log_level)

    stats = probe(args.category, args.limit, args.max_transcript_checks)
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    else:
        print_report(stats)


if __name__ == "__main__":
    main()
