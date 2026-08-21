"""Per-funnel quality-control profiles for the client-state agent.

Buyers and sellers are audited by different rules. A buyer is judged by
readiness to purchase — budget, timing, an agreed viewing. A seller is judged
by readiness to sell — asking price, exclusivity, documents, motivation.
Merging them into one prompt would average both into something that fits
neither, so the funnel is a parameter: shared pipeline, own prompt, own
signals, own temperature rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

_COMMON_RULES = """\
Правила:
1. Каждое утверждение подкрепляй дословной цитатой в массиве evidence.
   Без цитаты поле оставь пустым / unknown / добавь в missing.
2. «Не знаю» — нормальный ответ. Лучше низкий confidence и missing,
   чем правдоподобная выдумка.
3. Расшифровки звонков — сплошной текст без разделения спикеров.
   Если неясно, кто говорил, так и пиши; не приписывай реплику клиенту наугад.
4. null / отсутствие расшифровки означает «текст ещё не готов», а не «звонка не было».
5. recoverable=false — если по карточке нельзя восстановить картину клиента
   (типичный пример: «созвонился, договорились» без деталей).
6. Сверяй источники. Комментарий брокера — это пересказ, расшифровка звонка —
   первоисточник. Если они расходятся, занеси это в contradictions с ДВУМЯ
   цитатами: что записано в карточке и что слышно в разговоре. Не расходятся —
   оставь contradictions пустым. Расхождение ≠ обвинение: возможно, брокер
   просто не обновил карточку.
7. signals — только факты, каждый с цитатой. Не выводи их «по ощущению»:
   не нашёл подтверждения — ставь false / unknown.
"""

_COMMON_SCHEMA_HEAD = """\
Ответ — один JSON-объект (без markdown), строго по схеме:
{
  "client_goal": "string",
  "situation": "string",
  "last_event": {"what": "string", "when": "YYYY-MM-DD or unknown"},
  "next_step": {"what": "string", "when": "string", "who": "broker|client|unknown"},
  "blockers": ["string"],
  "risk": "low|medium|high",
  "recoverable": true,
  "missing": ["string"],
  "confidence": 0.0,
  "evidence": ["string"],
  "contradictions": [
    {"what": "в чём расходится", "in_card": "цитата из карточки",
     "in_call": "цитата из разговора", "severity": "low|medium|high"}
  ],
"""

BUYER_PROMPT = (
    "Ты аналитик CRM по сделкам ПОКУПАТЕЛЕЙ недвижимости.\n\n"
    "Задача: восстановить состояние клиента по карточке — что ищет, что\n"
    "происходит, следующий шаг, риски. Ты НЕ оцениваешь брокера и НЕ\n"
    "придумываешь факты.\n\n"
    + _COMMON_RULES
    + "\n"
    + _COMMON_SCHEMA_HEAD
    + """  "signals": {
    "budget_named": false,
    "budget_value": "string or unknown",
    "timeline_named": false,
    "timeline_horizon": "до месяца|1-3 месяца|более 3 месяцев|unknown",
    "next_step_agreed": false,
    "next_step_date": "YYYY-MM-DD or unknown",
    "client_responsive": true,
    "shows_count": 0,
    "objections": ["string"]
  }
}
"""
)

SELLER_PROMPT = (
    "Ты аналитик CRM по сделкам ПРОДАВЦОВ недвижимости.\n\n"
    "Задача: восстановить состояние собственника по карточке — что продаёт,\n"
    "за сколько, насколько готов продавать, что мешает. Ты НЕ оцениваешь\n"
    "брокера и НЕ придумываешь факты.\n\n"
    + _COMMON_RULES
    + "\n"
    + _COMMON_SCHEMA_HEAD
    + """  "signals": {
    "price_named": false,
    "price_value": "string or unknown",
    "price_discussed": false,
    "ready_to_negotiate": false,
    "exclusive_discussed": false,
    "exclusive_agreed": false,
    "documents_ready": false,
    "motivation": "срочно|не срочно|просто интерес|unknown",
    "next_step_agreed": false,
    "next_step_date": "YYYY-MM-DD or unknown",
    "owner_responsive": true,
    "objections": ["string"]
  }
}
"""
)


def _flag(data: dict[str, Any], key: str, default: bool = False) -> bool:
    return bool(data.get(key, default))


def _text(data: dict[str, Any], key: str) -> str:
    return str(data.get(key) or "").strip()


def _dated(data: dict[str, Any]) -> bool:
    return _text(data, "next_step_date") not in ("", "unknown")


def buyer_temperature(signals: dict[str, Any]) -> tuple[str, str]:
    """Buyer readiness. Definition confirmed with the agency.

    hot  — согласованный шаг с датой + назван бюджет + названы сроки
    cold — клиент не выходит на связь либо горизонт дальше трёх месяцев
    warm — всё остальное
    """
    if not _flag(signals, "client_responsive", True):
        return "cold", "клиент не выходит на связь"
    if _text(signals, "timeline_horizon") == "более 3 месяцев":
        return "cold", "горизонт покупки дальше трёх месяцев"

    agreed = _flag(signals, "next_step_agreed") and _dated(signals)
    budget = _flag(signals, "budget_named")
    timeline = _flag(signals, "timeline_named")
    if agreed and budget and timeline:
        return "hot", "есть согласованный шаг с датой, назван бюджет и сроки"

    gaps = []
    if not agreed:
        gaps.append("нет согласованного шага с датой")
    if not budget:
        gaps.append("не назван бюджет")
    if not timeline:
        gaps.append("не названы сроки")
    return "warm", "; ".join(gaps)


def seller_temperature(signals: dict[str, Any]) -> tuple[str, str]:
    """Seller readiness. Mirrors the buyer rule, but on selling readiness.

    Предложено по аналогии с правилом для покупателей и ждёт подтверждения:
    hot  — согласованный шаг с датой + названа цена + продаёт не «просто так»
    cold — собственник не отвечает, «просто интерес», либо не обсуждает цену
    warm — всё остальное
    """
    if not _flag(signals, "owner_responsive", True):
        return "cold", "собственник не выходит на связь"
    motivation = _text(signals, "motivation")
    if motivation == "просто интерес":
        return "cold", "собственник просто узнаёт цену, продавать не готов"
    if not _flag(signals, "price_discussed"):
        return "cold", "цена с собственником не обсуждалась"

    agreed = _flag(signals, "next_step_agreed") and _dated(signals)
    priced = _flag(signals, "price_named")
    motivated = motivation in ("срочно", "не срочно")
    if agreed and priced and motivated:
        return "hot", "есть согласованный шаг с датой, названа цена и мотивация"

    gaps = []
    if not agreed:
        gaps.append("нет согласованного шага с датой")
    if not priced:
        gaps.append("не названа цена")
    if not motivated:
        gaps.append("не ясна мотивация продажи")
    return "warm", "; ".join(gaps)


def _normalize_buyer_signals(data: dict[str, Any], num: Callable[[Any], int]) -> dict[str, Any]:
    horizon = str(data.get("timeline_horizon") or "unknown").strip().lower()
    if horizon not in BUYER_HORIZONS:
        horizon = "unknown"
    return {
        "budget_named": _flag(data, "budget_named"),
        "budget_value": _text(data, "budget_value") or "unknown",
        "timeline_named": _flag(data, "timeline_named"),
        "timeline_horizon": horizon,
        "next_step_agreed": _flag(data, "next_step_agreed"),
        "next_step_date": _text(data, "next_step_date") or "unknown",
        # Отсутствие ответа надо доказать: иначе молчание модели превратилось
        # бы в «холодный» и раздуло бы холодную часть базы.
        "client_responsive": _flag(data, "client_responsive", True),
        "shows_count": max(0, num(data.get("shows_count"))),
    }


def _normalize_seller_signals(data: dict[str, Any], num: Callable[[Any], int]) -> dict[str, Any]:
    motivation = str(data.get("motivation") or "unknown").strip().lower()
    if motivation not in SELLER_MOTIVATIONS:
        motivation = "unknown"
    return {
        "price_named": _flag(data, "price_named"),
        "price_value": _text(data, "price_value") or "unknown",
        "price_discussed": _flag(data, "price_discussed"),
        "ready_to_negotiate": _flag(data, "ready_to_negotiate"),
        "exclusive_discussed": _flag(data, "exclusive_discussed"),
        "exclusive_agreed": _flag(data, "exclusive_agreed"),
        "documents_ready": _flag(data, "documents_ready"),
        "motivation": motivation,
        "next_step_agreed": _flag(data, "next_step_agreed"),
        "next_step_date": _text(data, "next_step_date") or "unknown",
        "owner_responsive": _flag(data, "owner_responsive", True),
    }


BUYER_HORIZONS = frozenset({"до месяца", "1-3 месяца", "более 3 месяцев", "unknown"})
SELLER_MOTIVATIONS = frozenset({"срочно", "не срочно", "просто интерес", "unknown"})

# Текстовые поля сигналов, которые надо разворачивать обратно для людей.
BUYER_TEXT_SIGNALS = ("budget_value",)
SELLER_TEXT_SIGNALS = ("price_value",)


@dataclass(frozen=True)
class FunnelProfile:
    """Everything that differs between funnels."""

    key: str
    label: str
    prompt: str
    normalize_signals: Callable[[dict[str, Any], Callable[[Any], int]], dict[str, Any]]
    temperature: Callable[[dict[str, Any]], tuple[str, str]]
    text_signals: tuple[str, ...]


BUYER_PROFILE = FunnelProfile(
    key="buyers",
    label="Покупатели",
    prompt=BUYER_PROMPT,
    normalize_signals=_normalize_buyer_signals,
    temperature=buyer_temperature,
    text_signals=BUYER_TEXT_SIGNALS,
)

SELLER_PROFILE = FunnelProfile(
    key="sellers",
    label="Продавцы",
    prompt=SELLER_PROMPT,
    normalize_signals=_normalize_seller_signals,
    temperature=seller_temperature,
    text_signals=SELLER_TEXT_SIGNALS,
)

PROFILES = {p.key: p for p in (BUYER_PROFILE, SELLER_PROFILE)}


def profile_for(key: str) -> FunnelProfile:
    """Look up a funnel profile by key ('buyers' / 'sellers')."""
    try:
        return PROFILES[str(key).strip().lower()]
    except KeyError:
        raise ValueError(f"Unknown funnel profile: {key!r}") from None
