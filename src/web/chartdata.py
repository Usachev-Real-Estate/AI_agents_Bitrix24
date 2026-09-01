"""Подготовка данных для графиков.

Шаблон не должен перекладывать строки метрик в форму, понятную рисовалке:
это логика, и место ей в Python, где её видно и можно проверить тестом.
"""

from __future__ import annotations

from typing import Any, Iterable

BUCKET_LABELS = {
    "day": lambda value: f"{value[8:10]}.{value[5:7]}",
    "week": lambda value: value.replace("-W", " нед. "),
    "month": lambda value: f"{value[5:7]}.{value[0:4]}",
}


def timeseries_chart(rows: list[dict[str, Any]], grain: str = "day") -> dict[str, Any]:
    """Динамика: лиды, созданные сделки, выигранные сделки."""
    label_of = BUCKET_LABELS.get(grain, BUCKET_LABELS["day"])
    return {
        "height": 250,
        "points": [{
            "label": label_of(str(row["bucket"])),
            "leads": row["leads"],
            "deals": row["deals"],
            "won": row["won"],
        } for row in rows],
        "series": [
            {"key": "leads", "label": "Лиды"},
            {"key": "deals", "label": "Сделки созданы"},
            {"key": "won", "label": "Сделки выиграны"},
        ],
    }


def funnel_chart(funnel: dict[str, Any] | None) -> dict[str, Any]:
    """Воронка: один тон, длину полосы задаёт число дошедших до стадии."""
    if not funnel:
        return {"stages": []}
    return {"stages": [{
        "name": row["name"],
        "reached": row["reached"],
        "count_now": row["count_now"],
        "amount_open": row["amount_open"],
        "conversion_from_start": row["conversion_from_start"],
    } for row in funnel["stages"]]}


def lead_funnel_chart(funnel: dict[str, Any]) -> dict[str, Any]:
    """Лиды по статусам в той же форме, что и воронка сделок."""
    return {"stages": [{
        "name": row["name"],
        "reached": row["count"],
        "count_now": row["count"],
        "amount_open": 0,
        "conversion_from_start": row["share"],
    } for row in funnel.get("by_status", [])]}


def durations_chart(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Медианное время на стадии — горизонтальные полосы одного тона."""
    data = [row for row in rows if (row.get("median_days") or 0) > 0]
    return {
        "valueLabel": "Медиана",
        "rows": [{
            "name": row["name"],
            "value": row["median_days"],
            "display": _days(row["median_days"]),
            "tip": [
                ["Медиана", _days(row["median_days"])],
                ["75-й перцентиль", _days(row["p75_days"])],
                ["Завершённых интервалов", str(row["completed_count"])],
                ["Стоит сейчас", str(row["open_count"])],
            ],
        } for row in data],
    }


def net_flow_chart(movement: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Чистый поток по стадии: вошло минус вышло, вокруг нуля."""
    rows = []
    for row in movement:
        net = (row["entered"] or 0) - (row["left_count"] or 0)
        if net == 0 and not row["entered"] and not row["left_count"]:
            continue
        rows.append({
            "name": row["name"],
            "value": net,
            "tip": [
                ["Вошло", str(row["entered"])],
                ["Вышло", str(row["left_count"])],
                ["Чистый поток", f"{net:+d}"],
                ["Осталось на конец", str(row["remaining"])],
            ],
        })
    return {"rows": rows}


def people_chart(rows: Iterable[dict[str, Any]], limit: int = 12) -> dict[str, Any]:
    """Топ по выигранным деньгам."""
    data = [row for row in rows if (row.get("won_amount") or 0) > 0][:limit]
    return {
        "valueLabel": "Выиграно",
        "rows": [{
            "name": row["name"],
            "value": row["won_amount"],
            "display": _money(row["won_amount"]),
            "tip": [
                ["Выиграно денег", _money(row["won_amount"])],
                ["Выиграно сделок", str(row["won"])],
                ["Доля выигранных", f"{row['win_rate']}%".replace(".", ",")],
                ["Открытых сейчас", str(row["deals_open"])],
            ],
        } for row in data],
    }


def transitions_matrix(transitions: dict[str, Any] | None, stages: list[dict[str, Any]]):
    """Матрица переходов «откуда → куда» для тепловой карты.

    Тепловая карта, а не диаграмма потоков: при десятке стадий ленты
    накладываются и перестают читаться, а сетка остаётся точной и позволяет
    найти конкретную пару стадий глазами.
    """
    if not transitions or not stages:
        return {"stages": [], "rows": [], "max": 0}
    order = [stage["stage_id"] for stage in stages]
    names = {stage["stage_id"]: stage["name"] for stage in stages}
    counts: dict[tuple[str, str], int] = {}
    for row in transitions.get("transitions", []):
        counts[(row["from_stage"], row["to_stage"])] = row["moves"]

    present = [
        stage_id for stage_id in order
        if any(key[0] == stage_id or key[1] == stage_id for key in counts)
    ]
    # Строки — только у стадий, с которых реально уходят. У конечных стадий
    # («Договор закрыт», «Сделка проиграна») исходящих переходов нет, и
    # пустая строка через всю таблицу — чистый шум.
    sources = [s for s in present if any(key[0] == s for key in counts)]
    largest = max(counts.values()) if counts else 0
    return {
        "stages": [{"stage_id": s, "name": names.get(s, s)} for s in present],
        "rows": [{
            "stage_id": source,
            "name": names.get(source, source),
            "cells": [{
                "stage_id": target,
                "name": names.get(target, target),
                "moves": counts.get((source, target), 0),
                "intensity": _bucket(counts.get((source, target), 0), largest),
                "backward": order.index(target) < order.index(source),
            } for target in present],
        } for source in sources],
        "max": largest,
    }


def _bucket(value: int, largest: int) -> int:
    """Пять ступеней синей шкалы; 0 — пустая клетка."""
    if not value or not largest:
        return 0
    share = value / largest
    for index, threshold in enumerate((0.2, 0.4, 0.6, 0.8), start=1):
        if share <= threshold:
            return index
    return 5


def _days(value: float) -> str:
    if value is None:
        return "—"
    if value < 1:
        return f"{round(value * 24)} ч"
    return f"{value:.1f}".rstrip("0").rstrip(".").replace(".", ",") + " дн"


def _money(value: float) -> str:
    amount = float(value or 0)
    if abs(amount) >= 1_000_000:
        return f"{amount / 1_000_000:.1f}".replace(".", ",") + " млн ₽"
    if abs(amount) >= 1_000:
        return f"{amount / 1_000:,.0f}".replace(",", " ") + " тыс ₽"
    return f"{amount:,.0f}".replace(",", " ") + " ₽"
