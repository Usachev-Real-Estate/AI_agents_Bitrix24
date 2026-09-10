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
    "LLM_API_KEY": "sk-test",
    "BUYERS_CATEGORY_ID": "18",
    "SELLERS_CATEGORY_ID": "0",
    "REPORT_SINCE": "2026-06-02",
    "DRY_RUN": "true",
    # Пустой ключ выключает раздел «Объекты», и он не ходит в сеть. Без этого
    # прогон в рабочем каталоге с настоящим .env отправлял бы боевой ключ в
    # боевую Афину — на каждый тест, который открывает страницы дашборда.
    "AFINA_API_KEY": "",
    "AFINA_API_BASE_URL": "https://afina.invalid",
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


@pytest.fixture(autouse=True)
def agent_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Изолированная база агента на тест.

    ``db.DB_PATH`` — константа модуля, а не настройка, и указывает она на
    боевой data/violations.db. Без подмены прогон в рабочем каталоге читал
    бы настоящую память советов и настоящие нарушения: тест то проходил бы,
    то нет, в зависимости от того, что вчера сказала рассылка. Хуже того,
    любой тест, дошедший до init_db(), писал бы в неё.

    Тесты, которым нужна своя база в известном месте, подменяют DB_PATH
    сами — их подмена накладывается поверх этой.
    """
    import db

    path = tmp_path / "agent" / "violations.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(db, "DB_PATH", path)
    return path


@pytest.fixture
def analytics_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Изолированный файл витрины на тест.

    Путь к витрине читается из настроек, поэтому подменяем переменную
    окружения и сбрасываем кеш Settings — иначе тесты писали бы в боевую
    data/analytics.db.
    """
    from config import get_settings

    db_path = tmp_path / "analytics.db"
    monkeypatch.setenv("ANALYTICS_DB_PATH", str(db_path))
    get_settings.cache_clear()

    from analytics.schema import init_analytics_db

    init_analytics_db()
    yield db_path
    get_settings.cache_clear()
