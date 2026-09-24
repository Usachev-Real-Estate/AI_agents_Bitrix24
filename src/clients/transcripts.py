"""Расшифровки звонков: есть ли текст и нет ли в нём отказа.

Третий источник слоя и единственный, без которого он умеет обойтись.
Расшифровки живут в `data/violations.db`, таблица `call_transcripts`
(db.py) — то есть в базе аудита, а не в витрине. Открывается она **только
на чтение**, тем же способом, что и витрина: у неё свои писатели, и
подсаживаться к ним третьим слой не имеет права.

Отсюда кормятся два правила раздела 6.2:

* правило 3 «нет данных» — спрашивает, **есть ли текст**;
* правило 2 «отказ» — ищет в тексте маркеры отказа (`config.REFUSAL_MARKERS`).

**Почему `call_transcripts`, а не журнал постановок.** Рядом лежит
`transcript_launches` с исходами очереди, и ТЗ описывает правило 3 именно
её словарём — `queued | deferred | absent` `[V27]`. Таких значений там не
бывает ни одного: их вычисляет досье в памяти и кладёт в дневную выгрузку
`queue_<дата>.json`, а в базу пишутся пусто, `ok` и `failed`. Но главное
не это. Журнал отвечает «дошла ли очередь», а обоим правилам нужен САМ
ТЕКСТ: правилу 2 — чтобы его прочитать, правилу 3 — чтобы убедиться, что
читать есть что. Кэш текста отвечает на это прямо, и из него же дашборд
будет отдавать расшифровку по клику (раздел 9) — то есть слой и экран
говорят об одном и том же, а не о двух похожих.

**Текст наружу не выходит.** Правило 2 возвращает только номера звонков, в
которых маркер нашёлся, и `triage_reason` называет правило, а не цитату.
Разговор с клиентом остаётся в базе.

База недоступна — правила гаснут и попадают в `degraded_rules`. Падать
нельзя: расшифровки это довесок к портфелю, а не портфель.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Iterable, Iterator, Sequence

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("data/violations.db")

# Единственное значение статуса, означающее «текст скачан». Словарь кэша
# (transcripts.STATUS_OK), а не наш: заводить свою копию значило бы
# разойтись с ним в тот день, когда там добавят состояние.
TRANSCRIBED = "ok"

# Тот же размер пачки, что у витрины: у SQLite есть потолок числа
# параметров в запросе, и портфель на тридцать тысяч дел в него упрётся
# ночью, а не в тесте.
CHUNK = 500

# Имя правила для degraded_rules. Строкой, а не кодом: список уходит в
# журнал прогонов и читается человеком.
DEGRADED_NAME = "расшифровки звонков"


def _chunks(values: Sequence[int]) -> Iterator[tuple[int, ...]]:
    ordered = tuple(values)
    for start in range(0, len(ordered), CHUNK):
        yield ordered[start:start + CHUNK]


def transcribed_calls(
    activity_ids: Iterable[int],
    *,
    db_path: str | Path | None = None,
) -> set[int] | None:
    """Звонки, у которых расшифровка ЕСТЬ. ``None`` — журнал недоступен.

    Возвращается именно множество расшифрованных, а не словарь исходов:
    правилу 3 нужен один бит, и отдавать ему больше значило бы предложить
    решать за журнал, что означает `failed`.

    Статуса `ok` мало: строка с пустым текстом — это «скачали ничего», и
    читать там нечего так же, как если бы строки не было вовсе.

    ``None`` и пустое множество — разные ответы, и путать их нельзя.
    Пустое значит «кэш прочитан, расшифровок нет ни одной»: правило
    работает и объявляет «нет данных» честно. ``None`` значит «кэша не
    видно», и тогда правило обязано молчать, а не объявлять весь портфель
    безданным.
    """
    wanted = sorted({int(value) for value in activity_ids})
    if not wanted:
        return set()

    path = Path(db_path or DEFAULT_DB_PATH)
    try:
        # Тот же readonly-URI, что у витрины: соединение физически не умеет
        # писать, и случайный INSERT падает здесь, а не портит чужую базу.
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            out: set[int] = set()
            for chunk in _chunks(wanted):
                marks = ", ".join("?" * len(chunk))
                rows = conn.execute(
                    "SELECT activity_id FROM call_transcripts"
                    f" WHERE status = ? AND text != '' AND activity_id IN ({marks})",
                    (TRANSCRIBED, *chunk),
                ).fetchall()
                out.update(int(row[0]) for row in rows)
    except sqlite3.Error as error:
        # Файла нет, таблицы нет, база занята — все три случая одинаковы
        # для вызывающего: правило 3 в этом прогоне не работает.
        logger.warning("Кэш расшифровок недоступен (%s): %s", path, error)
        return None
    return out


def normalize(text: str) -> str:
    """Текст в сравнимом виде: регистр, «ё» и лишние пробелы.

    «Ё» приводится к «е» обеими сторонами сравнения. Без этого «нашёл сам»
    в расшифровке не совпал бы с «нашел сам» в списке маркеров, а какую из
    двух букв напишет брокер или распознавалка — вопрос случая.

    Пробелы схлопываются: в расшифровках встречаются двойные и переносы
    строк посреди фразы, и маркер «снял с продажи» мимо них не прошёл бы.
    """
    lowered = str(text or "").replace("ё", "е").replace("Ё", "Е").casefold()
    return " ".join(lowered.split())


def find_markers(text: str, markers: Iterable[str]) -> bool:
    """Есть ли в тексте хоть один маркер. Пустой список маркеров — нет."""
    haystack = normalize(text)
    if not haystack:
        return False
    return any(needle for needle in (normalize(m) for m in markers) if needle in haystack)


def calls_with_refusal(
    activity_ids: Iterable[int],
    markers: Iterable[str],
    *,
    db_path: str | Path | None = None,
) -> set[int] | None:
    """Звонки, в расшифровке которых нашёлся маркер отказа.

    ``None`` — кэш недоступен, правило 2 в этом прогоне не работает.
    Пустой список маркеров даёт пустое множество, а не ``None``: правило
    выключено осознанно, и вызывающий помечает его degraded сам.

    Наружу уходят только НОМЕРА звонков. Ни текста, ни цитаты, ни самого
    маркера: `triage_reason` называет правило, а разговор с клиентом
    остаётся в базе.

    Сравнение идёт в питоне, а не в SQL: `LIKE` в SQLite нечувствителен к
    регистру только для латиницы, и «Передумал» с большой буквы он бы
    пропустил. Текстов на прогон тысячи, а не миллионы.
    """
    wanted = sorted({int(value) for value in activity_ids})
    needles = tuple(m for m in (normalize(x) for x in markers) if m)
    if not wanted or not needles:
        return set()

    path = Path(db_path or DEFAULT_DB_PATH)
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            out: set[int] = set()
            for chunk in _chunks(wanted):
                marks = ", ".join("?" * len(chunk))
                rows = conn.execute(
                    "SELECT activity_id, text FROM call_transcripts"
                    f" WHERE status = ? AND text != '' AND activity_id IN ({marks})",
                    (TRANSCRIBED, *chunk),
                ).fetchall()
                for activity_id, text in rows:
                    haystack = normalize(text)
                    if any(needle in haystack for needle in needles):
                        out.add(int(activity_id))
    except sqlite3.Error as error:
        logger.warning("Кэш расшифровок недоступен (%s): %s", path, error)
        return None
    return out
