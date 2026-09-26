"""Разбор клиента моделью (раздел 6.3 ТЗ).

Правила `triage_state` отвечают на вопрос «кого смотреть первым» и обязаны
быть дешёвыми и объяснимыми построчно. На вопрос «что с этим человеком и
что теперь делать» правилами не ответишь: ответ лежит в словах клиента, а
слова читает модель.

Разбор идёт НЕ по всему портфелю. Охват — состояния, в которых нужно
решение: брошен, остыл, ждёт нас, без плана. Закрытых разбирать нечего,
отказавшихся — незачем, «в работе» и так движется. Остальные 1 450
клиентов стоили бы денег и не сказали бы ничего нового.

Поверх состояний — список стадий, которые разбор не трогает
(`CLIENTS_REVIEW_SKIP_STAGES_JSON`). Заведён под «Продавцы / Поиск
клиента»: там карточка живёт до появления покупателя, и работа идёт с
объектом, а не с человеком. Пропускается ЭТАП, а не человек: клиент с
живой сделкой в другой воронке разбирается как обычно.

**Разбор не переписывается, а дописывается.** `client_reviews` —
единственная таблица книги, которую нельзя пересобрать: всё остальное
выводится из витрины и портала, а это написала модель или человек. Новый
разбор ложится новой строкой, старый остаётся историей.

**Повторно разбираем только тех, у кого что-то произошло.** Сравнивается
`reviewed_through` последнего разбора с `last_event_at` клиента: не было
событий — нечего и пересматривать, и ночной прогон не платит за
переписывание вчерашнего вывода теми же словами.

**`enough_data` считаем мы, а не модель.** Спросив её саму, много ли у неё
данных, мы получили бы оценку от того, кто заинтересован ответить
уверенно. Признак берётся из выписки: есть ли хоть один расшифрованный
разговор или человеческая запись в карточке.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in (None, ""):  # запуск как `python src/clients/review.py`
    _HERE = Path(__file__).resolve().parent
    for _p in (str(_HERE.parent),):
        if _p not in sys.path:
            sys.path.insert(0, _p)

from clients import brief as brief_mod  # noqa: E402
from clients.brief import Brief, FeedEvent  # noqa: E402
from clients.schema import (  # noqa: E402
    ISSUE_ORDER, TRIAGE_ABANDONED, TRIAGE_COOLING, TRIAGE_NO_PLAN,
    TRIAGE_WAITING_US, clients_session,
)
from clients.transcripts import texts_of  # noqa: E402

logger = logging.getLogger(__name__)

# Кого разбираем. Те четыре состояния, в которых требуется решение
# человека: клиент жив, работа не идёт, и почему — из правил не видно.
SCOPE = (TRIAGE_ABANDONED, TRIAGE_COOLING, TRIAGE_WAITING_US, TRIAGE_NO_PLAN)

# Сколько клиентов за прогон. Не лимит провайдера, а обещание не потратить
# ночной бюджет разом: остальных возьмут следующие прогоны, а разбирать
# по второму разу некого — повторно берутся только изменившиеся.
DEFAULT_BUDGET = 150

# Кто написал. Колонка общая с ручными пометками человека (этап 2), и
# различать их придётся именно по ней.
AUTHOR = "модель"

# Закрытый словарь вердиктов. Открытый список превратил бы колонку в
# свалку синонимов: «надо позвонить», «нужен звонок», «позвонить» — три
# строки об одном, по которым не отфильтруешь.
VERDICTS = (
    "нужен звонок",
    "ждёт ответа от нас",
    "решение за клиентом",
    "скорее потерян",
    "работа идёт, вмешательство не нужно",
)

SYSTEM_PROMPT = """\
Ты разбираешь карточку клиента агентства недвижимости для руководителя
отдела продаж. Тебе дают выписку: кто клиент, его сделки, лента событий и
расшифровки телефонных разговоров.

Ответь строго одним JSON-объектом, без пояснений вокруг:

{
  "summary": "что происходит с клиентом — 2-4 предложения",
  "verdict": "одно значение из списка ниже",
  "issues": ["короткие формулировки проблем, до пяти"],
  "recommendation": "одно конкретное действие для брокера"
}

Допустимые значения verdict:
""" + "\n".join(f"- {name}" for name in VERDICTS) + """

Правила:
- Опирайся на слова клиента из расшифровок. Если расшифровок нет, говори о
  том, что видно из дат и записей, и не придумывай причин.
- В поле "чего не показано" сказано, сколько событий и разговоров осталось
  за кадром. Если там не нули, оговори это в summary.
- Если "есть на чём отвечать" равно false, начни summary со слов
  "Данных мало:" — брокер должен увидеть это первым.
- Закрыта карточка или нет, сказано полем "closed": 1 — закрыта, 0 — в
  работе. Название стадии этого НЕ означает. Например «Закрытая продажа
  (На сайт)» — рабочая стадия: объект продают без публичной рекламы, и
  задача брокера как раз довести клиента до выставления на ЦИАН. Никогда
  не выводи закрытие сделки из названия стадии.
- Не советуй того, чего клиент уже просил не делать.
- Пиши по-русски, без воды и без обращений к читателю.
"""


@dataclass(frozen=True)
class Review:
    """Ответ модели, приведённый к колонкам таблицы."""

    summary: str = ""
    verdict: str = ""
    issues: tuple[str, ...] = ()
    recommendation: str = ""


def parse(content: str) -> Review | None:
    """Разобрать ответ модели. ``None`` — ответ не читается.

    Пустой разбор в таблицу не пишется вовсе: строка без summary выглядит
    на карточке как «модель посмотрела и ничего не нашла», хотя на деле
    она ответила не тем форматом.
    """
    parsed = _json_object(content)
    if parsed is None:
        return None
    summary = _text(parsed.get("summary"))
    if not summary:
        logger.warning("Разбор без summary — не записан")
        return None
    return Review(
        summary=summary,
        verdict=_verdict(parsed.get("verdict")),
        issues=_issues(parsed.get("issues")),
        recommendation=_text(parsed.get("recommendation")),
    )


def _json_object(content: str) -> dict[str, Any] | None:
    """JSON из ответа, даже если модель обернула его в ```json."""
    text = str(content or "")
    if "{" not in text:
        return None
    start = text.index("{")
    try:
        parsed, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        logger.warning("Ответ модели не разбирается как JSON (%d символов)", len(text))
        return None
    return parsed if isinstance(parsed, dict) else None


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _verdict(value: Any) -> str:
    """Вердикт из словаря. Чужое значение не выбрасывается, а помечается.

    Выбросить — значит потерять след того, что подсказка разъехалась с
    кодом; подставить ближайшее — соврать. Поэтому чужое сохраняется как
    есть, а прогон считает такие случаи и показывает числом.
    """
    text = _text(value).casefold()
    for name in VERDICTS:
        if text == name.casefold():
            return name
    return _text(value)


def _issues(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    found = [_text(item) for item in value]
    return tuple(item for item in found if item)[:5]


# --------------------------------------------------------------------------
# приём разбора извне (раздел 7.2 ТЗ)
# --------------------------------------------------------------------------

def parse_issues(value: Any) -> tuple[tuple[str, ...], str]:
    """Коды проблем из тела запроса. Второе — причина отказа или пусто.

    Словарь закрыт: вкладка «Исключения» считает по нему, и код вне
    словаря не появился бы там никогда. Приняв такой молча, ручка сказала
    бы отправителю «записал», а записи не было бы — худший из отказов,
    потому что он не выглядит отказом.
    """
    if value is None:
        return (), ""
    if not isinstance(value, list):
        return (), "issues — массив кодов раздела 8"
    codes = tuple(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
    unknown = [code for code in codes if code not in ISSUE_ORDER]
    if unknown:
        return (), f"неизвестные коды проблем: {', '.join(unknown)}"
    return codes, ""


def valid_through(value: Any, *, now: datetime | None = None) -> tuple[str, str]:
    """Проверить `reviewed_through`. Второе — причина отказа или пусто.

    **Смещение обязательно.** Витрина хранит UTC, портал отдаёт +03:00, и
    разбор, отправленный вечером без смещения, оказался бы «из будущего» —
    а по этому сравнению карточка решает, свежий он или устарел.

    **Будущее не принимается.** Отметка вперёд означала бы «прочитано то,
    чего ещё не было», и такой разбор навсегда остался бы свежим: ни одно
    настоящее событие его уже не догонит.

    Пустое значение — тоже отказ. Без него `stale` не считается вовсе, и
    разбор притворялся бы актуальным до конца времён.
    """
    text = str(value or "").strip()
    if not text:
        return "", "reviewed_through обязателен"
    try:
        moment = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return "", "reviewed_through — не ISO-8601"
    if moment.tzinfo is None:
        return "", "reviewed_through без смещения: нужен +03:00 или Z"
    if moment > (now or datetime.now(timezone.utc)):
        return "", "reviewed_through в будущем"
    return text, ""


# --------------------------------------------------------------------------
# чтение книги
# --------------------------------------------------------------------------

_PENDING = """
SELECT c.client_key
FROM clients c
LEFT JOIN (
    SELECT client_key, MAX(reviewed_through) AS seen
    FROM client_reviews GROUP BY client_key
) r ON r.client_key = c.client_key
WHERE c.triage_state IN ({states})
  -- Ушедшего из портфеля разбирать нечего: сделку удалили или увели в
  -- чужую воронку, и состояние у строки заморожено на последнем прогоне,
  -- который её видел. Дашборд отсекает таких представлением `v_client`,
  -- но прогон читает книгу напрямую, и своя оговорка ему нужна.
  AND c.left_at IS NULL
  AND (r.seen IS NULL OR r.seen < c.last_event_at)
  {skip}
ORDER BY (COALESCE(c.calls_with_transcript, 0) > 0) DESC,
         c.silence_days DESC, c.client_key
"""

# Пропуск стадии — про ЭТАП, а не про человека. Клиент выпадает из
# разбора, только если ни одной живой карточки вне пропускаемых стадий у
# него нет; рядом стоящая сделка в другой воронке возвращает его обратно.
#
# Первое условие держит в очереди клиента без единой открытой карточки:
# у него нечему быть вне списка, и без оговорки он выпал бы заодно.
_SKIP_STAGES = """
  AND (NOT EXISTS (SELECT 1 FROM client_links o
                   WHERE o.client_key = c.client_key AND o.closed = 0)
       OR EXISTS (SELECT 1 FROM client_links l
                  WHERE l.client_key = c.client_key AND l.closed = 0
                    AND l.stage_id NOT IN ({stages})))
"""


def pending(conn: sqlite3.Connection, *, states: Sequence[str] = SCOPE,
            skip_stages: Sequence[str] = ()) -> list[str]:
    """Кого разбирать. Первыми — те, у кого есть что читать.

    Порядок стоил боевого прогона. Сначала очередь шла по одной тишине, и
    первые пять разборов достались клиентам без единого разговора и без
    единой записи человека: модель честно ответила «данных мало» пять раз
    подряд. Вывод по такой карточке уже сделан правилом — «завели и ни
    разу не коснулись», — и пересказывать его моделью значит платить за
    имитацию разбора.

    Пустые карточки из очереди не выброшены: они в конце. Дойдёт бюджет —
    разберём и их, но после тех, где есть слова клиента.

    Внутри каждой группы порядок прежний: дольше всего молчавшие первыми.

    Клиента без единого события берёт `r.seen IS NULL` — разбора у него
    ещё нет, а «завели и забыли» это самый повод. Но берёт ОДИН раз:
    второе условие сравнивает с `last_event_at`, и у пустого клиента оно
    ложно, потому что сравнение с NULL в SQL не истинно никогда.

    Это не случайность, а решение, и стоило оно отдельной правки. Сначала
    здесь стояло `c.last_event_at IS NULL OR ...` — с виду страховка от
    пустоты, на деле приговор: клиент, у которого событий нет и не будет,
    попадал бы в разбор каждую ночь до конца времён. Появятся события —
    сравнение оживёт само: пустая строка меньше любой даты.
    """
    marks = ", ".join("?" * len(states))
    # Пустой список — никакой оговорки в запросе вовсе. `NOT IN ()` SQLite
    # понимает, но читающему запрос пришлось бы вспоминать, как именно.
    skip = _SKIP_STAGES.format(stages=", ".join("?" * len(skip_stages))) \
        if skip_stages else ""
    sql = _PENDING.format(states=marks, skip=skip)
    rows = conn.execute(sql, (*states, *skip_stages)).fetchall()
    return [str(row[0]) for row in rows]


def load(conn: sqlite3.Connection, client_key: str) -> tuple[dict, list[dict], list[FeedEvent]]:
    """Клиент, его карточки и лента — всё, кроме текстов разговоров."""
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM clients WHERE client_key = ?", (client_key,),
    ).fetchone()
    cards = [dict(item) for item in conn.execute(
        "SELECT * FROM client_links WHERE client_key = ?"
        " ORDER BY entity_type, entity_id", (client_key,),
    )]
    events = [_feed_event(item) for item in conn.execute(
        "SELECT at, kind, source_id, is_system, author_is_assignee, payload_json"
        " FROM client_events WHERE client_key = ? ORDER BY at, id", (client_key,),
    )]
    return (dict(row) if row else {}), cards, events


def _feed_event(row: Mapping[str, Any]) -> FeedEvent:
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (TypeError, ValueError):
        payload = {}
    return FeedEvent(
        at=str(row["at"]),
        kind=str(row["kind"]),
        source_id=str(row["source_id"]),
        payload=payload if isinstance(payload, dict) else {},
        is_system=bool(row["is_system"]),
        author_is_assignee=row["author_is_assignee"],
    )


def make_brief(conn: sqlite3.Connection, client_key: str) -> Brief:
    """Выписка по одному клиенту, вместе с текстами разговоров."""
    client, cards, events = load(conn, client_key)
    call_ids = [int(event.source_id) for event in events
                if event.kind == "call" and str(event.source_id).isdigit()]
    return brief_mod.build(client, cards, events, texts_of(call_ids))


def save(conn: sqlite3.Connection, client_key: str, found: Brief,
         review: Review, *, now: datetime, through: str | None) -> None:
    """Записать разбор новой строкой.

    `reviewed_through` — время последнего события, которое разбор ВИДЕЛ.
    По нему карточка отличает свежий разбор от устаревшего, а прогон —
    кого пересматривать. Пусто у клиента без событий: тогда пересмотр
    назначит первое же его событие.
    """
    conn.execute(
        "INSERT INTO client_reviews(client_key, created_at, reviewed_through,"
        " summary, verdict, issues_json, recommendation, enough_data, author)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (client_key, now.isoformat(), through or "", review.summary,
         review.verdict, json.dumps(list(review.issues), ensure_ascii=False),
         review.recommendation, int(found.enough_data), AUTHOR),
    )


# --------------------------------------------------------------------------
# прогон
# --------------------------------------------------------------------------

def ask(llm: Any, found: Brief) -> tuple[Review | None, dict[str, int]]:
    """Спросить модель про одного клиента.

    Ошибку запроса не гасим молча и не роняем ею прогон: один недоступный
    ответ стоит одного клиента, а не ночи.
    """
    from langchain_core.messages import HumanMessage, SystemMessage
    from llm import extract_usage

    payload = json.dumps(found.as_payload(), ensure_ascii=False, indent=1)
    try:
        response = llm.invoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=payload),
        ])
    except Exception as error:  # noqa: BLE001 — один сбой не стоит прогона
        logger.warning("Модель не ответила: %s", error)
        return None, {}
    content = getattr(response, "content", response)
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return parse(str(content)), extract_usage(response)


def run(*, dry_run: bool = False, budget: int | None = DEFAULT_BUDGET,
        states: Sequence[str] = SCOPE, now: datetime | None = None,
        llm: Any = None, skip_stages: Sequence[str] | None = None) -> dict[str, Any]:
    """Разобрать тех, кому это нужно, и записать выводы."""
    moment = now or datetime.now(timezone.utc)
    summary: dict[str, Any] = {"охват": list(states)}

    if skip_stages is None:
        from config import get_settings
        skip_stages = get_settings().review_skip_stages
    if skip_stages:
        summary["пропускаем стадии"] = list(skip_stages)

    with clients_session(readonly=True) as conn:
        queue = pending(conn, states=states, skip_stages=skip_stages)
    keys = queue if budget is None else queue[:budget]
    # Два числа, а не одно. «К разбору 150» при очереди в семьсот человек
    # читается как «их всего сто пятьдесят», и по такому отчёту нельзя
    # понять, разгребается очередь или стоит на месте.
    summary["в очереди"] = len(queue)
    summary["к разбору"] = len(keys)

    if dry_run:
        summary["сухой прогон"] = True
        logger.info("Разбор клиентов: %s", summary)
        return summary
    if not keys:
        logger.info("Разбор клиентов: %s", summary)
        return summary

    if llm is None:
        from config import get_settings
        from llm import make_llm
        llm = make_llm(get_settings())

    done = failed = thin = odd = 0
    usage: dict[str, int] = {}
    for client_key in keys:
        # Соединение на клиента: прогон идёт часами, и держать открытую
        # транзакцию всё это время значит блокировать ночную пересборку.
        with clients_session() as conn:
            found = make_brief(conn, client_key)
            through = _last_event_at(conn, client_key)
        review, spent = ask(llm, found)
        for name, value in spent.items():
            usage[name] = usage.get(name, 0) + value
        if review is None:
            failed += 1
            continue
        if not found.enough_data:
            thin += 1
        if review.verdict not in VERDICTS:
            odd += 1
        with clients_session() as conn:
            save(conn, client_key, found, review, now=moment, through=through)
        done += 1

    summary.update({"разобрано": done, "не ответила": failed,
                    "данных мало": thin, "вердикт вне словаря": odd,
                    "токены": usage})
    logger.info("Разбор клиентов: %s", summary)
    return summary


def _last_event_at(conn: sqlite3.Connection, client_key: str) -> str | None:
    row = conn.execute(
        "SELECT last_event_at FROM clients WHERE client_key = ?", (client_key,),
    ).fetchone()
    return row[0] if row else None


def main() -> int:
    from config import get_settings, setup_logging
    from tools import quiet_the_portal_client

    parser = argparse.ArgumentParser(description="Разбор клиентов моделью")
    parser.add_argument("--dry-run", action="store_true",
                        help="показать, кого разобрали бы, и не спрашивать модель")
    parser.add_argument("--budget", type=int, default=None,
                        help=f"сколько клиентов разобрать за прогон "
                             f"(по умолчанию {DEFAULT_BUDGET}, 0 — без предела)")
    parser.add_argument("--state", action="append", choices=list(SCOPE), default=None,
                        help="разобрать только это состояние; можно повторять")
    args = parser.parse_args()

    setup_logging(get_settings().log_level)
    quiet_the_portal_client()

    if args.budget is None:
        budget = DEFAULT_BUDGET
    elif args.budget <= 0:
        budget = None
    else:
        budget = args.budget

    summary = run(dry_run=args.dry_run, budget=budget,
                  states=tuple(args.state) if args.state else SCOPE)
    logger.info("Готово: %s", summary)
    # Прогон, в котором модель не ответила НИ РАЗУ, — это сбой, а не тишина.
    return 1 if summary.get("к разбору") and not summary.get("разобрано") \
        and not summary.get("сухой прогон") else 0


if __name__ == "__main__":
    raise SystemExit(main())
