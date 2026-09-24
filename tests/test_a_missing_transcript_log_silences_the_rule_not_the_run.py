"""Кэш расшифровок — третий источник слоя и единственный необязательный.

Из него кормятся два правила раздела 6.2 ТЗ: правило 3 «нет данных»
спрашивает, есть ли у звонка текст, правило 2 «отказ» ищет в этом тексте
маркеры. Лежит кэш в `data/violations.db` — базе аудита, а не в витрине, и
её может не оказаться: другой каталог на другом сервере, чистка, занятый
файл. Портфель от этого не перестаёт существовать, поэтому недоступный кэш
гасит ДВА правила, а не прогон.

Разница между «кэша нет» и «кэш пуст» здесь дороже всего. Пустой кэш — это
«ни одного звонка не расшифровано», и правило 3 честно объявляет такие
карточки безданными. Отсутствующий кэш, посчитанный пустым, объявил бы
безданным ВЕСЬ портфель: тысячи клиентов разом уехали бы в одно состояние,
и список «кого смотреть первым» перестал бы отвечать на свой вопрос ровно в
тот день, когда кто-то переименовал файл.

Из кэша наружу не выходит ни строчки текста — только номера звонков.
Разговор с клиентом остаётся в базе, а `triage_reason` называет правило.
"""

import sqlite3

import pytest

from clients.transcripts import (
    TRANSCRIBED,
    calls_with_refusal,
    find_markers,
    normalize,
    transcribed_calls,
)


@pytest.fixture
def cache(tmp_path):
    """Кэш расшифровок: номер звонка → (текст, статус)."""
    def _make(rows: dict[int, tuple[str, str]]):
        path = tmp_path / "violations.db"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE call_transcripts (activity_id INTEGER PRIMARY KEY,"
            " deal_id INTEGER NOT NULL, text TEXT NOT NULL DEFAULT '',"
            " status TEXT NOT NULL, fetched_at TEXT NOT NULL DEFAULT '',"
            " chars INTEGER NOT NULL DEFAULT 0,"
            " activity_created TEXT NOT NULL DEFAULT '')"
        )
        conn.executemany(
            "INSERT INTO call_transcripts(activity_id, deal_id, text, status)"
            " VALUES (?, 1, ?, ?)",
            [(cid, text, status) for cid, (text, status) in rows.items()],
        )
        conn.commit()
        conn.close()
        return path

    return _make


def test_only_a_finished_transcript_counts_as_having_the_text():
    """Расшифровка есть только при исходе `ok`."""
    assert TRANSCRIBED == "ok"


def test_only_a_downloaded_text_counts(cache):
    """Скачанный текст — да; ошибка и пустая строка — нет.

    Строка со статусом `ok` и пустым текстом означает «скачали ничего»:
    читать там нечего так же, как если бы строки не было вовсе. Правилу 3
    важно не то, дошла ли очередь, а есть ли на чём принимать решение.
    """
    path = cache({1: ("разговор", "ok"), 2: ("разговор", "error"), 3: ("", "ok")})

    assert transcribed_calls([1, 2, 3], db_path=path) == {1}


def test_a_call_the_cache_never_saw_has_no_text_either(cache):
    """Звонка нет в кэше — значит и текста нет.

    Записи не будет у звонка, который не расшифровывали вовсе: короткий,
    не дошла очередь, кончился бюджет. Для правила 3 это тот же ответ.
    """
    path = cache({1: ("разговор", "ok")})

    assert transcribed_calls([1, 999], db_path=path) == {1}


def test_a_missing_cache_is_not_an_empty_cache(tmp_path):
    """Кэша нет — ``None``, а не пустое множество.

    Самая дорогая ошибка этого модуля. Пустое множество означало бы «ни
    одной расшифровки не существует», и правило 3 отправило бы в «нет
    данных» весь портфель разом. ``None`` означает «не знаем», и правило
    обязано промолчать.
    """
    assert transcribed_calls([1, 2], db_path=tmp_path / "которой-нет.db") is None
    assert calls_with_refusal([1], ["передумал"], db_path=tmp_path / "нет.db") is None


def test_a_cache_without_the_table_is_missing_too(tmp_path):
    """Файл есть, таблицы нет — тот же ``None``.

    База аудита переживает миграции, и таблица может не успеть появиться.
    Пустой ответ здесь соврал бы точно так же, как отсутствие файла.
    """
    path = tmp_path / "violations.db"
    sqlite3.connect(path).close()

    assert transcribed_calls([1], db_path=path) is None


def test_an_empty_cache_is_a_real_answer(cache):
    """Журнал прочитан и пуст — это знание, а не его отсутствие.

    Обратная половина предыдущих двух проверок: пустое множество обязано
    возвращаться там, где оно правда, иначе правило 3 не сработает никогда.
    """
    path = cache({})

    assert transcribed_calls([1, 2], db_path=path) == set()


def test_nothing_is_asked_when_there_are_no_calls(tmp_path):
    """Пустой список звонков не открывает базу вовсе.

    У большинства прогонов звонки есть, но прогон на пустом портфеле не
    должен падать в ``None`` из-за отсутствующего файла и тащить за собой
    ложный degraded.
    """
    assert transcribed_calls([], db_path=tmp_path / "которой-нет.db") == set()


def test_a_missing_cache_is_not_created_by_the_reader(tmp_path):
    """Слой не заводит чужую базу, когда её не находит.

    `sqlite3.connect(path)` по отсутствующему файлу его СОЗДАЁТ — пустой и
    без таблиц. Прочитать из такого нечего, и правило 3 промолчит всё
    равно, но в `data/` появится `violations.db`, которого никто не
    просил: аудит при следующем запуске увидит свою базу пустой, а
    следующий человек будет гадать, кто её обнулил.

    Проверяется именно файл, а не строка подключения: `mode=ro` в коде
    можно написать и потерять при правке, а отсутствие файла — это то,
    ради чего он там написан.
    """
    path = tmp_path / "violations.db"

    assert transcribed_calls([1], db_path=path) is None
    assert not path.exists(), "чужая база заведена на пустом месте"


def test_the_reader_cannot_write_even_when_the_cache_is_there(cache):
    """Соединение модуля физически не умеет писать.

    У базы аудита свои писатели, и слой к ним третьим не подсаживается.
    Запрет проверяется на том же URI, которым открывает модуль.
    """
    path = cache({1: ("разговор", "ok")})

    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute(
                "INSERT INTO call_transcripts(activity_id, deal_id, status)"
                " VALUES (2, 1, 'ok')"
            )


# ── Правило 2: маркеры отказа в расшифровке ────────────────────────────

def test_the_letter_yo_is_not_a_different_word(cache):
    """«Нашёл» и «нашел» — одно слово.

    Какую из двух букв напишет брокер или распознавалка — вопрос случая, и
    правило, зависящее от него, срабатывало бы через раз.
    """
    path = cache({1: ("клиент нашёл сам другой вариант", "ok")})

    assert calls_with_refusal([1], ["нашел сам"], db_path=path) == {1}
    assert normalize("Нашёл\n  САМ") == "нашел сам"


def test_a_marker_survives_a_line_break_in_the_middle(cache):
    """Перенос строки посреди фразы маркер не ломает.

    Расшифровка приходит кусками, и «снял с продажи» в ней запросто
    окажется разорванным. Схлопывание пробелов — не косметика: без него
    правило промахивалось бы на длинных маркерах, то есть на самых точных.
    """
    path = cache({1: ("собственник  снял\nс   продажи объект", "ok")})

    assert calls_with_refusal([1], ["снял с продажи"], db_path=path) == {1}


def test_case_does_not_hide_a_refusal(cache):
    """«ПЕРЕДУМАЛ» — тот же отказ, что «передумал».

    Сравнение идёт в питоне, а не через SQL LIKE: тот нечувствителен к
    регистру только для латиницы и русскую заглавную пропустил бы молча.
    """
    path = cache({1: ("Клиент сказал что ПЕРЕДУМАЛ", "ok")})

    assert calls_with_refusal([1], ["передумал"], db_path=path) == {1}


def test_a_call_without_a_marker_is_not_a_refusal(cache):
    """Обычный разговор отказом не объявляется."""
    path = cache({1: ("договорились о показе в субботу", "ok")})

    assert calls_with_refusal([1], ["передумал", "снял с продажи"], db_path=path) == set()


def test_an_unreadable_transcript_cannot_hold_a_refusal(cache):
    """Текст, который не скачался, отказа не содержит.

    Статус `error` означает «прочитать не удалось», а не «там ничего нет».
    Искать маркер в такой строке — искать в том, чего не читали.
    """
    path = cache({1: ("передумал", "error"), 2: ("", "ok")})

    assert calls_with_refusal([1, 2], ["передумал"], db_path=path) == set()


def test_an_empty_marker_list_finds_nobody_and_does_not_crash(cache):
    """Пустой список маркеров — пустой ответ, а не ``None``.

    Правило выключено осознанно (раздел 10 ТЗ), и отличать это от «кэш
    недоступен» обязательно: первое вызывающий пишет в degraded сам,
    второе узнаёт по ``None``.
    """
    path = cache({1: ("передумал", "ok")})

    assert calls_with_refusal([1], [], db_path=path) == set()
    assert calls_with_refusal([1], ["", "  "], db_path=path) == set()


def test_a_blank_marker_does_not_match_every_call(cache):
    """Пустая строка среди маркеров не делает отказавшими всех.

    Пустая подстрока есть в любом тексте. Один пробел, оставшийся в
    настройке после правки, объявил бы отказом каждый разговор портфеля.
    """
    path = cache({1: ("договорились о показе", "ok")})

    assert calls_with_refusal([1], ["  ", "передумал"], db_path=path) == set()
    assert find_markers("договорились о показе", ["", "  "]) is False


def test_the_search_returns_numbers_and_never_the_words(cache):
    """Наружу уходят номера звонков, а не текст и не цитата.

    Проверяется тип ответа, а не аккуратность вызывающего: пока функция
    отдаёт множество чисел, разговор с клиентом физически не может попасть
    ни в лог, ни в журнал прогонов, ни в чат.
    """
    path = cache({7: ("клиент передумал, купил в другом месте", "ok")})

    found = calls_with_refusal([7], ["передумал"], db_path=path)

    assert found == {7}
    assert all(isinstance(value, int) for value in found)
