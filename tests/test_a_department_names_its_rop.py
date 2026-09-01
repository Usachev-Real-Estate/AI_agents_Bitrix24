"""Отдел на дашборде знает своего РОПа — по списку фамилий, а не по догадке.

Учётку РОПа заводят руками, отделы задают числами. Ошибиться в числе значит
показать РОПу чужих брокеров, а в голом списке идентификаторов такую опечатку
никто не заметит. Фамилии РОПов агентство назвало поимённо (qc_delivery),
и дашборд берёт их оттуда же, откуда рассылка QC: двум ответам на вопрос
«чей это отдел» расходиться нельзя.
"""

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

import metrics
from qc_delivery import ROP_SURNAMES
from schema import analytics_session
from scope import Scope, scoped_session


def _person(conn, user_id, name, last_name, department_id, active=1):
    conn.execute(
        "INSERT INTO dim_user(user_id, name, last_name, department_id, "
        "department_name, is_active, synced_at) VALUES (?, ?, ?, ?, ?, ?, 'x')",
        (user_id, name, last_name, department_id, f"Отдел {department_id}", active),
    )


@pytest.fixture
def people(analytics_db):
    def seed(rows):
        with analytics_session() as conn:
            for row in rows:
                _person(conn, *row)
        return scoped_session(Scope.everything())

    return seed


def test_the_named_rop_is_found_by_surname(people):
    """Фамилия из списка агентства — отдел получает своего руководителя."""
    session = people([
        (11, "Юлия Шпырная", "Шпырная", 44),
        (12, "Иван Петров", "Петров", 44),
        (13, "Пётр Резников", "Резников", 50),
    ])
    with session as conn:
        directory = metrics.rop_by_department(conn)

    assert directory[44]["user_id"] == 11
    assert directory[50]["name"] == "Пётр Резников"
    assert 44 in directory and directory[44]["surname"] == "шпырная"


def test_a_broker_is_not_mistaken_for_a_rop(people):
    """Кого агентство не называло, тот РОПом не становится."""
    session = people([(21, "Иван Петров", "Петров", 44)])
    with session as conn:
        assert metrics.rop_by_department(conn) == {}


def test_two_people_with_one_surname_leave_the_department_without_a_rop(people):
    """Однофамильцев не разрешаем догадкой — как и рассылка QC.

    Взять первого попавшегося значит с вероятностью в половину открыть
    человеку чужой отдел. Пустой ответ честнее неверного: админ разберётся
    руками и укажет отделы явно.
    """
    session = people([
        (31, "Пётр Кретов", "Кретов", 44),
        (32, "Семён Кретов", "Кретов", 50),
    ])
    with session as conn:
        assert metrics.rop_by_department(conn) == {}


def test_a_fired_rop_no_longer_owns_the_department(people):
    """Уволенный РОП остаётся в витрине, но отделом больше не владеет."""
    session = people([(41, "Мария Трофимова", "Трофимова", 44, 0)])
    with session as conn:
        assert metrics.rop_by_department(conn) == {}


def test_the_surname_list_is_the_one_the_qc_mailing_uses():
    """Список фамилий не копируется в дашборд, а берётся из одного места."""
    assert "шпырная" in ROP_SURNAMES and "волкова" in ROP_SURNAMES


def test_an_unknown_surname_stops_the_command_instead_of_guessing(analytics_db):
    """`--rop Иванов` не заводит учётку «на всякий случай»: он падает.

    Учётка, собранная по догадке, открывает РОПу чужой отдел, и заметят это
    не сразу. Молчаливой недостачи тут быть не должно — команда говорит вслух
    и предлагает указать отделы руками.
    """
    import manage

    with pytest.raises(SystemExit) as failure:
        manage._departments_of_rop("Иванов")
    assert "не найден" in str(failure.value)
