"""Форматирование чисел и дат для шаблонов."""

from __future__ import annotations

from datetime import datetime
from typing import Any

NBSP = " "


def fmt_int(value: Any) -> str:
    """1234567 → «1 234 567» (неразрывные пробелы, чтобы число не рвалось)."""
    try:
        return f"{int(round(float(value))):,}".replace(",", NBSP)
    except (TypeError, ValueError):
        return "—"


def fmt_money(value: Any, *, short: bool = False) -> str:
    """Сумма в рублях. short=True — компактно для плиток KPI."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "—"
    if short:
        for limit, suffix, digits in (
            (1_000_000_000, "млрд", 2), (1_000_000, "млн", 1), (1_000, "тыс", 0),
        ):
            if abs(amount) >= limit:
                scaled = round(amount / limit, digits)
                text = f"{scaled:,.{digits}f}".replace(",", NBSP).replace(".", ",")
                return f"{text}{NBSP}{suffix}{NBSP}₽"
    return f"{fmt_int(amount)}{NBSP}₽"


def fmt_pct(value: Any, *, sign: bool = False) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    prefix = "+" if sign and number > 0 else ""
    text = f"{number:.1f}".rstrip("0").rstrip(".").replace(".", ",")
    return f"{prefix}{text}%"


def fmt_days(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if number < 1:
        hours = round(number * 24)
        return f"{hours}{NBSP}ч"
    return f"{number:.1f}".rstrip("0").rstrip(".").replace(".", ",") + f"{NBSP}дн"


def fmt_area(value: Any) -> str:
    """Площадь в квадратных метрах. Десятые — не украшение.

    int_ru округлил бы 62,4 до 62, а 44,5 и 45,5 — в разные стороны: две
    соседние квартиры в списке разъехались бы на метр из ниоткуда.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    text = f"{number:.1f}".rstrip("0").rstrip(".").replace(".", ",")
    return f"{text}{NBSP}м²"


def fmt_date(value: Any) -> str:
    """UTC ISO → дата в московском времени, как её видит пользователь портала."""
    if not value:
        return "—"
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)[:10]
    from datetime import timedelta, timezone

    return (moment.astimezone(timezone(timedelta(hours=3)))).strftime("%d.%m.%Y")


def fmt_datetime(value: Any) -> str:
    if not value:
        return "—"
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)[:16]
    from datetime import timedelta, timezone

    return (moment.astimezone(timezone(timedelta(hours=3)))).strftime("%d.%m.%Y %H:%M")


FILTERS = {
    "int_ru": fmt_int,
    "money": fmt_money,
    "pct": fmt_pct,
    "days": fmt_days,
    "area": fmt_area,
    "date_ru": fmt_date,
    "datetime_ru": fmt_datetime,
}
