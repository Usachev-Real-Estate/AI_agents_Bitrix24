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
