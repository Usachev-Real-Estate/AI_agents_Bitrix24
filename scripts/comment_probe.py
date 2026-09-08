"""Замер комментариев в таймлайне карточек перед их заливкой в витрину.

Комментарий брокера — это то, чего нет ни в одном другом источнике.
Звонок говорит «был контакт», стадия — «карточка сдвинулась», а комментарий
говорит, ЧТО с клиентом: почему не покупает, чего ждёт, когда вернуться.
Ради этого понимания замер и делается.

ОГРАНИЧЕНИЕ МЕТОДА, из-за которого замер обязателен. У
crm.timeline.comment.list фильтр по ENTITY_ID обязателен: одним запросом
все комментарии портала не забрать, только по одной карточке за раз.
Значит стоимость загрузки — это число карточек, а не число комментариев, и
прежде чем строить ETL, надо знать: сколько карточек, сколько на каждой
комментариев и сколько в них текста. От этих трёх чисел зависит и время
полного прогона, и то, можно ли вообще держать историю свежей.

Скрипт ТОЛЬКО ЧИТАЕТ и работает по выборке, а не по всему порталу: на
образце в несколько десятков карточек видно и среднее, и разброс, а полный
обход как раз и есть то решение, которое замер должен обосновать.

Запуск:

    git fetch origin claude/leadership-dashboard-ih3sjz
    git show origin/claude/leadership-dashboard-ih3sjz:scripts/comment_probe.py \\
        > /tmp/comment_probe.py
    docker run --rm -v $(pwd)/data:/app/data \\
      -v /tmp/comment_probe.py:/app/comment_probe.py --env-file .env \\
      b24-ai-auditor:latest python /app/comment_probe.py
"""

from __future__ import annotations

import re
import sys
import time
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

from client import BitrixClient  # noqa: E402
from config import get_settings  # noqa: E402
from schema import get_connection  # noqa: E402

# Сколько карточек взять в образец. Полсотни хватает, чтобы увидеть разброс,
# и стоит полсотни запросов — столько же, сколько стоило бы любопытство.
SAMPLE = 50

# Воронки, по которым берётся образец: продавцы и покупатели.
FUNNELS = ((0, "Продавцы"), (18, "Покупатели"))


def head(text: str) -> None:
    print(f"\n{'=' * 66}\n{text}\n{'=' * 66}")


def plain(html: str) -> str:
    """Комментарий приходит с разметкой BB-кодов и переводами строк."""
    text = re.sub(r"\[/?[^\]]{1,40}\]", " ", html or "")
    return re.sub(r"\s+", " ", text).strip()


def sample_deals(conn, category: int, limit: int) -> list[dict]:
    """Открытые карточки воронки, самые свежие по изменению.

    Свежие, а не случайные: комментарий пишут по ходу работы, и на давно
    брошенной карточке его не будет по определению — образец из таких
    ответил бы на вопрос «пишут ли вообще» словом «нет» независимо от
    того, как обстоит дело.
    """
    rows = conn.execute(
        """
        SELECT deal_id, title, assigned_by_id, date_modify
        FROM fact_deal
        WHERE category_id = ? AND is_deleted = 0 AND is_closed = 0
        ORDER BY date_modify DESC LIMIT ?
        """,
        (category, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def main() -> None:
    settings = get_settings()
    conn = get_connection(readonly=True)
    started = time.monotonic()

    with BitrixClient(settings.b24_webhook_url) as client:
        for category, name in FUNNELS:
            deals = sample_deals(conn, category, SAMPLE)
            head(f"{name}: образец из {len(deals)} свежих открытых карточек")
            if not deals:
                print("  карточек нет")
                continue

            counts, lengths, samples, authors = [], [], [], {}
            failures = 0
            for deal in deals:
                try:
                    rows = client.call("crm.timeline.comment.list", {
                        "filter": {"ENTITY_ID": deal["deal_id"],
                                   "ENTITY_TYPE": "deal"},
                        "select": ["ID", "CREATED", "AUTHOR_ID", "COMMENT"],
                    }) or []
                    failures = 0
                except Exception as error:  # pragma: no cover — портал ответил
                    print(f"  сделка {deal['deal_id']}: {error}")
                    # Три отказа подряд — это не невезение, а отсутствие
                    # права на метод. Каждый вызов клиент повторяет
                    # четырежды, и упрямство обошлось бы в две сотни
                    # бесполезных запросов и десять минут ожидания.
                    failures += 1
                    if failures >= 3:
                        print("  три отказа подряд — проверьте право crm "
                              "у вебхука; образец прерван")
                        break
                    continue
                counts.append(len(rows))
                for row in rows:
                    text = plain(row.get("COMMENT"))
                    if text:
                        lengths.append(len(text))
                        authors[str(row.get("AUTHOR_ID"))] = (
                            authors.get(str(row.get("AUTHOR_ID")), 0) + 1)
                    if len(samples) < 12 and len(text) > 40:
                        samples.append((deal["deal_id"], row.get("CREATED", "")[:10],
                                        text[:220]))

            total = sum(counts)
            with_any = sum(1 for n in counts if n)
            print(f"  комментариев всего:        {total}")
            print(f"  карточек с комментариями:  {with_any} из {len(counts)}")
            if counts:
                ordered = sorted(counts)
                print(f"  на карточку: медиана {ordered[len(ordered) // 2]}, "
                      f"максимум {ordered[-1]}")
            if lengths:
                ordered = sorted(lengths)
                print(f"  длина текста: медиана {ordered[len(ordered) // 2]} симв., "
                      f"максимум {ordered[-1]}")
            print(f"  разных авторов:            {len(authors)}")

            if samples:
                print("\n  Живые комментарии:")
                for deal_id, when, text in samples:
                    print(f"    [{when}] сделка {deal_id}: {text}")

    head("Во что это обойдётся")
    spent = time.monotonic() - started
    per_call = spent / max(client.request_count, 1)
    row = conn.execute(
        "SELECT COUNT(*) n FROM fact_deal WHERE is_deleted = 0 AND is_closed = 0"
    ).fetchone()
    open_deals = row["n"]
    print(f"  запросов сделано:      {client.request_count} за {spent:.0f} с "
          f"({per_call:.2f} с на запрос)")
    print(f"  открытых карточек:     {open_deals}")
    print(f"  полный обход открытых: ~{open_deals * per_call / 60:.0f} мин")
    print("\n  Фильтр по ENTITY_ID у метода обязателен: одним запросом все")
    print("  комментарии не забрать, стоимость загрузки — это число карточек.")
    conn.close()


if __name__ == "__main__":
    main()
