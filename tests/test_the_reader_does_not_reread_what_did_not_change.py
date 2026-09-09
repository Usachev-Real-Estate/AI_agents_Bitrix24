"""Читатель комментариев: платить дважды за один ответ незачем.

Карточек больше тысячи, и обращение к модели стоит денег на каждой. Если
перечитывать ту, где ничего не добавилось, счёт растёт каждый день, а ответ
приходит тот же самый. Отпечаток последней записи хранится рядом с ответом,
и карточка идёт к модели только когда он разошёлся.

Второе правило: модель НЕ решает, что просрочено. Она достаёт из текста
фразу и дату, а сравнивает с сегодняшним днём код. Иначе ответ на
арифметический вопрос поедет вместе с настроением модели.

Отдельно проверяется сама команда. Модули были покрыты насквозь, а main()
не тронут ни одним тестом — и падение случилось именно в нём, на первой же
строке: setup_logging() позвали без обязательного аргумента. Логика была
верна, ошибка в невнимательности, и увидеть её мог только запуск команды
целиком.

Третье: всё, что не разобралось в настоящую дату, датой не считается.
Модель просили вернуть календарный день, но она вернёт и «через неделю», и
2026-13-45. Пустое поле честнее выдуманного дня, по которому назавтра
поднимут человека.
"""

from datetime import date

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

import comment_reader
from schema import analytics_session


class _Answer:
    def __init__(self, content):
        self.content = content


class _Model:
    """Модель, считающая обращения к себе."""

    def __init__(self, content='{"promised": "Позвонить в пятницу",'
                               ' "promised_at": "2026-08-15"}'):
        self.content, self.calls = content, []

    def invoke(self, messages):
        self.calls.append(messages[-1].content)
        return _Answer(self.content)


def _deal(conn, deal_id):
    conn.execute(
        """
        INSERT INTO fact_deal(deal_id, title, category_id, stage_id, assigned_by_id,
            source_id, opportunity, currency_id, date_create, date_modify, closedate,
            is_closed, is_won, is_lost, contact_id, is_deleted, synced_at)
        VALUES (?, 'ВГ 747', 0, 'UC_FADPBF', 10, 'ADV', 0, 'RUB',
                '2026-06-01T00:00:00+00:00', '2026-06-01T00:00:00+00:00',
                NULL, 0, 0, 0, 5000, 0, 'x')
        """,
        (deal_id,),
    )


def _note(conn, comment_id, deal_id, body, *, auto=0,
          created="2026-08-10T10:00:00+00:00"):
    conn.execute(
        "INSERT INTO fact_comment(comment_id, entity_type, entity_id, author_id,"
        " body, is_auto, created_at, synced_at) VALUES (?, 'deal', ?, 10, ?, ?, ?, 'x')",
        (comment_id, deal_id, body, auto, created),
    )


@pytest.fixture
def mart(analytics_db):
    with analytics_session() as conn:
        _deal(conn, 1)
        _note(conn, 100, 1, "Позвонить в пятницу, согласовать фотосессию")
    return analytics_db


def _read(model, **kwargs):
    with analytics_session() as conn:
        return comment_reader.read_cards(
            conn, model, today=date(2026, 9, 9), **kwargs,
        )


def test_the_second_run_asks_nothing(mart):
    """Главная проверка: два прогона подряд — модель зовут один раз."""
    model = _Model()
    assert _read(model) == 1
    assert _read(model) == 0
    assert len(model.calls) == 1


def test_a_new_note_sends_the_card_back(mart):
    """Добавилась запись — карточку читают заново."""
    model = _Model()
    _read(model)
    with analytics_session() as conn:
        _note(conn, 101, 1, "Созвонились, договорились на среду",
              created="2026-09-08T10:00:00+00:00")

    assert _read(model) == 1
    assert "договорились на среду" in model.calls[-1]


def test_a_robot_note_is_not_worth_reading(mart):
    """«Новое обращение: Звонок с Cian» писал робот — читать там нечего."""
    with analytics_session() as conn:
        _deal(conn, 2)
        _note(conn, 200, 2, "Новое обращение: Звонок с Cian", auto=1)

    model = _Model()
    _read(model)

    assert len(model.calls) == 1, "карточка только с роботной записью пропущена"


def test_the_model_gets_today_and_the_note_dates(mart):
    """Без сегодняшнего дня «в пятницу» не превратить в календарную дату."""
    model = _Model()
    _read(model)
    payload = model.calls[0]

    assert "Сегодня: 2026-09-09" in payload
    assert "[2026-08-10]" in payload


@pytest.mark.parametrize("value", ["через неделю", "2026-13-45", "", None, "скоро"])
def test_a_date_that_is_not_a_date_is_not_stored(mart, value):
    """Пустое поле честнее выдуманного дня: по нему назавтра поднимут человека."""
    import json

    _read(_Model(json.dumps({"promised": "Позвонить", "promised_at": value})))

    with analytics_session() as conn:
        stored = conn.execute(
            "SELECT promised_at FROM fact_comment_read WHERE entity_id = 1"
        ).fetchone()[0]
    assert stored is None


def test_an_unparsable_answer_leaves_no_trace(mart):
    """Модель ответила не JSON-ом — записывать нечего, читаем в другой раз."""
    model = _Model("извините, не могу")
    assert _read(model) == 0

    with analytics_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM fact_comment_read").fetchone()[0] == 0


def test_a_failing_model_does_not_lose_the_other_cards(mart):
    """Отказ на одной карточке не роняет разбор остальных."""
    with analytics_session() as conn:
        _deal(conn, 2)
        _note(conn, 200, 2, "Согласовать просмотр на среду")

    class _Flaky(_Model):
        def invoke(self, messages):
            self.calls.append(messages[-1].content)
            if len(self.calls) == 1:
                raise RuntimeError("rate limit")
            return _Answer(self.content)

    assert _read(_Flaky()) == 1


def test_the_batch_limit_holds(mart):
    """Первый проход по портфелю растягивается на несколько запусков."""
    with analytics_session() as conn:
        for deal_id in range(2, 6):
            _deal(conn, deal_id)
            _note(conn, 200 + deal_id, deal_id, "Перезвонить")

    model = _Model()
    assert _read(model, limit=2) == 2
    assert len(model.calls) == 2


# --------------------------------------------------------------------------
# точка входа

def test_the_dry_run_actually_runs(mart, monkeypatch, capsys):
    """Проверка самой команды, а не только того, что она вызывает.

    Модули покрыты тестами насквозь, а main() не был тронут ни одним — и
    падение случилось именно в нём, на первой же строке: setup_logging()
    позвали без обязательного аргумента. Ни один тест этого не увидел,
    потому что ни один не запускал команду целиком.
    """
    monkeypatch.setattr("sys.argv", ["comment_reader.py", "--dry-run"])

    assert comment_reader.main() == 0

    out = capsys.readouterr().out
    assert "К прочтению: 1 карточек" in out
    assert "Сегодня:" in out, "модель обязана получить сегодняшний день"
    assert "Позвонить в пятницу" in out


def test_every_entry_point_passes_a_log_level():
    """setup_logging(level) требует аргумент — и требует его у всех.

    Проверка всего каталога, а не одного модуля: ошибка была не в логике, а
    в невнимательности, и следующая точка входа повторит её ровно так же.
    """
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "src"
    bare = []
    for path in src.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if re.search(r"(?<!def )setup_logging\(\s*\)", source):
            bare.append(path.name)

    assert not bare, f"setup_logging() без уровня журнала: {sorted(bare)}"


def test_the_review_shows_the_source_next_to_the_answer(mart, monkeypatch, capsys):
    """Проверить прочитанное можно только рядом с исходной записью.

    В таблице лежат аккуратные поля, и на вид они правдоподобны всегда.
    «Показывала 15.08», превращённое в обещание, выглядит идеально — пока
    не увидишь, что это прошедшее время.
    """
    _read(_Model('{"promised": "Позвонить в пятницу", "promised_at": "2026-08-15",'
                 ' "terms": "комиссия 3%"}'))
    monkeypatch.setattr("sys.argv", ["comment_reader.py", "--show", "5"])

    assert comment_reader.main() == 0

    out = capsys.readouterr().out
    assert "Позвонить в пятницу, согласовать фотосессию" in out, "исходная запись"
    assert "обещание: Позвонить в пятницу" in out, "что вынула модель"
    assert "условия: комиссия 3%" in out


def test_the_review_says_plainly_when_nothing_was_extracted(mart, monkeypatch, capsys):
    """Пустой ответ — тоже ответ, и он должен быть виден как пустой."""
    _read(_Model('{"promised": "", "promised_at": null}'))
    monkeypatch.setattr("sys.argv", ["comment_reader.py", "--show", "5"])
    comment_reader.main()

    assert "(пусто)" in capsys.readouterr().out


def test_the_review_without_anything_read_says_so(analytics_db, monkeypatch, capsys):
    """Пустая таблица — это не пустой экран, а объяснение, что делать."""
    monkeypatch.setattr("sys.argv", ["comment_reader.py", "--show", "5"])
    comment_reader.main()

    assert "сначала запустите без --show" in capsys.readouterr().out


def test_a_new_prompt_sends_every_card_back(mart, monkeypatch):
    """Правка правил чтения перечитывает всё, что читали по старым.

    Отпечаток записей не меняется от того, что мы стали читать иначе, и без
    версии промпта карточка осталась бы с прежним ответом навсегда. На живых
    данных это были ложные отказы: «есть свой агент, НО показывать можем»
    модель посчитала отказом, промпт поправили — а двадцать карточек так и
    лежали бы с обвинением.
    """
    model = _Model()
    assert _read(model) == 1
    assert _read(model) == 0, "по той же версии не перечитываем"

    monkeypatch.setattr(comment_reader, "PROMPT_VERSION", "следующая")
    assert _read(model) == 1, "по новой версии — заново"


def test_the_version_is_stored_with_the_answer(mart):
    """Иначе неизвестно, по каким правилам получен лежащий ответ."""
    _read(_Model())

    with analytics_session() as conn:
        stored = conn.execute(
            "SELECT prompt_version FROM fact_comment_read WHERE entity_id = 1"
        ).fetchone()[0]
    assert stored == comment_reader.PROMPT_VERSION


def test_an_old_mart_is_migrated_before_work(mart, monkeypatch, capsys):
    """Точка входа сама приводит схему в порядок.

    Читатель открывал витрину напрямую и падал на колонке, которой в ней
    ещё нет: миграция живёт в init_analytics_db(), а он её не звал. Тесты
    этого не видели — фикстура создаёт витрину уже по новой схеме, и колонка
    там была всегда. Настоящий случай — боевая база, созданная раньше.
    """
    with analytics_session() as conn:
        conn.execute("DROP TABLE fact_comment_read")
        conn.execute(
            """
            CREATE TABLE fact_comment_read (
                entity_type TEXT NOT NULL DEFAULT 'deal',
                entity_id INTEGER NOT NULL, source_hash TEXT NOT NULL,
                promised TEXT NOT NULL DEFAULT '', promised_at TEXT,
                wait_until TEXT, refused INTEGER NOT NULL DEFAULT 0,
                refused_why TEXT NOT NULL DEFAULT '',
                ready TEXT NOT NULL DEFAULT '', terms TEXT NOT NULL DEFAULT '',
                read_at TEXT NOT NULL, PRIMARY KEY (entity_type, entity_id)
            )
            """
        )
    monkeypatch.setattr("sys.argv", ["comment_reader.py", "--dry-run"])

    assert comment_reader.main() == 0
    assert "К прочтению" in capsys.readouterr().out


def test_the_answer_comes_wrapped_in_a_fence(mart):
    """Живая модель всегда отвечает JSON в ```json-заборе.

    Просили «только JSON без пояснений» — приходит забор. Формат, в
    котором ответ приходит на самом деле, обязан быть в тестах: иначе
    первая же правка разбора сломает боевое чтение, а тесты промолчат.
    """
    fenced = '```json\n{"promised": "Позвонить", "promised_at": "2026-08-15"}\n```'

    assert _read(_Model(fenced)) == 1
    with analytics_session() as conn:
        row = conn.execute(
            "SELECT promised, promised_at FROM fact_comment_read"
        ).fetchone()
    assert tuple(row) == ("Позвонить", "2026-08-15")


def test_a_talkative_answer_is_dropped_and_not_guessed(mart):
    """Пояснение вокруг ответа — это пропуск карточки, а не кривая запись.

    JSON вырезается жадно, от первой скобки до последней. Скобка внутри
    пояснения утащит в кусок лишнее, разбор не состоится — и это верное
    поведение: пустая строка в витрине честнее выдуманной.
    """
    chatty = ('Разбираю запись {тут брокер обещает}: '
              '```json\n{"promised": "Позвонить"}\n```')

    assert _read(_Model(chatty)) == 0
    with analytics_session() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM fact_comment_read"
        ).fetchone()[0] == 0
