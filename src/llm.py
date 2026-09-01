"""Shared OpenAI-compatible LLM client (RouterAI, DeepSeek, etc.)."""

from __future__ import annotations

from langchain_openai import ChatOpenAI

from config import Settings


def make_llm(settings: Settings) -> ChatOpenAI:
    """Build ChatOpenAI from Settings (base_url + model are provider-specific).

    max_tokens и reasoning_effort передаются, только если заданы: пустое
    значение означает «оставить дефолт провайдера», а не «выключить».
    """
    kwargs: dict[str, object] = {
        "api_key": settings.llm_api_key,
        "base_url": settings.llm_base_url,
        "model": settings.llm_model,
        "temperature": 0.1,
    }
    if settings.llm_max_tokens > 0:
        kwargs["max_tokens"] = settings.llm_max_tokens
    if settings.llm_reasoning_effort:
        kwargs["reasoning_effort"] = settings.llm_reasoning_effort
    return ChatOpenAI(**kwargs)


def estimate_cost(usage: dict[str, int], settings: Settings) -> float:
    """Стоимость прогона в рублях по тарифу из настроек.

    Закэшированный вход считается по своей — вдесятеро меньшей — цене, а
    записанный в кэш по своей, ещё меньшей. Оба вычитаются из свежего
    входа: это части ОДНОГО и того же числа input_tokens, оплаченные по
    разным ставкам, а не добавка к нему. Допущение проверить пока нечем —
    выгрузки с работающим кэшем у нас нет, — и первую такую надо будет с
    ним сверить.

    Токены размышлений уже входят в output_tokens и тарифицируются по цене
    выхода, поэтому отдельного слагаемого для них нет. RouterAI показывает
    их отдельной строкой счёта, но по той же ставке: сумма обеих строк
    равна output_tokens × цена выхода.
    """
    per_million = 1_000_000.0
    cached = max(0, int(usage.get("cached_tokens") or 0))
    written = max(0, int(usage.get("cache_write_tokens") or 0))
    total_input = max(0, int(usage.get("input_tokens") or 0))
    # cached_tokens и cache_write_tokens приходят от провайдера и в теории
    # могут превысить вход; без max() отрицательный остаток занизил бы счёт.
    fresh_input = max(0, total_input - cached - written)
    output = max(0, int(usage.get("output_tokens") or 0))
    return (
        fresh_input * settings.llm_price_input
        + cached * settings.llm_price_cache_read
        + written * settings.llm_price_cache_write
        + output * settings.llm_price_output
    ) / per_million
