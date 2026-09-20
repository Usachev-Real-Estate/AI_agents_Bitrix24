"""Выгрузка для внешнего разбора уезжает наружу — и не должна брать с собой лишнего.

Файл, который собирает scripts/export_for_review.py, отправляют чужой модели.
Обещание в его докстринге простое: «В файл не попадают: ФИО, телефоны,
названия карточек, тексты комментариев, id карточек». Обещание в докстринге
без проверки — это то, что в этом проекте уже ломалось: правило было записано,
а запрос его не соблюдал.

Второе, что здесь держится, — что выгрузка вообще доходит до конца. Разделы
обёрнуты в except, и падение печаталось внутрь готового файла: скрипт выходил
с кодом 0, печатал «Готово», а половины отчёта в нём не было. Один звонок с
пустым DIRECTION — и весь раздел про воронки заменялся сообщением об ошибке.
Такие данные в витрине есть: DIRECTION приходит из телефонии, а она отвечает
не всегда.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

BROKER_NAME = "Иванова Мария Петровна"
LEAD_TITLE = "Квартира на Тверской, 12-45"
PHONE = "+79161234567"
# Обе карточки и оба звонка живут внутри окна выгрузки по умолчанию (6 мес).
CALL_AT = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "export_for_review", _ROOT / "scripts" / "export_for_review.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


export = _load_script()


@pytest.fixture
def databases(tmp_path: Path) -> Path:
    """Две базы в форме боевых, набитые именами, телефонами и заголовками.

    Данные тут нарочно «говорящие»: если что-то из них окажется в готовом
    файле, тест это увидит, а не будет сверять пустые строки.
    """
    from analytics.schema import init_analytics_db
    from db import init_db

    data = tmp_path / "data"
    data.mkdir()

    import db

    db.DB_PATH = data / "violations.db"
    init_db()
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute(
            "INSERT INTO violation_states (entity_type, entity_id, rule, "
            "responsible_id, responsible_name, department, severity, "
            "first_detected_at, last_seen_at, resolved_at, times_seen) "
            "VALUES ('lead', 1, 'lead_new_over_24h', 7, ?, 'Отдел продаж', "
            "'high', datetime('now', '-3 days'), datetime('now'), NULL, 2)",
            (BROKER_NAME,),
        )
        conn.commit()

    analytics = data / "analytics.db"
    init_analytics_db(analytics)
    with sqlite3.connect(analytics) as conn:
        conn.execute(
            "INSERT INTO fact_lead (lead_id, title, status_id, source_id, "
            "assigned_by_id, date_create, is_converted, converted_deal_id, "
            "is_deleted, synced_at) VALUES (1, ?, 'NEW', 'CALL', 7, "
            "datetime('now', '-2 days'), 0, NULL, 0, datetime('now'))",
            (LEAD_TITLE,),
        )
        conn.execute(
            "INSERT INTO fact_deal (deal_id, title, category_id, stage_id, "
            "assigned_by_id, source_id, opportunity, currency_id, date_create, "
            "is_closed, is_won, is_lost, is_deleted, synced_at) VALUES "
            "(1, ?, 18, 'C18:NEW', 7, 'CALL', 100000, 'RUB', "
            "datetime('now', '-2 days'), 0, 0, 0, 0, datetime('now'))",
            (LEAD_TITLE,),
        )
        # Два звонка по одному лиду с ОДНОЙ отметкой времени, у первого
        # DIRECTION пуст. Отметка проставляется явно, а не datetime('now'):
        # падение возникает только при равных датах, когда сортировка
        # доходит до сравнения следующего элемента кортежа, и тест,
        # полагающийся на «оба INSERT успели в одну секунду», ловил бы его
        # через раз.
        for activity_id, direction in ((1, None), (2, 2)):
            conn.execute(
                "INSERT INTO fact_activity (activity_id, owner_type_id, "
                "owner_id, provider_type_id, direction, subject, "
                "responsible_id, created_at, start_time, completed, "
                "synced_at) VALUES (?, 1, 1, 'CALL', ?, ?, 7, ?, ?, 1, ?)",
                (activity_id, direction, PHONE, CALL_AT, CALL_AT, CALL_AT),
            )
        conn.commit()
    return data


def _run(data: Path, *extra: str) -> str:
    out = data / "review_export.md"
    sys.argv = ["export_for_review.py", "--data", str(data), "--out", str(out), *extra]
    export.main()
    return out.read_text(encoding="utf-8")


def test_a_call_without_a_direction_does_not_cost_the_whole_section(databases):
    """Два звонка в одну секунду, у одного DIRECTION пуст — сортировка
    доходила до сравнения None с числом и роняла раздел целиком."""
    report = _run(databases)

    assert "не собран" not in report
    assert "Скорость первого контакта" in report


def test_the_file_carries_no_names_no_phones_and_no_card_titles(databases):
    """Три вида данных, ради которых выгрузку и обезличивали."""
    report = _run(databases)

    assert BROKER_NAME not in report
    assert "Иванова" not in report
    assert PHONE not in report
    assert LEAD_TITLE not in report


def test_no_card_id_leaks_into_the_file(databases):
    """Докстринг обещает четыре вида данных, а проверялись три.

    Идентификатор карточки сам по себе — уже ключ: по нему карточка
    открывается в портале за один клик, и обезличивание теряет смысл.
    """
    report = _run(databases)

    assert "/crm/lead/details/" not in report
    assert "lead_id" not in report
    assert "deal_id" not in report


def test_a_broker_appears_under_a_pseudonym(databases):
    """Псевдоним нужен, чтобы читающая модель видела «один и тот же человек»,
    но не могла назвать его."""
    report = _run(databases)

    assert "Б-01" in report


def test_departments_can_be_left_out_entirely(databases):
    """Отдел плюс счётчики сужают круг до одного человека быстрее, чем кажется."""
    report = _run(databases, "--hide-departments")

    assert "Отдел продаж" not in report


def test_a_broken_section_reports_the_error_without_server_paths(
    databases, monkeypatch,
):
    """Трейсбек несёт пути каталогов сервера, а файл уезжает наружу.

    Разбирать падение всё равно тому, кто запускал: ему трейсбек печатается
    на stderr, а в файл идёт одна строка про тип ошибки.
    """
    def _boom(*_a, **_k):
        raise RuntimeError("таблица уехала")

    monkeypatch.setattr(export, "section_funnels", _boom)

    report = _run(databases)

    assert "Раздел analytics.db не собран" in report
    assert "RuntimeError: таблица уехала" in report
    assert "Traceback" not in report
    assert str(_ROOT) not in report


def test_a_missing_database_is_said_plainly_not_crashed(tmp_path):
    """Скрипт запускают руками из разных каталогов; «нет базы» — не падение."""
    empty = tmp_path / "data"
    empty.mkdir()

    report = _run(empty)

    assert "violations.db не найден" in report
    assert "analytics.db не найден" in report
