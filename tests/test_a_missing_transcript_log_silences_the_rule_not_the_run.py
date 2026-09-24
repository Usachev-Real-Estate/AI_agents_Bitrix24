"""Журнал расшифровок — третий источник слоя и единственный необязательный.

Правило 3 раздела 6.2 ТЗ («звонок был, решать не на чем») спрашивает, есть
ли у звонка текст. Ответ лежит в `data/violations.db` — базе аудита, а не в
витрине, и её может не оказаться: другой каталог на другом сервере, чистка,
занятый файл. Портфель от этого не перестаёт существовать, поэтому
недоступный журнал гасит ОДНО правило, а не прогон.

Разница между «журнала нет» и «журнал пуст» здесь дороже всего. Пустой
журнал — это «ни одного звонка не расшифровано», и правило 3 честно
объявляет такие карточки безданными. Отсутствующий журнал, посчитанный
пустым, объявил бы безданным ВЕСЬ портфель: тысячи клиентов разом уехали бы
в одно состояние, и список «кого смотреть первым» перестал бы отвечать на
свой вопрос ровно в тот день, когда кто-то переименовал файл.
"""

import sqlite3

import pytest

from clients.transcripts import TRANSCRIBED, transcribed_calls


@pytest.fixture
def journal(tmp_path):
    """Журнал постановок с заданными исходами."""
    def _make(outcomes: dict[int, str]):
        path = tmp_path / "violations.db"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE transcript_launches (activity_id INTEGER PRIMARY KEY,"
            " deal_id INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,"
            " first_queued_at TEXT NOT NULL DEFAULT '',"
            " last_queued_at TEXT NOT NULL DEFAULT '',"
            " outcome TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '')"
        )
        conn.executemany(
            "INSERT INTO transcript_launches(activity_id, deal_id, outcome)"
            " VALUES (?, 1, ?)",
            list(outcomes.items()),
        )
        conn.commit()
        conn.close()
        return path

    return _make


def test_only_a_finished_transcript_counts_as_having_the_text():
    """Расшифровка есть только при исходе `ok`."""
    assert TRANSCRIBED == "ok"


def test_the_waiting_and_the_failed_are_both_without_text(journal):
    """Пусто и `failed` — одинаково «текста нет».

    Почему очередь не справилась — вопрос к досье, а не к списку «кого
    смотреть первым». РОПу в обоих случаях решать не на чем, и делить эти
    два состояния значило бы предлагать ему разницу, на которую он всё
    равно не может повлиять.
    """
    path = journal({1: "ok", 2: "", 3: "failed"})

    assert transcribed_calls([1, 2, 3], db_path=path) == {1}


def test_a_call_the_journal_never_saw_has_no_text_either(journal):
    """Звонка нет в журнале — значит и текста нет.

    Записи не будет у звонка, который в очередь не попадал вовсе: короткий,
    не дошла очередь, кончился бюджет. Для правила 3 это тот же ответ.
    """
    path = journal({1: "ok"})

    assert transcribed_calls([1, 999], db_path=path) == {1}


def test_a_missing_journal_is_not_an_empty_journal(tmp_path):
    """Журнала нет — ``None``, а не пустое множество.

    Самая дорогая ошибка этого модуля. Пустое множество означало бы «ни
    одной расшифровки не существует», и правило 3 отправило бы в «нет
    данных» весь портфель разом. ``None`` означает «не знаем», и правило
    обязано промолчать.
    """
    assert transcribed_calls([1, 2], db_path=tmp_path / "которой-нет.db") is None


def test_a_journal_without_the_table_is_missing_too(tmp_path):
    """Файл есть, таблицы нет — тот же ``None``.

    База аудита переживает миграции, и таблица может не успеть появиться.
    Пустой ответ здесь соврал бы точно так же, как отсутствие файла.
    """
    path = tmp_path / "violations.db"
    sqlite3.connect(path).close()

    assert transcribed_calls([1], db_path=path) is None


def test_an_empty_journal_is_a_real_answer(journal):
    """Журнал прочитан и пуст — это знание, а не его отсутствие.

    Обратная половина предыдущих двух проверок: пустое множество обязано
    возвращаться там, где оно правда, иначе правило 3 не сработает никогда.
    """
    path = journal({})

    assert transcribed_calls([1, 2], db_path=path) == set()


def test_nothing_is_asked_when_there_are_no_calls(tmp_path):
    """Пустой список звонков не открывает базу вовсе.

    У большинства прогонов звонки есть, но прогон на пустом портфеле не
    должен падать в ``None`` из-за отсутствующего файла и тащить за собой
    ложный degraded.
    """
    assert transcribed_calls([], db_path=tmp_path / "которой-нет.db") == set()


def test_a_missing_journal_is_not_created_by_the_reader(tmp_path):
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


def test_the_reader_cannot_write_even_when_the_journal_is_there(journal):
    """Соединение модуля физически не умеет писать.

    У базы аудита свои писатели, и слой к ним третьим не подсаживается.
    Запрет проверяется на том же URI, которым открывает модуль.
    """
    path = journal({1: "ok"})

    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute(
                "INSERT INTO transcript_launches(activity_id, deal_id)"
                " VALUES (2, 1)"
            )
