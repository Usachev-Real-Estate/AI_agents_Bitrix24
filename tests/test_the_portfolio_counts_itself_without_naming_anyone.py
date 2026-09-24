"""Портфель пересчитывается числами — и ни одного имени в числах нет.

Правило ключа проверено тестами, но тест отвечает на вопрос «делает ли код
то, что задумано», а не «задумано ли верно». Второй ответ даёт живой
портфель, и первый же прогон на боевых данных показал, что телефонный ключ
не склеивает никого: приоритет 1 раздела 2.4 отменён правилом конфликта из
того же раздела.

Разбор ответил: из 237 спорных номеров 116 — один и тот же человек,
заведённый дважды, 120 — действительно разные люди. Правило по этому ответу
исправлено (см. clients.names), и разбор остаётся при нём дальше: он считает,
сколько номеров склеилось по совпавшему имени, а сколько осталось спорными и
почему именно. Без этого счёта изменение правила пришлось бы принимать на
веру каждую ночь.

Имён при этом не показывает ни одного: он уходит в лог крона, а лог
пересылают.
"""

import json

from clients import census
from clients.census import Linked
from clients.keys import Card, assign_keys

TYPES = {"UC_AG": "Агент по недвижимости", "CLIENT": "Клиент"}


def _contact(last="Петров", first="Пётр", phone="+79001112233", **extra):
    card = {"LAST_NAME": last, "NAME": first, "TYPE_ID": "CLIENT",
            "PHONE": [{"VALUE": phone}]}
    card.update(extra)
    return card


def _nameless(phone="+79001112233"):
    """Карточка, заведённая автоматом по входящему звонку: имени нет."""
    return {"LAST_NAME": "", "NAME": "", "TYPE_ID": "CLIENT",
            "PHONE": [{"VALUE": phone}]}


def _take(cards, contacts, linked=None):
    assignment = assign_keys(cards, contacts, type_names=TYPES)
    return census.take(assignment, contacts, cards=linked)


def _sharing_one_number(first_contact, second_contact, linked=None):
    """Два контакта на одном номере — то есть конфликт."""
    cards = [Card(1, contact_id=77), Card(2, contact_id=88)]
    return _take(cards, {77: first_contact, 88: second_contact}, linked)


def _verdicts(got):
    return got.conflicts.by_verdict


def test_one_person_entered_twice_becomes_one_client():
    """Имя совпало целиком — это дубль, и он склеивается, а не считается.

    Ради этого разбор и затевался: две карточки одного человека на одном
    номере обязаны стать одним клиентом, а не двумя строками списка, по
    которым брокер звонит дважды.
    """
    got = _sharing_one_number(_contact(), _contact())

    assert got.clients == 1
    assert got.merged_phones == 1
    assert got.merged_contacts == 2
    assert got.conflicts.total == 0, "склеенный номер спорным больше не числится"


def test_a_name_in_one_field_is_still_a_name():
    """Фамилия пуста, имя лежит строкой — сравнение всё равно работает.

    Ради этого разбор и переписан. Первая редакция опиралась на фамилию и
    отправила в «не знаем» 231 конфликт из 237: у карточек, заведённых
    автоматом по звонку, фамилия чаще всего пуста, а имя лежит одним
    полем.
    """
    got = _sharing_one_number(
        _contact(last="", first="иван петров"),
        _contact(last="", first="Иван Петров"),
    )

    assert got.clients == 1
    assert got.merged_phones == 1


def test_one_name_on_many_numbers_is_a_placeholder_not_a_person():
    """Одно имя на разных общих номерах — заполнитель, и склейки не даёт.

    Карточки, заведённые автоматом, получают одну и ту же подпись. Склейка
    по ней собрала бы в одного клиента незнакомых людей с разных номеров —
    самую дорогую из возможных ошибок. Защита видна в разборе: такие номера
    остаются спорными с вердиктом «тот же человек», и цена её известна
    числом в каждом прогоне.
    """
    cards = [Card(i, contact_id=70 + i) for i in range(1, 5)]
    contacts = {
        71: _contact(phone="+79001110001"), 72: _contact(phone="+79001110001"),
        73: _contact(phone="+79001110002"), 74: _contact(phone="+79001110002"),
    }

    got = _take(cards, contacts)

    assert got.merged_phones == 0
    assert got.conflicts.total == 2
    assert _verdicts(got)[census.SAME_PERSON] == 2
    assert got.clients == 4
    assert got.namesake_spread == {2: 1}, (
        "рядом с ценой защиты стоит её причина: одно имя на двух группах —"
        " это тёзки, на десятке — заполнитель, и отказ у них один и тот же"
    )


def test_a_nameless_stub_against_a_living_card_is_told_apart():
    """Огрызок без имени против живой карточки — отдельный разряд.

    Это самый вероятный вид дубля: карточку завёл автомат по входящему
    звонку, а человек уже был заведён руками.
    """
    got = _sharing_one_number(_nameless(), _contact())

    assert _verdicts(got)[census.ONE_NAMELESS] == 1
    assert _verdicts(got)[census.NO_NAMES] == 0


def test_two_nameless_stubs_say_nothing():
    """Двое без имён — «не знаем», и это честный ответ, а не разряд."""
    got = _sharing_one_number(_nameless(), _nameless())

    assert _verdicts(got)[census.NO_NAMES] == 1


def test_namesakes_on_one_number_are_not_declared_the_same_person():
    """Одна фамилия при разных именах — скорее родственники, чем дубль."""
    got = _sharing_one_number(_contact(first="Пётр"), _contact(first="Мария"))

    assert _verdicts(got)[census.SAME_SURNAME] == 1
    assert _verdicts(got)[census.SAME_PERSON] == 0


def test_two_different_people_on_one_number_are_counted_as_such():
    """Разные фамилии — отказ от склейки верен, и это видно отдельным числом."""
    got = _sharing_one_number(_contact(last="Петров"), _contact(last="Сидорова"))

    assert _verdicts(got)[census.DIFFERENT] == 1


def test_a_number_on_a_crowd_is_told_from_a_number_on_two():
    """Номер на двоих — скорее дубль; номер на пятерых — офис или агент."""
    cards = [Card(i, contact_id=70 + i) for i in range(1, 6)]
    contacts = {70 + i: _contact(last=f"Контакт{i}") for i in range(1, 6)}

    got = _take(cards, contacts)

    assert got.conflicts.total == 1
    assert got.conflicts.by_owners == {"4+": 1}


def test_a_seller_who_is_also_a_buyer_is_visible_in_the_conflict():
    """Две воронки на одном номере — довод за то, что это один человек.

    Агентство говорит прямо: клиент бывает и собственником, и покупателем.
    Такой конфликт объясняется ролями, а не двумя разными людьми.
    """
    got = _sharing_one_number(
        _nameless(), _nameless(),
        linked={1: Linked(category_id=0, assignee_id=10),
                2: Linked(category_id=18, assignee_id=10)},
    )

    assert got.conflicts.both_funnels == 1
    assert got.conflicts.several_brokers == 0


def test_two_brokers_on_one_number_are_visible():
    """Разные ответственные на одном номере — второй довод той же природы."""
    got = _sharing_one_number(
        _nameless(), _nameless(),
        linked={1: Linked(category_id=18, assignee_id=10),
                2: Linked(category_id=18, assignee_id=20)},
    )

    assert got.conflicts.several_brokers == 1
    assert got.conflicts.both_funnels == 0


def test_a_client_in_both_funnels_is_counted_at_the_portfolio_level():
    """Склеившийся клиент с двумя ролями считается и сам по себе.

    Нужно как база сравнения: если таких клиентов в портфеле и так много,
    то 237 спорных номеров — скорее всего то же самое, только не
    склеенное.
    """
    cards = [Card(1, contact_id=77), Card(2, contact_id=77)]
    linked = {1: Linked(category_id=0, assignee_id=10),
              2: Linked(category_id=18, assignee_id=20)}

    got = _take(cards, {77: _contact()}, linked)

    assert got.clients == 1
    assert got.both_funnels == 1
    assert got.several_brokers == 1


def test_a_client_with_one_role_and_one_broker_is_not_counted():
    """Обратная половина: без разброса счётчики молчат."""
    cards = [Card(1, contact_id=77), Card(2, contact_id=77)]
    linked = {1: Linked(category_id=18, assignee_id=10),
              2: Linked(category_id=18, assignee_id=10)}

    got = _take(cards, {77: _contact()}, linked)

    assert (got.both_funnels, got.several_brokers) == (0, 0)


def test_an_empty_field_is_not_a_second_value():
    """Пустой ответственный — не второй брокер, пустая воронка — не вторая роль.

    В витрине оба поля бывают пустыми. Считая пустоту значением, разбор
    насчитал бы разброс там, где его нет, и раздул бы ровно тот довод,
    ради которого затевался.
    """
    cards = [Card(1, contact_id=77), Card(2, contact_id=77)]
    linked = {1: Linked(category_id=18, assignee_id=10),
              2: Linked(category_id=None, assignee_id=None)}

    got = _take(cards, {77: _contact()}, linked)

    assert (got.both_funnels, got.several_brokers) == (0, 0)


def test_agents_are_split_by_what_gave_them_away():
    """Пять веток признака считаются по отдельности."""
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
        "тип контакта": 1, "должность": 1, "имя контакта": 1,
        "название сделки": 1, "стадия «Агент»": 1,
    }


def test_clients_are_counted_once_no_matter_how_many_cards():
    """Счёт идёт по клиентам: три сделки одного человека — один клиент."""
    cards = [Card(1, contact_id=77), Card(2, contact_id=77), Card(3, contact_id=77)]

    got = _take(cards, {77: _contact()})

    assert (got.cards, got.clients) == (3, 1)
    assert got.by_kind == {"p": 1}


def test_keys_are_split_by_kind_and_by_reason():
    """Видно и чем ключ кончился, и почему именно этим."""
    cards = [Card(1, contact_id=77), Card(2, title="агент", contact_id=88), Card(3)]
    contacts = {77: _contact(phone="+79001110001"), 88: _contact(phone="+79001110002")}

    got = _take(cards, contacts)

    assert got.by_kind == {"c": 1, "d": 1, "p": 1}
    assert got.by_reason["склейка по телефону"] == 1
    assert got.by_reason["агент: склейка по телефону запрещена"] == 1
    assert got.by_reason["ни контакта, ни телефона"] == 1


def test_the_count_holds_no_names_and_no_numbers():
    """В разбор не попадает ни имя клиента, ни его телефон.

    Проверяется не то, что мы их не печатаем нарочно, а то, что их там
    нет: счётчик, случайно собранный по ключам-номерам, прошёл бы любую
    проверку вида «мы печатаем только числа».
    """
    cards = [Card(1, title="Уникальныйзаголовок", contact_id=77), Card(2, contact_id=88)]
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
            "both_funnels": got.both_funnels, "several_brokers": got.several_brokers,
            "merged_phones": got.merged_phones,
            "merged_contacts": got.merged_contacts,
            "namesake_spread": got.namesake_spread,
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
