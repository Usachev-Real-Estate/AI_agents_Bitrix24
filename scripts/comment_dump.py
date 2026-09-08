"""Выгрузка комментариев карточек для разбора человеком или моделью.

Витрина умеет считать, что запись есть. Что в ней написано — не считается
ничем: «ушли думать, подбираю ещё объекты», «в середине июля будет известен
бонус», «показ может организовать 10 сентября после 19:00». Это причина
отказа, следующий шаг и срок, и ни одно поле портала их не хранит.

Скрипт достаёт эти тексты из витрины (в портал не ходит) и печатает их
карточками, в порядке от самых давно не тронутых. Прочитать их — работа
отдельная: сначала руками, чтобы понять, что там вообще есть, потом
моделью по тому же образцу.

Телефоны и почта закрываются. Всё остальное остаётся: имя клиента и адрес
объекта — это и есть содержание записи, и вычеркнув их, разбирать станет
нечего. Тот же принцип, что у masking.py, только короче — там маска нужна
двусторонняя, здесь текст никуда не возвращается.

Запуск:

    docker run --rm -v $(pwd)/data:/app/data \\
      -v /tmp/comment_dump.py:/app/comment_dump.py --env-file .env \\
      b24-ai-auditor:latest python /app/comment_dump.py --funnel 0 --cards 25
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

for _root in (Path(__file__).resolve().parent.parent, Path.cwd(), Path("/app")):
    _src = _root / "src"
    if (_src / "analytics" / "schema.py").exists():
        for _path in (_src, _src / "analytics"):
            if str(_path) not in sys.path:
                sys.path.insert(0, str(_path))
        break
else:  # pragma: no cover — на сервере каталог есть всегда
    raise SystemExit("не найден каталог src: запускайте из корня проекта")

from schema import get_connection  # noqa: E402

_PHONE = re.compile(r"(?<!\d)(?:\+?\d[\s\-()]?){10,14}(?!\d)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def hide(text: str) -> str:
    """Закрыть телефон и почту, оставив смысл записи нетронутым.

    Разделитель после последней цифры возвращается на место: шаблон
    телефона его съедает, и «тел. +7999… объявление» слипалось в
    «[телефон]объявление» — мелочь, но читать такой текст будет модель, и
    склеенные слова она разберёт хуже.
    """
    text = _EMAIL.sub("[почта]", text or "")
    return _PHONE.sub(lambda m: "[телефон]" + m.group(0)[len(m.group(0).rstrip(" -()")):],
                      text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--funnel", type=int, default=0,
                        help="воронка: 0 продавцы, 18 покупатели")
    parser.add_argument("--cards", type=int, default=25,
                        help="сколько карточек выгрузить")
    parser.add_argument("--auto", action="store_true",
                        help="показывать и роботные записи (по умолчанию нет)")
    args = parser.parse_args()

    conn = get_connection(readonly=True)
    auto = "" if args.auto else "AND c.is_auto = 0"
    cards = conn.execute(
        f"""
        SELECT d.deal_id, d.title, d.stage_id,
               COALESCE(s.name, d.stage_id) AS stage,
               COALESCE(u.name, '') AS broker,
               COUNT(c.comment_id) AS notes,
               MAX(c.created_at) AS last_note,
               ROUND(julianday('now') - julianday(MAX(c.created_at))) AS quiet
        FROM fact_deal d
        LEFT JOIN dim_stage s
               ON s.stage_id = d.stage_id AND s.category_id = d.category_id
        LEFT JOIN dim_user u ON u.user_id = d.assigned_by_id
        JOIN fact_comment c
             ON c.entity_type = 'deal' AND c.entity_id = d.deal_id {auto}
        WHERE d.category_id = :cat AND d.is_deleted = 0 AND d.is_closed = 0
        GROUP BY d.deal_id
        ORDER BY quiet DESC
        LIMIT :cards
        """,
        {"cat": args.funnel, "cards": args.cards},
    ).fetchall()

    print(f"# Воронка {args.funnel}: {len(cards)} карточек, "
          "от самых давно не тронутых\n")
    for card in cards:
        print(f"## Сделка {card['deal_id']} · {card['title']}")
        print(f"   стадия «{card['stage']}» · {card['broker']} · "
              f"молчит {int(card['quiet'] or 0)} дн · записей {card['notes']}")
        for note in conn.execute(
            f"""
            SELECT created_at, body FROM fact_comment
            WHERE entity_type = 'deal' AND entity_id = ? {auto.replace('c.', '')}
            ORDER BY created_at
            """,
            (card["deal_id"],),
        ):
            print(f"   [{note['created_at'][:10]}] {hide(note['body'])}")
        print()
    conn.close()


if __name__ == "__main__":
    main()
