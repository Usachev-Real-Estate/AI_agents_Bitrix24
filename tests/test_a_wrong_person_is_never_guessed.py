"""Имя сопоставляется человеку либо точно, либо никак.

Норма и роль привязываются к учётной записи по имени — другого ключа нет.
Ошибка здесь стоит дорого и незаметна: норма, ушедшая однофамильцу, попадает
в план чужого отдела, и оба отдела показывают неверное выполнение, оставаясь
правдоподобными.

Поэтому оба инструмента при неоднозначности отказываются работать и печатают
кандидатов, а не выбирают первого попавшегося. Здесь проверено, что отказ
действительно происходит, и что нормальные написания при этом проходят:
слишком строгое сравнение так же вредно, как слишком вольное — оно молча
теряет строки плана.
"""

import sys
from pathlib import Path

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

from schema import analytics_session, get_connection

# Скрипты лежат вне src/, а conftest кладёт на путь только его. Кладём
# каталог, а не грузим файл по пути: plan_roster импортирует plan_norms.
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import plan_norms  # noqa: E402
import plan_roster  # noqa: E402


def _user(conn, user_id, name, last_name, dept_id=60, dept_name="Кретов", active=1):
    conn.execute(
        "INSERT INTO dim_user(user_id, name, last_name, department_id,"
        " department_name, is_active, synced_at) VALUES (?, ?, ?, ?, ?, ?, 'x')",
        (user_id, name, last_name, dept_id, dept_name, active),
    )


@pytest.fixture
def portal(analytics_db):
    with analytics_session() as conn:
        _user(conn, 1, "Светлана Трутаева", "Трутаева")
        _user(conn, 2, "Пётр Семёнов", "Семёнов")
        _user(conn, 3, "Иван Петров", "Петров")
        _user(conn, 4, "Сергей Петров", "Петров", dept_id=50, dept_name="Волкова")
        _user(conn, 5, "Уволенный Тёзка", "Трутаева", active=0)
    return analytics_db


# --------------------------------------------------------------------------
# сопоставление
# --------------------------------------------------------------------------

@pytest.mark.parametrize("written", [
    "Трутаева Светлана",     # обратный порядок слов
    "Светлана Трутаева",
    "  светлана   трутаева ",  # лишние пробелы и регистр
])
def test_a_name_is_found_however_it_is_written(portal, written):
    """В списке от агентства порядок слов и регистр произвольные."""
    assert plan_norms.name_key(written) == plan_norms.name_key("Светлана Трутаева")


def test_yo_and_ye_are_the_same_person(portal):
    """«Семенов» и «Семёнов» — один человек, и норму терять на этом нельзя."""
    assert plan_norms.name_key("Петр Семенов") == plan_norms.name_key("Пётр Семёнов")


def test_a_namesake_is_refused_not_picked(portal):
    """Два Петровых — отказ. Норма не тому отделу выглядит нормально на экране."""
    conn = get_connection(portal, readonly=True)
    try:
        found = plan_roster.find_user(conn, "Петров")
    finally:
        conn.close()

    assert len(found) == 2
    assert plan_roster.put("Петров", plan_roster.ROLE_ROP, None, "", "*") == 2, (
        "при однозначном выборе инструмент обязан остановиться, а не выбрать первого"
    )


def test_a_full_name_resolves_what_a_surname_cannot(portal):
    """Полное имя разводит однофамильцев — этого и хватает."""
    conn = get_connection(portal, readonly=True)
    try:
        found = plan_roster.find_user(conn, "Сергей Петров")
    finally:
        conn.close()

    assert [u["user_id"] for u in found] == [4]


def test_an_unknown_name_is_reported_not_silently_dropped(portal):
    """Ненайденная строка — код возврата, а не тихий пропуск."""
    assert plan_roster.put("Нет Такого", plan_roster.ROLE_ROP, None, "", "*") == 1


# --------------------------------------------------------------------------
# разбор списка норм
# --------------------------------------------------------------------------

def test_a_fired_namesake_does_not_take_the_norm(portal):
    """Уволенный тёзка норму не перехватывает: сверка идёт по активным."""
    conn = get_connection(portal, readonly=True)
    try:
        result = plan_norms.match(conn, [("Светлана Трутаева", 3.5)])
    finally:
        conn.close()

    assert [row[2]["user_id"] for row in result["matched"]] == [1]
    assert not result["ambiguous"]


def test_a_comma_is_a_decimal_point(tmp_path):
    """«3,5» из русской таблицы — это 3.5, а не ошибка разбора."""
    path = tmp_path / "plan.tsv"
    path.write_text("Иван Петров\t3,5\n", encoding="utf-8")
    assert plan_norms.read_list(path) == [("Иван Петров", 3.5)]


def test_a_line_without_an_amount_is_an_error(tmp_path):
    """Строка без суммы обрывает разбор, а не пропускается.

    Пропущенная строка — это норма, которой не будет в плане, и отдел
    покажется лучше, чем он есть. Молчать здесь нельзя.
    """
    path = tmp_path / "plan.tsv"
    path.write_text("Иван Петров\n", encoding="utf-8")
    with pytest.raises(ValueError, match="нужны имя и сумма"):
        plan_norms.read_list(path)


def test_comments_and_blank_lines_are_skipped(tmp_path):
    path = tmp_path / "plan.tsv"
    path.write_text("# план 3 квартала\n\nИван Петров\t4.5\n", encoding="utf-8")
    assert plan_norms.read_list(path) == [("Иван Петров", 4.5)]
