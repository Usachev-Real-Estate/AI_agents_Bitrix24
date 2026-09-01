"""Отчёт приходит РОПу лично, а неопознанное — в общий чат.

Решение агентства от 31.08: РОП контролирует своих брокеров, значит и отчёт
должен быть его — а не общим списком, в котором надо искать себя.

Два правила, за которыми этот файл следит:

1. **РОПы названы поимённо.** Пять фамилий дало агентство. До этого карта
   строилась по подстроке в должности — догадка, которая ловит лишних и
   пропускает тех, у кого должность записана иначе.
2. **Молчаливой недостачи нет.** Не нашли РОПа, нашли двух однофамильцев,
   не знаем подразделения брокера — карточки уходят в общий чат, а не
   исчезают. Отчёт, не дошедший ни до кого, выглядит ровно как отчёт, в
   котором не было нарушений.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from qc_delivery import (  # noqa: E402
    ROP_SURNAMES,
    UNASSIGNED,
    build_rop_directory,
    group_deals_by_rop,
    split_delivery,
)


def _user(uid: int, last: str, name: str = "Имя") -> dict[str, Any]:
    return {"ID": str(uid), "LAST_NAME": last, "NAME": name}


def _users() -> list[dict[str, Any]]:
    return [
        _user(11, "Шпырная", "Юлия"),
        _user(12, "Резников", "Константин"),
        _user(13, "Кретов", "Антон"),
        _user(14, "Трофимова", "Светлана"),
        _user(15, "Волкова", "Вера"),
        _user(99, "Петров", "Брокер"),
    ]


def _deal(deal_id: int, broker: int) -> dict[str, Any]:
    return {"ID": str(deal_id), "ASSIGNED_BY_ID": str(broker)}


# --- кто такой РОП -------------------------------------------------------


def test_all_five_are_found_by_surname():
    directory = build_rop_directory(_users())
    assert sorted(r.surname for r in directory.values()) == sorted(ROP_SURNAMES)
    assert directory[11].full_name == "Шпырная Юлия"


def test_someone_who_is_not_on_the_list_is_not_a_rop():
    directory = build_rop_directory(_users())
    assert 99 not in directory


def test_a_missing_rop_is_said_out_loud(caplog):
    """Четыре письма вместо пяти — не результат, а недостача."""
    users = [u for u in _users() if u["LAST_NAME"] != "Кретов"]
    with caplog.at_level("WARNING"):
        directory = build_rop_directory(users)
    assert len(directory) == 4
    assert "кретов" in caplog.text.lower()


def test_namesakes_are_not_resolved_by_guessing(caplog):
    """Ошибка тут — отчёт чужого отдела постороннему человеку."""
    users = _users() + [_user(77, "Кретов", "Однофамилец")]
    with caplog.at_level("WARNING"):
        directory = build_rop_directory(users)
    assert 13 not in directory and 77 not in directory
    assert "однофамил" in caplog.text.lower() or "подходят" in caplog.text.lower()


def test_the_case_and_spaces_of_a_surname_do_not_matter():
    directory = build_rop_directory([_user(21, "  ШПЫРНАЯ  ", "Юлия")])
    assert directory[21].surname == "шпырная"


# --- как раскладываются сделки -------------------------------------------


DEPTS = {101: 1, 102: 2, 103: 3}          # брокер → подразделение
ROPS = {1: 11, 2: 15, 3: 0}               # подразделение → РОП


def test_each_deal_goes_to_the_rop_of_its_broker():
    directory = build_rop_directory(_users())
    groups = group_deals_by_rop(
        [_deal(1, 101), _deal(2, 102)], DEPTS, ROPS, directory,
    )
    assert [d["ID"] for d in groups[11]] == ["1"]
    assert [d["ID"] for d in groups[15]] == ["2"]


def test_a_broker_without_a_department_lands_in_unassigned():
    directory = build_rop_directory(_users())
    groups = group_deals_by_rop([_deal(3, 999)], DEPTS, ROPS, directory)
    assert [d["ID"] for d in groups[UNASSIGNED]] == ["3"]


def test_a_department_without_a_rop_lands_there_too():
    directory = build_rop_directory(_users())
    groups = group_deals_by_rop([_deal(4, 103)], DEPTS, ROPS, directory)
    assert [d["ID"] for d in groups[UNASSIGNED]] == ["4"]


def test_a_rop_outside_the_agencys_list_is_unassigned():
    """Список из пяти фамилий и есть решение о том, кто получает отчёт."""
    directory = build_rop_directory(_users())
    groups = group_deals_by_rop([_deal(5, 101)], {101: 9}, {9: 99}, directory)
    assert [d["ID"] for d in groups[UNASSIGNED]] == ["5"]


# --- лично или в чат -----------------------------------------------------


def test_volkovas_department_goes_to_the_chat_not_to_her():
    directory = build_rop_directory(_users())
    groups = group_deals_by_rop(
        [_deal(1, 101), _deal(2, 102)], DEPTS, ROPS, directory,
    )
    personal, to_chat = split_delivery(groups, directory)
    assert [rop.surname for rop, _ in personal] == ["шпырная"]
    assert [d["ID"] for d in to_chat] == ["2"]


def test_unassigned_cards_join_the_same_chat_message():
    directory = build_rop_directory(_users())
    groups = group_deals_by_rop(
        [_deal(2, 102), _deal(3, 999)], DEPTS, ROPS, directory,
    )
    _personal, to_chat = split_delivery(groups, directory)
    assert sorted(d["ID"] for d in to_chat) == ["2", "3"]


def test_personal_reports_come_in_a_stable_order():
    """Иначе один и тот же прогон каждый раз читается по-новому."""
    directory = build_rop_directory(_users())
    groups = group_deals_by_rop(
        [_deal(1, 101), _deal(6, 104), _deal(7, 105)],
        {101: 1, 104: 4, 105: 5},
        {1: 11, 4: 13, 5: 12},
        directory,
    )
    personal, _chat = split_delivery(groups, directory)
    assert [rop.surname for rop, _ in personal] == [
        "кретов", "резников", "шпырная",
    ]
