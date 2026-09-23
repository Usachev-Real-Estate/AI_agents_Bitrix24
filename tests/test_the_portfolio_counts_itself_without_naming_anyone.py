"""Портфель пересчитывается числами — и ни одного имени в числах нет.

Правило ключа проверено тестами, но тест отвечает на вопрос «делает ли код
то, что задумано», а не «задумано ли верно». Второй ответ даёт живой
портфель: сколько клиентов склеилось по телефону, скольким это запретил
признак агента, что на самом деле означают спорные номера.

Самый острый из этих вопросов — про конфликт телефона. ТЗ отказывается
склеивать два контакта с одним номером, предполагая двух разных людей. В
CRM же чаще встречается обратное: один человек, заведённый дважды, то есть
ровно тот случай, ради которого телефонный ключ и существует. Различаются
они по именам — и различить их можно, ни одного имени не показав.

Это второе требование здесь не меньше первого. Разбор уходит в лог крона,
а лог пересылают.
"""

import json

from clients import census
from clients.keys import Card, assign_keys

TYPES = {"UC_AG": "Агент по недвижимости", "CLIENT": "Клиент"}


def _contact(last="Петров", first="Пётр", phone="+79001112233", **extra):
    card = {"LAST_NAME": last, "NAME": first, "TYPE_ID": "CLIENT",
            "PHONE": [{"VALUE": phone}]}
    card.update(extra)
    return card


def _take(cards, contacts):
    assignment = assign_keys(cards, contacts, type_names=TYPES)
    return census.take(assignment, contacts)


def _sharing_one_number(first_contact, second_contact):
    """Два контакта на одном номере — то есть конфликт."""
    cards = [Card(1, contact_id=77), Card(2, contact_id=88)]
    return _take(cards, {77: first_contact, 88: second_contact})


def test_one_person_entered_twice_is_told_apart():
    """Совпали и фамилия, и имя — это дубль, а не два человека.

    Самый важный из четырёх разрядов: именно он говорит, что отказ от
    склейки по номеру вредит, а не защищает.
    """
    got = _sharing_one_number(_contact(), _contact())

    assert got.conflicts.total == 1
    assert got.conflicts.same_person == 1
    assert got.conflicts.different == 0


def test_namesakes_on_one_number_are_not_declared_the_same_person():
    """Одна фамилия при разных именах — скорее родственники, чем дубль."""
    got = _sharing_one_number(_contact(first="Пётр"), _contact(first="Мария"))

    assert got.conflicts.same_surname == 1
    assert got.conflicts.same_person == 0


def test_two_different_people_on_one_number_are_counted_as_such():
    """Разные фамилии — отказ от склейки верен, и это видно отдельным числом."""
    got = _sharing_one_number(_contact(last="Петров"), _contact(last="Сидорова"))

    assert got.conflicts.different == 1
    assert got.conflicts.same_person == 0


def test_a_contact_without_a_surname_is_not_guessed_about():
    """Пустая фамилия — «не знаем», а не «разные»."""
    got = _sharing_one_number(_contact(last=""), _contact(last="Петров"))

    assert got.conflicts.unknown == 1
    assert got.conflicts.different == 0


def test_a_contact_the_portal_did_not_return_is_not_guessed_about():
    """Контакт, которого нет в ответе портала, ничего не утверждает.

    Конфликт при этом остаётся: номер за двумя контактами числится по
    данным тех, кого портал всё-таки отдал.
    """
    cards = [Card(1, contact_id=77), Card(2, contact_id=88)]
    contacts = {77: _contact(), 88: _contact()}
    assignment = assign_keys(cards, contacts, type_names=TYPES)

    got = census.take(assignment, {77: contacts[77]})

    assert got.conflicts.total == 1
    assert got.conflicts.unknown == 1


def test_agents_are_split_by_what_gave_them_away():
    """Пять веток признака считаются по отдельности.

    Ради этого разбор и затевался: ветки неравноценны, и самая слабая из
    них — название сделки — усилена правилом накопления по контакту.
    """
    cards = [
        Card(1, title="2-к", contact_id=11),
        Card(2, title="3-к", contact_id=22),
        Card(3, title="1-к", contact_id=33),
        Card(4, title="Лариса агент, 2-к", contact_id=44),
        Card(5, title="студия", stage_id="C18:UC_2ZBA0G", contact_id=55),
    ]
    contacts = {
        11: _contact(phone="+79001110001", TYPE_ID="UC_AG"),
        22: _contact(phone="+79001110002", POST="агент"),
        33: _contact(last="Риелтор", phone="+79001110003"),
        44: _contact(phone="+79001110004"),
        55: _contact(phone="+79001110005"),
    }

    got = _take(cards, contacts)

    assert got.agents == 5
    assert got.agents_by_reason == {
        "тип контакта": 1,
        "должность": 1,
        "имя контакта": 1,
        "название сделки": 1,
        "стадия «Агент»": 1,
    }


def test_the_weakest_signal_is_visible_on_its_own():
    """Название сделки видно отдельным числом, а не в общей куче.

    Если бы всё складывалось в одно «агентов N», разобраться, слишком ли
    широко правило, было бы нечем.
    """
    cards = [Card(1, title="агент Пётр", contact_id=11), Card(2, contact_id=22)]
    contacts = {
        11: _contact(phone="+79001110001"),
        22: _contact(phone="+79001110002", POST="агент"),
    }

    got = _take(cards, contacts)

    assert got.agents_by_reason["название сделки"] == 1
    assert got.agents_by_reason["должность"] == 1


def test_clients_are_counted_once_no_matter_how_many_cards():
    """Счёт идёт по клиентам: три сделки одного человека — один клиент.

    По карточкам он бы сам себя переголосовал, и доля агентов поехала бы
    в сторону тех, у кого сделок больше.
    """
    cards = [Card(1, contact_id=77), Card(2, contact_id=77), Card(3, contact_id=77)]

    got = _take(cards, {77: _contact()})

    assert got.cards == 3
    assert got.clients == 1
    assert got.by_kind == {"p": 1}


def test_keys_are_split_by_kind_and_by_reason():
    """Видно и чем ключ кончился, и почему именно этим."""
    cards = [
        Card(1, contact_id=77),                       # обычный, склеится телефоном
        Card(2, title="агент", contact_id=88),        # агент — телефон запрещён
        Card(3),                                      # ни контакта, ни телефона
    ]
    contacts = {77: _contact(phone="+79001110001"), 88: _contact(phone="+79001110002")}

    got = _take(cards, contacts)

    assert got.by_kind == {"c": 1, "d": 1, "p": 1}
    assert got.by_reason["склейка по телефону"] == 1
    assert got.by_reason["агент: склейка по телефону запрещена"] == 1
    assert got.by_reason["ни контакта, ни телефона"] == 1


def test_the_count_holds_no_names_and_no_numbers():
    """В разбор не попадает ни имя клиента, ни его телефон.

    Разбор уходит в лог крона, а лог пересылают. Проверяется не то, что мы
    их не печатаем нарочно, а то, что их там нет: счётчик, случайно
    собранный по ключам-номерам, прошёл бы любую проверку на «мы печатаем
    только числа».
    """
    cards = [Card(1, title="Уникальныйзаголовок", contact_id=77),
             Card(2, contact_id=88)]
    contacts = {
        77: _contact(last="Неповторимов", first="Аристарх", phone="+79995554433"),
        88: _contact(last="Единственнова", first="Пелагея", phone="+79998887766"),
    }

    got = _take(cards, contacts)
    printed = json.dumps(
        {
            "by_kind": got.by_kind, "by_reason": got.by_reason,
            "agents_by_reason": got.agents_by_reason,
            "conflicts": got.conflicts.as_dict(),
            "cards": got.cards, "clients": got.clients, "agents": got.agents,
        },
        ensure_ascii=False,
    )

    for secret in ("Неповторимов", "Аристарх", "Единственнова", "Пелагея",
                   "79995554433", "79998887766", "Уникальныйзаголовок"):
        assert secret not in printed, f"в разбор попало «{secret}»"


def test_an_empty_portfolio_counts_to_zero():
    """Пустой прогон не выдумывает чисел."""
    got = _take([], {})

    assert (got.cards, got.clients, got.agents) == (0, 0, 0)
    assert got.conflicts.as_dict()["всего"] == 0
