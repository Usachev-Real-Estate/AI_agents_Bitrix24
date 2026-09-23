"""Телефон склеивает клиентов только тогда, когда он действительно чей-то один.

Номер — самый сильный признак и самый опасный. Он склеивает две карточки
одного человека, заведённые под разными именами, — ради этого он и выбран
первым приоритетом. Он же склеит двух разных людей, если брокер вписал
одному телефон другого, и тогда у одного «клиента» окажется чужая история,
чужой ответственный и разбор, написанный не про него.

Поэтому номер, числящийся за несколькими контактами, сам по себе ключа не
даёт: сначала спрашивают имя. Совпало у всех — это один человек, заведённый
дважды, и карточки склеиваются. Разошлось — карточки уходят на ключ по
контакту, а номер остаётся на экране: брокеру он нужен, даже когда не
годится для склейки.

Измерено на живом портфеле: из 237 общих номеров 116 — один человек, 120 —
разные люди. Правило, отказывавшее всем, ошибалось ровно в половине случаев.
"""

from clients.keys import (
    WHY_AGENT,
    WHY_CONFLICT,
    WHY_NO_CONTACT,
    WHY_NO_PHONE,
    WHY_PHONE,
    WHY_PHONE_INVALID,
    WHY_PHONE_SHARED,
    Card,
    assign_keys,
    phone_candidates,
)

TYPES = {"CLIENT": "Клиент"}


def _contact(*phones, name="Пётр", **extra):
    card = {"NAME": name, "TYPE_ID": "CLIENT",
            "PHONE": [{"VALUE": value} for value in phones]}
    card.update(extra)
    return card


def _one(cards, contacts):
    return assign_keys(cards, contacts, type_names=TYPES)


def test_a_city_number_starting_with_eight_is_not_mangled():
    """Десятизначный городской с кодом на восьмёрку остаётся собой.

    Правило первой редакции ТЗ («ведущую 8 заменить на 7; если 10 цифр —
    добавить 7») делало из 8442 55-66-77 номер +77442556677: чужой,
    несуществующий и уводящий клиента к другому ключу. Готовая
    `normalize_phone` проверяет длину ДО замены и потому верна — проверяем
    её результат, а не свою копию правила.
    """
    assert phone_candidates(_contact("8442 55-66-77")) == (("+78442556677", "8442 55-66-77"),)


def test_a_mobile_written_with_a_leading_eight_becomes_seven():
    """Привычная запись мобильного через восьмёрку разбирается."""
    assert phone_candidates(_contact("8 900 111-22-33"))[0][0] == "+79001112233"


def test_two_spellings_of_one_number_are_one_number():
    """Один номер, записанный дважды по-разному, не делает второго псевдонима."""
    got = _one([Card(1, contact_id=77)], {77: _contact("+7 900 111-22-33", "89001112233")})

    assert got.decisions[1].key == "p:+79001112233"
    assert [kind for kind, _ in got.decisions[1].aliases] == ["contact"]


def test_rubbish_is_kept_for_the_screen_and_not_for_the_key():
    """Мусорный номер не строит ключ, но с экрана не пропадает."""
    got = _one([Card(1, contact_id=77)], {77: _contact("нет телефона")}).decisions

    assert got[1].key == "c:77"
    assert got[1].key_reason == WHY_PHONE_INVALID
    assert got[1].phone_valid is False
    assert got[1].phone_raw == "нет телефона"


def test_the_key_takes_the_smallest_number_not_the_first():
    """Ключ не зависит от порядка значений мультиполя.

    Порядок телефонов в карточке Битрикс между прогонами не гарантирует.
    Ключ по «первому» прыгал бы от прогона к прогону, и каждый прыжок —
    это переезд ключа: разбор отцепляется, клиент в списке заводится заново.
    """
    forward = _one([Card(1, contact_id=77)], {77: _contact("+79002223344", "+79001112233")})
    backward = _one([Card(1, contact_id=77)], {77: _contact("+79001112233", "+79002223344")})

    assert forward.decisions[1].key == backward.decisions[1].key == "p:+79001112233"


def test_the_other_numbers_lead_to_the_same_client():
    """Второй номер контакта становится псевдонимом, а не вторым клиентом."""
    got = _one([Card(1, contact_id=77)], {77: _contact("+79001112233", "+79002223344")})

    assert ("phone", "+79002223344") in got.decisions[1].aliases
    assert ("contact", "77") in got.decisions[1].aliases
    assert ("phone", "+79001112233") not in got.decisions[1].aliases, (
        "номер, который сам стал ключом, псевдонимом не нужен: ключ и так ищется"
    )


def test_two_deals_of_one_contact_are_one_client():
    """Две сделки одного человека — один клиент. Ради этого всё и затевалось."""
    cards = [Card(1, contact_id=77), Card(2, contact_id=77)]

    got = _one(cards, {77: _contact("+79001112233")}).decisions

    assert got[1].key == got[2].key == "p:+79001112233"
    assert got[1].key_reason == WHY_PHONE


def test_two_different_people_with_one_number_glue_nobody():
    """Один телефон у двух разных людей — не склейка, а конфликт.

    Оба уходят на ключ по контакту, конфликт записывается, и повторная
    встреча того же номера ничего нового не создаёт: таблица конфликтов
    ключуется номером.
    """
    cards = [Card(1, contact_id=77), Card(2, contact_id=88)]
    contacts = {77: _contact("+79001112233", name="Пётр"),
                88: _contact("+79001112233", name="Мария")}

    got = _one(cards, contacts)

    assert got.decisions[1].key == "c:77"
    assert got.decisions[2].key == "c:88"
    assert got.decisions[1].key_reason == WHY_CONFLICT
    assert got.conflicts == {"+79001112233": (77, 88)}
    assert got.merged == {}
    assert got.decisions[1].phone_valid is True, (
        "номер разобран — запрещена склейка по нему, а не сам номер"
    )
    assert got.decisions[1].phone_norm is None


def test_one_person_entered_twice_is_glued_after_all():
    """Один номер и одно имя на двух карточках — один клиент.

    Это и есть исправление, ради которого правило пересматривали: половина
    общих номеров портфеля — дубль одного человека, и отказ склеивать их
    оставлял брокеру две строки на одного покупателя.
    """
    cards = [Card(1, contact_id=77), Card(2, contact_id=88)]
    contacts = {77: _contact("+79001112233", name="Пётр"),
                88: _contact("+79001112233", name="пётр ")}

    got = _one(cards, contacts)

    assert got.decisions[1].key == got.decisions[2].key == "p:+79001112233"
    assert got.decisions[1].key_reason == WHY_PHONE_SHARED
    assert got.conflicts == {}
    assert got.merged == {"+79001112233": (77, 88)}


def test_a_second_number_does_not_pull_the_twin_off_the_shared_key():
    """Своих номеров у карточки бывает два — ключ всё равно один на двоих.

    Номер выбирается на группу, а не на карточку. Иначе у карточки с двумя
    номерами выигрывал бы её собственный меньший, у её двойника — общий, и
    склейка, ради которой всё затевалось, не состоялась бы ни разу там, где
    у человека записан второй телефон.
    """
    cards = [Card(1, contact_id=77), Card(2, contact_id=88)]
    contacts = {
        77: _contact("+79001112233", "+79000001111"),
        88: _contact("+79001112233"),
    }

    got = _one(cards, contacts)

    assert got.decisions[1].key == got.decisions[2].key == "p:+79000001111"
    assert ("phone", "+79001112233") in got.decisions[1].aliases
    assert got.decisions[1].key_reason == WHY_PHONE_SHARED, (
        "причина смотрит на карточку, а не на номер: клиент собран по имени,"
        " хотя ключом уехал на второй телефон, который ни за кем не числится"
    )


def test_a_chain_of_shared_numbers_is_one_person():
    """Общий номер с одним соседом и другой с другим — все трое один человек.

    У человека бывает три карточки и в каждой свой набор телефонов. Разбор
    парами оставил бы его двумя клиентами.
    """
    cards = [Card(1, contact_id=77), Card(2, contact_id=88), Card(3, contact_id=99)]
    contacts = {
        77: _contact("+79000001111"),
        88: _contact("+79000001111", "+79000002222"),
        99: _contact("+79000002222"),
    }

    got = _one(cards, contacts).decisions

    assert got[1].key == got[2].key == got[3].key == "p:+79000001111"


def test_one_name_on_two_numbers_glues_neither():
    """Одно имя на РАЗНЫХ общих номерах — заполнитель, а не человек.

    Карточки, заведённые автоматом, получают одинаковую подпись. Склейка по
    ней свела бы в одного клиента незнакомых людей с разных номеров. Отказ
    стоит дёшево: эти карточки остаются ровно там, где были до правила.
    """
    cards = [Card(i, contact_id=70 + i) for i in range(1, 5)]
    contacts = {
        71: _contact("+79000001111"), 72: _contact("+79000001111"),
        73: _contact("+79000002222"), 74: _contact("+79000002222"),
    }

    got = _one(cards, contacts)

    assert got.merged == {}
    assert sorted(got.conflicts) == ["+79000001111", "+79000002222"]
    assert {d.key for d in got.decisions.values()} == {"c:71", "c:72", "c:73", "c:74"}


def test_a_nameless_twin_is_not_glued():
    """Безымянная карточка рядом с живой не склеивается.

    Почти наверняка это её же автоматический двойник — но «почти» здесь и
    есть ответ: цену ошибочной склейки платит брокер, получивший чужую
    историю в карточке клиента.
    """
    cards = [Card(1, contact_id=77), Card(2, contact_id=88)]
    contacts = {77: _contact("+79001112233"), 88: _contact("+79001112233", name="")}

    got = _one(cards, contacts)

    assert got.merged == {}
    assert got.decisions[1].key == "c:77"


def test_two_nameless_cards_are_not_declared_one_person():
    """Двое безымянных на одном номере — не один человек, а «не знаем».

    Отдельно от соседнего теста: там одна карточка названа, и сравнение
    отвергает пару уже по разнице имён. Здесь имён нет ни у кого, и
    отвергнуть пару может только прямое требование «названы все». Без него
    два пустых имени совпали бы друг с другом, и семейный телефон свёл бы
    в одного клиента двух разных людей.
    """
    cards = [Card(1, contact_id=77), Card(2, contact_id=88)]
    contacts = {77: _contact("+79001112233", name=""),
                88: _contact("+79001112233", name="")}

    got = _one(cards, contacts)

    assert got.merged == {}
    assert got.decisions[1].key == "c:77"
    assert got.decisions[2].key == "c:88"


def test_an_agent_found_on_one_twin_covers_the_other():
    """Признак агента переходит на все карточки одного человека.

    Решение агентства — «одна сделка назвала агентом, значит агент» — про
    человека, а не про строку справочника. Без перехода тот же человек
    оказался бы наполовину агентом: одна его карточка склеилась бы по
    телефону, вторая нет.
    """
    cards = [Card(1, contact_id=77), Card(2, title="Лариса агент, 2-к", contact_id=88)]
    contacts = {77: _contact("+79001112233"), 88: _contact("+79001112233")}

    got = _one(cards, contacts).decisions

    assert got[1].is_agent is True
    assert got[2].is_agent is True
    assert got[1].key == "c:77", "агент по телефону не склеивается"
    assert got[1].key_reason == WHY_AGENT


def test_a_shared_number_is_not_offered_as_an_alias_either():
    """Спорный номер не становится и псевдонимом.

    Псевдоним — это «искать клиента по этому значению». По спорному номеру
    искать нельзя: он привёл бы к одному из двух наугад, а ключ таблицы
    псевдонимов не дал бы записать второго — прогон упал бы на вставке.
    """
    cards = [Card(1, contact_id=77), Card(2, contact_id=88)]
    contacts = {
        77: _contact("+79001112233", "+79002223344", name="Пётр"),
        88: _contact("+79001112233", name="Мария"),
    }

    got = _one(cards, contacts)

    assert got.decisions[1].key == "p:+79002223344", "свой номер у контакта остался"
    everything = got.decisions[1].aliases + got.decisions[2].aliases
    assert all(value != "+79001112233" for _kind, value in everything)


def test_a_contact_the_portal_did_not_return_still_groups_its_deals():
    """Удалённый контакт остаётся ключом для своих сделок.

    Сваливать их в `d:<сделка>` значит рассыпать одного клиента на
    карточки — ровно в тот день, когда кто-то почистил справочник.
    """
    cards = [Card(1, contact_id=77), Card(2, contact_id=77)]

    got = _one(cards, {}).decisions

    assert got[1].key == got[2].key == "c:77"
    assert got[1].key_reason == WHY_NO_PHONE


def test_a_deal_with_neither_contact_nor_phone_stands_alone():
    """Последний приоритет: своя сделка — свой клиент."""
    got = _one([Card(9)], {}).decisions

    assert got[9].key == "d:9"
    assert got[9].key_reason == WHY_NO_CONTACT
    assert got[9].aliases == ()


def test_a_phone_written_as_a_plain_string_is_still_read():
    """Форма поля не должна стоить контакту телефона.

    Портал отдаёт мультиполе списком словарей, а выгрузки и фикстуры кладут
    туда голую строку. Упасть или промолчать здесь — значит потерять номер
    целого контакта.
    """
    got = _one([Card(1, contact_id=77)], {77: {"NAME": "П", "PHONE": "89001112233"}})

    assert got.decisions[1].key == "p:+79001112233"


def test_a_stray_contact_does_not_take_a_number_from_its_owner():
    """Контакт, которого нет ни на одной карточке, конфликта не создаёт.

    Справочник контактов приезжает из портала целиком, и посторонняя
    карточка с тем же номером не должна отбирать склейку у того, кто
    действительно в портфеле.
    """
    got = _one([Card(1, contact_id=77)], {77: _contact("+79001112233"),
                                          99: _contact("+79001112233")})

    assert got.conflicts == {}
    assert got.decisions[1].key == "p:+79001112233"
