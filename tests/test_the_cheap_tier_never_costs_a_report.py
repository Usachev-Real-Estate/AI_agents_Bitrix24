"""Дешёвый режим RouterAI не должен стоить нам отчёта.

Документация RouterAI: `service_tier: "flex"` — «обычно −50 %», ценой
скорости и «без гарантий по мощностям»; при нехватке мощностей flex
«вернётся ошибка, средства не спишутся».

Для QC это выгодная сделка: прогон идёт по крону в полдень, занимает
двадцать минут и никто его не ждёт. Модель та же, ответ тот же — платим
временем планирования, а не качеством разбора.

Но голый flex менял бы половину счёта на несостоявшийся отчёт: отказ по
мощностям для нас неотличим от аварии, пять подряд обрывают прогон
(LLM_FAILURE_STREAK), и РОПы не получают ничего. Поэтому карточка,
отказавшая на дешёвом режиме, тут же повторяется в обычном, и llm_error
засчитывается, только если упали ОБЕ попытки.

Граница здесь тонкая, и оба её края надо держать тестом: провал одной
попытки не должен обрывать прогон, а провал обеих — должен, ровно как
раньше.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import client_state as cs  # noqa: E402
from client_state import LLM_FAILURE_STREAK, run_client_state  # noqa: E402
from funnel_profiles import SELLER_PROFILE  # noqa: E402
from llm import make_llm, provider_body  # noqa: E402


class _Tariff:
    llm_api_key = "k"
    llm_base_url = "https://example/api/v1"
    llm_model = "google/gemini-3.7-flash"
    llm_max_tokens = 0
    llm_reasoning_effort = ""
    llm_service_tier = ""
    llm_provider = ""


def test_the_tier_reaches_the_client_only_when_asked():
    """Пусто — параметр не отправляем: это дефолт провайдера, а не «выключить»."""
    assert make_llm(_Tariff()).service_tier is None
    assert make_llm(_Tariff(), service_tier="flex").service_tier == "flex"


# ── Закреплённый провайдер ─────────────────────────────────────────────
def test_pinning_sends_the_provider_block():
    """Закрепляем ОДНОГО провайдера и запрещаем откат.

    Разрешённый откат вернул бы распределение нагрузки между провайдерами
    — ровно то, из-за чего неявный кэш и не срабатывал.
    """
    assert provider_body("google-ai-studio") == {
        "provider": {"only": ["google-ai-studio"], "allow_fallbacks": False},
    }


def test_the_tier_is_a_suffix_of_the_provider_tag():
    """У RouterAI тариф — часть тега эндпоинта, а не отдельное поле."""
    assert provider_body("google-ai-studio", "flex") == {
        "provider": {
            "only": ["google-ai-studio/flex"], "allow_fallbacks": False,
        },
    }


def test_nothing_is_sent_when_nothing_is_pinned():
    assert provider_body("") == {}
    assert provider_body("", "flex") == {}


def test_the_tier_is_not_said_twice():
    """Закреплён провайдер — service_tier полем не уходит.

    Два способа сказать одно и то же в одном запросе — это способ однажды
    сказать разное.
    """
    client = make_llm(_Tariff(), service_tier="flex", provider="google-ai-studio")
    assert client.service_tier is None
    assert client.extra_body == {
        "provider": {
            "only": ["google-ai-studio/flex"], "allow_fallbacks": False,
        },
    }


def test_the_spare_client_keeps_the_same_provider():
    """Запасной — тот же провайдер, обычный тариф: префикс уже прогрет.

    Уйди откат к другому провайдеру — потеряли бы кэш ровно там, где и так
    платим полную цену.
    """
    spare = make_llm(_Tariff(), provider="google-ai-studio")
    assert spare.extra_body == {
        "provider": {"only": ["google-ai-studio"], "allow_fallbacks": False},
    }


def _deals(count: int) -> list[dict[str, Any]]:
    return [
        {"ID": str(2000 + i), "TITLE": f"Сделка {i}", "STAGE_ID": "NEW"}
        for i in range(count)
    ]


@pytest.fixture()
def two_tiers(monkeypatch):
    """Подменяем оба клиента: важно только, кто из них ответил."""
    calls: list[str] = []

    def _install(cheap_fails: bool, plain_fails: bool) -> None:
        def _fake_make(settings, *, service_tier: str = "", provider: str = ""):
            return f"cheap:{service_tier}" if service_tier else "plain"

        def _fake_analyze(deal, prev, new_events, all_events, client,
                          profile, **_kw):
            calls.append(str(client))
            broken = cheap_fails if str(client).startswith("cheap") else plain_fails
            if broken:
                raise RuntimeError(f"{client} отказал")
            return cs._normalize_state({"client_goal": "дом"}, profile)

        monkeypatch.setattr(cs, "make_llm", _fake_make)
        monkeypatch.setattr(cs, "analyze_with_llm", _fake_analyze)
        monkeypatch.setattr(
            cs, "prepare_deal_record",
            lambda d, **k: {
                **d,
                "timeline": [{
                    "ID": 1, "AUTHOR_ID": 1,
                    "COMMENT": "созвонились, показ в четверг",
                    "CREATED": "2026-09-01T09:00:00+03:00",
                }],
            },
        )

    return _install, calls


def _run(monkeypatch, tier: str, count: int = 3) -> dict[str, Any]:
    settings = cs.get_settings()
    monkeypatch.setattr(settings, "llm_service_tier", tier, raising=False)
    monkeypatch.setattr(settings, "llm_provider", "", raising=False)
    # force: иначе карточки придут из кэша прошлых прогонов и до модели
    # не дойдут вовсе — а тест ровно про то, кто из клиентов ответил.
    return run_client_state(
        SELLER_PROFILE, _deals(count), settings=settings, force=True,
    )


def test_without_a_tier_there_is_no_second_client(two_tiers, monkeypatch):
    """Ничего не просили — ничего и не меняется: один клиент, как прежде."""
    install, calls = two_tiers
    install(cheap_fails=False, plain_fails=False)
    _run(monkeypatch, "")
    assert calls == ["plain", "plain", "plain"]


def test_the_cheap_tier_is_used_when_asked(two_tiers, monkeypatch):
    install, calls = two_tiers
    install(cheap_fails=False, plain_fails=False)
    _run(monkeypatch, "flex")
    assert calls == ["cheap:flex"] * 3


def test_a_refused_card_is_retried_on_the_normal_tier(two_tiers, monkeypatch):
    """Отказ по мощностям проходит незаметно и стоит обычной цены."""
    install, calls = two_tiers
    install(cheap_fails=True, plain_fails=False)
    stats = _run(monkeypatch, "flex")
    assert calls == ["cheap:flex", "plain"] * 3
    assert stats["errors"] == 0
    assert not stats["aborted"]


def test_a_real_outage_still_aborts_the_run(two_tiers, monkeypatch):
    """Кончился баланс — валятся обе попытки, и защита срабатывает как прежде.

    Ради этого откат и сделан повторным ВЫЗОВОМ, а не выключением
    проверки: настоящая авария от нехватки мощностей отличается ровно тем,
    что переживает смену режима.
    """
    install, calls = two_tiers
    install(cheap_fails=True, plain_fails=True)
    stats = _run(monkeypatch, "flex", count=200)
    assert stats["aborted"] == "llm_unavailable"
    # По две попытки на карточку, и ровно до порога серии.
    assert len(calls) == LLM_FAILURE_STREAK * 2


def test_the_fallback_does_not_bill_the_failed_attempt(two_tiers, monkeypatch):
    """За отказавший flex провайдер не списывает — и мы не считаем.

    Счётчик токенов обнуляется перед повтором. Иначе неудачная попытка
    попала бы в расход, и отчёт снова назвал бы цифру, которой не было в
    счёте, — ровно то, из-за чего тариф вдвое расходился с выгрузкой.
    """
    install, _calls = two_tiers

    def _fake_analyze(deal, prev, new_events, all_events, client, profile, **kw):
        client = str(client)
        sink = kw.get("usage_sink")
        if isinstance(sink, dict):
            sink["input_tokens"] = sink.get("input_tokens", 0) + 1_000
        if client.startswith("cheap"):
            raise RuntimeError("нет мощностей")
        return cs._normalize_state({"client_goal": "дом"}, profile)

    install(cheap_fails=True, plain_fails=False)
    monkeypatch.setattr(cs, "analyze_with_llm", _fake_analyze)
    stats = _run(monkeypatch, "flex", count=1)
    # Тысяча за неудачную попытку не должна доехать до счёта.
    assert stats["usage"]["input_tokens"] == 1_000
