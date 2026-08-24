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
8. stage_facts — отдельная секция. Для КАЖДОГО ключа из payload.facts_needed
   верни объект {present: bool, quote: string}. Если факт найден в карточке —
   ставь present=true и обязательно приводи дословную цитату. Не нашёл —
   present=false, quote="". Не додумывай.
9. Уровни расхождения (severity в contradictions):
   * low — мелкое: сдвиг дат, разница по цифрам до 30 %, уточнение
     формулировок. Не влияет на итоговый вердикт.
   * medium/high — существенное: искажена позиция клиента, готовность к
     сделке, разница по цифрам БОЛЕЕ 30 %, контрагент назван неверно.
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
  "stage_facts": {
    "<ключ из facts_needed>": {"present": true, "quote": "дословная цитата"}
  },
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


# Обязательные факты по этапам. Накопительно, кроме APOLOGY (проиграна).
BUYER_STAGE_REQUIREMENTS: dict[str, tuple[tuple[str, str], ...]] = {
    # Подбор — квалификация клиента
    "C18:NEW": (
        ("property_type", "тип объекта"),
        ("budget", "бюджет"),
        ("district", "район"),
        ("timeline", "сроки покупки"),
        ("next_step", "следующий шаг с датой"),
    ),
    # Первый показ — накопительно + описание показа
    "C18:UC_UFPFKK": (
        ("property_type", "тип объекта"),
        ("budget", "бюджет"),
        ("district", "район"),
        ("timeline", "сроки покупки"),
        ("next_step", "следующий шаг с датой"),
        ("shown_objects", "какие объекты показаны"),
        ("show_reaction", "реакция клиента на показ"),
    ),
    # Повторный показ — те же поля показа (по согласованию с агентством)
    "C18:UC_DVW1P9": (
        ("property_type", "тип объекта"),
        ("budget", "бюджет"),
        ("district", "район"),
        ("timeline", "сроки покупки"),
        ("next_step", "следующий шаг с датой"),
        ("shown_objects", "какой объект показан"),
        ("show_reaction", "реакция клиента"),
    ),
    # Офер — плюс условия и реакция продавца
    "C18:UC_8Z3SP6": (
        ("property_type", "тип объекта"),
        ("budget", "бюджет"),
        ("district", "район"),
        ("timeline", "сроки покупки"),
        ("next_step", "следующий шаг с датой"),
        ("offer_terms", "предложенные цена и условия"),
        ("seller_reaction", "реакция продавца"),
        ("deal_blockers", "что мешает выйти на задаток"),
    ),
    # Отложенный спрос — только специфика этапа
    "C18:LOSE": (
        ("postponed_reason", "причина откладывания"),
        ("return_when", "когда вернуться к клиенту"),
    ),
    # Сделка проиграна — своя причина, накопительное не применяется
    "C18:APOLOGY": (
        ("lost_reason", "причина проигрыша"),
    ),
    # "Агент" — прямая проверка типа контакта (не LLM), см. qualification_fields
}


BUYER_QUAL_STAGE = "C18:NEW"
# Обязательные поля Bitrix для проверки на "Подборе" — согласованы с агентством.
BUYER_QUALIFICATION_FIELDS: tuple[tuple[str, str], ...] = (
    ("UF_CRM_1774363333518", "бюджет"),
    ("UF_CRM_1774364869184", "район/локация"),
    ("UF_CRM_1747291787883", "тип недвижимости"),
)

# Другие известные коды полей — включать в проверку по согласованию, отдельно.
# Оставлены здесь как справочник, чтобы не искать заново.
BUYER_KNOWN_UF_FIELDS: dict[str, str] = {
    "UF_CRM_1774521607469": "стоимость объекта",
    "UF_CRM_1774364892961": "название ЖК",
    "UF_CRM_1660497970783": "цель покупки",
    "UF_CRM_1780911079": "ID объекта Афины",
    "UF_CRM_1659375809326": "дата встречи",
}


BUYER_STAGES_OUT_OF_QC = frozenset({
    "C18:UC_RUCRAH",  # Задаток — у руководства
    "C18:UC_8X12HI",  # Сделка — у руководства
    "C18:WON",        # Договор закрыт
    "C18:UC_2ZBA0G",  # Агент — только прямая проверка типа контакта, не LLM
})


BUYER_GRACE_HOURS: dict[str, int] = {
    "C18:NEW": 72,   # Подбор: 3 дня — свежие лиды с Cian, бюджет ещё не мог возникнуть
    "_default": 24,
}


SELLER_STAGE_REQUIREMENTS: dict[str, tuple[tuple[str, str], ...]] = {
    # Назначение встречи (мотивация убрана по решению агентства)
    "NEW": (
        ("property_address", "адрес объекта"),
        ("property_type", "тип объекта"),
        ("selling_timeline", "срок продажи"),
        ("next_step", "следующий шаг с датой"),
    ),
    # Подготовка в рекламу — цена обязательна;
    # фотосессия / документы — условные (см. optional_facts).
    "FINAL_INVOICE": (
        ("property_address", "адрес объекта"),
        ("property_type", "тип объекта"),
        ("selling_timeline", "срок продажи"),
        ("next_step", "следующий шаг с датой"),
        ("listing_price", "цена выставления"),
    ),
    # Отложенная продажа
    "LOSE": (
        ("postponed_reason", "причина откладывания"),
        ("return_when", "когда вернуться"),
    ),
    # Проиграна
    "APOLOGY": (
        ("lost_reason", "причина проигрыша"),
    ),
    # "Закрытая продажа (На сайт)" — ID Афины проверяет основной аудит
    # "Переговоры" — вне QC (у руководства)
    # "Поиск клиента" — вне аудита
}


SELLER_STAGES_OUT_OF_QC = frozenset({
    "UC_KEOOG8",       # Переговоры — у руководства
    "UC_FADPBF",       # Поиск клиента — вне аудита
    "WON",             # Договор закрыт
})


SELLER_GRACE_HOURS: dict[str, int] = {"_default": 24}


# Условные факты — LLM их извлекает, но за отсутствие не наказываем.
BUYER_OPTIONAL_FACTS: dict[str, tuple[tuple[str, str], ...]] = {}
SELLER_OPTIONAL_FACTS: dict[str, tuple[tuple[str, str], ...]] = {
    "FINAL_INVOICE": (
        ("photo_session", "фотосессия назначена/проведена"),
        ("documents_ready", "готовность документов"),
    ),
}


def all_facts_for_stage(profile_key: str, stage_id: str) -> list[tuple[str, str, bool]]:
    """Все факты для этапа: (ключ, имя, обязательно?).

    Используется, чтобы одним списком передать LLM в human message.
    """
    req_map = BUYER_STAGE_REQUIREMENTS if profile_key == "buyers" else SELLER_STAGE_REQUIREMENTS
    opt_map = BUYER_OPTIONAL_FACTS if profile_key == "buyers" else SELLER_OPTIONAL_FACTS
    facts: list[tuple[str, str, bool]] = []
    seen: set[str] = set()
    for key, name in req_map.get(stage_id, ()):
        if key not in seen:
            facts.append((key, name, True))
            seen.add(key)
    for key, name in opt_map.get(stage_id, ()):
        if key not in seen:
            facts.append((key, name, False))
            seen.add(key)
    return facts


@dataclass(frozen=True)
class FunnelProfile:
    """Everything that differs between funnels."""

    key: str
    label: str
    prompt: str
    normalize_signals: Callable[[dict[str, Any], Callable[[Any], int]], dict[str, Any]]
    temperature: Callable[[dict[str, Any]], tuple[str, str]]
    text_signals: tuple[str, ...]
    # Обязательные факты по этапам: {stage_id: (ключ факта, человеческое имя)}.
    # На требования из старших этапов ложатся требования младших — это
    # накопительно, кроме "Проиграна", где своя причина заменяет всё.
    stage_requirements: dict[str, tuple[tuple[str, str], ...]]
    # Часы отсрочки от входа на этап до применения требований. Ключ ``_default``
    # применяется к этапам, отсутствующим в таблице. Ключ ``_skipped`` (список)
    # — этапы вне контроля качества (ведёт руководство).
    grace_hours: dict[str, int]
    stages_out_of_qc: frozenset[str]
    # Поля карточки, которые проверяются напрямую в CRM: это НЕ задача LLM.
    # Значение — коды UF, которые надо подставить в crm.deal.get.
    # Заполнить реальными кодами перед включением проверки.
    qualification_fields: tuple[tuple[str, str], ...] = ()
    qualification_stage: str = ""  # этап, на котором проверка обязательна


BUYER_PROFILE = FunnelProfile(
    key="buyers",
    label="Покупатели",
    prompt=BUYER_PROMPT,
    normalize_signals=_normalize_buyer_signals,
    temperature=buyer_temperature,
    text_signals=BUYER_TEXT_SIGNALS,
    stage_requirements=BUYER_STAGE_REQUIREMENTS,
    grace_hours=BUYER_GRACE_HOURS,
    stages_out_of_qc=BUYER_STAGES_OUT_OF_QC,
    qualification_fields=BUYER_QUALIFICATION_FIELDS,
    qualification_stage=BUYER_QUAL_STAGE,
)

SELLER_PROFILE = FunnelProfile(
    key="sellers",
    label="Продавцы",
    prompt=SELLER_PROMPT,
    normalize_signals=_normalize_seller_signals,
    temperature=seller_temperature,
    text_signals=SELLER_TEXT_SIGNALS,
    stage_requirements=SELLER_STAGE_REQUIREMENTS,
    grace_hours=SELLER_GRACE_HOURS,
    stages_out_of_qc=SELLER_STAGES_OUT_OF_QC,
)

PROFILES = {p.key: p for p in (BUYER_PROFILE, SELLER_PROFILE)}


def profile_for(key: str) -> FunnelProfile:
    """Look up a funnel profile by key ('buyers' / 'sellers')."""
    try:
        return PROFILES[str(key).strip().lower()]
    except KeyError:
        raise ValueError(f"Unknown funnel profile: {key!r}") from None
