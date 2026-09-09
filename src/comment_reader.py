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
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent
for _path in (_SRC, _SRC / "analytics"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402

from config import get_settings, setup_logging  # noqa: E402
from llm import make_llm  # noqa: E402
from schema import analytics_session  # noqa: E402

logger = logging.getLogger(__name__)

# Сколько карточек читать за один запуск. Первый проход по всему портфелю
# растянется на несколько запусков — и пусть: разбор не срочный, а тысяча
# обращений подряд стоит денег и времени сразу.
BATCH = 120

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
# 3: ограничение отделено от отказа («есть свой агент, НО показывать можем»
#    считалось отказом), состояние — от обещания («в рекламу»).
# 2: прошедшее время не обещание, торг не отказ, записи-пустышки пусты.
# 1: первая редакция, написана до чтения живых записей.
PROMPT_VERSION = "3"

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

promised_at и wait_until — разное. Первое: риелтор обязался действовать к
этому дню. Второе: договорились ЖДАТЬ до этого дня, и молчать до него
правильно. «Созвонимся в конце осени» — это wait_until, не обещание.

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
{"promised": "", "promised_at": null, "wait_until": "2026-11-30",
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


def read_cards(conn, llm, *, limit: int = BATCH, today: date | None = None) -> int:
    """Прочитать очередную партию карточек. Возвращает число прочитанных."""
    now = datetime.now(timezone.utc).isoformat()
    today = today or datetime.now(timezone.utc).date()
    done = 0
    for card in _targets(conn, limit):
        try:
            response = llm.invoke([
                SystemMessage(content=PROMPT),
                HumanMessage(content=_payload(card, today)),
            ])
        except Exception as error:
            logger.warning("Карточка %s не прочитана: %s", card["deal_id"], error)
            continue
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
    parser.add_argument("--show", type=int, metavar="N",
                        help="показать N последних прочитанных карточек: "
                             "исходные записи рядом с тем, что вынула модель")
    args = parser.parse_args()
    # Настройки сначала: уровень журнала берётся из них, как во всех
    # остальных точках входа проекта.
    settings = get_settings()
    setup_logging(settings.log_level)

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
        read_cards(conn, make_llm(settings), limit=args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
