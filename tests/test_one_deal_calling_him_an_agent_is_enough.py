"""Признак агента накапливается по контакту, а не живёт в карточке.

Решение агентства: одна сделка назвала контрагента агентом — агент и все
остальные сделки этого контакта.

Без накопления признак зависел бы от карточки. У `classify_counterparty`
есть ветка по названию СДЕЛКИ, и брокеры действительно подписывают такие
заголовки словом «агент» — но подписывают не все и не всегда. Один и тот же
контакт выходил бы агентом в той сделке, где заголовок подписан, и клиентом
в соседней, где не подписан. Две сделки одного человека уезжали бы к разным
клиентам, а правка заголовка молча переносила бы карточку из одного в
другой, отцепляя от неё разбор.

Цена обратной ошибки известна и записана в ТЗ как V4: риелтор с сорока
сделками от сорока покупателей становится одним «клиентом» с одним
ответственным из сорока.
"""

from clients.keys import AGENT_STAGE_WHY, WHY_AGENT, WHY_PHONE, Card, assign_keys

TYPES = {"UC_AG": "Агент", "CLIENT": "Клиент"}

# Один контакт, один телефон, обычный человек по всем признакам.
BUYER = {"NAME": "Пётр", "TYPE_ID": "CLIENT", "PHONE": [{"VALUE": "+7 900 111-22-33"}]}


def _cards(*pairs):
    """Карточки из пар (номер сделки, заголовок)."""
    return [Card(deal_id=did, title=title, contact_id=77) for did, title in pairs]


def test_a_deal_titled_agent_marks_the_whole_contact():
    """Подписали заголовок в одной сделке — агент во всех сделках контакта."""
    cards = _cards((1, "2-к, до 8 млн"), (2, "Лариса агент, 3-к"))

    got = assign_keys(cards, {77: BUYER}, type_names=TYPES).decisions

    assert got[1].is_agent and got[2].is_agent
    assert got[1].key == got[2].key == "c:77", "обе сделки обязаны остаться у одного ключа"
    assert got[1].key_reason == WHY_AGENT
    assert got[1].agent_reason == got[2].agent_reason, (
        "причина обязана быть одна: иначе в карточках клиента она зависит от порядка"
    )


def test_without_the_second_deal_the_same_contact_is_an_ordinary_client():
    """Обратная половина: без агентской сделки тот же контакт склеивается по телефону.

    Без неё предыдущий тест был бы истинен и для правила «всех подряд
    считать агентами».
    """
    got = assign_keys(_cards((1, "2-к, до 8 млн")), {77: BUYER}, type_names=TYPES).decisions

    assert got[1].is_agent is False
    assert got[1].key == "p:+79001112233"
    assert got[1].key_reason == WHY_PHONE


def test_half_the_portfolio_gives_a_different_answer():
    """Ключи нельзя считать партиями — и это записано тестом, а не только словами.

    Прогон с `--limit`, увидевший первую сделку и не увидевший вторую,
    назовёт того же человека клиентом и склеит его по телефону. Поэтому
    неполный прогон ключи писать не имеет права.
    """
    cards = _cards((1, "2-к, до 8 млн"), (2, "Лариса агент, 3-к"))

    whole = assign_keys(cards, {77: BUYER}, type_names=TYPES).decisions
    half = assign_keys(cards[:1], {77: BUYER}, type_names=TYPES).decisions

    assert whole[1].key != half[1].key


def test_an_agents_number_never_becomes_a_key_or_an_alias():
    """Телефон агента не глядит наружу ни ключом, ни псевдонимом.

    Он единственный владелец номера, то есть конфликта нет и от склейки его
    защищает только это правило. Появись `p:` по его номеру хоть на один
    прогон — покупатель, которому брокер вписал тот же телефон, приклеился
    бы к агенту, а отклеивать пришлось бы переездом ключа.
    """
    agent = {"NAME": "Ольга", "POST": "агент", "PHONE": [{"VALUE": "+79009998877"}]}

    got = assign_keys(_cards((1, "1-к, срочно")), {77: agent}, type_names=TYPES).decisions

    assert got[1].key == "c:77"
    assert got[1].phone_norm is None, "номер агента не склеивает"
    assert got[1].phone_valid is True, "но разобран он успешно — это разные вещи"
    assert got[1].phone_raw == "+79009998877", "на экране брокеру он нужен"
    assert all(kind != "phone" for kind, _ in got[1].aliases)


def test_the_agent_stage_counts_even_when_nothing_else_says_so():
    """Стадия «Агент» — признак сама по себе.

    В `classify_counterparty` её проверки нет вовсе, хотя ТЗ ей эту проверку
    приписывает `[V19]`. Дописывать туда нельзя: функцию зовёт client_state.
    """
    card = Card(deal_id=1, title="3-к у парка", stage_id="C18:UC_2ZBA0G", contact_id=77)

    got = assign_keys([card], {77: BUYER}, type_names=TYPES).decisions

    assert got[1].is_agent
    assert got[1].agent_reason == AGENT_STAGE_WHY
    assert got[1].key == "c:77"


def test_a_contact_marked_agent_by_type_needs_no_title():
    """Тип контакта проставляют руками именно для этого — он и есть первый признак."""
    agent = {"NAME": "Олег", "TYPE_ID": "UC_AG", "PHONE": [{"VALUE": "+79005554433"}]}

    got = assign_keys(_cards((1, "3-к")), {77: agent}, type_names=TYPES).decisions

    assert got[1].is_agent
    assert "Агент" in got[1].agent_reason


def test_a_card_with_no_contact_is_marked_by_its_own_title():
    """Сделка без контакта судится по себе — и стоит отдельным клиентом."""
    card = Card(deal_id=5, title="Квартира от агента", contact_id=None)

    got = assign_keys([card], {}, type_names=TYPES).decisions

    assert got[5].is_agent
    assert got[5].key == "d:5"


def test_our_own_agency_is_not_a_counterparty_agent():
    """«Агентство» в заголовке — это мы, а не контрагент.

    Отсечка живёт в `counterparty._AGENT_RE`, и проверка стоит здесь, чтобы
    её случайная потеря уронила ключи, а не только состояние карточки.
    """
    got = assign_keys(
        _cards((1, "Квартира от агентства партнёра")), {77: BUYER}, type_names=TYPES,
    ).decisions

    assert got[1].is_agent is False
    assert got[1].key == "p:+79001112233"


def test_the_answer_does_not_depend_on_the_order_the_portal_answered_in():
    """Порядок выдачи портала не влияет ни на ключ, ни на причину.

    Порядок карточек между прогонами Битрикс не гарантирует. Если бы
    причина бралась от «первой встреченной» агентской сделки, она бы
    менялась от прогона к прогону — и карточка клиента переписывалась бы
    каждую ночь без единого изменения в CRM.
    """
    forward = _cards((1, "2-к"), (2, "агент Лариса"), (3, "1-к"))
    backward = list(reversed(forward))

    a = assign_keys(forward, {77: BUYER}, type_names=TYPES).decisions
    b = assign_keys(backward, {77: BUYER}, type_names=TYPES).decisions

    assert {k: (v.key, v.agent_reason) for k, v in a.items()} == {
        k: (v.key, v.agent_reason) for k, v in b.items()
    }
