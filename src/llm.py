"""Shared OpenAI-compatible LLM client (RouterAI, DeepSeek, etc.)."""

from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI

from config import Settings


def provider_body(provider: str, service_tier: str = "") -> dict[str, object]:
    """Тело `provider` для RouterAI: закрепить провайдера и тариф.

    Тариф у RouterAI — суффикс тега эндпоинта, а не отдельное поле:
    `google-ai-studio/flex` стоит ровно половину `google-ai-studio`. Оба
    указывают на ОДНОГО провайдера, поэтому неявный кэш префикса греется и
    переживает откат с дешёвого тарифа на обычный.

    allow_fallbacks=False намеренно: разрешённый откат вернул бы нас к
    распределению нагрузки между провайдерами, ради отмены которого всё и
    делается. Цена отказа — ошибка на карточке, и её ловит повтор в
    analyze_deal.
    """
    if not provider:
        return {}
    tag = f"{provider}/{service_tier}" if service_tier else provider
    return {"provider": {"only": [tag], "allow_fallbacks": False}}


def make_llm(
    settings: Settings,
    *,
    service_tier: str = "",
    provider: str = "",
) -> ChatOpenAI:
    """Build ChatOpenAI from Settings (base_url + model are provider-specific).

    max_tokens, reasoning_effort, service_tier и provider передаются, только
    если заданы: пустое значение означает «оставить дефолт провайдера», а не
    «выключить».

    service_tier и provider приходят аргументами, а не из настроек, ровно
    потому, что клиентов нужно два: основной в дешёвом режиме и запасной в
    обычном (см. analyze_deal).

    Когда провайдер закреплён, тариф уходит суффиксом его тега, а поле
    service_tier не отправляется вовсе: два способа сказать одно и то же в
    одном запросе — это способ однажды сказать разное.
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
    body = provider_body(provider, service_tier)
    if body:
        kwargs["extra_body"] = body
    elif service_tier:
        kwargs["service_tier"] = service_tier
    return ChatOpenAI(**kwargs)


def _coerce_int(value: Any) -> int:
    """Число из чего угодно, 0 при неудаче."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


USAGE_KEYS = (
    "input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens",
    # Токены, записанные в кэш. У RouterAI своя, самая дешёвая ставка, и
    # без этого счётчика она не к чему было бы применить.
    "cache_write_tokens",
)


def extract_usage(response: Any) -> dict[str, int]:
    """Токены одного ответа: {input_tokens, output_tokens, cached_tokens}.

    Провайдеры отдают счётчики по-разному, поэтому читаем и стандартное поле
    LangChain (usage_metadata), и сырой token_usage из response_metadata.
    Ничего не нашли — возвращаем нули: телеметрия не повод ронять разбор.
    """
    usage = {key: 0 for key in USAGE_KEYS}
    meta = getattr(response, "usage_metadata", None)
    if isinstance(meta, dict):
        usage["input_tokens"] = _coerce_int(meta.get("input_tokens"))
        usage["output_tokens"] = _coerce_int(meta.get("output_tokens"))
        details = meta.get("input_token_details")
        if isinstance(details, dict):
            usage["cached_tokens"] = _coerce_int(details.get("cache_read"))
            usage["cache_write_tokens"] = _coerce_int(
                details.get("cache_creation"),
            )
        out_details = meta.get("output_token_details")
        if isinstance(out_details, dict):
            usage["reasoning_tokens"] = _coerce_int(out_details.get("reasoning"))
    raw = getattr(response, "response_metadata", None)
    if isinstance(raw, dict):
        token_usage = raw.get("token_usage")
        if isinstance(token_usage, dict):
            if not usage["input_tokens"]:
                usage["input_tokens"] = _coerce_int(token_usage.get("prompt_tokens"))
            if not usage["output_tokens"]:
                usage["output_tokens"] = _coerce_int(
                    token_usage.get("completion_tokens"),
                )
            if not usage["cached_tokens"]:
                # DeepSeek называет это prompt_cache_hit_tokens, OpenAI прячет
                # в prompt_tokens_details.cached_tokens.
                details = token_usage.get("prompt_tokens_details")
                if isinstance(details, dict):
                    usage["cached_tokens"] = _coerce_int(
                        details.get("cached_tokens"),
                    )
                if not usage["cached_tokens"]:
                    usage["cached_tokens"] = _coerce_int(
                        token_usage.get("prompt_cache_hit_tokens"),
                    )
            if not usage["reasoning_tokens"]:
                # Самая дорогая строка тарифа RouterAI — её нужно видеть
                # отдельно, а не в общей сумме выходных токенов.
                out_details = token_usage.get("completion_tokens_details")
                if isinstance(out_details, dict):
                    usage["reasoning_tokens"] = _coerce_int(
                        out_details.get("reasoning_tokens"),
                    )
    return usage


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
