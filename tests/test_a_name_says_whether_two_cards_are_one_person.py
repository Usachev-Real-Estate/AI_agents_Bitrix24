"""Сравнение имён — отдельный вопрос со своим ответом.

Правило ключа спрашивает его про каждый номер, который числится за
несколькими контактами: один это человек, заведённый дважды, или разные
люди на одном телефоне. Измерено на живом портфеле — из 237 таких номеров
116 первое и 120 второе, то есть цена ошибки здесь платится в обе стороны
и примерно поровну.

Проверяется напрямую, а не через ключи: у `one_person` свой договор
(«да — только если названы все и названы одинаково»), и держаться он обязан
сам по себе. Вызывающая сторона отбраковывает безымянный отпечаток ещё раз,
и через неё нарушение договора не видно — а функция публичная, и следующий
её вызов может оказаться без второй проверки.
"""

from clients.names import fingerprint, name_parts, one_person


def _named(last="Петров", first="Пётр", middle=""):
    return {"LAST_NAME": last, "NAME": first, "SECOND_NAME": middle}


NAMELESS = {"LAST_NAME": "", "NAME": "", "SECOND_NAME": ""}


def test_the_same_name_twice_is_one_person():
    """Ради этого случая всё и затевалось: дубль одного человека."""
    assert one_person([_named(), _named()]) is True


def test_nobody_named_is_not_an_answer():
    """Две безымянные карточки — «не знаем», а не «один человек».

    Самый опасный случай: пустое имя совпадает с пустым, и правило,
    сравнивающее только «одинаково ли», свело бы в одного клиента двух
    незнакомых людей с общего семейного телефона.
    """
    assert one_person([NAMELESS, NAMELESS]) is False


def test_one_nameless_card_against_a_living_one_is_not_an_answer_either():
    """Огрызок рядом с живой карточкой — почти наверняка дубль, но «почти».

    Цену ошибочной склейки платит брокер, которому в карточку клиента
    приедет чужая история и чужой ответственный.
    """
    assert one_person([NAMELESS, _named()]) is False


def test_different_names_are_different_people():
    """Разные имена на одном номере — семья, работа, номер брокера."""
    assert one_person([_named(first="Пётр"), _named(first="Мария")]) is False


def test_a_missing_contact_counts_as_nameless():
    """Контакта нет вовсе — это не совпадение имён, а его отсутствие.

    Портал отдаёт не всех: контакт мог быть удалён между прогонами. Упасть
    здесь нельзя, а посчитать отсутствие за совпадение — тем более.
    """
    assert one_person([None, _named()]) is False
    assert one_person([None, None]) is False


def test_nothing_at_all_is_not_one_person():
    """Пустой список не делает никого одним человеком."""
    assert one_person([]) is False


def test_one_card_is_trivially_itself():
    """Одна названная карточка — это она сама, и спорить не о чем."""
    assert one_person([_named()]) is True


def test_case_and_spaces_do_not_make_a_second_person():
    """«ИВАНОВ » и «Иванов» — один человек.

    Разойтись они имеют право в базе, а не в ответе на вопрос «кто это».
    """
    assert one_person([_named(last="ИВАНОВ "), _named(last="иванов")]) is True


def test_a_name_typed_into_the_wrong_field_is_still_the_same_name():
    """Имя и фамилия, перепутанные местами, — тот же человек.

    Карточку заводят руками и в спешке. Сравнение, зависящее от того, кто
    куда что положил, отвергло бы дубль ровно там, где он виден глазом.
    """
    assert one_person([_named(last="Петров", first="Пётр"),
                       _named(last="Пётр", first="Петров")]) is True


def test_a_part_of_the_name_is_not_the_whole_name():
    """Отчество, заполненное у одного и пустое у другого, — не совпадение.

    Совпасть должен ВЕСЬ набор частей: «Петров Пётр» и «Петров Пётр
    Сергеевич» бывают и одним человеком, и отцом с сыном, а отличить их
    карточка не даёт.
    """
    assert one_person([_named(), _named(middle="Сергеевич")]) is False


def test_an_empty_field_does_not_become_a_part_of_the_name():
    """Пустое поле не участвует в сравнении и не занимает места."""
    assert name_parts(_named(middle="")) == ("петров", "пётр")
    assert fingerprint(NAMELESS) == "", "имени нет — и отпечатка нет"
