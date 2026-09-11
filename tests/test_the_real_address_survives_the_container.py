"""Адрес клиента за обратным прокси, когда дашборд живёт в контейнере.

Проверка на localhost была верна ровно до переезда в Docker. В контейнере
сосед другой: nginx стучится через мост, и запрос приходит с адреса шлюза
вроде 172.20.0.1. Заголовкам не верили, и каждый клиент получал один и тот
же адрес.

Ломалась при этом не колонка на экране, а блокировка перебора. Она считает
неудачи по логину ИЛИ по адресу, и с одним адресом на всех пятеро человек,
промахнувшихся мимо пароля, запирали дашборд всем сразу — включая
директора. А защита от подбора по списку логинов, ради которой счёт по
адресу и заведён, не работала вовсе: различать было нечего.

Обратная сторона обязана сохраниться: с публичного адреса заголовкам
верить нельзя, иначе подделать адрес и обойти счётчик сможет кто угодно.
"""

from __future__ import annotations

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

from security import client_ip

REAL = "77.88.55.60"       # настоящий публичный адрес клиента


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeRequest:
    """Ровно то, что читает client_ip: сосед и заголовки."""

    def __init__(self, peer: str, headers: dict[str, str] | None = None) -> None:
        self.client = _FakeClient(peer) if peer else None
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}


# ── Своему прокси верим ────────────────────────────────────────────────
@pytest.mark.parametrize("peer", [
    "127.0.0.1",          # дашборд запущен прямо на хосте
    "::1",
    "172.20.0.1",         # шлюз docker-моста — наш случай
    "172.17.0.1",         # мост по умолчанию
    "10.0.0.5",
    "192.168.1.10",
])
def test_a_private_neighbour_is_our_own_nginx(peer):
    request = _FakeRequest(peer, {"X-Forwarded-For": REAL})

    assert client_ip(request) == REAL


def test_the_first_address_in_the_chain_wins():
    """X-Forwarded-For накапливается слева направо: клиент первый."""
    request = _FakeRequest(
        "172.20.0.1", {"X-Forwarded-For": f"{REAL}, 10.1.1.1, 10.1.1.2"})

    assert client_ip(request) == REAL


def test_x_real_ip_is_the_fallback():
    request = _FakeRequest("172.20.0.1", {"X-Real-IP": REAL})

    assert client_ip(request) == REAL


# ── Чужому — нет ───────────────────────────────────────────────────────
def test_a_public_neighbour_cannot_name_itself():
    """Иначе подделать адрес и обойти счётчик попыток сможет кто угодно.

    Адрес взят настоящий публичный, а не из документационного диапазона:
    198.51.100.0/24 и 203.0.113.0/24 Python считает ЧАСТНЫМИ (они не
    маршрутизируются в интернете), и проверка на них молча проходила бы
    по ветке «свой прокси», ничего не проверяя.
    """
    request = _FakeRequest("8.8.8.8", {"X-Forwarded-For": "127.0.0.1"})

    assert client_ip(request) == "8.8.8.8"


def test_a_neighbour_without_headers_stays_itself():
    assert client_ip(_FakeRequest("172.20.0.1")) == "172.20.0.1"


def test_a_broken_peer_is_not_trusted():
    """Мусор вместо адреса — не повод верить заголовкам."""
    request = _FakeRequest("не-адрес", {"X-Forwarded-For": REAL})

    assert client_ip(request) == "не-адрес"


def test_no_client_at_all_gives_nothing():
    assert client_ip(_FakeRequest("", {"X-Forwarded-For": REAL})) == ""


def test_the_stored_address_is_bounded():
    """Заголовок пишет кто угодно, а колонка в базе не резиновая."""
    request = _FakeRequest("172.20.0.1", {"X-Forwarded-For": "9" * 500})

    assert len(client_ip(request)) <= 64


# ── Ради чего всё ──────────────────────────────────────────────────────
def test_two_people_behind_one_proxy_are_told_apart(analytics_db):
    """Главное следствие: счётчик неудач больше не общий на всех.

    Пятеро, промахнувшихся мимо пароля, запирали дашборд всем сразу —
    порог по адресу срабатывал на сумме их попыток.
    """
    import store
    from config import get_settings

    get_settings.cache_clear()
    store.init_store()
    store.create_user("boss", "correct-horse-battery", role=store.ROLE_ADMIN)

    first = client_ip(_FakeRequest("172.20.0.1", {"X-Forwarded-For": REAL}))
    second = client_ip(_FakeRequest("172.20.0.1", {"X-Forwarded-For": "77.88.55.99"}))
    assert first != second

    # Пятнадцать неудач с чужого адреса не запирают своего человека.
    for _ in range(15):
        store.record_attempt("kretov", first, ok=False)

    assert store.is_locked_out("boss", second, max_attempts=5, window_minutes=15) is False
    assert store.is_locked_out("kretov", first, max_attempts=5, window_minutes=15) is True
