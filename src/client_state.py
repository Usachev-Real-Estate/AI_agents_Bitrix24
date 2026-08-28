"""Buyer-funnel agent: recover client state from CRM card evidence."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Protocol

from langchain_core.messages import HumanMessage, SystemMessage

from config import Settings, get_settings
from db import get_client_state, init_db, save_client_state
from lead_quality_audit import _message_content_to_str
from funnel_profiles import BUYER_PROFILE, SELLER_PROFILE, FunnelProfile
from broker_work import (
    PORTAL_TZ,
    _extract_date,
    assess_broker_work,
    comment_without_a_call,
    has_any_call,
    has_open_future_task,
    next_action,
    open_future_deadline,
    evidence_of_the_broker,
)
from counterparty import (
    WHO_CLIENT,
    classify_counterparty,
    set_contact_type_names,
)
from llm import estimate_cost, make_llm
from masking import MaskMap, apply_mask, build_mask_map, unmask
from tools import (
    _as_list,
    _bx_get_all_sync,
    _clean_str,
    _coerce_int,
    _evidence_incomplete,
    _fetch_deal_activities,
    _fetch_entity_timeline,
    fetch_contact_type_names,
)
from transcripts import STATUS_NOT_READY, STATUS_OK, fetch_and_cache

logger = logging.getLogger(__name__)

CALL_ACTIVITY_TYPE_ID = 2
VALID_RISKS = frozenset({"low", "medium", "high"})
VALID_NEXT_STEP_WHO = frozenset({"broker", "client", "unknown"})
# Чем модель отвечает «не знаю». Отдельной константой, потому что по
# этому значению сверяются поля из разных секций её ответа.
UNKNOWN_VALUE = "unknown"


class LLMClient(Protocol):
    def invoke(self, messages: list[Any]) -> Any:
        ...


def _parse_iso_datetime(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _event_sort_key(item: dict[str, Any]) -> str:
    for key in ("created", "when", "CREATED", "START_TIME", "fetched_at"):
        text = _clean_str(item.get(key))
        if text:
            return text
    return ""


def _normalize_timeline_item(item: dict[str, Any]) -> dict[str, Any]:
    # has_files намеренно НЕ входит в _event_identity: иначе появление поля
    # переписало бы content_hash всем карточкам сразу и весь портфель ушёл бы
    # в модель заново. Доказательства работы считаются из живой карточки на
    # каждом прогоне, а не из кэша, поэтому в отпечатке им делать нечего.
    return {
        "kind": "comment",
        "id": _coerce_int(item.get("id") or item.get("ID")),
        "created": _clean_str(item.get("created") or item.get("CREATED")),
        "text": _clean_str(item.get("comment") or item.get("COMMENT")),
        "author_id": _coerce_int(item.get("author_id") or item.get("AUTHOR_ID")),
        "has_files": bool(item.get("has_files")),
    }


def _normalize_activity_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "activity",
        "direction": _coerce_int(item.get("DIRECTION") or item.get("direction")),
        "id": _coerce_int(item.get("ID") or item.get("id")),
        "created": _clean_str(
            item.get("CREATED") or item.get("START_TIME") or item.get("created"),
        ),
        "subject": _clean_str(item.get("SUBJECT") or item.get("subject")),
        "description": _clean_str(item.get("DESCRIPTION") or item.get("description")),
        "completed": _clean_str(item.get("COMPLETED") or item.get("completed")),
        "type_id": _coerce_int(item.get("TYPE_ID") or item.get("type_id")),
        # Срок дела нужен рекомендации: если по карточке уже стоит живое дело
        # на будущее, советовать «запланируйте дело» бессмысленно.
        "deadline": _clean_str(item.get("DEADLINE") or item.get("deadline")),
        # Кто завёл дело. В отпечаток карточки не входит (_event_identity),
        # поэтому поле ничего не стоит и портфель заново в модель не уедет.
        # Правила его пока не читают — см. открытый вопрос про дела, которые
        # заводит бизнес-процесс, в plans/qc-client-state-rules.md.
        "author_id": _coerce_int(
            item.get("AUTHOR_ID") or item.get("author_id")
            or item.get("RESPONSIBLE_ID") or item.get("responsible_id"),
        ),
    }


def _normalize_transcript_item(item: dict[str, Any]) -> dict[str, Any]:
    status = _clean_str(item.get("status"))
    text = _clean_str(item.get("text"))
    return {
        "kind": "transcript",
        "id": _coerce_int(item.get("activity_id") or item.get("id")),
        "created": _clean_str(item.get("activity_created") or item.get("fetched_at")),
        "status": status,
        "text": text if status == STATUS_OK else "",
        "note": (
            "расшифровка не готова"
            if status == STATUS_NOT_READY
            else ("ошибка загрузки" if status == "error" else "")
        ),
    }


COMMENT_FILE_ONLY = "Вложение без текста"


def _activity_without_text(activity: dict[str, Any]) -> str:
    """Чем является дело без темы и описания — или "" если ничем.

    Битрикс заводит звонки и задачи с пустой темой, и такое дело
    выбрасывалось из доказательств целиком: события без текста нечего
    показывать модели. Но у звонка доказательством является сам факт
    звонка, а у дела — его срок, и без них рушится половина правил: «звонка
    в таймлайне нет» на карточке, где брокер звонил; «дела нет» там, где
    оно стоит; «брошена N дней» с датой, считанной мимо этих событий.

    Текст мы не выдумываем — только называем то, что и так знаем из полей.
    """
    if int(activity.get("type_id") or 0) == CALL_ACTIVITY_TYPE_ID:
        direction = int(activity.get("direction") or 0)
        if direction == 1:
            return "Входящий звонок (без описания)"
        if direction == 2:
            return "Исходящий звонок (без описания)"
        return "Звонок (без описания)"
    if str(activity.get("deadline") or "").strip():
        return "Дело без описания"
    return ""


def build_evidence_events(
    timeline: list[dict[str, Any]],
    activities: list[dict[str, Any]],
    transcripts: list[dict[str, Any]],
    mask_map: MaskMap,
) -> list[dict[str, Any]]:
    """Merge card evidence into a single chronologically sorted event list."""
    events: list[dict[str, Any]] = []
    for item in timeline:
        if not isinstance(item, dict):
            continue
        normalized = _normalize_timeline_item(item)
        # Комментарий из одних пробелов — это пустой комментарий: он ничего
        # не рассказывает, но раньше проходил как след работы.
        normalized["text"] = normalized["text"].strip()
        if not normalized["text"]:
            # Комментарий без текста, но с вложением — это и есть скриншот
            # переписки. Выбрасывая его, мы теряли единственное подтверждение
            # «написал клиенту» и упрекали брокера в том, что он приложил.
            if not normalized["has_files"]:
                continue
            normalized["text"] = COMMENT_FILE_ONLY
            events.append(normalized)
            continue
        normalized["text"] = apply_mask(normalized["text"], mask_map)
        events.append(normalized)
    for item in activities:
        if not isinstance(item, dict):
            continue
        normalized = _normalize_activity_item(item)
        body = " — ".join(
            part for part in (normalized["subject"], normalized["description"]) if part
        )
        if not body:
            body = _activity_without_text(normalized)
        if not body:
            continue
        normalized["text"] = apply_mask(body, mask_map)
        # Маска кладётся в text, а сырые subject и description оставались в
        # том же событии — и уезжали в модель рядом с замаскированной копией.
        # Маскируем и их: событие целиком уходит в JSON запроса, и «поле, из
        # которого мы собрали текст» ничем не безопаснее самого текста.
        normalized["subject"] = apply_mask(normalized["subject"], mask_map)
        normalized["description"] = apply_mask(normalized["description"], mask_map)
        events.append(normalized)
    for item in transcripts:
        if not isinstance(item, dict):
            continue
        normalized = _normalize_transcript_item(item)
        if normalized["text"]:
            normalized["text"] = apply_mask(normalized["text"], mask_map)
        elif not normalized["note"]:
            continue
        events.append(normalized)
    events.sort(key=_event_sort_key)
    return events


FINGERPRINT_LEN = 16


def _event_identity(event: dict[str, Any]) -> dict[str, Any]:
    """Поля, по которым событие считается тем же самым."""
    return {
        "kind": event.get("kind"),
        "id": event.get("id"),
        "created": event.get("created"),
        "text": event.get("text"),
        "status": event.get("status"),
        "note": event.get("note"),
    }


def event_fingerprint(event: dict[str, Any]) -> str:
    """Отпечаток одного события.

    Отредактированный комментарий и дозагруженная расшифровка меняют текст, а
    значит и отпечаток — такое событие уедет в модель заново, и это правильно.
    """
    raw = json.dumps(_event_identity(event), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:FINGERPRINT_LEN]


def compute_content_hash(
    events: list[dict[str, Any]],
    profile: FunnelProfile | None = None,
    stage_id: str = "",
) -> str:
    """Отпечаток карточки: по нему решается, звать ли модель заново.

    Кроме событий в отпечаток входит сам промпт. Без этого разбор, сделанный
    по старым правилам, считался свежим до конца времён: карточка не менялась,
    хэш совпадал, и новый факт, который мы просим у модели, на ней не
    появлялся никогда. Так молчала пауза на #14776 — «клиент в отпуске до
    сентября» лежало в карточке, а разбор был сделан до того, как мы
    научились этот факт спрашивать.

    Промпт меняется только вместе с набором фактов, которые модель извлекает,
    поэтому его отпечаток и есть версия правил. Смена правил стоит одного
    повторного разбора по каждой карточке — ровно столько, сколько стоит
    честный ответ вместо устаревшего.
    """
    payload = [_event_identity(e) for e in events]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if profile is not None:
        raw += "\n" + profile.prompt
        # Набор фактов, которые мы спрашиваем, задаётся этапом. Сделка,
        # переехавшая на другой этап без единого нового события, отдавалась
        # из кэша — и её судили по требованиям нового этапа теми фактами,
        # которых у старого никто не спрашивал. Поля выглядели незаполненными
        # ровно потому, что вопрос не задавали.
        from funnel_profiles import all_facts_for_stage
        keys = sorted(
            key for key, _name, _req in all_facts_for_stage(profile.key, stage_id)
        )
        raw += "\n" + json.dumps(keys, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_analyzed_events(stored: str | None) -> set[str]:
    """Отпечатки из БД; мусор в колонке читается как «ничего не разобрано»."""
    if not stored:
        return set()
    try:
        data = json.loads(stored)
    except (json.JSONDecodeError, TypeError):
        return set()
    if not isinstance(data, list):
        return set()
    return {str(item) for item in data if isinstance(item, (str, int))}


def filter_new_events(
    events: list[dict[str, Any]],
    analyzed_at: str | None,
    seen: set[str] | None = None,
) -> list[dict[str, Any]]:
    """События, которых модель ещё не видела.

    Основной критерий — отпечаток: он не зависит от того, удалось ли разобрать
    дату. По watermark отбираем только когда отпечатков ещё нет (карточка
    разбиралась до появления колонки analyzed_events).
    """
    if seen:
        return [e for e in events if event_fingerprint(e) not in seen]
    if not analyzed_at:
        return list(events)
    watermark = _parse_iso_datetime(analyzed_at)
    if watermark is None:
        return list(events)
    fresh: list[dict[str, Any]] = []
    for event in events:
        created = _parse_iso_datetime(str(event.get("created") or ""))
        if created is None or created > watermark:
            fresh.append(event)
    return fresh


def trim_events_to_budget(
    events: list[dict[str, Any]],
    max_chars: int,
) -> tuple[list[dict[str, Any]], int]:
    """Keep the most recent events within a character budget.

    Returns (kept, dropped). The first analysis of a card sends its whole
    history, so a card with two dozen calls would otherwise blow past the
    model's context limit.
    """
    if max_chars <= 0:
        return list(events), 0
    kept: list[dict[str, Any]] = []
    used = 0
    for event in reversed(events):
        text = str(event.get("text") or "")
        size = len(text) + len(str(event.get("note") or ""))
        if kept and used + size > max_chars:
            break
        if not kept and size > max_chars:
            # Одна расшифровка длинного разговора может сама не влезть в лимит.
            # Обрезаем её, а не выбрасываем: начало звонка обычно и несёт суть.
            event = dict(event, text=text[:max_chars], truncated=True)
            size = max_chars
        used += size
        kept.append(event)
    kept.reverse()
    return kept, len(events) - len(kept)


def _normalize_for_match(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def verify_evidence(
    quotes: list[str],
    events: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """Split quotes into (found in the card, not found).

    The prompt demands verbatim citations, but nothing stopped the model from
    inventing one. A quote that is not in the source is the clearest signal
    that the rest of the answer was imagined too.
    """
    corpus = _normalize_for_match(
        " ".join(str(e.get("text") or "") for e in events),
    )
    verified: list[str] = []
    invented: list[str] = []
    for quote in quotes:
        needle = _normalize_for_match(quote)
        if needle and needle in corpus:
            verified.append(quote)
        else:
            invented.append(quote)
    return verified, invented


# Ключ факта «следующий шаг с датой»: единственный, который можно закрыть
# не текстом карточки, а самой CRM.
NEXT_STEP_FACT = "next_step"

VALID_VERDICTS = frozenset({"good", "tolerable", "poor", "too_early", "out_of_qc"})


# Откуда берём момент входа на этап, в порядке точности. stage_entered_at
# ставит основной аудит из crm.stagehistory.list; MOVED_TIME отдаёт сам
# crm.deal.list; DATE_CREATE — последний рубеж, он верен для карточки, которая
# с создания никуда не двигалась (а это как раз свежий лид с Циан).
STAGE_ENTRY_FIELDS = ("stage_entered_at", "MOVED_TIME", "DATE_CREATE")
# Те же поля, что можно запросить у crm.deal.list (stage_entered_at ставит
# основной аудит из истории стадий, у Битрикса такого поля нет).
STAGE_ENTRY_SELECT = ("MOVED_TIME", "DATE_CREATE")
# Источник нужен, чтобы отличить холодную базу (выгрузка, реестр) от сделок,
# где клиент уже есть: терять там нечего, потому что терять пока некого.
SOURCE_SELECT = ("SOURCE_ID",)


def _stage_hours(deal: dict[str, Any], now: datetime) -> float | None:
    """Часы с момента входа на текущий этап, или None если момент неизвестен.

    None означает «отсрочку применить не к чему», и вердикт тогда судит
    карточку так, будто она уже старая. Поэтому важно, чтобы хотя бы одно из
    полей доезжало из Битрикса: без них too_early недостижим в принципе.
    """
    from tools import _parse_datetime
    for field in STAGE_ENTRY_FIELDS:
        entered = _parse_datetime(deal.get(field))
        if entered is None:
            continue
        if entered.tzinfo is None:
            entered = entered.replace(tzinfo=timezone.utc)
        return max(0.0, (now - entered).total_seconds() / 3600.0)
    return None


def grace_period_for(profile: FunnelProfile, stage_id: str) -> int:
    """Отсрочка (часы) от входа на этап до применения требований."""
    if stage_id in profile.grace_hours:
        return int(profile.grace_hours[stage_id])
    return int(profile.grace_hours.get("_default", 24))


def check_qualification_fields(
    deal: dict[str, Any],
    profile: FunnelProfile,
    stage_id: str = "",
) -> tuple[bool | None, list[str]]:
    """Прямая проверка UF-полей карточки для этапа (без LLM).

    Возвращает (все_заполнены?, список_пустых). None — для этапа проверка не
    настроена, и вердикт её не применяет.

    Значение считается заполненным, если поле не пустое, не False, не None
    и не строка вида ""/"0". UF Битрикса бывают строкой, числом, списком —
    все три случая покрываем как пустоту если "содержательного" ничего нет.
    """
    fields = profile.fields_for_stage(stage_id)
    if not fields:
        return None, []
    missing: list[str] = []
    for code, name in fields:
        value = deal.get(code)
        if value in (None, "", "0", 0, False, []):
            missing.append(name)
    return len(missing) == 0, missing


def stage_skips_analysis(stage_id: str, profile: FunnelProfile) -> str:
    """Причина не звать модель по этому этапу, или "" если звать надо.

    Этапы из stages_out_of_qc агентство сняло с контроля качества («у
    руководства», «вне аудита»), и compute_completeness_verdict возвращает по
    ним out_of_qc независимо от того, что скажет модель. Значит разбор такой
    карточки — оплаченный запрос, результат которого заведомо не используется.
    Этап известен из crm.deal.list, поэтому отсечь можно до сбора таймлайна.

    Этапы БЕЗ требований сюда намеренно не попадают: вердикт у них тоже
    out_of_qc, но это «правила ещё не написаны», а не «не наше дело», и
    температура по таким карточкам РОПу всё ещё нужна.
    """
    if not stage_id:
        # Пустой этап — не повод молча пропустить карточку.
        return ""
    if stage_id in profile.stages_out_of_qc:
        return "stage_out_of_qc"
    return ""


def compute_completeness_verdict(
    stage_id: str,
    state: dict[str, Any],
    profile: FunnelProfile,
    *,
    hours_on_stage: float | None,
    qualification_ok: bool | None = None,
    task_scheduled: bool = False,
) -> tuple[str, str]:
    """Итог по карточке: хорошо / терпимо / плохо / рано судить / вне QC.

    Правила согласованы с агентством (см. plans/qc-completeness-draft.md):
    * good — обязательные факты есть, комментарий похож на разговор (нет
      material расхождений), ключевые поля квалификации заполнены (если
      проверка настроена);
    * tolerable — есть только minor расхождения ИЛИ не хватает одного факта,
      при этом основные поля квалификации заполнены;
    * poor — есть material расхождение, ИЛИ (после отсрочки) recoverable=false
      либо не хватает двух и более обязательных фактов;
    * too_early — этап моложе отсрочки. Проверяется раньше, чем пустота
      карточки: свежий лид пуст не по вине брокера.
    """
    if stage_id in profile.stages_out_of_qc:
        return "out_of_qc", "этап не в контроле качества (у руководства)"

    requirements = profile.stage_requirements.get(stage_id, ())
    required_keys = [key for key, _name in requirements]
    # Человеческие названия фактов — их читает РОП. Служебному ключу
    # («budget», «timeline») в отчёте делать нечего.
    fact_names = {key: name for key, name in requirements}
    if not required_keys:
        field_names = [name for _code, name in profile.fields_for_stage(stage_id)]
        if field_names:
            # Этап проверяется только полями CRM («Закрытая продажа» — ID
            # Афины). Фактов из текста здесь не требуют, поэтому вердикт
            # целиком определяет прямая проверка.
            listed = ", ".join(field_names)
            if qualification_ok is False:
                return "poor", f"не заполнено: {listed}"
            if qualification_ok is None:
                # Проверку не выполнили — судить не о чем. Ставить «хорошо»
                # по непроверенному полю значит выдать пробел за результат.
                return "no_rules", f"поля этапа не проверены: {listed}"
            return "good", f"заполнено: {listed}"
        # Отдельный вердикт, а не out_of_qc. «Сняли с контроля» — решение
        # агентства, «правила не написаны» — наша недоделка, и показывать
        # вторую как первую значит спрятать пробел за формулировкой.
        # Разбор при этом идёт: температура, риск потери и подтверждение
        # работы брокера от полноты карточки не зависят.
        return "no_rules", "правила полноты для этапа не заданы"

    # Отсрочка проверяется РАНЬШЕ, чем «карточка неинформативна». Лид, который
    # пришёл час назад, пуст по определению — брокер ещё не звонил. Пометить
    # такую карточку «плохо» значит обвинить брокера в том, чего он не успел.
    # Расхождение с разговором остаётся выше отсрочки: это про достоверность
    # написанного, а не про то, сколько времени прошло.
    grace = grace_period_for(profile, stage_id)
    if hours_on_stage is not None and hours_on_stage < grace:
        # #15594: «этап моложе отсрочки (24 ч < 24 ч)» — неравенство, которое
        # само себя опровергает. Карточке было 23.6 часа, округление до целого
        # съело разницу, на которой стоит вердикт. У границы показываем
        # десятую долю: цифра, которой обосновано решение, должна сходиться.
        shown = (
            f"{hours_on_stage:.1f}"
            if round(hours_on_stage) >= grace else f"{hours_on_stage:.0f}"
        )
        return "too_early", (
            f"этап моложе отсрочки ({shown} ч < {grace} ч)"
        )

    if not state.get("recoverable", True):
        return "poor", "по карточке нельзя восстановить картину клиента"

    stage_facts = state.get("stage_facts") or {}

    def _present(key: str) -> bool:
        if (stage_facts.get(key) or {}).get("present"):
            return True
        # Следующий шаг засчитывается двумя способами: он написан в
        # комментарии — или по карточке стоит дело в Битриксе. Дело с датой
        # и есть следующий шаг, причём доказательство лучше пересказа: его
        # видно в CRM, а не только на словах брокера.
        return key == NEXT_STEP_FACT and task_scheduled

    missing = [
        fact_names.get(key, key) for key in required_keys
        if not _present(key)
    ]

    qual_gap = qualification_ok is False
    # Сколько фактов всё-таки есть. Без этой цифры «плохо» у карточки с шестью
    # фактами из восьми и у пустой карточки выглядит одинаково, и РОП не может
    # понять, с какой начинать. Порог вердикта при этом не меняется.
    present = len(required_keys) - len(missing)
    score = f" (есть {present} из {len(required_keys)})"
    if len(missing) >= 2 or (missing and qual_gap):
        return "poor", (
            "не хватает обязательных фактов: " + ", ".join(missing) + score
            + (" · ключевые поля карточки не заполнены" if qual_gap else "")
        )
    if missing or qual_gap:
        reasons = []
        if missing:
            reasons.append(f"нет факта: {missing[0]}{score}")
        if qual_gap:
            reasons.append("не все ключевые поля карточки заполнены")
        return "tolerable", "; ".join(reasons)
    return "good", "обязательные факты на месте"


def compute_temperature(
    signals: dict[str, Any],
    *,
    recoverable: bool = True,
    profile: FunnelProfile = BUYER_PROFILE,
    counterparty: str = "",
    task_scheduled: bool = False,
) -> tuple[str, str]:
    """Derive client temperature from extracted signals. Returns (level, why).

    Deliberately computed here rather than asked of the model: the definition
    belongs to the agency, must not drift between runs, and has to be arguable
    with a ROP. The rule itself lives in the funnel profile — buyers and
    sellers are ready for different things.
    """
    if not recoverable:
        return "unknown", "по карточке нельзя восстановить картину клиента"
    return profile.temperature(signals, counterparty, task_scheduled)


def _parse_state_json(content: str) -> dict[str, Any] | None:
    if "{" not in content:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    json_str = fence.group(1) if fence else content[content.index("{") :]
    try:
        parsed, _ = json.JSONDecoder().raw_decode(json_str)
    except ValueError:
        logger.warning("Client state: LLM response is not valid JSON (%d chars)", len(content))
        return None
    return parsed if isinstance(parsed, dict) else None


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


VALID_HORIZONS = frozenset({
    "до месяца", "1-3 месяца", "более 3 месяцев", "unknown",
})
# LLM ставит severity низкий/средний/высокий. Порог по цифрам (30 %) прописан
# в промпте; здесь только разделяем на мелкое (low) и существенное (medium/high).


def merge_stage_facts(
    fresh: dict[str, Any],
    previous: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Дополнить свежие факты этапа теми, что модель в этот раз не видела.

    При дозапросе модели показывают только новые события, а факт мог быть
    найден в старом комментарии. Ответ «present: false» на таком запросе
    означает «в новых событиях этого нет», а не «в карточке этого нет», —
    и разница между ними решает, назовём мы карточку заполненной плохо или
    хорошо. Молчание модели о факте — тем более не находка.

    Забирается только то, что было подтверждено раньше. Цитаты всё равно
    проверяются по живой карточке следом: если комментарий из таймлайна
    удалили, факт отвалится там, а не выживет за счёт кэша.
    """
    merged = _normalize_stage_facts(fresh)
    for key, value in (previous or {}).items():
        if not isinstance(value, dict) or not value.get("present"):
            continue
        if (merged.get(key) or {}).get("present"):
            continue
        quote = str(value.get("quote") or "").strip()
        if not quote:
            continue
        merged[key] = {"present": True, "quote": quote}
    return merged


def _normalize_stage_facts(raw: Any) -> dict[str, dict[str, Any]]:
    """Нормализация stage_facts: {key: {"present": bool, "quote": str}}."""
    data = raw if isinstance(raw, dict) else {}
    out: dict[str, dict[str, Any]] = {}
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        out[str(key)] = {
            "present": bool(value.get("present")),
            "quote": _clean_str(value.get("quote")),
        }
    return out


def _normalize_broker_work(raw: Any) -> dict[str, Any]:
    """Что модель прочитала про подтверждение работы.

    По умолчанию comment_informative=True: молчание модели не должно
    превращаться в претензию к брокеру.
    """
    data = raw if isinstance(raw, dict) else {}
    return {
        "claims_messaged": bool(data.get("claims_messaged")),
        "claims_messaged_quote": _clean_str(data.get("claims_messaged_quote")),
        "claims_no_answer": bool(data.get("claims_no_answer")),
        "claims_no_answer_quote": _clean_str(data.get("claims_no_answer_quote")),
        "comment_informative": bool(data.get("comment_informative", True)),
        # Названная причина паузы на стороне клиента: отпуск, документы,
        # продажа своего объекта. Не то же, что «клиент не отвечает»: там
        # причины не знают, здесь она есть в тексте карточки.
        "pause_explained": bool(data.get("pause_explained")),
        "pause_reason_quote": _clean_str(data.get("pause_reason_quote")),
        "pause_until": _clean_str(data.get("pause_until")) or "unknown",
    }


def _normalize_signals(raw: Any, profile: FunnelProfile) -> dict[str, Any]:
    """Facts the temperature rules are computed from (funnel-specific)."""
    data = raw if isinstance(raw, dict) else {}
    signals = profile.normalize_signals(
        data, lambda v: int(_coerce_float(v, 0)),
    )
    objections = data.get("objections") if isinstance(data.get("objections"), list) else []
    signals["objections"] = [_clean_str(x) for x in objections if _clean_str(x)]
    return signals


def _normalize_state(
    raw: dict[str, Any],
    profile: FunnelProfile = BUYER_PROFILE,
) -> dict[str, Any]:
    last_event = raw.get("last_event") if isinstance(raw.get("last_event"), dict) else {}
    next_step = raw.get("next_step") if isinstance(raw.get("next_step"), dict) else {}
    blockers = raw.get("blockers") if isinstance(raw.get("blockers"), list) else []
    missing = raw.get("missing") if isinstance(raw.get("missing"), list) else []
    evidence = raw.get("evidence") if isinstance(raw.get("evidence"), list) else []
    risk = str(raw.get("risk") or "medium").strip().lower()
    if risk not in VALID_RISKS:
        risk = "medium"
    who = str(next_step.get("who") or "unknown").strip().lower()
    if who not in VALID_NEXT_STEP_WHO:
        who = "unknown"
    return {
        "client_goal": _clean_str(raw.get("client_goal")),
        "situation": _clean_str(raw.get("situation")),
        "last_event": {
            "what": _clean_str(last_event.get("what")),
            "when": _clean_str(last_event.get("when")) or "unknown",
        },
        "next_step": {
            "what": _clean_str(next_step.get("what")),
            "when": _clean_str(next_step.get("when")) or "unknown",
            "who": who,
        },
        "blockers": [_clean_str(x) for x in blockers if _clean_str(x)],
        "risk": risk,
        "recoverable": bool(raw.get("recoverable", True)),
        "missing": [_clean_str(x) for x in missing if _clean_str(x)],
        "confidence": max(0.0, min(1.0, _coerce_float(raw.get("confidence"), 0.0))),
        "evidence": [_clean_str(x) for x in evidence if _clean_str(x)],
        "signals": _normalize_signals(raw.get("signals"), profile),
        "stage_facts": _normalize_stage_facts(raw.get("stage_facts")),
        "broker_work": _normalize_broker_work(raw.get("broker_work")),
    }


EMPTY_CARD_NOTE = "в карточке нет ни комментариев, ни дел, ни расшифровок"


def empty_card_state(profile: FunnelProfile = BUYER_PROFILE) -> dict[str, Any]:
    """Состояние карточки, в которой модели нечего читать.

    Ответ здесь предрешён: без единого комментария, дела и расшифровки
    восстановить картину клиента нельзя, и модель вернёт ровно это же — за
    деньги. Пустота карточки видна из самих данных, поэтому вывод делаем в
    коде. Вердикт и температура считаются дальше обычными правилами: пустая
    карточка на свежем этапе — «рано судить», на застоявшемся — «плохо».
    """
    return _normalize_state(
        {
            "recoverable": False,
            "confidence": 0.0,
            "risk": "medium",
            "missing": [EMPTY_CARD_NOTE],
        },
        profile,
    )


def unmask_state(
    state: dict[str, Any],
    mask_map: MaskMap,
    profile: FunnelProfile = BUYER_PROFILE,
) -> dict[str, Any]:
    """Human-facing copy with real names restored.

    The stored copy stays masked: it is fed back to the model as
    previous_state on the next run, and unmasking before saving would leak
    the contacts the masking exists to protect.
    """
    out = json.loads(json.dumps(state, ensure_ascii=False))
    for key in ("client_goal", "situation"):
        out[key] = unmask(out.get(key, ""), mask_map)
    for key in ("last_event", "next_step"):
        block = out.get(key)
        if isinstance(block, dict):
            block["what"] = unmask(block.get("what", ""), mask_map)
    for key in ("blockers", "missing", "evidence"):
        values = out.get(key)
        if isinstance(values, list):
            out[key] = [unmask(str(v), mask_map) for v in values]
    signals = out.get("signals")
    if isinstance(signals, dict):
        for key in profile.text_signals:
            if key in signals:
                signals[key] = unmask(str(signals.get(key) or ""), mask_map)
        signals["objections"] = [
            unmask(str(v), mask_map) for v in signals.get("objections") or []
        ]
    # Совет и тема дела собираются из замаскированных событий, а читает их
    # РОП. Без разворота строка «➡️ связаться по делу «Звонок КЛИЕНТ_1»»
    # стоит под строкой с настоящим именем в шапке карточки.
    out["next_action"] = unmask(str(out.get("next_action") or ""), mask_map)
    evidence = out.get("work_evidence")
    if isinstance(evidence, dict):
        due = evidence.get("due_task")
        if isinstance(due, dict):
            due["subject"] = unmask(str(due.get("subject") or ""), mask_map)
        # Текст дела-заглушки печатается РОПу как довод: по нему видно, за
        # что карточка получила претензию. Без разворота там будет
        # «в деле только: „Позвонить КЛИЕНТ_1"».
        if evidence.get("task_text"):
            evidence["task_text"] = unmask(
                str(evidence.get("task_text") or ""), mask_map,
            )

    work = out.get("broker_work")
    if isinstance(work, dict):
        # Причину паузы читают люди в отчёте — иначе там будет «КЛИЕНТ_1
        # в отпуске». В базе копия остаётся замаскированной.
        work["pause_reason_quote"] = unmask(
            str(work.get("pause_reason_quote") or ""), mask_map,
        )
    for entry in (out.get("stage_facts") or {}).values():
        if isinstance(entry, dict):
            entry["quote"] = unmask(str(entry.get("quote") or ""), mask_map)
    return out


def build_llm_payload(
    deal: dict[str, Any],
    previous_state: dict[str, Any] | None,
    new_events: list[dict[str, Any]],
    all_events: list[dict[str, Any]],
    profile: FunnelProfile = BUYER_PROFILE,
    mask_map: MaskMap | None = None,
) -> str:
    """Human message body for incremental state update.

    Список фактов этапа приходит сюда, а не в системный промпт: правила
    меняются в profile без переписывания промпта, а модель отвечает по тем
    же ключам, что мы передали.
    """
    from funnel_profiles import all_facts_for_stage
    stage_id = _clean_str(deal.get("STAGE_ID") or deal.get("stage_id"))
    facts_needed = [
        {"key": key, "name": name, "required": required}
        for key, name, required in all_facts_for_stage(profile.key, stage_id)
    ]
    # Порядок ключей подобран под кэш префикса у провайдера: stage_id и
    # facts_needed одинаковы для всех карточек одного этапа, поэтому идут
    # первыми — так закэшированный префикс тянется дальше системного промпта.
    # На смысл запроса порядок ключей в JSON не влияет.
    payload = {
        "stage_id": stage_id,
        "facts_needed": facts_needed,
        "deal_id": _coerce_int(deal.get("ID") or deal.get("id")),
        # Название сделки в этом агентстве — это ФИО клиента, а нередко и
        # телефон: «Лариса (Клекова) агент», «Гуля Базарова (Шахмурад)».
        # Событиям маску ставили, а названию — нет, и оно уезжало в модель
        # первой же строкой запроса.
        "title": (
            apply_mask(_clean_str(deal.get("TITLE") or deal.get("title")), mask_map)
            if mask_map is not None
            else _clean_str(deal.get("TITLE") or deal.get("title"))
        ),
        "previous_state": previous_state,
        "new_events": new_events,
        "event_count_total": len(all_events),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


USAGE_KEYS = (
    "input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens",
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


def analyze_with_llm(
    deal: dict[str, Any],
    previous_state: dict[str, Any] | None,
    new_events: list[dict[str, Any]],
    all_events: list[dict[str, Any]],
    llm: LLMClient,
    profile: FunnelProfile = BUYER_PROFILE,
    usage_sink: dict[str, int] | None = None,
    mask_map: MaskMap | None = None,
) -> dict[str, Any] | None:
    """Call LLM and return normalized client state."""
    human = build_llm_payload(
        deal, previous_state, new_events, all_events, profile, mask_map,
    )
    response = llm.invoke([
        SystemMessage(content=profile.prompt),
        HumanMessage(content=human),
    ])
    if usage_sink is not None:
        for key, value in extract_usage(response).items():
            usage_sink[key] = usage_sink.get(key, 0) + value
    content = _message_content_to_str(getattr(response, "content", response))
    parsed = _parse_state_json(content)
    if not parsed:
        return None
    return _normalize_state(parsed, profile)


def fetch_deal_contacts(deal: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """Контакты сделки для маскировки и признак «прочитали не всё».

    Возвращает (контакты, failed). Раньше сбойный crm.contact.get просто
    пропускался, и карточка с непрочитанным контактом выглядела как карточка
    без контактов: маска пустая, маскировать нечего — и имя с телефоном из
    комментариев уезжали в модель открытым текстом. Пустой список контактов
    и несостоявшееся чтение контактов — разные вещи, и различить их может
    только вызывающий код.
    """
    if isinstance(deal.get("contacts"), list):
        return [c for c in deal["contacts"] if isinstance(c, dict)], False
    failed = False
    contact_ids: list[int] = []
    main_id = _coerce_int(deal.get("CONTACT_ID") or deal.get("contact_id"))
    if main_id > 0:
        contact_ids.append(main_id)
    deal_id = _coerce_int(deal.get("ID") or deal.get("id"))
    if deal_id > 0:
        try:
            raw = _bx_get_all_sync(
                "crm.deal.contact.items.get",
                {"id": deal_id},
            )
        except Exception:
            logger.warning("Deal contacts fetch failed for deal id=%s", deal_id)
            raw = []
            failed = True
        for row in _as_list(raw):
            if isinstance(row, dict):
                cid = _coerce_int(row.get("CONTACT_ID") or row.get("contact_id"))
                if cid > 0:
                    contact_ids.append(cid)
    contacts: list[dict[str, Any]] = []
    seen: set[int] = set()
    for cid in contact_ids:
        if cid in seen:
            continue
        seen.add(cid)
        try:
            row = _bx_get_all_sync("crm.contact.get", {"id": cid})
        except Exception:
            logger.warning("Contact fetch failed for deal contact id=%s", cid)
            failed = True
            continue
        if isinstance(row, dict):
            contacts.append(row)
    return contacts, failed


def prepare_deal_record(deal: dict[str, Any], settings: Settings | None = None) -> dict[str, Any]:
    """Attach timeline, activities, transcripts; mark incomplete evidence."""
    settings = settings or get_settings()
    deal_id = _coerce_int(deal.get("ID") or deal.get("id"))
    _, timeline, timeline_failed = _fetch_entity_timeline(deal_id, "deal")
    _, activities, activities_failed = _fetch_deal_activities(deal_id)
    record = dict(deal)
    record["timeline"] = timeline
    record["activities"] = activities
    contacts, contacts_failed = fetch_deal_contacts(deal)
    record["contacts"] = contacts
    record["evidence_incomplete"] = (
        timeline_failed or activities_failed or contacts_failed
    )
    if not record["evidence_incomplete"]:
        record["transcripts"] = fetch_and_cache(deal_id, settings=settings)
    else:
        record["transcripts"] = []
    return record


def reconcile_step_date(state: dict[str, Any]) -> str:
    """Свести к одной дату следующего шага и вернуть её ("" — даты нет).

    Дату шага модель заполняет дважды: в signals.next_step_date, откуда её
    берёт температура, и в next_step.when, который печатается в отчёте и по
    которому строится совет. Поля независимы, и модель отвечала по ним
    вразнобой. #14362: «🔥 горячий — есть согласованный шаг с датой» и
    строкой ниже «Шаг: … (не указано, брокер)», а советом — «запланировать
    дело с датой». Три строки на одной карточке, и все три про одно и то же.

    Кто прав, выбирать не из чего: обе половины — ответ одной и той же
    модели про одну и ту же карточку. Поэтому не выбираем, а сводим: дата,
    названная в любой из половин, становится общей. Ничего не выдумывается —
    пустое поле заполняется соседним, а два разных значения не трогаются:
    там спорить не с чем, дата есть в обоих.

    Заодно это выносит на свет число, на котором стоит вердикт: раньше
    температура опиралась на дату, которой в отчёте не было видно, и
    проверить её РОП не мог.
    """
    signals = state.get("signals") if isinstance(state.get("signals"), dict) else None
    step = state.get("next_step") if isinstance(state.get("next_step"), dict) else None
    if signals is None and step is None:
        return ""

    signal_date = _clean_str((signals or {}).get("next_step_date"))
    if signal_date.lower() == UNKNOWN_VALUE:
        signal_date = ""
    when = _clean_str((step or {}).get("when"))
    if when.lower() == UNKNOWN_VALUE:
        when = ""

    # Из фразы «в пятницу» даты не выйдет — и это правильный ответ «даты
    # нет»: совет как раз просит её уточнить.
    parsed = _extract_date(when) if when else None
    when_date = parsed.astimezone(PORTAL_TZ).date().isoformat() if parsed else ""

    resolved = when_date or signal_date
    if not resolved:
        return ""
    if signals is not None and not signal_date:
        signals["next_step_date"] = resolved
    if step is not None and not when:
        step["when"] = resolved
    return resolved


def allowed_task_authors(
    broker_id: int,
    broker_dept_map: dict[int, int] | None,
    rop_map: dict[int, int] | None,
) -> set[int]:
    """Чьи дела засчитываются: брокер и его РОП.

    Пустое множество означает «правило не применяем»: карты не построились
    или брокер неизвестен. Это сознательно — см. evidence_of_the_broker.
    """
    if not broker_id or not broker_dept_map or not rop_map:
        return set()
    allowed = {broker_id}
    dept = broker_dept_map.get(broker_id)
    if dept:
        rop_id = rop_map.get(dept)
        if rop_id:
            allowed.add(rop_id)
    return allowed


def apply_derived_verdict(
    state: dict[str, Any],
    record: dict[str, Any],
    profile: FunnelProfile,
    envelope: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
    settings: Settings | None = None,
    task_authors: set[int] | None = None,
) -> dict[str, Any]:
    """Пересчитать температуру и вердикт по уже извлечённым фактам.

    И то и другое — чистые функции от state, этапа и его возраста, а не ответ
    модели. Считать их один раз и класть в кэш нельзя по двум причинам:

    * возраст этапа растёт. Карточка, разобранная внутри отсрочки, навсегда
      осталась бы «рано судить», даже если после этого в ней месяц ничего не
      происходит — а это ровно тот случай, ради которого аудит и существует;
    * правила меняются. content_hash покрывает содержимое карточки, но не
      порог вердикта, поэтому после правки правил кэш выдавал бы старые
      оценки как текущие.

    Поэтому дорогое (разбор моделью) кэшируется, а дешёвое считается заново
    на каждом прогоне.
    """
    deal_id = _coerce_int(record.get("ID") or record.get("id"))
    envelope = envelope if envelope is not None else {}

    stage_id = _clean_str(record.get("STAGE_ID") or record.get("stage_id"))

    # Клиент или агент. Признак ищется в CRM, а не у модели, и считается
    # заново: тип контакта могли проставить уже после разбора. Считается до
    # температуры: у агента горизонт покупки принадлежит его клиентам, а не
    # ему, и холодом карточку не делает.
    state["counterparty"] = (
        classify_counterparty(record, _as_list(record.get("contacts")))
        if profile.counterparty_can_be_agent
        else {"who": WHO_CLIENT, "why": ""}
    )

    reconcile_step_date(state)

    # Дело с датой в Битриксе и есть следующий шаг — так уже считает вердикт
    # (см. NEXT_STEP_FACT). Отчёт про это не знал и печатал «шаг не назначен»
    # на карточке, у которой дело стоит: #13520 — «шаг не назначен» и строкой
    # ниже «дело стоит на 2026-09-01». Кладём дату сюда, чтобы обе строки
    # говорили об одном. Считается до температуры: её формулировка про
    # несогласованный шаг тоже зависит от того, стоит ли дело.
    # Чужие дела не считаются нигде: если бы вердикт видел дело робота, а
    # оценка работы — нет, карточка получила бы «шаг назначен» и «следов
    # работы нет» разом.
    own_events = evidence_of_the_broker(events or [], task_authors)
    _scheduled = open_future_deadline(own_events)
    state["scheduled_task_at"] = (
        _scheduled.astimezone(PORTAL_TZ).date().isoformat()
        if _scheduled is not None else ""
    )

    level, why = compute_temperature(
        state.get("signals", {}),
        recoverable=bool(state.get("recoverable", True)),
        profile=profile,
        counterparty=str(state["counterparty"].get("who") or ""),
        task_scheduled=_scheduled is not None,
    )
    state["temperature"] = level
    state["temperature_reason"] = why
    envelope["temperature"] = level
    hours_on_stage = _stage_hours(record, datetime.now(timezone.utc))
    envelope["stage_age_known"] = hours_on_stage is not None
    if hours_on_stage is None:
        logger.info(
            "Deal %s: момент входа на этап неизвестен — отсрочка не применяется",
            deal_id,
        )

    # Прямая проверка полей квалификации — только на этапе, для которого
    # она настроена. Иначе qualification_ok=None, вердикт её не учитывает.
    qualification_ok, missing_fields = check_qualification_fields(
        record, profile, stage_id,
    )
    envelope["qualification_missing"] = missing_fields
    if missing_fields:
        logger.info(
            "Deal %s: unfilled qualification fields: %s",
            deal_id, ", ".join(missing_fields),
        )

    verdict, verdict_reason = compute_completeness_verdict(
        stage_id, state, profile,
        hours_on_stage=hours_on_stage,
        qualification_ok=qualification_ok,
        task_scheduled=has_open_future_task(own_events),
    )
    state["verdict"] = verdict
    state["verdict_reason"] = verdict_reason
    envelope["verdict"] = verdict

    state["source_id"] = _clean_str(
        record.get("SOURCE_ID") or record.get("source_id"),
    )

    # Подтверждение работы брокера считается по живой карточке на каждом
    # прогоне: скриншот могли приложить уже после разбора, а обвинение по
    # устаревшим данным — худшее, что этот отчёт может сделать.
    work = state.get("broker_work") or {}
    # Цитата ведёт к претензии, поэтому планка та же, что у evidence:
    # не нашли дословно — считаем, что утверждения не было.
    corpus = (
        _normalize_for_match(" ".join(str(e.get("text") or "") for e in events))
        if events is not None else None
    )

    def _claimed(flag_key: str, quote_key: str, label: str) -> bool:
        if not work.get(flag_key):
            return False
        if corpus is None:
            return True
        needle = _normalize_for_match(work.get(quote_key))
        if not needle or needle not in corpus:
            logger.info(
                "Deal %s: цитата «%s» не найдена в карточке — "
                "утверждение не засчитано",
                deal_id, label,
            )
            return False
        return True

    _step = state.get("next_step") if isinstance(state.get("next_step"), dict) else {}
    # Что осталось от утверждений модели после сверки цитат. Без этого по
    # прогону нельзя отличить «модель не поставила флаг» от «цитата не нашлась
    # в карточке», а это два разных разговора: первый — про промпт, второй —
    # про то, что модель сослалась на несуществующую фразу.
    state["work_claims"] = {
        "comment_informative": (
            bool(work.get("comment_informative", True))
            or state.get("recoverable") is not False
        ),
        "claims_messaged": _claimed(
            "claims_messaged", "claims_messaged_quote", "написал клиенту",
        ),
        "claims_no_answer": _claimed(
            "claims_no_answer", "claims_no_answer_quote", "клиент не отвечает",
        ),
        "pause_explained": _claimed(
            "pause_explained", "pause_reason_quote", "причина паузы",
        ),
    }
    # Молчит ли контрагент — и знаем ли мы это, или только предполагаем.
    #
    # Ключ у воронок разный (client_responsive против owner_responsive), а
    # совету нужен один ответ; оба умолчанием True, поэтому отсутствующий
    # ключ на итог не влияет.
    #
    # Одного флага модели мало. Прогон 28.08 13:07: на карточках, где
    # «связи с клиентом пока не зафиксировано» и «факт коммуникации
    # отсутствует», модель ставила owner_responsive=false — не потому, что
    # собственник отказывается говорить, а потому, что разговора в карточке
    # нет вовсе. Совет выходил обвинительным не в ту сторону: брокеру,
    # который ни разу не звонил, предлагалось записать, «сколько раз уже
    # пробовали». На соседних карточках с тем же содержанием флаг
    # оставался true — значит по нему одному судить нельзя.
    #
    # Молчание контрагента — это событие, и оно требует следа: попытки
    # дозвона в таймлайне или слов брокера «не отвечает», подтверждённых
    # цитатой. Нет следа — нет и утверждения о молчании.
    _signals = state.get("signals") if isinstance(state.get("signals"), dict) else {}
    _model_says_silent = not (
        bool(_signals.get("client_responsive", True))
        and bool(_signals.get("owner_responsive", True))
    )
    state["counterparty_silent"] = bool(
        _model_says_silent
        and (
            has_any_call(events or [])
            or state["work_claims"]["claims_no_answer"]
        ),
    )

    # Пометка, а не претензия: работа описана комментарием, а разговора по
    # карточке не видно. Считается отдельно от оценки работы и на разделы
    # отчёта не влияет.
    state["no_call"] = comment_without_a_call(
        # Пометка про комментарий без звонка считается по тем же
        # комментариям, что и оценка работы: иначе «работа описана
        # комментарием» стояло бы на карточке, где комментарий чужой.
        own_events,
        profile=profile,
        stage_id=stage_id,
        comment_informative=state["work_claims"]["comment_informative"],
        hours_on_stage=hours_on_stage,
    )
    state["work_evidence"] = assess_broker_work(
        events or [],
        profile=profile,
        stage_id=stage_id,
        hours_on_stage=hours_on_stage,
        claims_messaged=state["work_claims"]["claims_messaged"],
        claims_no_answer=state["work_claims"]["claims_no_answer"],
        # «Из комментария не понять, что с клиентом» засчитывается только
        # тогда, когда модель и сама признала, что картину не восстановила.
        # #16192: она написала три предложения про собственника, дизайн-проект
        # и ожидание оценки — и тем же ответом заявила, что комментарий ни о
        # чём. Обвинять брокера на основании ответа, который спорит сам с
        # собой, нельзя.
        comment_informative=state["work_claims"]["comment_informative"],
        abandoned_days=float(
            (settings or get_settings()).client_state_abandoned_days,
        ),
        # Ход за контрагентом снимает претензию за тишину: он сам назвал срок.
        next_step_who=str(_step.get("who") or ""),
        next_step_when=str(_step.get("when") or ""),
        # Названная причина паузы — тоже снимает, но только вместе с делом
        # на контроле и только на тот срок, который причина покрывает.
        pause_explained=state["work_claims"]["pause_explained"],
        pause_until=str(work.get("pause_until") or ""),
        # Дело засчитывается, только если его завёл брокер или его РОП
        # (решение агентства от 28.08).
        task_authors=task_authors,
    )
    envelope["work_proven"] = state["work_evidence"]["proven"]

    # Рецепт, а не только диагноз. Считается заново на каждом прогоне: дело
    # могли поставить уже после разбора, и тогда совет надо снять.
    state["next_action"] = next_action(
        state, events or [], task_authors=task_authors,
    )
    return state


def analyze_deal(
    deal: dict[str, Any],
    *,
    profile: FunnelProfile = BUYER_PROFILE,
    settings: Settings | None = None,
    llm: LLMClient | None = None,
    prepared: bool = False,
    force: bool = False,
    rop_map: dict[int, int] | None = None,
    broker_dept_map: dict[int, int] | None = None,
) -> dict[str, Any]:
    """Analyze one deal by its funnel profile; returns state or skip reason."""
    settings = settings or get_settings()
    deal_id = _coerce_int(deal.get("ID") or deal.get("id"))
    # Чьи дела засчитываем: ответственного брокера и его РОПа (решение
    # агентства от 28.08). Карты строит прогон один раз на всю выборку —
    # если их не передали, множество пустое, и тогда правило не применяется
    # вовсе: незнание автора не должно превращаться в претензию к брокеру.
    task_authors = allowed_task_authors(
        _coerce_int(deal.get("ASSIGNED_BY_ID") or deal.get("assigned_by_id")),
        broker_dept_map,
        rop_map,
    )
    envelope: dict[str, Any] = {
        "deal_id": deal_id,
        "skipped": False,
        "reason": "",
        "state": None,
        "content_hash": "",
    }
    if deal_id <= 0:
        envelope["skipped"] = True
        envelope["reason"] = "invalid_deal_id"
        return envelope

    # Этап приходит из crm.deal.list, до сбора таймлайна и до модели. Если по
    # нему вердикт всё равно out_of_qc — не платим за разбор.
    early_skip = stage_skips_analysis(
        _clean_str(deal.get("STAGE_ID") or deal.get("stage_id")), profile,
    )
    if early_skip and not force:
        envelope["skipped"] = True
        envelope["reason"] = early_skip
        envelope["verdict"] = "out_of_qc"
        return envelope

    if prepared:
        record = deal
    else:
        try:
            record = prepare_deal_record(deal, settings=settings)
        except Exception as exc:  # noqa: BLE001 — одна карточка не роняет батч
            logger.warning("Card collection failed for deal %s: %s", deal_id, exc)
            envelope["skipped"] = True
            envelope["reason"] = "collect_error"
            return envelope
    if _evidence_incomplete(record):
        envelope["skipped"] = True
        envelope["reason"] = "evidence_incomplete"
        return envelope

    mask_map = build_mask_map(record, record.get("contacts"))
    events = build_evidence_events(
        _as_list(record.get("timeline")),
        _as_list(record.get("activities")),
        _as_list(record.get("transcripts")),
        mask_map,
    )
    content_hash = compute_content_hash(
        events, profile, _clean_str(record.get("STAGE_ID") or record.get("stage_id")),
    )
    envelope["content_hash"] = content_hash
    # Звонок в таймлайне — главное доказательство работы после того, как
    # сверку пересказа с расшифровкой сняли. Строка «разговор читается у 0
    # из 10» читалась как «звонков не было», хотя на деле звонки были, а не
    # расшифрован ни один: 19 записей «не готово» на десять карточек.
    # Считаем и то и другое, чтобы не путать отсутствие звонка с отсутствием
    # расшифровки. До кэша: событий это не меняет, а цифра нужна и по
    # карточкам из кэша.
    envelope["transcripts"] = _transcript_counts(events)
    envelope["has_call"] = has_any_call(events)

    # Кэш читается и в DRY_RUN: чтение ничего не меняет, а без него тестовый
    # прогон заново гоняет модель по всем карточкам. Повторный разбор — по force.
    stored = get_client_state(deal_id)
    if stored and stored.get("content_hash") == content_hash and not force:
        envelope["skipped"] = True
        envelope["reason"] = "unchanged"
        cached = json.loads(stored.get("state_json") or "{}")
        # Факты берём из кэша, оценку считаем заново: этап успел постареть,
        # а правила могли поменяться с прошлого прогона.
        apply_derived_verdict(
            cached, record, profile, envelope, events, settings, task_authors,
        )
        # В БД состояние лежит замаскированным — разворачиваем, иначе отчёт
        # покажет КЛИЕНТ_1 вместо имени всюду, где карточка взята из кэша.
        envelope["state"] = unmask_state(cached, mask_map, profile)
        if not settings.dry_run:
            save_client_state(
                deal_id=deal_id,
                state_json=json.dumps(cached, ensure_ascii=False),
                confidence=float(cached.get("confidence") or 0.0),
                content_hash=content_hash,
                analyzed_at=str(stored.get("analyzed_at") or ""),
                model=str(stored.get("model") or ""),
                analyzed_events=str(stored.get("analyzed_events") or ""),
            )
        return envelope

    previous_state = None
    analyzed_at = None
    seen_fingerprints: set[str] = set()
    if stored:
        try:
            previous_state = json.loads(stored.get("state_json") or "{}")
        except json.JSONDecodeError:
            previous_state = None
        analyzed_at = str(stored.get("analyzed_at") or "")
        seen_fingerprints = parse_analyzed_events(stored.get("analyzed_events"))

    new_events = filter_new_events(events, analyzed_at, seen_fingerprints)
    if previous_state is None or force:
        # force — это «перечитай карточку целиком», а не «пропусти проверку
        # хэша»: иначе ручной перезапуск упирался в отпечатки событий и
        # выходил через no_new_events, ничего не перечитав.
        new_events = events
    elif not new_events:
        # Хэш карточки поменялся, а новых событий нет — значит событие удалили
        # из таймлайна. Спрашивать модель не о чем: прошлое состояние остаётся
        # верным. Перезаписываем хэш, чтобы следующий прогон не пришёл сюда же.
        envelope["skipped"] = True
        envelope["reason"] = "no_new_events"
        apply_derived_verdict(
            previous_state, record, profile, envelope, events, settings,
            task_authors,
        )
        envelope["state"] = unmask_state(previous_state, mask_map, profile)
        if not settings.dry_run:
            init_db()
            save_client_state(
                deal_id=deal_id,
                state_json=json.dumps(previous_state, ensure_ascii=False),
                confidence=float(previous_state.get("confidence") or 0.0),
                content_hash=content_hash,
                analyzed_at=str(stored.get("analyzed_at") or "") if stored else "",
                model=str(stored.get("model") or "") if stored else "",
                analyzed_events=json.dumps(
                    sorted({event_fingerprint(e) for e in events}),
                    ensure_ascii=False,
                ),
            )
        return envelope
    new_events, dropped = trim_events_to_budget(
        new_events, int(settings.client_state_max_event_chars),
    )
    if dropped:
        logger.info(
            "Deal %s: %d oldest events dropped to fit the context budget",
            deal_id,
            dropped,
        )

    if not events:
        # Читать нечего — ответ модели предрешён, а стоит столько же.
        logger.info("Deal %s: карточка пуста, разбор не требуется", deal_id)
        state = empty_card_state(profile)
        envelope["empty_card"] = True
    else:
        model = llm or make_llm(settings)
        usage: dict[str, int] = {key: 0 for key in USAGE_KEYS}
        envelope["usage"] = usage
        try:
            state = analyze_with_llm(
                record, previous_state, new_events, events, model, profile,
                usage_sink=usage, mask_map=mask_map,
            )
        except Exception as exc:  # noqa: BLE001 — одна карточка не роняет батч
            logger.warning("Client state LLM failed for deal %s: %s", deal_id, exc)
            envelope["skipped"] = True
            envelope["reason"] = "llm_error"
            return envelope

        if state is None:
            logger.warning("Client state parse failed for deal %s", deal_id)
            envelope["skipped"] = True
            envelope["reason"] = "parse_error"
            return envelope

    if previous_state and new_events is not events and len(new_events) < len(events):
        # Дозапрос: модель видела не всю карточку. Всё, что она не назвала,
        # добираем из прошлого разбора — и тут же отдаём на проверку цитат
        # ниже, по живому таймлайну.
        state["stage_facts"] = merge_stage_facts(
            state.get("stage_facts"), previous_state.get("stage_facts"),
        )

    verified, invented = verify_evidence(state.get("evidence", []), events)
    state["evidence"] = verified
    if invented:
        logger.warning(
            "Deal %s: %d quote(s) not found in the card — dropped as invented",
            deal_id,
            len(invented),
        )
    if not verified and events:
        # Ни одной подтверждённой цитаты — доверять такому выводу нельзя,
        # каким бы уверенным он ни выглядел. У пустой карточки цитат нет и
        # быть не может, и мы уже сказали об этом понятнее.
        state["confidence"] = min(float(state.get("confidence") or 0.0), 0.3)
        if "подтверждённые цитаты" not in state["missing"]:
            state["missing"].append("подтверждённые цитаты")
    envelope["evidence_dropped"] = len(invented)

    # Проверить цитаты stage_facts: любая невалидная цитата → factum отсутствует.
    # Если модель поставила present=True, но цитаты нет в карточке — это
    # выдумка, обнуляем факт вместо того чтобы засчитать его.
    corpus = _normalize_for_match(
        " ".join(str(e.get("text") or "") for e in events),
    )
    stage_facts = state.get("stage_facts") or {}
    unquoted = 0
    for key, value in stage_facts.items():
        if not isinstance(value, dict) or not value.get("present"):
            continue
        needle = _normalize_for_match(value.get("quote"))
        if not needle or needle not in corpus:
            unquoted += 1
            value["present"] = False
            value["quote"] = ""
    if unquoted:
        logger.warning(
            "Deal %s: %d stage_facts without a valid quote — marked absent",
            deal_id, unquoted,
        )
    envelope["stage_facts_dropped"] = unquoted

    apply_derived_verdict(
        state, record, profile, envelope, events, settings, task_authors,
    )

    # В БД уходит замаскированная копия: на следующем прогоне она вернётся
    # в модель как previous_state. Разворачиваем только то, что читают люди.
    envelope["state"] = unmask_state(state, mask_map, profile)

    if not settings.dry_run:
        init_db()
        now_iso = datetime.now(timezone.utc).isoformat()
        save_client_state(
            deal_id=deal_id,
            state_json=json.dumps(state, ensure_ascii=False),
            confidence=float(state.get("confidence") or 0.0),
            content_hash=content_hash,
            analyzed_at=now_iso,
            model=settings.llm_model,
            # Отпечатки всех событий карточки, а не только отправленных: то,
            # что не влезло в бюджет, уже не попадёт в модель — но и повторно
            # платить за него на каждом прогоне незачем.
            analyzed_events=json.dumps(
                sorted({event_fingerprint(e) for e in events}),
                ensure_ascii=False,
            ),
        )
    else:
        logger.info(
            "DRY_RUN: client state for deal %s — recoverable=%s confidence=%.2f",
            deal_id,
            state.get("recoverable"),
            float(state.get("confidence") or 0.0),
        )
    return envelope


def _transcript_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    """Расшифровки карточки: читается / ещё не готова / не загрузилась.

    «Не готова» и «не загрузилась» — разные вещи, и складывать их нельзя.
    Первое означает, что Битрикс ещё не расшифровал разговор, и ждать имеет
    смысл. Второе — что запрос не прошёл, и это уже наша сторона. Одна цифра
    на двоих превращала бы нашу ошибку в чужую задержку.
    """
    counts = {"ready": 0, "pending": 0, "failed": 0}
    for event in events:
        if event.get("kind") != "transcript":
            continue
        if str(event.get("text") or "").strip():
            counts["ready"] += 1
        elif str(event.get("status") or "") == STATUS_NOT_READY:
            counts["pending"] += 1
        else:
            counts["failed"] += 1
    return counts


def card_digest(result: dict[str, Any]) -> dict[str, Any]:
    """Карточка прогона в машиночитаемом виде: только коды и числа.

    Нужна, чтобы два прогона можно было сравнить построчно, а не глазами по
    двум простыням текста, и чтобы по файлу было видно, почему правило не
    сработало: модель не поставила флаг или цитата не нашлась в карточке.

    Текста здесь нет намеренно. Цель клиента, ситуация и цитаты — это имена,
    адреса и суммы; в отчёт они уходят по делу, а в файл, который потом
    таскают между прогонами и пересылают, попадать не должны.
    """
    state = result.get("state") or {}
    work = state.get("work_evidence") or {}
    raw = state.get("broker_work") or {}
    facts = state.get("stage_facts") or {}
    present = sum(
        1 for f in facts.values()
        if isinstance(f, dict) and f.get("present")
    )
    party = state.get("counterparty") if isinstance(
        state.get("counterparty"), dict
    ) else {}
    return {
        "deal_id": _coerce_int(result.get("deal_id")),
        "skipped_reason": _clean_str(result.get("reason")),
        "verdict": _clean_str(state.get("verdict")),
        "temperature": _clean_str(state.get("temperature")),
        "risk": _clean_str(state.get("risk")),
        "confidence": round(float(state.get("confidence") or 0.0), 2),
        "recoverable": state.get("recoverable"),
        "counterparty": _clean_str(party.get("who")),
        "work_reason": _clean_str(work.get("reason")),
        "work_proven": bool(work.get("proven")),
        "days_quiet": work.get("days_quiet"),
        "window_days": work.get("window_days"),
        "facts_present": present,
        "facts_needed": len(facts),
        "missing": len(state.get("missing") or []),
        "evidence": len(state.get("evidence") or []),
        # Что сказала модель и что от этого осталось после сверки цитат.
        "model_flags": {
            key: bool(raw.get(key))
            for key in (
                "claims_messaged", "claims_no_answer", "pause_explained",
            )
        },
        "verified_flags": dict(state.get("work_claims") or {}),
        "comment_informative": bool(raw.get("comment_informative", True)),
        "pause_until": _clean_str(raw.get("pause_until")),
        "has_next_action": bool(str(state.get("next_action") or "").strip()),
        "no_call": bool(state.get("no_call")),
        "transcripts": dict(result.get("transcripts") or {}),
        "has_call": bool(result.get("has_call")),
    }


def _is_agent_card(state: dict[str, Any]) -> bool:
    """По ту сторону карточки агент, а не клиент."""
    party = state.get("counterparty")
    return isinstance(party, dict) and str(party.get("who") or "") == "agent"


def run_client_state(
    profile: FunnelProfile = BUYER_PROFILE,
    deals: list[dict[str, Any]] | None = None,
    *,
    settings: Settings | None = None,
    llm: LLMClient | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Batch runner for one funnel. Reads its open deals when list is omitted."""
    settings = settings or get_settings()
    # Справочник типов контакта — один запрос на прогон: без него код
    # «UC_2G0TD3» ничего не говорит, а угадывать, какой из них агент, нельзя.
    set_contact_type_names(fetch_contact_type_names())
    category_id = (
        settings.sellers_category_id
        if profile.key == "sellers"
        else settings.buyers_category_id
    )
    if deals is None:
        raw = _bx_get_all_sync(
            "crm.deal.list",
            {
                "filter": {
                    "CATEGORY_ID": category_id,
                    "CLOSED": "N",
                },
                "select": [
                    "ID", "TITLE", "STAGE_ID", "ASSIGNED_BY_ID", "CONTACT_ID",
                    # Без них _stage_hours возвращает None, отсрочка не
                    # применяется, и свежая карточка судится как застоявшаяся.
                    *STAGE_ENTRY_SELECT,
                    *SOURCE_SELECT,
                    # UF-поля, которые нужны прямой проверке качества
                    *[code for code, _name in profile.qualification_fields],
                ],
            },
        )
        deals = [d for d in _as_list(raw) if isinstance(d, dict)]

    stats: dict[str, Any] = {
        "funnel": profile.key,
        "funnel_label": profile.label,
        "total": len(deals),
        "analyzed": 0,
        "skipped_unchanged": 0,
        "skipped_incomplete": 0,
        "skipped_out_of_qc": 0,
        "skipped_other": 0,
        "errors": 0,
        "unrecoverable": 0,
        "agent_cards": 0,
        "cards_without_a_call": 0,
        "evidence_dropped": 0,
        # Карточек, по которым разговор вообще можно прочитать. Без этого
        # «расхождений 0» неотличимо от «сравнивать было не с чем».
        "cards_with_call": 0,
        # Карточек, таймлайн которых прогон вообще прочитал. Знаменатель для
        # «звонки есть у N из M»: непрочитанная карточка звонка дать не может.
        "cards_read": 0,
        "cards_with_transcript": 0,
        "transcripts_pending": 0,
        "transcripts_failed": 0,
        "usage": {key: 0 for key in USAGE_KEYS},
        "llm_calls": 0,
        "cost_rub": 0.0,
        "cost_rub_per_card": 0.0,
        # Сколько карточек судилось без известного возраста этапа. Больше
        # нуля — отсрочка не работает и вердикты завышены в сторону «плохо».
        "stage_age_unknown": 0,
        # Пустые карточки: разобраны без модели, потому что читать нечего.
        "empty_cards": 0,
        # Неинформативные карточки, по которым мы сами отказались судить:
        # лид, заведённый два часа назад, пуст не по вине брокера. Без этой
        # цифры «неинформативных 8 из 10» на выборке свежих лидов читается
        # как претензия к людям, хотя работать по ним ещё не начинали.
        "unrecoverable_too_early": 0,
        # Состав выборки по этапам. Три прогона подряд дали неинформативный
        # итог из-за перекоса выборки, и каждый раз это приходилось
        # раскапывать. Пусть перекос будет виден сразу.
        "stages": {},
        # Состав выборки по источникам: РОПу важно, из какого канала пришли
        # карточки, которые не отработали.
        "sources": {},
        "temperature": {"hot": 0, "warm": 0, "cold": 0, "unknown": 0},
        "verdicts": {
            "good": 0, "tolerable": 0, "poor": 0,
            "too_early": 0, "out_of_qc": 0, "no_rules": 0,
        },
        # Этапы, по которым правила полноты ещё не написаны: {этап: сколько}.
        "stages_without_rules": {},
        "results": [],
    }
    # Карты авторов строятся один раз на всю выборку: два REST-запроса на
    # прогон против двух на карточку. Сбой не роняет прогон — множество
    # авторов останется пустым, и правило «дело брокера или его РОПа»
    # просто не применится, а не заработает наоборот.
    try:
        from tools import _build_broker_dept_map, _build_rop_map

        rop_map = _build_rop_map()
        broker_ids = {
            _coerce_int(d.get("ASSIGNED_BY_ID") or d.get("assigned_by_id"))
            for d in deals
        }
        broker_dept_map = _build_broker_dept_map({b for b in broker_ids if b})
    except Exception as exc:  # noqa: BLE001 — прогон важнее одного правила
        logger.warning(
            "Не удалось построить карту авторов дел (%s): "
            "дела будут засчитываться независимо от того, кто их завёл",
            exc,
        )
        rop_map, broker_dept_map = {}, {}

    for deal in deals:
        stage_code = _clean_str(deal.get("STAGE_ID") or deal.get("stage_id"))
        stats["stages"][stage_code or "(без этапа)"] = (
            stats["stages"].get(stage_code or "(без этапа)", 0) + 1
        )
        try:
            result = analyze_deal(
                deal, profile=profile, settings=settings, llm=llm, force=force,
                rop_map=rop_map, broker_dept_map=broker_dept_map,
            )
        except Exception as exc:  # noqa: BLE001 — одна карточка не роняет батч
            logger.warning(
                "Client state failed for deal %s: %s",
                _coerce_int(deal.get("ID") or deal.get("id")),
                exc,
            )
            result = {
                "deal_id": _coerce_int(deal.get("ID") or deal.get("id")),
                "skipped": True,
                "reason": "unexpected_error",
                "state": None,
                "content_hash": "",
            }
        source_code = _clean_str(deal.get("SOURCE_ID") or deal.get("source_id"))
        source_key = source_code or "(без источника)"
        stats["sources"][source_key] = stats["sources"].get(source_key, 0) + 1
        stats["results"].append(result)
        # Считаем и по карточкам из кэша: расшифровка от разбора не зависит.
        counts = result.get("transcripts") or {}
        # Ключ появляется только после успешного чтения карточки — по его
        # наличию и считаем прочитанные, а не по значению: False здесь
        # значит «звонков нет», а не «не читали».
        if "has_call" in result:
            stats["cards_read"] += 1
        if result.get("has_call"):
            stats["cards_with_call"] += 1
        if int(counts.get("ready") or 0) > 0:
            stats["cards_with_transcript"] += 1
        stats["transcripts_pending"] += int(counts.get("pending") or 0)
        stats["transcripts_failed"] += int(counts.get("failed") or 0)
        # Токены считаем и по упавшим карточкам: запрос к модели уже оплачен,
        # даже если ответ не разобрался.
        call_usage = result.get("usage")
        if isinstance(call_usage, dict):
            stats["llm_calls"] += 1
            for key in USAGE_KEYS:
                stats["usage"][key] += int(call_usage.get(key) or 0)
        reason = result.get("reason") or ""
        if result.get("skipped"):
            if reason == "unchanged":
                stats["skipped_unchanged"] += 1
            elif reason == "evidence_incomplete":
                stats["skipped_incomplete"] += 1
            elif reason == "no_new_events":
                stats["skipped_unchanged"] += 1
            elif reason == "stage_out_of_qc":
                stats["skipped_out_of_qc"] += 1
                stats["verdicts"]["out_of_qc"] += 1
            elif reason in {
                "llm_error", "parse_error", "collect_error", "unexpected_error",
            }:
                stats["errors"] += 1
            else:
                stats["skipped_other"] += 1
            if reason in {"unchanged", "no_new_events"}:
                # Карточка из кэша — состояние по ней известно и попадает в
                # отчёт, значит должна попадать и в сводку. Иначе шапка
                # покажет 4 тёплых из 20, пока в теле их пятнадцать.
                cached = result.get("state") or {}
                cached_level = str(cached.get("temperature") or "unknown")
                if cached_level in stats["temperature"]:
                    stats["temperature"][cached_level] += 1
                cached_verdict = str(cached.get("verdict") or "")
                if cached_verdict in stats["verdicts"]:
                    stats["verdicts"][cached_verdict] += 1
                if cached.get("recoverable") is False:
                    stats["unrecoverable"] += 1
                    if cached_verdict == "too_early":
                        stats["unrecoverable_too_early"] += 1
                if _is_agent_card(cached):
                    stats["agent_cards"] += 1
                if cached.get("no_call"):
                    stats["cards_without_a_call"] += 1
            continue
        stats["analyzed"] += 1
        if result.get("empty_card"):
            stats["empty_cards"] += 1
        if result.get("stage_age_known") is False:
            stats["stage_age_unknown"] += 1
        stats["evidence_dropped"] += int(result.get("evidence_dropped") or 0)
        state = result.get("state") or {}
        level = str(state.get("temperature") or "unknown")
        if level in stats["temperature"]:
            stats["temperature"][level] += 1
        verdict = str(state.get("verdict") or "out_of_qc")
        if verdict in stats["verdicts"]:
            stats["verdicts"][verdict] += 1
        if verdict == "no_rules":
            stats["stages_without_rules"][stage_code] = (
                stats["stages_without_rules"].get(stage_code, 0) + 1
            )
        if state.get("recoverable") is False:
            stats["unrecoverable"] += 1
            if verdict == "too_early":
                stats["unrecoverable_too_early"] += 1
        if _is_agent_card(state):
            stats["agent_cards"] += 1
        if state.get("no_call"):
            stats["cards_without_a_call"] += 1
    usage = stats["usage"]
    # Делим неокруглённую сумму: цена за карточку — это копейки, и округление
    # до рублей перед делением её заметно искажает.
    cost = estimate_cost(usage, settings)
    stats["cost_rub"] = round(cost, 2)
    stats["cost_rub_per_card"] = (
        round(cost / stats["llm_calls"], 4) if stats["llm_calls"] else 0.0
    )
    logger.info(
        "Client state [%s]: %d cards, %d LLM calls, %.2f ₽ (%.3f ₽/card) · "
        "in %d (%d from cache, %.0f%%) · out %d (%d reasoning, %.0f%%)",
        profile.key,
        stats["total"],
        stats["llm_calls"],
        stats["cost_rub"],
        stats["cost_rub_per_card"],
        usage["input_tokens"],
        usage["cached_tokens"],
        100.0 * usage["cached_tokens"] / usage["input_tokens"]
        if usage["input_tokens"] else 0.0,
        usage["output_tokens"],
        usage["reasoning_tokens"],
        100.0 * usage["reasoning_tokens"] / usage["output_tokens"]
        if usage["output_tokens"] else 0.0,
    )
    return stats


def run_buyer_client_state(
    deals: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Backwards-compatible entry point for the buyers funnel."""
    return run_client_state(BUYER_PROFILE, deals, **kwargs)


def run_seller_client_state(
    deals: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Entry point for the sellers funnel."""
    return run_client_state(SELLER_PROFILE, deals, **kwargs)


def analyze_buyer_deal(deal: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Backwards-compatible wrapper used by existing callers and tests."""
    kwargs.setdefault("profile", BUYER_PROFILE)
    return analyze_deal(deal, **kwargs)
