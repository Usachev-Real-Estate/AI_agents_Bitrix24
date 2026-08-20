"""Pytest configuration: import path + hermetic settings environment."""

import sys
from pathlib import Path
from typing import Iterator

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Значения-заглушки: тесты не ходят в сеть, но Settings требует обязательные поля.
# Без них любой тест, который дотянется до get_settings(), падал на ValidationError.
_TEST_ENV = {
    "B24_WEBHOOK_URL": "https://example.bitrix24.ru/rest/1/test-token/",
    "DEEPSEEK_API_KEY": "sk-test",
    "BUYERS_CATEGORY_ID": "18",
    "SELLERS_CATEGORY_ID": "0",
    "REPORT_SINCE": "2026-06-02",
    "DRY_RUN": "true",
}


@pytest.fixture(autouse=True)
def settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Provide required env vars and reset the cached Settings per test."""
    from config import get_settings

    for key, value in _TEST_ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def no_bitrix_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any unstubbed Bitrix REST call instead of hitting the network.

    Helpers such as _build_rop_map degrade to an empty map on error, which is
    exactly the shape unit tests expect. A test that needs real payloads
    monkeypatches these itself — its patch is applied after this fixture.
    """
    import tools

    def _blocked(method: str, params: dict) -> object:
        raise RuntimeError(f"Bitrix REST disabled in tests: {method}")

    monkeypatch.setattr(tools, "_bx_get_all_sync", _blocked)
