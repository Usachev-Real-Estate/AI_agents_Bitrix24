#!/usr/bin/env python3
"""Сравнить прогоны QC-агента: где разошлись и на сколько.

Зачем: у модели температура 0.1, а не ноль, поэтому два прогона с
одинаковыми настройками всё равно расходятся. Без замера этого расхождения
любую разницу между настройками можно списать на что угодно. Отсюда порядок
опыта: два прогона на текущих настройках дают коридор шума, третий — на
новых, и вопрос только один: вышел он за коридор или нет.

    DRY_RUN=1 python3 scripts/run_client_state_qc_pilot.py --limit 25 --force
    cp data/client_state_qc_pilot.json data/run_A1.json
    ... то же для A2 и B ...
    python3 scripts/compare_qc_runs.py data/run_A1.json data/run_A2.json data/run_B.json

Сравниваются только карточки, попавшие во все прогоны: судить по разным
выборкам нельзя, а выборка случайная и между прогонами может разойтись.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

# Метрики, по которым видно просадку качества. Порядок — по убыванию
# чувствительности: цитаты рушатся первыми, вердикт последним.
QUALITY_KEYS: tuple[tuple[str, str, str], ...] = (
    ("evidence_dropped", "отброшено цитат", "меньше — лучше"),
    ("errors", "ошибок разбора", "меньше — лучше"),
    ("unrecoverable", "неинформативных", "меньше — лучше"),
    ("contradictions_found", "расхождений найдено", "больше — лучше"),
    ("contradictions_dropped", "расхождений отброшено", "меньше — лучше"),
    ("agent_cards", "карточек с агентом", "равно"),
)

# Поля выжимки, расхождение по которым считается изменением решения.
DECISION_KEYS: tuple[str, ...] = (
    "verdict", "temperature", "work_reason", "recoverable", "counterparty",
)


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def cards_by_id(run: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Все карточки прогона обеих воронок, по номеру сделки."""
    out: dict[int, dict[str, Any]] = {}
    for funnel in run.get("funnels") or []:
        for card in funnel.get("cards") or []:
            deal_id = int(card.get("deal_id") or 0)
            if deal_id:
                out[deal_id] = card
    return out


def totals(run: dict[str, Any]) -> dict[str, float]:
    """Сводка прогона одной строкой: суммы по обеим воронкам."""
    acc: dict[str, float] = {}
    for funnel in run.get("funnels") or []:
        for key, _label, _dir in QUALITY_KEYS:
            acc[key] = acc.get(key, 0.0) + float(funnel.get(key) or 0)
        acc["cost_rub"] = acc.get("cost_rub", 0.0) + float(
            funnel.get("cost_rub") or 0,
        )
        acc["llm_calls"] = acc.get("llm_calls", 0.0) + float(
            funnel.get("llm_calls") or 0,
        )
        usage = funnel.get("usage") or {}
        for key in ("input_tokens", "output_tokens", "reasoning_tokens"):
            acc[key] = acc.get(key, 0.0) + float(usage.get(key) or 0)
    return acc


def facts_share(cards: dict[int, dict[str, Any]], ids: list[int]) -> float:
    """Доля найденных обязательных фактов — главная работа агента."""
    present = sum(int(cards[i].get("facts_present") or 0) for i in ids)
    needed = sum(int(cards[i].get("facts_needed") or 0) for i in ids)
    return present / needed * 100.0 if needed else 0.0


def disagreements(
    base: dict[int, dict[str, Any]],
    other: dict[int, dict[str, Any]],
    ids: list[int],
) -> dict[str, list[int]]:
    """По каким полям и на каких сделках прогоны решили по-разному."""
    out: dict[str, list[int]] = {key: [] for key in DECISION_KEYS}
    for deal_id in ids:
        for key in DECISION_KEYS:
            if base[deal_id].get(key) != other[deal_id].get(key):
                out[key].append(deal_id)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Сравнение прогонов QC-агента")
    parser.add_argument("files", nargs="+", type=Path, help="JSON прогонов")
    parser.add_argument(
        "--show", type=int, default=8,
        help="Сколько номеров сделок печатать в строке расхождений",
    )
    args = parser.parse_args()
    if len(args.files) < 2:
        parser.error("нужно как минимум два прогона: одинаковый и изменённый")

    runs = [(path.stem, load(path)) for path in args.files]
    all_cards = [cards_by_id(run) for _name, run in runs]
    common = sorted(set.intersection(*(set(c) for c in all_cards)))
    if not common:
        print("Общих карточек нет — сравнивать нечего.")
        return

    names = [name for name, _run in runs]
    width = max(len(n) for n in names) + 2
    print(f"Общих карточек: {len(common)} из "
          f"{', '.join(str(len(c)) for c in all_cards)}\n")

    print("── Деньги " + "─" * 50)
    head = "".join(n.rjust(width) for n in names)
    print(f"{'':28s}{head}")
    sums = [totals(run) for _name, run in runs]
    for key, label in (
        ("cost_rub", "рублей за прогон"),
        ("llm_calls", "вызовов модели"),
        ("output_tokens", "токенов ответа"),
        ("reasoning_tokens", "из них размышлений"),
    ):
        row = "".join(f"{s.get(key, 0):.2f}".rstrip("0").rstrip(".").rjust(width)
                      for s in sums)
        print(f"{label:28s}{row}")
    share = "".join(
        (f"{s.get('reasoning_tokens', 0) / s['output_tokens'] * 100:.0f} %"
         if s.get("output_tokens") else "—").rjust(width)
        for s in sums
    )
    print(f"{'доля размышлений':28s}{share}")

    print("\n── Качество " + "─" * 48)
    print(f"{'':28s}{head}")
    for key, label, hint in QUALITY_KEYS:
        row = "".join(f"{s.get(key, 0):.0f}".rjust(width) for s in sums)
        print(f"{label:28s}{row}   ({hint})")
    facts = "".join(
        f"{facts_share(cards, common):.0f} %".rjust(width) for cards in all_cards
    )
    print(f"{'фактов найдено':28s}{facts}   (больше — лучше)")

    print("\n── Решения разошлись " + "─" * 39)
    print("Первый прогон — точка отсчёта. Пара одинаковых настроек задаёт")
    print("коридор шума; выход за него и есть эффект изменения.\n")
    base = all_cards[0]
    print(f"{'':28s}{''.join(n.rjust(width) for n in names[1:])}")
    per_run = [disagreements(base, other, common) for other in all_cards[1:]]
    for key in DECISION_KEYS:
        row = "".join(
            f"{len(d[key])}/{len(common)}".rjust(width) for d in per_run
        )
        print(f"{key:28s}{row}")
    for name, diff in zip(names[1:], per_run):
        shown = {k: v for k, v in diff.items() if v}
        if not shown:
            print(f"\n{name}: решения совпали по всем карточкам.")
            continue
        print(f"\n{name}:")
        for key, ids in shown.items():
            tail = "" if len(ids) <= args.show else f" … ещё {len(ids) - args.show}"
            listed = ", ".join(f"#{i}" for i in ids[:args.show])
            print(f"  {key}: {listed}{tail}")


if __name__ == "__main__":
    main()
