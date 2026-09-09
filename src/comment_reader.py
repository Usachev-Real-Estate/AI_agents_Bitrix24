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

Обещание — только то, что риелтор собирался сделать САМ: «позвонить в
пятницу», «согласовать просмотр на среду», «подготовлю объект». Не считай
обещанием то, что должен сделать клиент.

Даты приводи к календарю. Тебе дана дата записи и сегодняшняя дата. «В
пятницу» — ближайшая пятница ПОСЛЕ даты записи. «Середина августа» —
15 августа того года, в котором запись. «В конце осени» — 30 ноября. Год
бери из даты записи; если срок явно перешёл на следующий год, увеличь год.

promised_at и wait_until — разное. Первое: риелтор обязался действовать к
этому дню. Второе: договорились ЖДАТЬ до этого дня, и молчать до него
правильно. «Созвонимся в конце осени» — это wait_until, не обещание.

refused — клиент отказался работать или отказал в условиях: свой риелтор,
не хочет договор, не планирует продавать. Не ставь refused, если клиент
просто думает или не отвечает.

Пустое поле — пустая строка, отсутствующая дата — null. Не выдумывай
ничего, чего нет в тексте: пустой ответ полезнее придуманного."""


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
               r.source_hash AS known
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
        if card["hash"] == card["known"]:
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
                ready, terms, read_at)
            VALUES ('deal', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(entity_type, entity_id) DO UPDATE SET
                source_hash=excluded.source_hash, promised=excluded.promised,
                promised_at=excluded.promised_at, wait_until=excluded.wait_until,
                refused=excluded.refused, refused_why=excluded.refused_why,
                ready=excluded.ready, terms=excluded.terms, read_at=excluded.read_at
            """,
            (card["deal_id"], card["hash"], _text(data.get("promised")),
             _day(data.get("promised_at")), _day(data.get("wait_until")),
             1 if data.get("refused") else 0, _text(data.get("refused_why")),
             _text(data.get("ready")), _text(data.get("terms")), now),
        )
        done += 1
    logger.info("Прочитано карточек: %s", done)
    return done


def main() -> int:
    parser = argparse.ArgumentParser(description="Чтение комментариев карточек")
    parser.add_argument("--limit", type=int, default=BATCH)
    parser.add_argument("--dry-run", action="store_true",
                        help="показать, что уйдёт модели, и не звать её")
    args = parser.parse_args()
    # Настройки сначала: уровень журнала берётся из них, как во всех
    # остальных точках входа проекта.
    settings = get_settings()
    setup_logging(settings.log_level)

    with analytics_session() as conn:
        if args.dry_run:
            cards = _targets(conn, args.limit)
            print(f"К прочтению: {len(cards)} карточек\n")
            for card in cards[:3]:
                print("=" * 60)
                print(_payload(card, datetime.now(timezone.utc).date()))
                print()
            return 0
        read_cards(conn, make_llm(settings), limit=args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
