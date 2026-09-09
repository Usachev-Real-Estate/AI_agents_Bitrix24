"""Чтение комментариев брокеров: что обещали, чего ждут, кто отказал.

Витрина умеет считать, что запись есть. Что в ней написано, не считает
ничто — а там лежит единственный ответ на вопрос «что с клиентом»:

    «Позвонить в пятницу, согласовать фотосессию»
    «Договорились созвониться в конце осени, будем сотрудничать»
    «В семье есть свой риэлтор (мама), просмотр только с покупателем, 1,5%»
    «Задаток с другим агентством сорвался. Мы снова в рекламе»

Ни одно поле портала этого не хранит, и никакая метрика этого не выведет.

ПОЧЕМУ ПЯТЬ ПОЛЕЙ, А НЕ ПЕРЕСКАЗ. Пересказ карточки читать некому: их
восемьсот. Отчёту нужны признаки, по которым карточку можно поставить в
список и проверить назавтра, и все пять взяты из живых записей, а не
придуманы:

* **обещание со сроком** — «позвонить в пятницу», «ориентир середина
  августа». Дата в прошлом плюс отсутствие новой записи — самый надёжный
  сигнал из всех: конкретный, проверяемый и названный самим брокером;
* **срок законного молчания** — «созвонимся в конце осени». Без него отчёт
  ругает за правильную работу: карточка молчит сорок дней, и так и надо;
* **отказ клиента** — «свой риэлтор», «договор не хочет». Карточка висит на
  «Назначении встречи» третий месяц, а встречи не будет никогда;
* **готовность к шагу** — «можно приводить покупателя», «мы снова в
  рекламе». Здесь деньги ближе всего;
* **условия** — комиссия, цена, задаток. Их нет ни в одном поле.

ЧИТАЕТСЯ ТОЛЬКО ИЗМЕНИВШЕЕСЯ. Карточек больше тысячи, и перечитывать ту,
где ничего не добавилось, значит платить за один и тот же ответ каждый
день. Отпечаток последней записи хранится рядом с ответом.

МОДЕЛЬ НЕ РЕШАЕТ, ЧТО ПРОСРОЧЕНО. Она только достаёт из текста дату и
фразу; сравнивает с сегодняшним днём код. Дай ей судить — и разбор
«просрочено ли» поедет вместе с настроением модели, а вопрос это
арифметический.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent
for _path in (_SRC, _SRC / "analytics"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402

from config import get_settings, setup_logging  # noqa: E402
from llm import (  # noqa: E402
    USAGE_KEYS, estimate_cost, extract_usage, make_llm,
)
from schema import analytics_session, init_analytics_db  # noqa: E402

logger = logging.getLogger(__name__)

# Сколько карточек читать за один запуск. Первый проход по всему портфелю
# растянется на несколько запусков — и пусть: разбор не срочный, а тысяча
# обращений подряд стоит денег и времени сразу.
BATCH = 120

# Сколько карточек спрашивать одновременно. Ждём мы здесь не своего
# процессора, а чужой сети: карточка отвечает за полминуты, и тысяча
# карточек подряд — это десять часов, то есть первый проход не влезает ни
# в какую ночь ни при какой модели. Карточки друг от друга не зависят,
# поэтому лечится это не выбором модели, а тем, чтобы не ждать по одной.
#
# Четыре, а не сорок: у провайдера есть предел частоты, и упереться в него
# значит купить отказы вместо скорости. Точное число подбирается замером,
# для того прогон и печатает секунды на карточку.
WORKERS = 4

# Сколько последних записей давать модели. Четырёх хватает: обещание живёт
# в последней, а предыдущие нужны, только чтобы понять, повторяется ли оно.
# Вся история карточки удорожает запрос и ответ не улучшает.
NOTES = 4

# Версия промпта. Поднимается КАЖДЫЙ раз, когда правила чтения меняются:
# карточка, прочитанная по старым правилам, иначе осталась бы с прежним
# ответом навсегда — отпечаток её записей не меняется от того, что мы стали
# читать иначе. Поднятая версия сама ставит все карточки в очередь, и
# перечитываются они тем же кругом, а не разом.
#
# 4: обещание от паузы отличает ТОТ, КТО ДЕЙСТВУЕТ. «Договорились
#    созвониться в пятницу» уходило в wait_until — то есть карточка
#    получала освобождение от проверки вместо просрочки.
# 3: ограничение отделено от отказа («есть свой агент, НО показывать можем»
#    считалось отказом), состояние — от обещания («в рекламу»).
# 2: прошедшее время не обещание, торг не отказ, записи-пустышки пусты.
# 1: первая редакция, написана до чтения живых записей.
PROMPT_VERSION = "4"

PROMPT = """Ты разбираешь записи риелтора в карточке сделки и достаёшь из них факты.

Верни ТОЛЬКО JSON без пояснений, с полями:

{
  "promised": "что риелтор обещал сделать, его словами, коротко",
  "promised_at": "YYYY-MM-DD или null — к какому дню обещал",
  "wait_until": "YYYY-MM-DD или null — до какого дня договорились не беспокоить",
  "refused": true/false,
  "refused_why": "чем именно клиент отказал, коротко",
  "ready": "что готово к следующему шагу, коротко",
  "terms": "комиссия, цена, задаток — если названы"
}

Правила.

Обещание — только то, что риелтор собирался сделать САМ и ещё НЕ сделал:
«позвонить в пятницу», «согласовать просмотр на среду», «подготовлю
объект». Не считай обещанием то, что должен сделать клиент.

Прошедшее время — не обещание. «Показывала квартиру 15.08», «был показ
15.06», «провели фотосессию» — это уже сделано, и дата в них означает день
события, а не срок. Такое идёт в ready, а promised_at остаётся null.

Состояние — тоже не обещание. «В рекламе», «в рекламу», «работаю с ним»,
«в работе» описывают положение дел, а не намеченное действие. Обещание
всегда содержит глагол того, что риелтор СДЕЛАЕТ: позвонить, съездить,
подготовить, согласовать.

Даты приводи к календарю. Тебе дана дата записи и сегодняшняя дата. «В
пятницу» — ближайшая пятница ПОСЛЕ даты записи. «Середина августа» —
15 августа того года, в котором запись. «В конце осени» — 30 ноября. Год
бери из даты записи; если срок явно перешёл на следующий год, увеличь год.

promised_at и wait_until — разное, и различает их ТОТ, КТО ДЕЙСТВУЕТ.

Дата, к которой привязано действие риелтора, — это promised_at, как бы
фраза ни начиналась. «Договорились созвониться в пятницу», «свяжемся в
конце месяца», «созвонимся в конце осени» — обещания: день назван, и в
этот день звонить нам. Слово «договорились» ничего не меняет: звонок от
этого не перестаёт быть нашим.

wait_until — только пауза, внутри которой риелтору делать НЕЧЕГО, потому
что ход не за ним: «собственник в отпуске, показы с 26 августа», «вернётся
к поиску, когда продаст свою», «ждёт решения банка до 15-го». Действия
здесь нет — есть чужое обстоятельство, которого мы ждём.

Названо и то и другое — заполняй обещание. До названного дня карточку
никто не тронет и без wait_until: раньше срока обещание не просрочено.

refused — клиент не хочет, чтобы агентство занималось объектом ДАЛЬШЕ: не
планирует продавать, снял с продажи, отказался от эксклюзива, не хочет
договор.

ОГРАНИЧЕНИЕ — НЕ ОТКАЗ. «Есть свой агент, но показывать можем приводить»,
«рекламирует другое агентство, но взаимодействуем напрямую», «показ только
через представителя», «просмотр только с покупателем» — работа идёт, просто
на своих условиях. Это не refused; способ работы опиши в ready.

Читай фразу до конца. Половина записей устроена как «нет, НО да»: первая
половина звучит отказом, а вторая говорит, что сотрудничество есть.
Решает вторая.

Торг — тоже не отказ: «не хотят опускать цену», «больше 2% не платит» —
позиция в переговорах, её место в terms.

Одно и то же не повторяй в двух полях. terms — только про деньги и цифры:
комиссия, цена, задаток, метраж. «Принят задаток» как факт работы идёт в
ready, а «задаток 2 млн» — в terms.

Пустое поле — пустая строка, отсутствующая дата — null. Не выдумывай
ничего, чего нет в тексте: пустой ответ полезнее придуманного. Записи вида
«На связи», «Общаемся», «в работе» фактов не несут — верни всё пустым.

Примеры.

Записи: «На связи» · «Общаемся»
{"promised": "", "promised_at": null, "wait_until": null, "refused": false,
 "refused_why": "", "ready": "", "terms": ""}

Запись от 2026-08-10: «Позвонить в пятницу, согласовать фотосессию.
Готов платить комиссию 3%»
{"promised": "Позвонить, согласовать фотосессию", "promised_at": "2026-08-14",
 "wait_until": null, "refused": false, "refused_why": "", "ready": "",
 "terms": "комиссия 3%"}

Запись от 2026-07-31: «Договорились созвониться в конце осени, будем
сотрудничать»
{"promised": "Созвониться", "promised_at": "2026-11-30", "wait_until": null,
 "refused": false, "refused_why": "", "ready": "", "terms": ""}

Запись от 2026-08-25: «Связалась, они в отпуске, показы с 26 августа»
{"promised": "", "promised_at": null, "wait_until": "2026-08-26",
 "refused": false, "refused_why": "", "ready": "", "terms": ""}

Запись от 2026-08-17: «ПОКАЗЫВАЛА КВАРТИРУ 15.08. Больше 2% не платит,
эксклюзив не хочет категорически»
{"promised": "", "promised_at": null, "wait_until": null, "refused": true,
 "refused_why": "не хочет эксклюзив", "ready": "показ состоялся 15.08",
 "terms": "не платит больше 2%"}

Запись: «у неё есть свой агент, но в случае необходимости показа можем к ней
приводить»
{"promised": "", "promised_at": null, "wait_until": null, "refused": false,
 "refused_why": "", "ready": "показ через собственника, у неё свой агент",
 "terms": ""}

Запись: «в рекламе, работаю с ним»
{"promised": "", "promised_at": null, "wait_until": null, "refused": false,
 "refused_why": "", "ready": "объект в рекламе", "terms": ""}"""


def _hash(rows: list[dict[str, Any]]) -> str:
    """Отпечаток записей карточки: по нему видно, что читать заново нечего."""
    raw = "|".join(f"{row['comment_id']}:{row['created_at']}" for row in rows)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _targets(conn, limit: int) -> list[dict[str, Any]]:
    """Открытые карточки с записями, которых ещё не читали или уже неверно.

    Сравнение отпечатков делается в Python, а не в SQL: считать хеш умеет
    только вызывающий, и запрос всё равно вернул бы все карточки.
    """
    rows = conn.execute(
        """
        SELECT d.deal_id, d.title, COALESCE(u.name, '') AS broker,
               r.source_hash AS known,
               COALESCE(r.prompt_version, '') AS version
        FROM fact_deal d
        LEFT JOIN dim_user u ON u.user_id = d.assigned_by_id
        LEFT JOIN fact_comment_read r
               ON r.entity_type = 'deal' AND r.entity_id = d.deal_id
        WHERE d.is_deleted = 0 AND d.is_closed = 0
          AND EXISTS (SELECT 1 FROM fact_comment c
                       WHERE c.entity_type = 'deal' AND c.entity_id = d.deal_id
                         AND c.is_auto = 0)
        ORDER BY COALESCE(r.read_at, '') ASC
        """
    ).fetchall()
    out = []
    for row in rows:
        card = dict(row)
        card["notes"] = [dict(note) for note in conn.execute(
            """
            SELECT comment_id, created_at, body FROM fact_comment
            WHERE entity_type = 'deal' AND entity_id = ? AND is_auto = 0
            ORDER BY created_at DESC LIMIT ?
            """,
            (card["deal_id"], NOTES),
        )]
        if not card["notes"]:
            continue
        card["hash"] = _hash(card["notes"])
        if card["hash"] == card["known"] and card["version"] == PROMPT_VERSION:
            continue
        out.append(card)
        if len(out) >= limit:
            break
    return out


def _payload(card: dict[str, Any], today: date) -> str:
    lines = [f"Сегодня: {today.isoformat()}", f"Карточка: {card['title']}", "",
             "Записи, от новых к старым:"]
    for note in card["notes"]:
        lines.append(f"[{note['created_at'][:10]}] {note['body']}")
    return "\n".join(lines)


_JSON = re.compile(r"\{.*\}", re.S)


def _parse(text: str) -> dict[str, Any] | None:
    match = _JSON.search(text or "")
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _day(value: Any) -> str | None:
    """Дата из ответа модели или None.

    Модель просили вернуть календарный день, но она возвращает и «через
    неделю», и пустую строку, и 2026-13-45. Всё, что не разбирается в
    настоящую дату, — не дата: пустое поле честнее выдуманного дня, по
    которому назавтра поднимут человека.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10]).isoformat()
    except ValueError:
        return None


def _text(value: Any, limit: int = 300) -> str:
    return " ".join(str(value or "").split())[:limit]


def _clients(settings: Any) -> tuple[Any, Any | None]:
    """Основной клиент и запасной — ровно как у аудитора.

    Читатель звал make_llm(settings) без аргументов, и это тихо стоило
    вдвое. Тариф и закреплённый провайдер приходят в make_llm аргументами,
    а не из настроек, поэтому голый вызов означал «обычная цена, любой
    провайдер» — при том что в .env стоит flex, и аудитор им пользуется.

    Провайдер важнее тарифа. Неявный кэш живёт У ПРОВАЙДЕРА, а у читателя
    постоянная часть запроса — это ВЕСЬ промпт, переменная же — четыре
    коротких записи. Ни у одной другой нашей задачи доля прогретого
    префикса не бывает так высока, и ни одна другая так не проигрывает от
    того, что RouterAI раскидывает запросы по провайдерам.

    Запасной клиент — тот же провайдер, обычный тариф: flex обещает при
    нехватке мощностей вернуть ошибку, и половина цены куплена риском не
    получить ответ. Аудитор платит этот риск повтором; читателю достаётся
    тот же приём.
    """
    tier = (settings.llm_service_tier or "").strip()
    pinned = (settings.llm_provider or "").strip()
    model = make_llm(settings, service_tier=tier, provider=pinned)
    spare = make_llm(settings, provider=pinned) if tier else None
    return model, spare


def _ask(llm: Any, spare: Any | None, card: dict[str, Any], today: date) -> Any:
    """Спросить про одну карточку: дешёвым клиентом, при отказе — обычным."""
    messages = [SystemMessage(content=PROMPT),
                HumanMessage(content=_payload(card, today))]
    try:
        return llm.invoke(messages)
    except Exception as error:  # noqa: BLE001 — отказ flex неотличим от аварии
        if spare is None:
            raise
        logger.info("Карточка %s: повтор в обычном режиме (%s)",
                    card["deal_id"], error)
        return spare.invoke(messages)


def read_cards(conn, llm, *, limit: int = BATCH, today: date | None = None,
               spare: Any | None = None, workers: int = WORKERS,
               usage: dict[str, int] | None = None) -> int:
    """Прочитать очередную партию карточек. Возвращает число прочитанных.

    Спрашиваем несколько карточек сразу, а пишем по одной и здесь: очередь
    карточек собрана заранее и в базу ходит только этот поток, так что
    соединение SQLite остаётся там же, где было создано.
    """
    now = datetime.now(timezone.utc).isoformat()
    today = today or datetime.now(timezone.utc).date()
    cards = _targets(conn, limit)
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        pending = {pool.submit(_ask, llm, spare, card, today): card
                   for card in cards}
        for future in as_completed(pending):
            card = pending[future]
            try:
                response = future.result()
            except Exception as error:  # noqa: BLE001 — одна карточка не роняет партию
                logger.warning("Карточка %s не прочитана: %s",
                               card["deal_id"], error)
                continue
            # Токены считаем до разбора ответа: за нечитаемый ответ уже
            # заплачено, и прятать его из счёта значит занижать цену
            # прогона ровно на самых неудачных карточках.
            if usage is not None:
                for key, value in extract_usage(response).items():
                    usage[key] = usage.get(key, 0) + value
            content = getattr(response, "content", response)
            data = _parse(content if isinstance(content, str) else str(content))
            if data is None:
                logger.warning("Карточка %s: ответ не разобран", card["deal_id"])
                continue
            conn.execute(
                """
                INSERT INTO fact_comment_read(entity_type, entity_id, source_hash,
                    promised, promised_at, wait_until, refused, refused_why,
                    ready, terms, read_at, prompt_version)
                VALUES ('deal', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(entity_type, entity_id) DO UPDATE SET
                    source_hash=excluded.source_hash, promised=excluded.promised,
                    promised_at=excluded.promised_at, wait_until=excluded.wait_until,
                    refused=excluded.refused, refused_why=excluded.refused_why,
                    ready=excluded.ready, terms=excluded.terms,
                    read_at=excluded.read_at, prompt_version=excluded.prompt_version
                """,
                (card["deal_id"], card["hash"], _text(data.get("promised")),
                 _day(data.get("promised_at")), _day(data.get("wait_until")),
                 1 if data.get("refused") else 0, _text(data.get("refused_why")),
                 _text(data.get("ready")), _text(data.get("terms")), now,
                 PROMPT_VERSION),
            )
            done += 1
    logger.info("Прочитано карточек: %s", done)
    return done


def _report(done: int, usage: dict[str, int], settings: Any,
            seconds: float) -> None:
    """Чем кончился прогон: время, рубли, куда ушли и то и другое.

    Без этой строки вопрос «дорого ли и долго ли читать тысячу карточек»
    решается голосованием. С ней он решается делением: секунды на карточку
    говорят, хватит ли ночи, доля размышлений в выходе — сколько мы платим
    за раздумья над задачей, где думать не над чем, доля кэша во входе —
    попадаем ли мы в прогретый префикс. Три числа, по которым видно, что
    менять: настройки, промпт или всё-таки модель.
    """
    cost = estimate_cost(usage, settings)
    logger.info(
        "Чтение: %d карточек за %.0f с (%.1f с/карточка), %.2f ₽ "
        "(%.3f ₽/карточка) · вход %d (%d из кэша, %.0f%%) · "
        "выход %d (%d размышления, %.0f%%)",
        done, seconds, seconds / done if done else 0.0,
        cost, cost / done if done else 0.0,
        usage["input_tokens"], usage["cached_tokens"],
        100.0 * usage["cached_tokens"] / usage["input_tokens"]
        if usage["input_tokens"] else 0.0,
        usage["output_tokens"], usage["reasoning_tokens"],
        100.0 * usage["reasoning_tokens"] / usage["output_tokens"]
        if usage["output_tokens"] else 0.0,
    )


def _raw(conn, llm, spare, limit: int, today: date) -> None:
    """Сырой ответ модели рядом с тем, во что он обошёлся.

    Счёт говорит: 640 токенов выхода на карточку при ответе строк на
    восемьдесят. Разница либо размышления, которых провайдер не показывает
    отдельно, либо пояснения вокруг JSON, которых мы просили не писать.
    Разница между этими двумя случаями большая: первое лечится только
    сменой модели, второе — одной строкой промпта.

    Заодно видно, чем рискует разбор. Из ответа мы вырезаем JSON жадным
    поиском от первой скобки до последней, и болтливая модель однажды
    подсунет туда лишнюю пару.

    В витрину не пишем: проба не должна менять состояние.
    """
    for card in _targets(conn, limit):
        response = _ask(llm, spare, card, today)
        content = getattr(response, "content", response)
        content = content if isinstance(content, str) else str(content)
        usage = extract_usage(response)
        print("=" * 62)
        print(f"Сделка {card['deal_id']} · {card['title']}")
        print(f"  выход {usage['output_tokens']} токенов, "
              f"из них размышления {usage['reasoning_tokens']}; "
              f"в ответе {len(content)} знаков")
        print("  ── ответ целиком ──")
        for line in content.splitlines():
            print(f"  {line}")
        print()


def _show(conn, limit: int) -> None:
    """Что модель вынула, рядом с тем, из чего вынимала.

    Без этого прочитанное проверить нечем: в таблице лежат аккуратные поля,
    и на вид они правдоподобны всегда. Ошибку видно только рядом с исходной
    записью — «показывала 15.08» превращённое в обещание выглядит идеально,
    пока не увидишь, что это прошедшее время.
    """
    rows = conn.execute(
        """
        SELECT r.entity_id, d.title, r.promised, r.promised_at, r.wait_until,
               r.refused, r.refused_why, r.ready, r.terms, r.read_at
        FROM fact_comment_read r
        JOIN fact_deal d ON d.deal_id = r.entity_id
        ORDER BY r.read_at DESC, r.entity_id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    if not rows:
        print("Прочитанных карточек нет — сначала запустите без --show")
        return
    print(f"Последние {len(rows)} прочитанных\n")
    for row in rows:
        print("=" * 62)
        print(f"Сделка {row['entity_id']} · {row['title']}")
        print("  Записи:")
        for note in conn.execute(
            "SELECT created_at, body FROM fact_comment WHERE entity_type = 'deal'"
            " AND entity_id = ? AND is_auto = 0 ORDER BY created_at DESC LIMIT ?",
            (row["entity_id"], NOTES),
        ):
            print(f"    [{note['created_at'][:10]}] {note['body'][:150]}")
        print("  Вынуто:")
        for label, value in (
            ("обещание", row["promised"]),
            ("срок обещания", row["promised_at"]),
            ("ждём до", row["wait_until"]),
            ("отказ", row["refused_why"] if row["refused"] else ""),
            ("готово", row["ready"]),
            ("условия", row["terms"]),
        ):
            if value:
                print(f"    {label}: {value}")
        if not any((row["promised"], row["promised_at"], row["wait_until"],
                    row["refused"], row["ready"], row["terms"])):
            print("    (пусто)")
        print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Чтение комментариев карточек")
    parser.add_argument("--limit", type=int, default=BATCH)
    parser.add_argument("--dry-run", action="store_true",
                        help="показать, что уйдёт модели, и не звать её")
    parser.add_argument("--workers", type=int, default=WORKERS,
                        help="сколько карточек спрашивать одновременно")
    parser.add_argument("--raw", type=int, metavar="N",
                        help="прочитать N карточек и показать сырой "
                             "ответ модели целиком, ничего не записывая")
    parser.add_argument("--show", type=int, metavar="N",
                        help="показать N последних прочитанных карточек: "
                             "исходные записи рядом с тем, что вынула модель")
    args = parser.parse_args()
    # Настройки сначала: уровень журнала берётся из них, как во всех
    # остальных точках входа проекта.
    settings = get_settings()
    setup_logging(settings.log_level)
    # Схема приводится в порядок до работы, как это делают ETL и дашборд.
    # Читатель открывал витрину напрямую и падал на колонке, которой ещё нет:
    # миграция живёт в init_analytics_db(), а он её не звал — точка входа,
    # работающая с витриной, обязана сначала убедиться, что схема на месте.
    init_analytics_db()

    with analytics_session() as conn:
        if args.show:
            _show(conn, args.show)
            return 0
        if args.dry_run:
            # Очередь целиком, а не партия: «120 карточек» при лимите 120
            # не отвечает на вопрос, сколько их всего и на сколько
            # запусков растянется первый проход.
            queue = _targets(conn, 10_000)
            print(f"К прочтению: {len(queue)} карточек "
                  f"(за запуск читается до {args.limit})\n")
            cards = queue[:args.limit]
            for card in cards[:3]:
                print("=" * 60)
                print(_payload(card, datetime.now(timezone.utc).date()))
                print()
            return 0
        model, spare = _clients(settings)
        if args.raw:
            _raw(conn, model, spare, args.raw,
                 datetime.now(timezone.utc).date())
            return 0
        usage = {key: 0 for key in USAGE_KEYS}
        started = time.monotonic()
        done = read_cards(conn, model, limit=args.limit, spare=spare,
                          workers=args.workers, usage=usage)
        _report(done, usage, settings, time.monotonic() - started)
        # Остаток очереди — не любопытство, а единственный способ увидеть,
        # что ночной лимит стал узким местом. Прочитано ровно столько,
        # сколько разрешено, — и с виду прогон удачен, а разбор при этом
        # отстаёт на день, потом на два, и советы тихо стареют.
        left = _targets(conn, 10_000)
        if left:
            logger.warning(
                "В очереди осталось %s карточек: партия мала или "
                "карточки не прочитались", len(left),
            )
        # Ничего не прочитали, а читать было что — это авария, и она обязана
        # быть слышна. По крону скрипт молча вернул бы ноль, советы неделю
        # опирались бы на устаревший разбор, и заметили бы это по тому, что
        # утренняя сводка перестала называть новые карточки. Ненулевой код
        # поднимает штатный алерт из cron_job.sh.
        if done == 0 and left:
            logger.error("Ни одной карточки не прочитано, а очередь не пуста")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
