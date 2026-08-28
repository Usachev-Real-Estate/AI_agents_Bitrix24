"""Per-funnel quality-control profiles for the client-state agent.

Buyers and sellers are audited by different rules. A buyer is judged by
readiness to purchase — budget, timing, an agreed viewing. A seller is judged
by readiness to sell — asking price, exclusivity, documents.
Merging them into one prompt would average both into something that fits
neither, so the funnel is a parameter: shared pipeline, own prompt, own
signals, own temperature rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

_COMMON_RULES = """\
Правила:
1. Каждое утверждение подкрепляй дословной цитатой в массиве evidence.
   Без цитаты поле оставь пустым / unknown / добавь в missing.
   Цитата — короткий фрагмент до 300 символов, ровно та фраза, которая
   доказывает утверждение. Не пересказывай и не переписывай абзац целиком:
   цитату сверяют с исходным текстом дословно, длинная её не усиливает.
2. «Не знаю» — нормальный ответ. Лучше низкий confidence и missing,
   чем правдоподобная выдумка.
3. Расшифровки звонков — сплошной текст без разделения спикеров.
   Если неясно, кто говорил, так и пиши; не приписывай реплику клиенту наугад.
4. null / отсутствие расшифровки означает «текст ещё не готов», а не «звонка не было».
5. recoverable=false — если по карточке нельзя восстановить картину клиента
   (типичный пример: «созвонился, договорились» без деталей).
6. Расшифровка звонка — первоисточник, комментарий брокера — пересказ. Читай
   оба, восстанавливая картину клиента; сверять их между собой и искать
   расхождения не нужно.
7. signals — только факты, каждый с цитатой. Не выводи их «по ощущению»:
   не нашёл подтверждения — ставь false / unknown.
8. stage_facts — отдельная секция. Для КАЖДОГО ключа из payload.facts_needed
   верни объект {present: bool, quote: string}. Если факт найден в карточке —
   ставь present=true и обязательно приводи дословную цитату. Не нашёл —
   present=false, quote="". Не додумывай.
9. broker_work — про то, ЧЕМ брокер подтверждает работу, а не хорошо ли он
   работает. Оценку даёт код, ты только читаешь текст:
   * claims_messaged=true, если в последних комментариях брокер утверждает,
     что писал клиенту (в вотсап, телеграм, почту, «отправил», «скинул»).
     Обязательно приведи цитату в claims_messaged_quote. Не нашёл — false.
   * comment_informative=true, если по последним комментариям брокера понятно,
     что происходит с клиентом и что дальше. «Созвонился», «ок», «ждём» —
     это false: такой комментарий не заменяет разговора.
   * claims_no_answer=true ТОЛЬКО если брокер объясняет отсутствие продвижения
     тем, что до клиента не получается достучаться: не берёт трубку,
     сбрасывает, не отвечает на звонки, номер недоступен. Обязательно приведи
     цитату в claims_no_answer_quote.
     Это НЕ тот случай, когда у клиента есть понятная и названная причина
     паузы: отпуск, командировка, разъезды, лечение, «вернётся после 30.08»,
     «сейчас в другом городе». Отсутствие с объяснением и сроком — это
     состояние клиента, а не отговорка брокера; там ставь false.
     Флаг ведёт к претензии в адрес брокера, поэтому сомневаешься — false.
   * pause_explained=true, если в комментариях или расшифровке НАЗВАНА ПРИЧИНА
     паузы на стороне клиента: отпуск, командировка, лечение, разъезды, ждёт
     продажи своего объекта, вывозит вещи, собирает документы, «вернётся
     после 30.08», «определится в сентябре». Обязательно приведи цитату в
     pause_reason_quote — ровно ту фразу, где причина названа.
     Если в ней назван срок окончания паузы, поставь его в pause_until в
     формате YYYY-MM-DD; назван месяц без числа — бери первое число этого
     месяца; срока нет — unknown.
     Это НЕ то же самое, что claims_no_answer: там брокер не может достучаться
     и причины не знает, здесь причина названа. Одновременно оба флага не
     ставь.
     Просьба самого клиента связаться позже с названным сроком — тоже
     причина: «просил перезвонить в начале следующей недели», «наберите
     через месяц», «пока не готов обсуждать, свяжитесь позже». Отсрочку
     назначил клиент, и брокер не виноват, что её соблюдает.
     Причина должна быть в тексте, а не в твоей догадке: «клиент думает»,
     «ждём обратную связь», «на паузе», «клиент на связи» — это не причина,
     там false. Просьба без срока — тоже причина, но pause_until=unknown.
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
  "stage_facts": {
    "<ключ из facts_needed>": {"present": true, "quote": "дословная цитата"}
  },
  "broker_work": {
    "claims_messaged": false,
    "claims_messaged_quote": "",
    "claims_no_answer": false,
    "claims_no_answer_quote": "",
    "comment_informative": true,
    "pause_explained": false,
    "pause_reason_quote": "",
    "pause_until": "YYYY-MM-DD or unknown"
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


def buyer_temperature(
    signals: dict[str, Any],
    counterparty: str = "",
) -> tuple[str, str]:
    """Buyer readiness. Definition confirmed with the agency.

    hot  — согласованный шаг с датой + назван бюджет + названы сроки
    cold — клиент не выходит на связь либо горизонт дальше трёх месяцев
    warm — всё остальное

    У агента горизонт не считается. #11954: «Лариса (Клекова) агент» уехала
    в «теряем клиента» с причиной «горизонт дальше трёх месяцев». Горизонт
    там принадлежит клиентам агента, а не самому агенту, и правило,
    написанное для прямых покупателей, на агентской карточке даёт ложную
    тревогу. Молчание агента холодом остаётся: не выходит на связь —
    значит контакт теряем, кто бы он ни был.
    """
    if not _flag(signals, "client_responsive", True):
        return "cold", "клиент не выходит на связь"
    if (
        counterparty != "agent"
        and _text(signals, "timeline_horizon") == "более 3 месяцев"
    ):
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


def seller_temperature(
    signals: dict[str, Any],
    counterparty: str = "",
) -> tuple[str, str]:
    """Seller readiness.

    hot  — согласованный шаг с датой + названа цена
    cold — собственник не выходит на связь
    warm — всё остальное

    «Цена не обсуждалась» холодной карточку больше не делает. Это отсутствие
    темы, а не событие остывания: карточка на пятом часу жизни и карточка,
    которую ведут месяц, получали одинаковый ярлык, из-за чего ярлык переставал
    что-либо значить. #17002 — собственник подтвердил, что продажа актуальна, и
    попросил написать в WhatsApp, а карточка возрастом 5 часов уехала в раздел
    «теряем клиента». Пробел никуда не делся: он виден в причине «не названа
    цена» и в списке «не хватает».

    Мотивация («срочно / не срочно / просто интерес») из правила убрана по
    решению агентства: заинтересованность видно по разговору, а не по тому,
    как её пересказал брокер в комментарии. Модель по-прежнему читает и
    цитирует ситуацию клиента — просто не подменяет её ярлыком, из-за
    которого карточка получала «холодный» на пересказе.
    """
    if not _flag(signals, "owner_responsive", True):
        return "cold", "собственник не выходит на связь"

    agreed = _flag(signals, "next_step_agreed") and _dated(signals)
    priced = _flag(signals, "price_named")
    if agreed and priced:
        return "hot", "есть согласованный шаг с датой и названа цена"

    gaps = []
    if not agreed:
        gaps.append("нет согласованного шага с датой")
    if not priced:
        gaps.append("не названа цена")
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
    return {
        "price_named": _flag(data, "price_named"),
        "price_value": _text(data, "price_value") or "unknown",
        "price_discussed": _flag(data, "price_discussed"),
        "ready_to_negotiate": _flag(data, "ready_to_negotiate"),
        "exclusive_discussed": _flag(data, "exclusive_discussed"),
        "exclusive_agreed": _flag(data, "exclusive_agreed"),
        "documents_ready": _flag(data, "documents_ready"),
        "next_step_agreed": _flag(data, "next_step_agreed"),
        "next_step_date": _text(data, "next_step_date") or "unknown",
        "owner_responsive": _flag(data, "owner_responsive", True),
    }


BUYER_HORIZONS = frozenset({"до месяца", "1-3 месяца", "более 3 месяцев", "unknown"})
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
    # «Офер» снят с контроля качества — см. BUYER_STAGES_OUT_OF_QC.
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
    # Три этапа, где по регламенту обсуждение идёт В ЧАТЕ по сделке, а чаты
    # агент не читает. Пока это так, любая проверка здесь наказывает брокера
    # за то, что он работал ровно как предписано — просто не там, где мы
    # смотрим. «Задаток» и «Сделка» вели у руководства и раньше; «Офер»
    # снят по решению агентства от 26.08 по той же причине.
    "C18:UC_8Z3SP6",  # Офер — развёрнутый комментарий ИЛИ чат
    "C18:UC_RUCRAH",  # Задаток — обсуждения в чате
    "C18:UC_8X12HI",  # Сделка — обсуждения в чате
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


SELLER_STAGE_CLOSED_SALE = "UC_A94BGF"


SELLER_STAGES_OUT_OF_QC = frozenset({
    "UC_KEOOG8",       # Переговоры — у руководства
    "UC_FADPBF",       # Поиск клиента — вне аудита
    "WON",             # Договор закрыт
    # «Закрытая продажа (На сайт)» — клиента здесь не квалифицируем, значит и
    # разбирать нечего. Единственное требование этапа — заполненный ID объекта
    # Афины, и его уже проверяет основной аудит (SELLERS_AFINA_REQUIRED_STAGES,
    # нарушение seller_afina_id_missing). Дублировать проверку в QC значит
    # получать два разных сообщения об одном и том же.
    SELLER_STAGE_CLOSED_SALE,
})


SELLER_GRACE_HOURS: dict[str, int] = {"_default": 24}


# Сколько дней у брокера есть на след работы по клиенту. Значения взяты из
# каденса основного аудита (BUYERS_STAGE_AUDIT_RULE / SELLERS_STAGE_CADENCE):
# двум правилам об одном и том же расходиться нельзя, иначе брокер получит
# два разных срока за одну и ту же работу.
BUYER_WORK_WINDOW_DAYS: dict[str, int] = {
    "C18:NEW": 2,           # Подбор
    "C18:UC_UFPFKK": 3,     # Первый показ
    "C18:UC_DVW1P9": 3,     # Повторный показ
    "C18:LOSE": 5,          # Отложенный спрос
    "C18:APOLOGY": 7,       # Сделка проиграна
    "_default": 7,
}

SELLER_WORK_WINDOW_DAYS: dict[str, int] = {
    "NEW": 1,               # Назначение встречи — каденс 24 часа
    "FINAL_INVOICE": 7,     # Подготовка в рекламу
    "LOSE": 7,              # Отложенная продажа
    "APOLOGY": 7,           # Проиграна
    "_default": 7,
}


# Условные факты — LLM их извлекает, но за отсутствие не наказываем.
BUYER_OPTIONAL_FACTS: dict[str, tuple[tuple[str, str], ...]] = {}
SELLER_OPTIONAL_FACTS: dict[str, tuple[tuple[str, str], ...]] = {
    "FINAL_INVOICE": (
        ("photo_session", "фотосессия назначена/проведена"),
        ("documents_ready", "готовность документов"),
    ),
}


def fact_name_table() -> dict[str, str]:
    """Все ключи фактов обеих воронок → русское имя.

    Модель иногда возвращает в missing сам ключ, а не человеческое имя:
    «Не хватает: budget, district, timeline» (#13340), «property_address,
    listing_price, documents_ready» (#14912). Русский отчёт с английскими
    кодами полей читать нельзя, а имена у нас уже есть — здесь они собраны
    в один словарь.
    """
    table: dict[str, str] = {}
    for source in (
        BUYER_STAGE_REQUIREMENTS, SELLER_STAGE_REQUIREMENTS,
        BUYER_OPTIONAL_FACTS, SELLER_OPTIONAL_FACTS,
    ):
        for fields in source.values():
            for key, name in fields:
                table.setdefault(key, name)
    return table


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
    temperature: Callable[[dict[str, Any], str], tuple[str, str]]
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
    # Прямые проверки полей CRM по этапам: {этап: ((код UF, имя), ...)}.
    # Это НЕ задача модели — поле либо заполнено, либо нет.
    direct_field_checks: dict[str, tuple[tuple[str, str], ...]] = field(
        default_factory=dict,
    )

    @property
    def qualification_fields(self) -> tuple[tuple[str, str], ...]:
        """Все коды UF, которые надо запросить у crm.deal.list."""
        seen: dict[str, str] = {}
        for fields in self.direct_field_checks.values():
            for code, name in fields:
                seen.setdefault(code, name)
        return tuple(seen.items())

    def fields_for_stage(self, stage_id: str) -> tuple[tuple[str, str], ...]:
        return self.direct_field_checks.get(stage_id, ())
    # Сколько дней у брокера есть на след работы по этапу. Взято из каденса
    # основного аудита: две нормы на одно и то же не должны расходиться.
    # Ключ ``_default`` — для этапов вне таблицы.
    work_window_days: dict[str, int] = field(default_factory=lambda: {"_default": 7})
    # По ту сторону сделки бывает агент, а не сам клиент. У продавцов такого
    # не бывает: продаёт собственник, и «агент» в карточке продавца означал бы
    # либо ошибку разметки, либо нашего же сотрудника.
    counterparty_can_be_agent: bool = True
    # Холодный клиент — это потеря, холодный собственник — нет. У покупателя
    # «остыл» значит, что он был в разговоре и вышел из него: событие, и
    # дорогое. У продавца из холодной базы «не выходит на связь» — обычное
    # начало: контакт взят с Циана, собственник не звал нас и отправляет
    # общаться со своим агентом. Если такую карточку звать потерей, тревожный
    # раздел заполняется первым контактом по каждой второй сделке и перестаёт
    # что-либо значить. Решение агентства: собственник холодный, но не
    # потерянный. Ярлык «холодный» при этом остаётся — уходит только тревога.
    cold_means_losing: bool = True


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
    direct_field_checks={BUYER_QUAL_STAGE: BUYER_QUALIFICATION_FIELDS},
    work_window_days=BUYER_WORK_WINDOW_DAYS,
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
    work_window_days=SELLER_WORK_WINDOW_DAYS,
    counterparty_can_be_agent=False,
    cold_means_losing=False,
)

PROFILES = {p.key: p for p in (BUYER_PROFILE, SELLER_PROFILE)}


def profile_for(key: str) -> FunnelProfile:
    """Look up a funnel profile by key ('buyers' / 'sellers')."""
    try:
        return PROFILES[str(key).strip().lower()]
    except KeyError:
        raise ValueError(f"Unknown funnel profile: {key!r}") from None
