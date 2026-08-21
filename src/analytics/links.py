"""Ссылки на карточки Bitrix24.

Повторяет логику tools._build_crm_link, но без импорта tools: тот тянет за
собой fast_bitrix24 и langchain, и веб-процессу это лишние секунды старта и
лишняя поверхность. Совпадение поведения закреплено тестом.
"""

from __future__ import annotations


def portal_domain(webhook_url: str) -> str:
    """Домен портала из URL входящего вебхука.

    Всё, что после /rest/, — это токен вебхука, дающий полный доступ к CRM.
    Он обязан остаться на сервере и никогда не попасть в HTML.
    """
    url = (webhook_url or "").strip()
    return url.split("/rest/")[0] if "/rest/" in url else url.rstrip("/")


def crm_link(entity_type: str, entity_id: int, webhook_url: str) -> str:
    """Ссылка на карточку лида или сделки."""
    domain = portal_domain(webhook_url)
    kind = "lead" if entity_type == "lead" else "deal"
    return f"{domain}/crm/{kind}/details/{int(entity_id)}/"
