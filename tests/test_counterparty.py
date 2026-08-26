"""Клиент или агент по ту сторону карточки."""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from counterparty import (  # noqa: E402
    WHO_AGENT,
    WHO_CLIENT,
    classify_counterparty,
    set_contact_type_names,
)

TYPES = {"UC_2G0TD3": "Собственник", "UC_AG": "Агент", "CLIENT": "Клиент"}


def test_contact_type_wins():
    got = classify_counterparty(
        {"TITLE": "ЖК «Dominion» Ольга"},
        [{"TYPE_ID": "UC_AG"}],
        TYPES,
    )
    assert got["who"] == WHO_AGENT
    assert got["why"] == "тип контакта «Агент»"


def test_plain_client_type_stays_a_client():
    got = classify_counterparty(
        {"TITLE": "ЖК «Dominion» Ольга"}, [{"TYPE_ID": "CLIENT"}], TYPES,
    )
    assert got["who"] == WHO_CLIENT


def test_mark_in_the_deal_title():
    """Самый частый признак на портале: брокер дописывает «агент» в название."""
    got = classify_counterparty({"TITLE": "Лариса (Клекова) агент"}, [], TYPES)
    assert got["who"] == WHO_AGENT
    assert got["why"] == "пометка «агент» в названии сделки"


def test_mark_in_the_contact_name():
    got = classify_counterparty(
        {"TITLE": "ЖК «Will Towers»"}, [{"NAME": "Марина", "LAST_NAME": "агент"}], TYPES,
    )
    assert got["who"] == WHO_AGENT
    assert got["why"] == "пометка «агент» в имени контакта"


def test_post_says_realtor():
    got = classify_counterparty(
        {"TITLE": "ЖК «Will Towers»"}, [{"POST": "риэлтор"}], TYPES,
    )
    assert got["who"] == WHO_AGENT


def test_agency_is_not_an_agent():
    """«Агентство» — это мы сами; без отсечки такая сделка ушла бы в агентские."""
    got = classify_counterparty({"TITLE": "Квартира от агентства партнёра"}, [], TYPES)
    assert got["who"] == WHO_CLIENT


def test_silence_reads_as_client():
    """Агентская карточка — исключение; молчание CRM не значит «непонятно кто»."""
    got = classify_counterparty({"TITLE": "3-4 сп, до 130"}, [{"TYPE_ID": ""}], TYPES)
    assert got["who"] == WHO_CLIENT
    assert got["why"] == ""


def test_unknown_type_code_does_not_crash():
    got = classify_counterparty({"TITLE": "х"}, [{"TYPE_ID": "UC_NEW"}], TYPES)
    assert got["who"] == WHO_CLIENT


def test_module_cache_is_used_when_no_table_passed():
    set_contact_type_names(TYPES)
    try:
        got = classify_counterparty({"TITLE": "х"}, [{"TYPE_ID": "UC_AG"}])
        assert got["who"] == WHO_AGENT
    finally:
        set_contact_type_names({})


def test_sellers_never_get_an_agent_counterparty():
    """У продавцов агента быть не может: продаёт собственник."""
    from funnel_profiles import BUYER_PROFILE, SELLER_PROFILE

    assert BUYER_PROFILE.counterparty_can_be_agent is True
    assert SELLER_PROFILE.counterparty_can_be_agent is False
