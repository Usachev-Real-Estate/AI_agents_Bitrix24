"""Догрузить расшифровки по звонкам, которые видит книга клиентов.

Зачем понадобилось. На 25.09 из 5337 звонков портфеля расшифровано 353 —
шесть процентов. Причина не в очереди и не в портале, а в том, у КОГО мы
спрашиваем: `src/transcripts.py:29` перечисляет дела фильтром
``OWNER_TYPE_ID: 2``, то есть только звонки, висящие на сделках. Звонков на
контактах на боевом портале больше, чем на сделках (11 690 против 7 102 за
год — см. `analytics/etl.py`), и у портала про них не спрашивали ни разу.

Отсюда устройство этого шага.

**Идентификаторы берутся из книги, а не из портала.** Старый путь зовёт
`crm.activity.list` на каждую сделку — тысячи запросов ради списка, который
уже собран и лежит в `client_events`. Здесь ровно один запрос на звонок, по
которому расшифровки нет, и ни одного лишнего.

**Спрашиваем только про разговоры длиннее минуты.** У недозвона
расшифровывать нечего; спросив, мы получили бы пустой ответ, записали бы
`not_ready` и заняли бы этим звонком место в повторах — навсегда, потому
что текст у него не появится никогда.

**Пишем той же функцией, что и досье** (`db.upsert_call_transcript`).
Второй писатель в ту же таблицу разошёлся бы с первым в первый же день,
когда одному из них поменяют правила.

**Привязка к сделке не понижается.** Звонок на контакте сделки не имеет, и
в колонку `deal_id` пошёл бы ноль. Если кэш уже знает сделку — она
сохраняется: `list_call_transcripts_for_deal` иначе потеряла бы звонок,
который раньше находила.

**Чего этот шаг не делает и не может.** Он не расшифровывает сам, и никакой
другой шаг тоже не сможет: в REST Битрикса нет метода, запускающего
расшифровку. `crm.activity.call.getTranscript` только читает готовое и
возвращает `null`, когда его нет. Расшифровку делает сам портал в момент
окончания звонка — при включённой функции и наличии ИИ-кредитов, — и
задним числом её не запустить ничем.

Поэтому догрузка — это не «расшифровать», а «забрать то, что портал уже
расшифровал, но у нас не спросили». Сколько таких на самом деле, видно
из `voximplant.statistic.get`: там у каждого звонка есть `TRANSCRIPT_ID`
и `CALL_RECORD_URL`. Звонок без записи не расшифрует никто и никогда.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

if __package__ in (None, ""):  # запуск как `python src/clients/pull_transcripts.py`
    _HERE = Path(__file__).resolve().parent
    for _p in (str(_HERE.parent),):
        if _p not in sys.path:
            sys.path.insert(0, _p)

from clients.facts import duration_sec  # noqa: E402
from clients.schema import EVENT_CALL, OWNER_DEAL, clients_session  # noqa: E402
from clients.transcripts import Cached, cache_state  # noqa: E402
from clients.triage import MEANINGFUL_CALL_SEC  # noqa: E402

logger = logging.getLogger(__name__)

# Сколько звонков берём за прогон, когда предел не задан. Сутки на то,
# чтобы портал отдал остальное, дешевле одной ночи, в которую прогон упёрся
# в лимит запросов и оставил после себя половину записанной очереди.
DEFAULT_BUDGET = 400


@dataclass(frozen=True)
class Call:
    """Звонок так, как его знает книга."""

    activity_id: int
    entity_type: str
    entity_id: int
    seconds: int | None

    @property
    def deal_id(self) -> int:
        """Сделка звонка. Ноль — звонок висит на контакте."""
        return self.entity_id if self.entity_type == OWNER_DEAL else 0


@dataclass(frozen=True)
class Plan:
    """Кого спрашиваем и почему остальных не спрашиваем."""

    calls: int = 0
    meaningful: int = 0
    with_text: int = 0
    waiting: int = 0
    # Кандидаты по тому, где висит звонок. Это и есть ответ на вопрос
    # «почему расшифровано шесть процентов»: если тут преобладает
    # `contact`, дело не в очереди, а в том, что про эти звонки не
    # спрашивали никогда.
    by_owner: dict[str, int] = field(default_factory=dict)
    candidates: tuple[Call, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "звонков": self.calls,
            "длиннее минуты": self.meaningful,
            "уже с текстом": self.with_text,
            "рано повторять": self.waiting,
            "спросим": len(self.candidates),
            "где висят": dict(sorted(self.by_owner.items())),
        }


def read_calls(conn: sqlite3.Connection) -> list[Call]:
    """Звонки книги вместе с длительностью.

    Длительность считается здесь же из полезной нагрузки события, а не
    берётся из витрины: поля `duration` в `fact_activity` нет `[V28]`, и
    единственный способ узнать длину разговора — разность меток.
    """
    conn.row_factory = sqlite3.Row
    out: list[Call] = []
    for row in conn.execute(
        "SELECT source_id, entity_type, entity_id, payload_json"
        " FROM client_events WHERE kind = ?", (EVENT_CALL,),
    ):
        try:
            activity_id = int(row["source_id"])
        except (TypeError, ValueError):
            # Звонок без числового идентификатора спросить не у кого:
            # `getTranscript` принимает activityId и ничего больше.
            continue
        payload = _payload(row["payload_json"])
        out.append(Call(
            activity_id=activity_id,
            entity_type=str(row["entity_type"] or ""),
            entity_id=int(row["entity_id"] or 0),
            seconds=duration_sec(payload),
        ))
    return out


def plan(
    calls: Sequence[Call],
    cached: Mapping[int, Cached],
    *,
    now: datetime,
    retry_hours: float,
    budget: int | None = None,
) -> Plan:
    """Кого спросить у портала в этот прогон.

    Чистая функция: ни базы, ни портала. Решение «спрашивать или нет»
    стоит денег и запросов, и проверяться оно должно без обоих.
    """
    meaningful = with_text = waiting = 0
    by_owner: dict[str, int] = {}
    candidates: list[Call] = []

    for call in calls:
        if call.seconds is None or call.seconds < MEANINGFUL_CALL_SEC:
            continue
        meaningful += 1
        known = cached.get(call.activity_id)
        if known and known.status == "ok":
            with_text += 1
            continue
        if not _retry_due(known, retry_hours=retry_hours, now=now):
            waiting += 1
            continue
        by_owner[call.entity_type or "?"] = by_owner.get(call.entity_type or "?", 0) + 1
        candidates.append(call)

    # Сначала те, про кого не спрашивали ни разу: у звонка со свежим
    # `not_ready` шанс получить текст заведомо ниже, чем у нетронутого, и
    # отдавать ему бюджет первым значило бы тратить прогон на повторы.
    candidates.sort(key=lambda call: (call.activity_id in cached, -call.activity_id))
    limited = tuple(candidates if budget is None else candidates[:budget])
    return Plan(
        calls=len(calls), meaningful=meaningful, with_text=with_text,
        waiting=waiting, by_owner=by_owner, candidates=limited,
    )


def _retry_due(known: Cached | None, *, retry_hours: float, now: datetime) -> bool:
    """Пора ли спрашивать снова. Не спрашивали ни разу — да."""
    if known is None:
        return True
    if known.status == "ok":
        return False
    moment = _moment(known.fetched_at)
    if moment is None:
        return True
    return (now - moment).total_seconds() >= retry_hours * 3600


def _moment(value: str) -> datetime | None:
    text = (value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _payload(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def pull(*, dry_run: bool = False, budget: int | None = DEFAULT_BUDGET,
         now: datetime | None = None) -> dict[str, Any]:
    """Спросить у портала расшифровки по звонкам без текста.

    ``budget`` передаётся в `plan` КАК ЕСТЬ, а умолчание стоит прямо в
    подписи. Пока оно подставлялось внутри, ``None`` значил здесь «не
    задан», а там «без предела» — и `--budget 0`, обещавший снять предел,
    молча возвращал те же четыреста. Одно значение с двумя смыслами
    находится не чтением кода, а прогоном на живом портфеле.
    """
    from config import get_settings

    settings = get_settings()
    moment = now or datetime.now(timezone.utc)

    with clients_session(readonly=True) as conn:
        calls = read_calls(conn)

    cached = cache_state(call.activity_id for call in calls)
    if cached is None:
        # Кэш не открылся: не зная, у кого текст уже есть, спрашивать
        # портал — значит спросить про все пять тысяч звонков разом.
        logger.error("Кэш расшифровок недоступен — догрузка отменена")
        return {"кэш": "недоступен", "спрошено": 0}

    made = plan(calls, cached, now=moment,
                retry_hours=float(settings.clients_transcript_retry_hours),
                budget=budget)
    summary = made.as_dict()
    if dry_run:
        summary["сухой прогон"] = True
        logger.info("Догрузка расшифровок: %s", summary)
        return summary

    summary.update(_ask(made.candidates, cached, moment=moment))
    logger.info("Догрузка расшифровок: %s", summary)
    return summary


def _ask(candidates: Iterable[Call], cached: Mapping[int, Cached], *,
         moment: datetime) -> dict[str, int]:
    """Спросить портал по каждому кандидату и записать, что он ответил."""
    from db import upsert_call_transcript
    from transcripts import fetch_transcript_from_api

    got = empty = failed = 0
    stamp = moment.isoformat()
    # По одному запросу на звонок, и собрать их в пачку нельзя: документация
    # метода прямо называет ошибку ERROR_BATCH_METHOD_NOT_ALLOWED — «method
    # is not allowed for batch usage». Отсюда же бюджет прогона: сложить
    # тысячу запросов в один вызов не выйдет, их будет ровно тысяча.
    for call in candidates:
        text, status = fetch_transcript_from_api(call.activity_id)
        # Привязка к сделке не понижается: у звонка на контакте её нет, но
        # если кэш уже знал сделку, стереть её нулём значило бы отобрать
        # звонок у `list_call_transcripts_for_deal`.
        known = cached.get(call.activity_id)
        deal_id = call.deal_id or (known.deal_id if known else 0)
        upsert_call_transcript(
            activity_id=call.activity_id, deal_id=deal_id, text=text or "",
            status=status, fetched_at=stamp, chars=len(text or ""),
        )
        if text:
            got += 1
        elif status == "error":
            failed += 1
        else:
            empty += 1
    return {"спрошено": got + empty + failed, "текст получен": got,
            "портал ответил пусто": empty, "ошибок": failed}


def main() -> int:
    from config import get_settings, setup_logging
    from tools import quiet_the_portal_client

    parser = argparse.ArgumentParser(
        description="Догрузить расшифровки по звонкам книги клиентов")
    parser.add_argument("--dry-run", action="store_true",
                        help="показать, кого спросили бы, и не спрашивать")
    parser.add_argument("--budget", type=int, default=None,
                        help=f"сколько звонков спросить за прогон "
                             f"(по умолчанию {DEFAULT_BUDGET}, 0 — без предела)")
    args = parser.parse_args()

    setup_logging(get_settings().log_level)
    # Иначе каждый из четырёхсот запросов нарисует в cron.log полосу
    # прогресса и строку INFO. Выключатель общий с досье — см. tools.
    quiet_the_portal_client()
    # Ноль и меньше — «без предела». Отрицательное число иначе доехало бы
    # до среза `candidates[:budget]` и молча отрезало бы с конца.
    if args.budget is None:
        budget = DEFAULT_BUDGET
    elif args.budget <= 0:
        budget = None
    else:
        budget = args.budget
    summary = pull(dry_run=args.dry_run, budget=budget)
    logger.info("Готово: %s", summary)
    return 1 if summary.get("кэш") == "недоступен" else 0


if __name__ == "__main__":
    raise SystemExit(main())
