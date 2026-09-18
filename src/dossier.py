"""Досье по сделкам: вся история работы с клиентом одной строкой JSONL.

ЗАЧЕМ. Аудитор читает комментарии и шесть полей карточки. Этого хватает,
чтобы поймать нарушение регламента, и не хватает, чтобы ответить на вопрос
«работает ли брокер». Три вещи он не видит вовсе:

* **кто автор записи.** Работу по карточке часто пишет не ответственный, а
  третий человек — РОП, колл-центр, коллега. «По сделке семь комментариев»
  и «по сделке семь комментариев, и ни один не написал брокер» — это два
  разных отчёта, и второй в портале ничем не отличается от первого;
* **массовые переносы стадий.** Шесть сделок шести брокеров, переведённые в
  одну минуту, — это не движение воронки, это подтягивание отчётности.
  Увидеть это можно только рядом, по всему портфелю сразу;
* **содержание звонков.** Половина работы происходит голосом и в
  комментарии не попадает вовсе.

Модуль ничего не оценивает. Он выгружает факты, а оценку делает внешняя
модель, читая JSONL. Поэтому здесь нет ни одного правила аудита: правила
живут в tools.py и меняются отдельно от формата выгрузки.

ЧТО СЧИТАЕТСЯ ЗДЕСЬ, А НЕ У ЧИТАТЕЛЯ. `author_is_assignee` вычисляется при
сборе. Признак главный, и вычислять его на стороне читателя значит
требовать от каждого читателя одинаково правильно сравнить два
идентификатора — а он один раз ошибётся, и весь отчёт перевернётся.

РАСШИФРОВКИ ЗВОНКОВ. Метода запуска расшифровки в REST нет, есть только
чтение. Поэтому модуль делает две вещи: забирает готовые тексты и
формирует очередь звонков, по которым расшифровку стоит запустить руками.
Статус расшифровки отдаётся отдельным полем, и у него есть значение
`queued` — ради одного правила, записанного в README выгрузки: карточка, у
которой на активной стадии висит незабранный разговор, получает «данных
недостаточно», а не «работа не ведётся». Был случай, когда после появления
текста оценка карточки поменялась на противоположную.

Запуск:
    python src/dossier.py --mode delta
    python src/dossier.py --mode full --include-closed
    python -m src.dossier --mode delta --limit 20
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from config import get_settings, setup_logging  # noqa: E402
from db import (  # noqa: E402
    close_transcript_launch, get_call_transcript, get_transcript_launches,
    init_db, record_transcript_launch, upsert_call_transcript,
)
from tools import (  # noqa: E402
    _as_list, _bx_get_all_sync, _clean_str, _coerce_float, _coerce_int,
    _fetch_deal_activities, _fetch_entity_timeline, _fetch_funnel_stage_names,
    _fetch_stage_history_rows, _parse_datetime, MAX_TIMELINE_WORKERS,
)
from transcripts import (  # noqa: E402
    CALL_ACTIVITY_TYPE_ID, STATUS_OK, fetch_transcript_from_api,
)

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# словарь состояний расшифровки
# --------------------------------------------------------------------------

# Для читающей модели значима одна граница: текст видели или нет. Всё, что
# по эту сторону границы, различается только затем, чтобы мы сами видели,
# почему текста нет, и не путали «его не будет» с «мы не дошли».
T_OK = "ok"              # текст есть
T_ABSENT = "absent"      # портал ответил, расшифровки нет
T_QUEUED = "queued"      # поставлен в очередь на запуск, ждём
T_DEFERRED = "deferred"  # не забирали: бюджет или сбой чтения
T_FAILED = "failed"      # две постановки без результата либо ошибка запуска
# Шестое значение сверх согласованных пяти, и вот почему оно обязано быть.
# Короткие звонки мы не спрашиваем НИКОГДА — это политика, а не нехватка
# бюджета. Положить их в deferred значит выдать «данных недостаточно» по
# 1197 звонкам из 1645 (ровно ноль секунд длятся 489), и правило README,
# ради которого затевался queued, перестанет что-либо значить.
T_TOO_SHORT = "too_short"

# Ниже этого порога разговора не было. Замер по всему портфелю (18.09,
# 1645 звонков): 489 длятся ровно 0 секунд, ещё 486 — меньше тридцати,
# 222 — меньше минуты. Медиана 18 секунд. Отсечка снимает 1197 обращений
# за расшифровками из 1645 — 73 % — ДО первого запроса, и стоит она ноль:
# обе границы приходят вместе со списком дел.
LONG_CALL_SEC = 60

# Постановок в очередь на звонок — не больше двух. Если после второй текст
# не появился, дело не в очереди. Третий взгляд фиксирует failed.
MAX_LAUNCH_ATTEMPTS = 2
# Повторно тот же звонок — не раньше чем через сутки: запуск асинхронный, и
# текст появляется позже. Ставить его в очередь каждый прогон значит гонять
# человека по одному и тому же списку.
REQUEUE_AFTER_HOURS = 24

# --------------------------------------------------------------------------
# отбор
# --------------------------------------------------------------------------

# Стадии, где торг уже кончился: задаток внесён, сделка закрыта, карточка
# проиграна. Для очереди на расшифровку и для правила «молчит неделю» они не
# годятся — там нечего ускорять. Оба словаря стадий в одном множестве: у
# покупателей идентификаторы с префиксом воронки, у продавцов без него.
INACTIVE_STAGE_IDS: frozenset[str] = frozenset({
    "C18:UC_RUCRAH",   # Покупатели: Задаток
    "C18:UC_8Z3SP6",   # Покупатели: Офер
    "C18:UC_8X12HI",   # Покупатели: Сделка
    "C18:APOLOGY",     # Покупатели: Сделка проиграна
    "C18:WON",         # Покупатели: Договор закрыт
    "WON",             # Продавцы: Договор закрыт
    "APOLOGY",         # Продавцы: Сделка проиграна
})

# Окно «что-то происходило» для режима delta.
DELTA_WINDOW = timedelta(hours=24)
# Сколько дней тишины на активной стадии делают карточку интересной. Тот же
# порог, что у аудита: семь дней — это неделя, за которую по живой сделке
# обязано было случиться хоть что-нибудь.
DELTA_SILENT_DAYS = 7

# Приоритеты очереди на расшифровку.
PRIORITY_ASSIGNEE_SILENT = 1  # брокер не написал ни одного комментария
PRIORITY_RECENT_STAGE = 2     # стадия менялась за последнюю неделю
PRIORITY_REST = 3
RECENT_STAGE_DAYS = 7

# Заголовок пользовательского поля, в котором лежит идентификатор объекта
# Афины. Ищем по нему, а не по коду: коды UF_CRM_* портальные и меняются при
# пересоздании поля.
AFINA_FIELD_TITLE = "ID объекта Афины"
FIELD_CACHE_NAME = "dossier_fields.json"

DIRECTION_IN = 1
DIRECTION_OUT = 2


# --------------------------------------------------------------------------
# время
# --------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> str:
    """Отметка времени как ISO-строка; пусто, если не разобралась."""
    parsed = _parse_datetime(value)
    return parsed.isoformat() if parsed else ""


def _days_since(value: Any, now: datetime) -> float | None:
    """Сколько суток прошло. None — отметки нет, и это не ноль.

    Округление до сотых не ради красоты: строку читает модель, и семнадцать
    знаков после запятой — это семнадцать знаков, которые она обязана
    прочитать, чтобы понять «девять дней». Точнее сотых здесь не нужно
    никому: пороги отбора измеряются днями.
    """
    parsed = _parse_datetime(value)
    if parsed is None:
        return None
    days = (now - parsed.astimezone(timezone.utc)).total_seconds() / 86400.0
    return round(max(days, 0.0), 2)


# --------------------------------------------------------------------------
# справочники
# --------------------------------------------------------------------------

def load_users() -> dict[int, str]:
    """Идентификатор сотрудника → ФИО. Один запрос на прогон.

    Уволенные тоже нужны: карточку мог вести человек, которого в компании
    уже нет, и подписать его комментарий номером вместо фамилии значит
    потерять ровно ту строку, ради которой автора и вытаскивали.
    """
    try:
        raw = _bx_get_all_sync("user.get", {})
    except Exception:
        logger.warning("user.get не ответил — имена в выгрузке будут пустыми")
        return {}
    names: dict[int, str] = {}
    for user in _as_list(raw):
        user_id = _coerce_int(user.get("ID"))
        if user_id <= 0:
            continue
        parts = [
            _clean_str(user.get("LAST_NAME")).strip(),
            _clean_str(user.get("NAME")).strip(),
        ]
        names[user_id] = " ".join(p for p in parts if p) or f"ID:{user_id}"
    return names


def _field_cache_path() -> Path:
    return Path(get_settings().dossier_dir) / FIELD_CACHE_NAME


def afina_field_code() -> str:
    """Код пользовательского поля «ID объекта Афины».

    Ищем по заголовку через crm.deal.fields и кладём в кэш-файл. Если портал
    не ответил, берём код из tools.py: одна сетевая осечка не должна молча
    обнулить поле во всей выгрузке — это выглядело бы как «Афины нет ни у
    одной сделки», а такой вывод неотличим от правды.
    """
    cache = _field_cache_path()
    try:
        stored = json.loads(cache.read_text(encoding="utf-8"))
        code = str(stored.get("afina_field") or "")
        if code:
            return code
    except Exception:
        pass

    code = ""
    try:
        raw = _bx_get_all_sync("crm.deal.fields", {})
        fields = raw if isinstance(raw, dict) else {}
        for field_code, meta in fields.items():
            if not isinstance(meta, dict):
                continue
            title = _clean_str(meta.get("formLabel") or meta.get("title")).strip()
            if title == AFINA_FIELD_TITLE:
                code = str(field_code)
                break
    except Exception:
        logger.warning("crm.deal.fields не ответил — код поля Афины берём из tools")

    if not code:
        from tools import SELLERS_AFINA_UF
        code = SELLERS_AFINA_UF
        logger.info("Код поля Афины взят запасным путём: %s", code)
    else:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(
                json.dumps({"afina_field": code}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            logger.warning("Не удалось записать кэш кодов полей")
    return code


# --------------------------------------------------------------------------
# сбор
# --------------------------------------------------------------------------

def list_deals(
    category_id: int,
    afina_code: str,
    *,
    include_closed: bool = False,
) -> list[dict[str, Any]]:
    """Сделки воронки. По умолчанию только незакрытые."""
    deal_filter: dict[str, Any] = {"CATEGORY_ID": category_id}
    if not include_closed:
        deal_filter["CLOSED"] = "N"
    select = [
        "ID", "TITLE", "CATEGORY_ID", "STAGE_ID", "ASSIGNED_BY_ID",
        "DATE_CREATE", "DATE_MODIFY", "OPPORTUNITY", "SOURCE_ID", "CLOSED",
    ]
    if afina_code:
        select.append(afina_code)
    raw = _bx_get_all_sync(
        "crm.deal.list", {"filter": deal_filter, "select": select},
    )
    return [d for d in _as_list(raw) if isinstance(d, dict)]


def list_assignees(category_ids: list[int]) -> dict[int, int]:
    """Весь портфель в двух полях: сделка → ответственный.

    Отдельным дешёвым запросом, а не из выгрузки. Журнал переназначений
    обязан сравнивать ВЕСЬ портфель: в режиме delta выгружается сотня
    карточек, и если снимок обновлять по ним, на следующем прогоне тысяча
    несобранных карточек отрапортует смену ответственного, которой не было.
    """
    out: dict[int, int] = {}
    for category_id in category_ids:
        try:
            raw = _bx_get_all_sync(
                "crm.deal.list",
                {
                    "filter": {"CATEGORY_ID": category_id, "CLOSED": "N"},
                    "select": ["ID", "ASSIGNED_BY_ID"],
                },
            )
        except Exception:
            logger.warning(
                "Не прочитан список ответственных воронки %s — "
                "журнал переназначений за этот прогон неполон", category_id,
            )
            continue
        for deal in _as_list(raw):
            deal_id = _coerce_int(deal.get("ID"))
            if deal_id > 0:
                out[deal_id] = _coerce_int(deal.get("ASSIGNED_BY_ID"))
    return out


def fetch_timelines(
    deal_ids: list[int],
) -> tuple[dict[int, list[dict[str, Any]]], set[int]]:
    """Комментарии по сделкам. Возвращает (комментарии, сделки с ошибкой).

    Пустой таймлайн и непрочитанный таймлайн обязаны остаться различимыми:
    «комментариев нет» — это вывод о работе брокера, «не смогли прочитать» —
    вывод о нашей сети, и подменять один другим нельзя.
    """
    comments: dict[int, list[dict[str, Any]]] = {}
    failed: set[int] = set()
    with ThreadPoolExecutor(max_workers=MAX_TIMELINE_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_entity_timeline, did, "deal"): did
            for did in deal_ids
        }
        for future in as_completed(futures):
            deal_id = futures[future]
            try:
                _, timeline, broken = future.result()
                comments[deal_id] = timeline
                if broken:
                    failed.add(deal_id)
            except Exception:
                logger.warning("Комментарии сделки %s не прочитаны", deal_id)
                comments[deal_id] = []
                failed.add(deal_id)
    return comments, failed


def fetch_activities(
    deal_ids: list[int],
) -> tuple[dict[int, list[dict[str, Any]]], set[int]]:
    """Дела и звонки по сделкам. Возвращает (дела, сделки с ошибкой)."""
    activities: dict[int, list[dict[str, Any]]] = {}
    failed: set[int] = set()
    with ThreadPoolExecutor(max_workers=MAX_TIMELINE_WORKERS) as pool:
        futures = {pool.submit(_fetch_deal_activities, did): did for did in deal_ids}
        for future in as_completed(futures):
            deal_id = futures[future]
            try:
                _, rows, broken = future.result()
                activities[deal_id] = rows
                if broken:
                    failed.add(deal_id)
            except Exception:
                logger.warning("Дела сделки %s не прочитаны", deal_id)
                activities[deal_id] = []
                failed.add(deal_id)
    return activities, failed


def stage_history(deal_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """История стадий по сделкам, отсортированная по времени.

    crm.stagehistory.list отдаёт только стадию, В которую перешли. Откуда
    ушли — выводится сопоставлением соседних строк, и только внутри одной
    воронки: переход из общей базы в рабочую воронку не движение по этапам,
    а другое событие, и пара «чужая стадия → своя» смысла не имеет.
    """
    rows = _fetch_stage_history_rows(deal_ids)
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        deal_id = _coerce_int(row.get("OWNER_ID"))
        if deal_id > 0:
            grouped.setdefault(deal_id, []).append(row)
    for deal_id, items in grouped.items():
        items.sort(key=lambda r: _iso(r.get("CREATED_TIME")))
    return grouped


def build_stage_history(
    rows: list[dict[str, Any]],
    stage_names: dict[str, str],
) -> list[dict[str, Any]]:
    """Переходы «откуда → куда» из строк истории стадий."""
    out: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    for row in rows:
        stage_to = _clean_str(row.get("STAGE_ID"))
        stage_from = _clean_str(previous.get("STAGE_ID")) if previous else ""
        out.append({
            "date": _iso(row.get("CREATED_TIME")),
            "stage_from": stage_from,
            "stage_to": stage_to,
            "stage_from_name": stage_names.get(stage_from, stage_from),
            "stage_to_name": stage_names.get(stage_to, stage_to),
        })
        previous = row
    return out


# --------------------------------------------------------------------------
# расшифровки и очередь на запуск
# --------------------------------------------------------------------------

def call_duration_sec(activity: dict[str, Any]) -> int:
    """Длительность звонка из END_TIME − START_TIME.

    CALL_DURATION живёт в статистике телефонии, а она на этом портале не
    наполняется: 222 записи против 3555 звонков в crm.activity.list, потому
    что звонки регистрирует внешняя АТС через REST-приложение. Разность
    границ — единственный доступный источник, и приходит он бесплатно.

    Нет одной из границ — считаем нулём. Неизвестную длительность нельзя
    записывать в длинные: очередь на расшифровку наберётся из пустых.
    """
    start = _parse_datetime(activity.get("START_TIME"))
    end = _parse_datetime(activity.get("END_TIME"))
    if start is None or end is None:
        return 0
    seconds = int((end - start).total_seconds())
    return seconds if seconds > 0 else 0


class TranscriptResolver:
    """Забирает тексты разговоров, считая бюджет на весь прогон.

    Порядок проверок задан ценой: длительность (бесплатно) → кэш (диск) →
    портал (запрос). Менять его местами значит платить за то, что уже
    известно.
    """

    def __init__(self, budget: int) -> None:
        self.budget = budget
        self.fetched = 0
        self.absent = 0
        self.deferred_budget = 0
        self.deferred_error = 0
        self.too_short = 0

    def resolve(
        self,
        activity: dict[str, Any],
        deal_id: int,
        launch: dict[str, Any] | None,
    ) -> tuple[str, str]:
        """Вернуть (статус, текст) по одному звонку."""
        activity_id = _coerce_int(activity.get("ID"))
        if activity_id <= 0:
            return T_ABSENT, ""

        if call_duration_sec(activity) < LONG_CALL_SEC:
            self.too_short += 1
            return T_TOO_SHORT, ""

        cached = get_call_transcript(activity_id)
        if cached and cached.get("status") == STATUS_OK and cached.get("text"):
            return T_OK, _clean_str(cached.get("text"))

        if self.fetched >= self.budget:
            self.deferred_budget += 1
            return T_DEFERRED, ""

        text, status = fetch_transcript_from_api(activity_id)
        self.fetched += 1
        upsert_call_transcript(
            activity_id=activity_id,
            deal_id=deal_id,
            text=text or "",
            status=status,
            fetched_at=_now().isoformat(),
            chars=len(text or ""),
            activity_created=_clean_str(activity.get("CREATED")),
        )
        if status == STATUS_OK and text:
            return T_OK, _clean_str(text)
        if status != STATUS_OK and status != "not_ready":
            # Ошибка чтения, а не ответ «текста нет». Текст, возможно, есть —
            # мы его не забрали, и это ближе к deferred, чем к absent.
            self.deferred_error += 1
            return T_DEFERRED, ""

        # Портал ответил, текста нет. Дальше слово за очередью запуска.
        if launch and launch.get("outcome") == T_FAILED:
            return T_FAILED, ""
        if launch and int(launch.get("attempts") or 0) > 0:
            return T_QUEUED, ""
        self.absent += 1
        return T_ABSENT, ""


def queue_priority(row: dict[str, Any], now: datetime) -> int:
    """Чем карточка интереснее, тем раньше её разговор стоит расшифровать."""
    if row["counters"]["comments_by_assignee"] == 0:
        return PRIORITY_ASSIGNEE_SILENT
    for move in row["stage_history"]:
        days = _days_since(move.get("date"), now)
        if days is not None and days <= RECENT_STAGE_DAYS:
            return PRIORITY_RECENT_STAGE
    return PRIORITY_REST


def collect_queue(
    rows: list[dict[str, Any]],
    launches: dict[int, dict[str, Any]],
    budget: int,
    now: datetime,
) -> list[dict[str, Any]]:
    """Отобрать звонки, по которым стоит запустить расшифровку руками.

    Отбор нарочно узкий. Очередь отстреливает человек, и список на три
    тысячи строк — это список, который не будет отработан вовсе. Лучше сто
    звонков по карточкам, где ответ меняет вывод.
    """
    candidates: list[dict[str, Any]] = []
    for row in rows:
        if row["stage_id"] in INACTIVE_STAGE_IDS:
            continue
        priority = queue_priority(row, now)
        for call in row["calls"]:
            if call["transcript_status"] != T_ABSENT:
                continue
            if call["duration"] < LONG_CALL_SEC:
                continue
            activity_id = call["activity_id"]
            launch = launches.get(activity_id)
            if launch:
                attempts = int(launch.get("attempts") or 0)
                if attempts >= MAX_LAUNCH_ATTEMPTS or launch.get("outcome"):
                    continue
                since = _days_since(launch.get("last_queued_at"), now)
                if since is not None and since * 24 < REQUEUE_AFTER_HOURS:
                    continue
            candidates.append({
                "activity_id": activity_id,
                "deal_id": row["id"],
                "duration": call["duration"],
                "call_date": call["date"],
                "priority": priority,
                "attempts": int(launch.get("attempts") or 0) if launch else 0,
            })

    candidates.sort(key=lambda c: (c["priority"], -c["duration"]))
    return candidates[:budget] if budget > 0 else candidates


def launch_transcription(activity_id: int, deal_id: int) -> None:
    """Точка расширения: запуск расшифровки со стороны сервера.

    Сейчас не реализовано намеренно. В REST метода запуска нет — есть только
    чтение (crm.activity.call.getTranscript). Запуск делается тем же
    обращением, каким его делает сама карточка таймлайна:

        POST /bitrix/services/main/ajax.php
             ?action=crm.timeline.ai.launchCopilot
        sessid=<BX.bitrix_sessid()>
        data[activityId]=<id дела-звонка>
        data[ownerTypeId]=2
        data[ownerId]=<id сделки>
        data[scenario]=transcribe_record

    Эндпоинт живой и параметры принимает, но обращение к нему требует
    сессии пользователя портала, а не вебхука. Пока очередь отстреливается
    руками из браузера; серверный запуск — отдельная задача, и до неё эта
    функция остаётся местом, куда он встанет, а параметры — здесь, чтобы
    их не пришлось искать заново.
    """
    raise NotImplementedError(
        "Запуск расшифровки делается вручную: см. очередь queue_*.json",
    )


def settle_launches(
    rows: list[dict[str, Any]],
    launches: dict[int, dict[str, Any]],
) -> int:
    """Закрыть очередь там, где текст появился или ждать больше нечего.

    Возвращает число звонков, признанных безнадёжными на этом прогоне.
    """
    failed = 0
    for row in rows:
        for call in row["calls"]:
            activity_id = call["activity_id"]
            launch = launches.get(activity_id)
            if not launch or launch.get("outcome"):
                continue
            if call["transcript_status"] == T_OK:
                close_transcript_launch(activity_id, T_OK, "текст появился")
                continue
            if int(launch.get("attempts") or 0) >= MAX_LAUNCH_ATTEMPTS:
                close_transcript_launch(
                    activity_id, T_FAILED, "две постановки без результата",
                )
                call["transcript_status"] = T_FAILED
                failed += 1
    return failed


# --------------------------------------------------------------------------
# сборка строки
# --------------------------------------------------------------------------

def _comment_rows(
    raw: list[dict[str, Any]],
    assignee_id: int,
    users: dict[int, str],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in raw:
        author_id = _coerce_int(item.get("AUTHOR_ID"))
        out.append({
            "created": _iso(item.get("CREATED")),
            "author_id": author_id,
            "author_name": users.get(author_id, f"ID:{author_id}" if author_id else ""),
            # Главный признак выгрузки, и считается он здесь. У читателя нет
            # ни одного повода вычислять его заново, а повод ошибиться есть.
            "author_is_assignee": author_id == assignee_id and author_id > 0,
            "text": _clean_str(item.get("COMMENT")),
        })
    out.sort(key=lambda c: c["created"])
    return out


def _activity_rows(
    raw: list[dict[str, Any]],
    users: dict[int, str],
) -> list[dict[str, Any]]:
    """Дела карточки без звонков: звонки идут отдельным списком."""
    out: list[dict[str, Any]] = []
    for item in raw:
        if _coerce_int(item.get("TYPE_ID")) == CALL_ACTIVITY_TYPE_ID:
            continue
        author_id = _coerce_int(item.get("AUTHOR_ID"))
        responsible_id = _coerce_int(item.get("RESPONSIBLE_ID"))
        out.append({
            "type": "task",
            "subject": _clean_str(item.get("SUBJECT")),
            # Название отвечает «что», описание — «о чём договорились».
            "description": _clean_str(item.get("DESCRIPTION")),
            "created": _iso(item.get("CREATED")),
            "deadline": _iso(item.get("DEADLINE") or item.get("END_TIME")),
            "completed": _clean_str(item.get("COMPLETED")).upper() == "Y",
            "author_id": author_id,
            "author_name": users.get(author_id, ""),
            "responsible_id": responsible_id,
            "responsible_name": users.get(responsible_id, ""),
        })
    out.sort(key=lambda a: a["created"])
    return out


def _call_rows(
    raw: list[dict[str, Any]],
    deal_id: int,
    resolver: TranscriptResolver,
    launches: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in raw:
        if _coerce_int(item.get("TYPE_ID")) != CALL_ACTIVITY_TYPE_ID:
            continue
        activity_id = _coerce_int(item.get("ID"))
        direction = _coerce_int(item.get("DIRECTION"))
        status, text = resolver.resolve(item, deal_id, launches.get(activity_id))
        out.append({
            "activity_id": activity_id,
            "direction": "in" if direction == DIRECTION_IN
            else "out" if direction == DIRECTION_OUT else "unknown",
            "duration": call_duration_sec(item),
            "date": _iso(item.get("CREATED")),
            "has_transcript": status == T_OK,
            "transcript_status": status,
            "text": text,
        })
    out.sort(key=lambda c: c["date"])
    return out


def _last_work_moment(
    comments: list[dict[str, Any]],
    activities: list[dict[str, Any]],
    calls: list[dict[str, Any]],
) -> str:
    """Самый свежий след РАБОТЫ по карточке.

    Перенос стадии сюда не входит. Это один клик, и засчитать его работой
    значит отдать брокеру способ обнулять счётчик молчания, ничего не
    сделав, — а отбор в delta как раз и держится на этом счётчике.
    """
    moments = [c["created"] for c in comments]
    moments += [a["created"] for a in activities]
    moments += [c["date"] for c in calls]
    moments = [m for m in moments if m]
    return max(moments) if moments else ""


def _days_in_stage(
    history: list[dict[str, Any]],
    stage_id: str,
    date_create: str,
    now: datetime,
) -> float | None:
    """Сколько дней карточка стоит на текущей стадии."""
    entered = ""
    for move in history:
        if move["stage_to"] == stage_id and move["date"]:
            entered = move["date"]
    return _days_since(entered or date_create, now)


def build_row(
    deal: dict[str, Any],
    *,
    comments_raw: list[dict[str, Any]],
    activities_raw: list[dict[str, Any]],
    history_raw: list[dict[str, Any]],
    reassignments: list[dict[str, Any]],
    users: dict[int, str],
    stage_names: dict[str, str],
    afina_code: str,
    resolver: TranscriptResolver,
    launches: dict[int, dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    """Собрать одну строку досье."""
    deal_id = _coerce_int(deal.get("ID"))
    assignee_id = _coerce_int(deal.get("ASSIGNED_BY_ID"))
    stage_id = _clean_str(deal.get("STAGE_ID"))

    comments = _comment_rows(comments_raw, assignee_id, users)
    activities = _activity_rows(activities_raw, users)
    calls = _call_rows(activities_raw, deal_id, resolver, launches)
    history = build_stage_history(history_raw, stage_names)

    last_work = _last_work_moment(comments, activities, calls)
    open_activities = [a for a in activities if not a["completed"]]

    return {
        "id": deal_id,
        "title": _clean_str(deal.get("TITLE")),
        "category_id": _coerce_int(deal.get("CATEGORY_ID")),
        "stage_id": stage_id,
        "stage_name": stage_names.get(stage_id, stage_id),
        "assigned_by_id": assignee_id,
        "assigned_by_name": users.get(assignee_id, ""),
        "date_create": _iso(deal.get("DATE_CREATE")),
        "date_modify": _iso(deal.get("DATE_MODIFY")),
        "opportunity": _coerce_float(deal.get("OPPORTUNITY")),
        "source_id": _clean_str(deal.get("SOURCE_ID")),
        "afina_id": _clean_str(deal.get(afina_code)) if afina_code else "",
        # Резолв идентификатора в объект Афины — отдельный этап. Место в
        # схеме занято заранее, чтобы читатель не пересобирался под него.
        "afina_object": None,
        "days_since_last_activity": _days_since(last_work, now),
        "days_in_stage": _days_in_stage(
            history, stage_id, _iso(deal.get("DATE_CREATE")), now,
        ),
        "stage_history": history,
        "comments": comments,
        "activities": activities,
        "calls": calls,
        "reassignments": reassignments,
        "counters": {
            "comments_total": len(comments),
            "comments_by_assignee": sum(1 for c in comments if c["author_is_assignee"]),
            "calls_total": len(calls),
            "calls_with_transcript": sum(1 for c in calls if c["has_transcript"]),
            "activities_open": len(open_activities),
        },
    }


# --------------------------------------------------------------------------
# журнал смены ответственного
# --------------------------------------------------------------------------

def _snapshot_path() -> Path:
    return Path(get_settings().dossier_dir) / "assignee_snapshot.json"


def _log_path() -> Path:
    return Path(get_settings().dossier_dir) / "assignee_log.jsonl"


def load_snapshot() -> dict[int, int]:
    try:
        raw = json.loads(_snapshot_path().read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {int(k): int(v) for k, v in raw.items() if str(k).isdigit()}


def save_snapshot(snapshot: dict[int, int]) -> None:
    path = _snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({str(k): v for k, v in sorted(snapshot.items())},
                   ensure_ascii=False, indent=0),
        encoding="utf-8",
    )


def diff_assignees(
    previous: dict[int, int],
    current: dict[int, int],
    users: dict[int, str],
    now: datetime,
) -> list[dict[str, Any]]:
    """Что изменилось между снимками.

    В REST истории смены ответственного нет, и задним числом она не
    восстанавливается — журнал копится только вперёд. Поэтому здесь важнее
    всего не наврать: сделка, которой в прошлом снимке не было, — это не
    переназначение, а новая карточка, и в журнал она не попадает.
    """
    events: list[dict[str, Any]] = []
    stamp = now.isoformat()
    for deal_id, to_id in sorted(current.items()):
        if deal_id not in previous:
            continue
        from_id = previous[deal_id]
        if from_id == to_id:
            continue
        events.append({
            "deal_id": deal_id,
            "detected_at": stamp,
            "from_id": from_id,
            "to_id": to_id,
            "from_name": users.get(from_id, ""),
            "to_name": users.get(to_id, ""),
        })
    return events


def append_log(events: list[dict[str, Any]]) -> None:
    if not events:
        return
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def load_log() -> dict[int, list[dict[str, Any]]]:
    """Весь накопленный журнал: сделка → список переназначений."""
    path = _log_path()
    if not path.exists():
        return {}
    out: dict[int, list[dict[str, Any]]] = {}
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                deal_id = _coerce_int(event.get("deal_id"))
                if deal_id > 0:
                    out.setdefault(deal_id, []).append(event)
    except OSError:
        logger.warning("Журнал переназначений не прочитан")
    return out


# --------------------------------------------------------------------------
# отбор в delta
# --------------------------------------------------------------------------

def delta_reasons(row: dict[str, Any], now: datetime) -> list[str]:
    """Почему карточка попала в ежедневную выгрузку. Пусто — не попала.

    Причины перечисляются, а не сводятся к «да/нет»: когда через месяц
    выгрузка распухнет, по ним будет видно, какое правило её раздувает.
    """
    reasons: list[str] = []
    edge = now - DELTA_WINDOW

    def _fresh(stamp: str) -> bool:
        moment = _parse_datetime(stamp)
        return moment is not None and moment.astimezone(timezone.utc) >= edge

    if (any(_fresh(c["created"]) for c in row["comments"])
            or any(_fresh(a["created"]) for a in row["activities"])
            or any(_fresh(c["date"]) for c in row["calls"])
            or any(_fresh(m["date"]) for m in row["stage_history"])):
        reasons.append("свежее событие за сутки")

    silent = row["days_since_last_activity"]
    if (row["stage_id"] not in INACTIVE_STAGE_IDS
            and silent is not None and silent >= DELTA_SILENT_DAYS):
        reasons.append(f"молчит {int(silent)} дней на активной стадии")

    for activity in row["activities"]:
        if activity["completed"] or not activity["deadline"]:
            continue
        deadline = _parse_datetime(activity["deadline"])
        if deadline is not None and deadline.astimezone(timezone.utc) < now:
            reasons.append("просроченное дело")
            break

    if any(_fresh(event.get("detected_at", "")) for event in row["reassignments"]):
        reasons.append("сменился ответственный")

    return reasons


# --------------------------------------------------------------------------
# вывод
# --------------------------------------------------------------------------

README_TEXT = """# Досье по сделкам: как это читать

Одна строка JSONL — одна сделка. Файл собирает выгрузка из Битрикс24 и
ничего в нём не оценивает: оценку делает читающая модель.

## Правило, которое нельзя нарушать

Если у карточки есть звонок со статусом `queued` или `deferred`, и карточка
стоит на активной стадии, вердикт «работа не ведётся» по ней выносить
НЕЛЬЗЯ. Правильный вердикт — «данных недостаточно».

Причина не теоретическая. Расшифровка разговора появляется в портале не
сразу, а по запросу, и до её появления карточка выглядит молчащей. Был
случай, когда после появления текста оценка карточки поменялась на
противоположную: брокер работал, работа была голосом, а в комментариях её
не было.

## Статусы расшифровки

| Статус | Что значит |
|---|---|
| `ok` | текст разговора есть, он в поле `text` |
| `absent` | портал ответил: расшифровки нет |
| `queued` | поставлен в очередь на запуск, текст может появиться позже |
| `deferred` | не забирали: исчерпан бюджет запросов либо сбой чтения |
| `failed` | две постановки в очередь без результата — текста не будет |
| `too_short` | звонок короче 60 секунд, разговора в нём нет |

`too_short` — это не нехватка данных. Такие звонки не спрашиваются никогда,
и «данных недостаточно» по ним ставить не нужно.

## Главные поля

* `comments[].author_is_assignee` — написал ли запись сам ответственный.
  Посчитано при сборе, пересчитывать не нужно. `counters.comments_by_assignee`
  равное нулю при непустом `comments_total` означает, что по карточке пишут
  все, кроме брокера, которому она поручена.
* `days_since_last_activity` — дни с последнего следа РАБОТЫ: комментария,
  дела или звонка. Перенос стадии сюда не входит: это один клик, и считать
  его работой значит дать способ обнулять счётчик, ничего не сделав.
* `reassignments` — смены ответственного, замеченные нами. Журнал копится
  только вперёд: в REST истории переназначений нет, и до первого прогона
  выгрузки её не существует. Пустой список НЕ означает, что карточку не
  передавали.
* `stage_history` — идентификаторы стадий полные, с префиксом воронки
  (`C18:NEW`, `C0:PREPARATION`). Без префикса стадии двух воронок
  неразличимы.
"""


def write_lines(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )


# Имена выгрузок, и только они. Уборка обязана знать их наперечёт, а не
# угадывать по маске: рядом лежит assignee_log.jsonl — журнал, который
# копится только вперёд и задним числом не восстанавливается. Маска
# «*_*.jsonl» съела бы его первым же, и заметили бы это через месяц, когда
# понадобилось бы узнать, кто вёл карточку до нынешнего брокера.
EXPORT_PREFIXES = ("full_", "delta_")


def export_stems(names: list[str]) -> list[str]:
    """Основы имён выгрузок из списка файлов, свежие первыми."""
    stems = {
        name[: -len(".jsonl")]
        for name in names
        if name.endswith(".jsonl") and name.startswith(EXPORT_PREFIXES)
    }
    return sorted(stems, reverse=True)


def doomed_names(names: list[str], keep: int) -> list[str]:
    """Какие файлы каталога выгрузки пора удалить.

    Комплект прогона уходит целиком — строки, мета и очередь. Строки без
    своей меты это выгрузка, о которой ничего не известно: ни сколько
    карточек не собралось, ни сколько разговоров не прочиталось.

    Очередь именуется по дню, а не по режиму, и один день могут делить два
    прогона (полный по понедельникам идёт в тот же день, что и ежедневный).
    Поэтому очередь удаляется только тогда, когда за этот день не осталось
    ни одной живой выгрузки.
    """
    if keep <= 0:
        return []
    stems = export_stems(names)
    alive, dead = stems[:keep], stems[keep:]
    if not dead:
        return []

    def _date(stem: str) -> str:
        return stem.split("_", 1)[1] if "_" in stem else ""

    alive_dates = {_date(stem) for stem in alive}
    doomed_dates = {_date(stem) for stem in dead} - alive_dates - {""}

    out: list[str] = []
    for name in names:
        if any(name.startswith(f"{stem}.") for stem in dead):
            out.append(name)
        elif any(name == f"queue_{date}.json" for date in doomed_dates):
            out.append(name)
    return sorted(set(out))


def prune_old(directory: Path, keep: int) -> list[str]:
    """Оставить последние `keep` выгрузок, старые удалить."""
    if keep <= 0 or not directory.exists():
        return []
    names = [p.name for p in directory.iterdir() if p.is_file()]
    removed: list[str] = []
    for name in doomed_names(names, keep):
        try:
            (directory / name).unlink()
            removed.append(name)
        except OSError:
            logger.warning("Не удалён старый файл %s", name)
    return removed


# --------------------------------------------------------------------------
# прогон
# --------------------------------------------------------------------------

def run(
    mode: str,
    *,
    limit: int = 0,
    include_closed: bool = False,
    transcript_budget: int | None = None,
    launch_budget: int | None = None,
) -> dict[str, Any]:
    """Собрать выгрузку. Возвращает мету прогона."""
    settings = get_settings()
    started = time.monotonic()
    now = _now()
    init_db()

    categories = [settings.buyers_category_id, settings.sellers_category_id]
    categories = sorted({c for c in categories})
    resolver = TranscriptResolver(
        settings.dossier_transcript_budget
        if transcript_budget is None else transcript_budget,
    )

    users = load_users()
    afina_code = afina_field_code()

    # Журнал переназначений — по всему портфелю, до всякого отбора.
    current = list_assignees(categories)
    events = diff_assignees(load_snapshot(), current, users, now)
    append_log(events)
    save_snapshot({**load_snapshot(), **current})
    log_by_deal = load_log()

    deals: list[dict[str, Any]] = []
    stage_names: dict[str, str] = {}
    errors: list[dict[str, Any]] = []
    for category_id in categories:
        try:
            found = list_deals(category_id, afina_code, include_closed=include_closed)
        except Exception as exc:  # noqa: BLE001 — одна воронка не роняет прогон
            logger.exception("Воронка %s не прочитана", category_id)
            errors.append({"scope": f"category:{category_id}", "error": str(exc)})
            continue
        deals.extend(found)
        try:
            stage_names.update(_fetch_funnel_stage_names(category_id))
        except Exception:
            logger.warning("Названия стадий воронки %s не прочитаны", category_id)
    if limit > 0:
        deals = deals[:limit]

    deal_ids = [_coerce_int(d.get("ID")) for d in deals]
    deal_ids = [d for d in deal_ids if d > 0]
    logger.info("Сделок к разбору: %d", len(deal_ids))

    comments, comment_errors = fetch_timelines(deal_ids)
    activities, activity_errors = fetch_activities(deal_ids)
    history = stage_history(deal_ids)
    launches = get_transcript_launches(deal_ids)

    for deal_id in sorted(comment_errors | activity_errors):
        errors.append({"deal_id": deal_id, "error": "не прочитаны комментарии/дела"})

    rows: list[dict[str, Any]] = []
    for deal in deals:
        deal_id = _coerce_int(deal.get("ID"))
        if deal_id <= 0:
            continue
        try:
            rows.append(build_row(
                deal,
                comments_raw=comments.get(deal_id, []),
                activities_raw=activities.get(deal_id, []),
                history_raw=history.get(deal_id, []),
                reassignments=log_by_deal.get(deal_id, []),
                users=users,
                stage_names=stage_names,
                afina_code=afina_code,
                resolver=resolver,
                launches=launches,
                now=now,
            ))
        except Exception as exc:  # noqa: BLE001 — одна карточка не роняет прогон
            logger.exception("Сделка %s не собрана", deal_id)
            errors.append({"deal_id": deal_id, "error": str(exc)})

    if mode == "delta":
        kept = []
        for row in rows:
            reasons = delta_reasons(row, now)
            if reasons:
                row["delta_reasons"] = reasons
                kept.append(row)
        logger.info("Отобрано в delta: %d из %d", len(kept), len(rows))
        rows = kept

    failed_now = settle_launches(rows, launches)
    queue = collect_queue(
        rows, launches,
        settings.dossier_launch_budget if launch_budget is None else launch_budget,
        now,
    )
    for item in queue:
        item["attempts"] = record_transcript_launch(
            item["activity_id"], item["deal_id"], now.isoformat(),
        )
        for row in rows:
            if row["id"] != item["deal_id"]:
                continue
            for call in row["calls"]:
                if call["activity_id"] == item["activity_id"]:
                    call["transcript_status"] = T_QUEUED

    directory = Path(settings.dossier_dir)
    stem = f"{mode}_{now.date().isoformat()}"
    meta = {
        "mode": mode,
        "started_at": now.isoformat(),
        "seconds": round(time.monotonic() - started, 1),
        "deals_seen": len(deal_ids),
        "deals_written": len(rows),
        "errors": len(errors),
        "failed_ids": [e.get("deal_id") for e in errors if e.get("deal_id")],
        "error_details": errors[:50],
        "reassignments_detected": len(events),
        "transcripts": {
            "fetched": resolver.fetched,
            "absent": resolver.absent,
            "queued": len(queue),
            "failed": failed_now,
            # Две причины «не забрали» разнесены нарочно: сетевые сбои,
            # смешанные с исчерпанным бюджетом, растворяются в норме, и
            # деградация чтения обнаруживается только когда станет больно.
            "deferred_budget": resolver.deferred_budget,
            "deferred_error": resolver.deferred_error,
            "too_short": resolver.too_short,
        },
    }

    write_lines(directory / f"{stem}.jsonl", rows)
    write_json(directory / f"{stem}.meta.json", meta)
    write_json(directory / f"queue_{now.date().isoformat()}.json", queue)
    (directory / "README.md").write_text(README_TEXT, encoding="utf-8")

    uploaded = _deliver(directory, stem, now)
    meta["uploaded"] = uploaded
    write_json(directory / f"{stem}.meta.json", meta)

    removed = prune_old(directory, settings.dossier_keep_files)
    if removed:
        logger.info("Удалено старых файлов: %d", len(removed))

    logger.info(
        "Готово: %s, сделок %d, ошибок %d, расшифровок забрано %d, "
        "в очередь %d, за %.1f с",
        stem, len(rows), len(errors), resolver.fetched, len(queue),
        meta["seconds"],
    )
    return meta


def _deliver(directory: Path, stem: str, now: datetime) -> bool:
    """Отправить комплект в Drive. Неудача не роняет прогон.

    Импорт внутри функции, а не наверху файла: библиотек Google может не
    быть в образе, и тогда сбор — который уже закончился успешно — падал бы
    на строке import, не дойдя до записи меты.
    """
    settings = get_settings()
    if not (settings.gdrive_credentials_file and settings.gdrive_folder_id):
        logger.info("Google Drive не настроен — файлы остаются в %s", directory)
        return False
    # Docker, не найдя файла для монтирования, создаёт на его месте КАТАЛОГ.
    # Без этой проверки прогон падал бы внутри библиотеки Google с ошибкой
    # про чтение ключа, и искать её пришлось бы в стеке, а не в одной
    # понятной строке лога.
    if not Path(settings.gdrive_credentials_file).is_file():
        logger.warning(
            "Ключ Google не файл: %s — выгрузка остаётся в %s",
            settings.gdrive_credentials_file, directory,
        )
        return False
    try:
        from dossier_drive import upload_many
        names = [
            f"{stem}.jsonl",
            f"{stem}.meta.json",
            f"queue_{now.date().isoformat()}.json",
            "README.md",
        ]
        upload_many(directory, names)
        return True
    except Exception:
        logger.exception("Загрузка в Google Drive не удалась — файлы на диске")
        return False


def _quiet_the_portal_client() -> None:
    """Убрать из лога построчный отчёт клиента портала.

    fast_bitrix24 пишет INFO на каждый запрос и рисует прогрессбар. На
    ручном прогоне это полезно, в cron — нет: полная выгрузка делает две с
    половиной тысячи запросов, и столько же пар строк уходит в общий
    cron.log, где их никто не ищет. Однажды этот файл уже дорос до 14 ГБ
    при диске в 41 (см. scripts/cron_job.sh), и класть туда мегабайт в день
    ради «Starting get_all» незачем.

    Гасится только шум. Ошибки портала остаются: уровень WARNING, а не
    CRITICAL. Итоги прогона модуль пишет сам — одной строкой.
    """
    import tools  # noqa: PLC0415 — флаг модуля, а не импорт ради имени

    tools.BX_VERBOSE = False
    logging.getLogger("fast_bitrix24").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Выгрузка досье по сделкам")
    parser.add_argument("--mode", choices=("full", "delta"), default="delta",
                        help="full — весь портфель, delta — изменившееся за сутки")
    parser.add_argument("--limit", type=int, default=0,
                        help="Ограничить число сделок (0 — без ограничения)")
    parser.add_argument("--include-closed", action="store_true",
                        help="Включить закрытые сделки")
    parser.add_argument("--transcript-budget", type=int, default=None,
                        help="Потолок запросов расшифровок за прогон")
    parser.add_argument("--launch-budget", type=int, default=None,
                        help="Потолок очереди на запуск расшифровок")
    args = parser.parse_args(argv)

    settings = get_settings()
    setup_logging(settings.log_level)
    _quiet_the_portal_client()
    run(
        args.mode,
        limit=args.limit,
        include_closed=args.include_closed,
        transcript_budget=args.transcript_budget,
        launch_budget=args.launch_budget,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
