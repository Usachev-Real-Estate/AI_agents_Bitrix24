"""Фильтр «Вчера» и утренняя рассылка обязаны говорить об одном дне.

Два определения вчерашнего дня — это два разных ответа на вопрос «сколько
вчера закрыли», и расходиться они будут молча: сводка назовёт одно число,
экран другое, и оба будут выглядеть правдоподобно.

Поэтому фильтр не считает «вчера» заново, а берёт то же окно, которым
ведётся рассылка: прошлый РАБОЧИЙ день. В понедельник это пятница вместе с
выходными — иначе фильтр показывал бы воскресенье, в котором закономерно
ничего не закрыто. Подпись при этом не молчит: вместо «Вчера» она называет
даты, потому что по такому числу сверяют выручку.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

import metrics

MSK = metrics.BUSINESS_TZ


def _msk(day: str) -> datetime:
    return datetime.fromisoformat(f"{day}T10:00:00").replace(tzinfo=MSK)


# ── Одно определение на экран и на сообщение ───────────────────────────
def test_the_filter_takes_the_window_the_digest_uses():
    """Не «то же самое по смыслу», а буквально то же окно."""
    window = metrics.yesterday_window()
    period = metrics.resolve_period("yesterday")

    assert period["since"] == window["since"]
    assert period["until"] == window["until"]


def test_the_preset_is_offered_in_the_filter():
    assert metrics.PERIOD_PRESETS["yesterday"] == "Вчера"


# ── Границы ────────────────────────────────────────────────────────────
def test_on_an_ordinary_day_it_is_one_day():
    """Четверг: вчера — это среда, и подпись короткая."""
    window = metrics.yesterday_window(_msk("2026-09-10"))

    assert window["label"] == "вчера"
    since = datetime.fromisoformat(window["since"]).astimezone(MSK)
    until = datetime.fromisoformat(window["until"]).astimezone(MSK)
    assert since.date().isoformat() == "2026-09-09"
    assert (until - since) == timedelta(days=1)


def test_on_monday_it_reaches_back_to_friday():
    """Иначе фильтр показал бы воскресенье, в котором закрывать нечего."""
    window = metrics.yesterday_window(_msk("2026-09-14"))

    since = datetime.fromisoformat(window["since"]).astimezone(MSK)
    until = datetime.fromisoformat(window["until"]).astimezone(MSK)
    assert since.date().isoformat() == "2026-09-11", "пятница"
    assert (until - since) == timedelta(days=3), "пятница плюс выходные"
    assert window["label"] == "11.09–13.09", "подпись обязана назвать даты"


def test_the_boundaries_are_moscow_midnights_not_utc():
    """Витрина хранит UTC, а календарь у агентства московский.

    Взять из строки первые десять символов значит ошибиться на сутки: в
    UTC московское «вчера» начинается позавчера в 21:00, и поля «с» и «по»
    показали бы дату на день раньше выбранной.
    """
    period = metrics.resolve_period("yesterday")
    since = datetime.fromisoformat(period["since"]).astimezone(MSK)

    assert (since.hour, since.minute) == (0, 0)
    assert period["start"] == since.date().isoformat()
    assert period["end"] == period["start"] or period["end"] > period["start"]


def test_the_label_stays_honest_when_the_window_is_longer():
    """«Вчера» на три дня обязано сказать, что это три дня."""
    window = metrics.yesterday_window(_msk("2026-09-14"))
    label = ("Вчера" if window["label"] == "вчера"
             else f"Вчера · {window['label']}")

    assert label == "Вчера · 11.09–13.09"
