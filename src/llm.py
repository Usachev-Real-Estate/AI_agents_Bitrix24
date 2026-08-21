"""Shared OpenAI-compatible LLM client (RouterAI, DeepSeek, etc.)."""

from __future__ import annotations

from langchain_openai import ChatOpenAI

from config import Settings


def make_llm(settings: Settings) -> ChatOpenAI:
    """Build ChatOpenAI from Settings (base_url + model are provider-specific)."""
    return ChatOpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        temperature=0.1,
    )
