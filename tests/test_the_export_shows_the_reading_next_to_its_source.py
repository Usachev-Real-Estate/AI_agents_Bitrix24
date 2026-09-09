"""Выгрузка разбора: сводка на экран, исходник рядом с вынутым.

Проверять разбор через `--show` можно, пока карточек двадцать. Когда
прочитан весь портфель, нужна сводка — форма разбора целиком, потому что
перекос («у всех обещания без срока», «треть портфеля отказалась»)
виден только на всём объёме, а на десятке карточек неотличим от правды.

CSV кладёт рядом вынутое и исходные записи. Без исходника проверять
нечего: поля в таблице выглядят правдоподобно всегда, ошибку видно только
рядом с текстом, из которого её достали.

Отдельно закреплён порядок признаков. Карточка бывает и с обещанием, и с
отказом, и с условиями сразу; называть её надо самым сильным, что в ней
есть, иначе просроченное обещание потеряется среди «есть факты».
"""

from __future__ import annotations

import csv
import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

from schema import analytics_session

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "comment_export.py"


@pytest.fixture
def export():
    spec = importlib.util.spec_from_file_location("comment_export", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["comment_export"] = module
    spec.loader.exec_module(module)
    return module


def _day(shift: int) -> str:
    return (date.today() + timedelta(days=shift)).isoformat()


def _card(conn, deal_id, *, broker="Иванова Мария", body="Позвонить в пятницу",
          **read):
    conn.execute(
        """
        INSERT INTO fact_deal(deal_id, title, category_id, stage_id,
            assigned_by_id, source_id, opportunity, currency_id, date_create,
            date_modify, closedate, is_closed, is_won, is_lost, contact_id,
            is_deleted, synced_at)
        VALUES (?, ?, 0, 'UC_FADPBF', ?, 'ADV', 0, 'RUB',
                '2026-06-01T00:00:00+00:00', '2026-06-01T00:00:00+00:00',
                NULL, 0, 0, 0, 5000, 0, 'x')
        """,
        (deal_id, f"ВГ {deal_id}", deal_id),
    )
    conn.execute("INSERT OR IGNORE INTO dim_user(user_id, name, is_active,"
                 " synced_at) VALUES (?, ?, 1, 'x')", (deal_id, broker))
    conn.execute(
        "INSERT INTO fact_comment(comment_id, entity_type, entity_id,"
        " author_id, body, is_auto, created_at, synced_at)"
        " VALUES (?, 'deal', ?, 1, ?, 0, ?, 'x')",
        (deal_id * 10, deal_id, body, f"{_day(-30)}T10:00:00+00:00"),
    )
    fields = {"promised": "", "promised_at": None, "wait_until": None,
              "refused": 0, "refused_why": "", "ready": "", "terms": ""}
    fields.update(read)
    conn.execute(
        """
        INSERT INTO fact_comment_read(entity_type, entity_id, source_hash,
            promised, promised_at, wait_until, refused, refused_why, ready,
            terms, read_at, prompt_version)
        VALUES ('deal', ?, 'h', :promised, :promised_at, :wait_until,
                :refused, :refused_why, :ready, :terms, '2026-09-09', '4')
        """.replace("?", str(deal_id)),
        fields,
    )


@pytest.fixture
def mart(analytics_db):
    with analytics_session() as conn:
        _card(conn, 9, promised="Позвонить", promised_at=_day(-20))
        _card(conn, 2, promised="Позвонить", promised_at=_day(+5))
        _card(conn, 3, broker="Петров Пётр", refused=1,
              refused_why="свой агент", body="У него свой риэлтор")
        _card(conn, 4, wait_until=_day(+10), body="Собственник в отпуске")
        _card(conn, 5, ready="в рекламе", body="Запустили рекламу")
        _card(conn, 6, body="На связи")
    return analytics_db


# ── Признак карточки ───────────────────────────────────────────────────
def test_the_card_is_named_by_the_strongest_thing_in_it(mart, export):
    """Просроченное обещание сильнее всего остального — ради него всё и делалось."""
    both = {"promised": "Позвонить", "promised_at": _day(-3),
            "ready": "в рекламе", "terms": "2%", "refused": 1,
            "refused_why": "свой агент", "wait_until": None,
            "overdue_days": 3.0}
    assert export._sign(both, _day(0)) == "просрочено"

    # Обещание есть, срок не наступил — карточка ждёт, а не просрочена.
    waiting = dict(both, promised_at=_day(+3), overdue_days=-3.0,
                   refused=0)
    assert export._sign(waiting, _day(0)) == "обещано"

    assert export._sign({"promised": "", "promised_at": None, "refused": 1,
                         "wait_until": None, "ready": "в рекламе",
                         "terms": "", "overdue_days": None},
                        _day(0)) == "отказ"


def test_an_empty_card_says_so(export):
    empty = {"promised": "", "promised_at": None, "wait_until": None,
             "refused": 0, "ready": "", "terms": "", "overdue_days": None}
    assert export._sign(empty, _day(0)) == "пусто"


# ── Сводка ─────────────────────────────────────────────────────────────
def test_the_summary_shows_the_shape_of_the_whole_reading(mart, export,
                                                          capsys, tmp_path):
    """Перекос виден только на всём портфеле, поэтому сводка обязательна."""
    sys.argv = ["comment_export.py", "--out", str(tmp_path)]
    export.main()

    out = capsys.readouterr().out
    assert "# Прочитано карточек: 6" in out
    assert "v4    6" in out
    assert "просрочено   1" in out
    assert "отказ        1" in out
    assert "пусто        1" in out
    assert "обещаний со сроком 2, без срока 0" in out
    assert "Иванова Мария" in out


# ── CSV ────────────────────────────────────────────────────────────────
def test_the_csv_carries_the_source_next_to_the_reading(mart, export,
                                                        tmp_path):
    """Поля без исходной записи проверить нечем: они правдоподобны всегда."""
    sys.argv = ["comment_export.py", "--out", str(tmp_path)]
    export.main()

    with (tmp_path / "read_cards.csv").open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 6
    first = rows[0]
    assert first["deal_id"] == "9", "первой идёт самая просроченная, а не"\
                                    " первая по номеру"
    assert first["sign"] == "просрочено"
    assert first["promised"] == "Позвонить"
    assert "Позвонить в пятницу" in first["notes"]
    assert first["broker"] == "Иванова Мария"


def test_only_one_kind_can_be_asked_for(mart, export, tmp_path):
    """Чтобы посмотреть глазами, файл должен быть небольшим."""
    sys.argv = ["comment_export.py", "--out", str(tmp_path), "--only", "отказ"]
    export.main()

    with (tmp_path / "read_отказ.csv").open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))

    assert [row["deal_id"] for row in rows] == ["3"]
    assert rows[0]["refused_why"] == "свой агент"


def test_the_phone_is_hidden_but_the_meaning_is_not(mart, export, tmp_path):
    """Телефон закрыт, имя и адрес остались — иначе разбирать станет нечего."""
    with analytics_session() as conn:
        conn.execute(
            "UPDATE fact_comment SET body = 'представитель Влад 89156542609'"
            " WHERE entity_id = 5"
        )
    sys.argv = ["comment_export.py", "--out", str(tmp_path)]
    export.main()

    with (tmp_path / "read_cards.csv").open(encoding="utf-8-sig") as handle:
        notes = {row["deal_id"]: row["notes"] for row in csv.DictReader(handle)}

    assert "89156542609" not in notes["5"]
    assert "представитель Влад" in notes["5"]


def test_an_agreed_silence_cancels_the_overdue(export):
    """Договорились ждать — обещание, данное раньше, этим и отменено.

    Ровно так считает work.promises(), откуда советы берут просрочку. Пока
    этой проверки здесь не было, выгрузка насчитывала девяносто две
    просрочки против тридцати семи в сводке — и число из диагностики
    спорило с числом, по которому работают.
    """
    card = {"promised": "Позвонить", "promised_at": _day(-10),
            "overdue_days": 10.0, "wait_until": _day(+20),
            "refused": 0, "ready": "", "terms": ""}

    assert export._sign(card, _day(0)) == "обещано"

    # Срок ожидания истёк — обещание снова спрашивается.
    assert export._sign(dict(card, wait_until=_day(-1)), _day(0)) == "просрочено"


def test_a_closed_card_is_not_in_the_export(mart, export, tmp_path):
    """Работа по закрытой карточке кончилась, разбирать её незачем."""
    with analytics_session() as conn:
        conn.execute("UPDATE fact_deal SET is_closed = 1 WHERE deal_id = 9")
    sys.argv = ["comment_export.py", "--out", str(tmp_path)]
    export.main()

    with (tmp_path / "read_cards.csv").open(encoding="utf-8-sig") as handle:
        ids = [row["deal_id"] for row in csv.DictReader(handle)]
    assert "9" not in ids
