"""Выгрузка разбора комментариев: что модель вынула и из чего.

Проверять разбор по одной карточке через `--show` можно, пока карточек
двадцать. Когда прочитан весь портфель, нужен другой вид: сводка на
экран, чтобы увидеть форму целиком, и CSV, чтобы посмотреть глазами
подозрительные строки.

Сводка отвечает на вопрос «сработал ли промпт»: сколько обещаний со
сроком против обещаний без срока, сколько пауз, сколько отказов. Резкий
перекос в любую сторону — это ошибка разбора, а не свойство портфеля.

CSV кладёт рядом вынутое и исходные записи. Без исходника проверить
нечего: в таблице лежат аккуратные поля, и на вид они правдоподобны
всегда — ошибку видно только рядом с текстом, из которого её достали.

Телефоны и почта закрываются. Имя клиента и адрес объекта остаются: это и
есть содержание записи, вычеркнув их, разбирать станет нечего.

Скрипт намеренно самодостаточен — ничего из src, кроме schema. Пробы у
нас запускаются одним подмонтированным файлом поверх собранного образа, и
любая новая зависимость требует монтировать ещё и её.

Запуск:

    docker run --rm -v $(pwd)/data:/app/data \\
      -v /tmp/comment_export.py:/app/comment_export.py --env-file .env \\
      b24-ai-auditor:latest python /app/comment_export.py
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Any

for _root in (Path(__file__).resolve().parent.parent, Path.cwd(), Path("/app")):
    _src = _root / "src"
    if (_src / "analytics" / "schema.py").exists():
        for _path in (_src, _src / "analytics"):
            if str(_path) not in sys.path:
                sys.path.insert(0, str(_path))
        break
else:  # pragma: no cover — на сервере каталог есть всегда
    raise SystemExit("не найден каталог src: запускайте из корня проекта")

from config import get_settings  # noqa: E402
from plans import plan_category_ids  # noqa: E402
from scope import Scope, scoped_session  # noqa: E402
from work import promises as overdue_promises  # noqa: E402

_PHONE = re.compile(r"(?<!\d)(?:\+?\d[\s\-()]?){10,14}(?!\d)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")

COLUMNS = (
    "deal_id", "funnel", "broker", "stage", "title", "quiet_days", "sign",
    "promised", "promised_at", "overdue_days", "wait_until",
    "refused", "refused_why", "ready", "terms",
    "prompt_version", "read_at", "notes",
)


def hide(text: str) -> str:
    """Закрыть телефон и почту, оставив смысл записи нетронутым."""
    text = _EMAIL.sub("[почта]", text or "")
    return _PHONE.sub(
        lambda m: "[телефон]" + m.group(0)[len(m.group(0).rstrip(" -()")):],
        text,
    )


def rows(conn, late: set[int]):
    """Разбор рядом с записями, из которых он сделан."""
    found = conn.execute(
        """
        SELECT r.entity_id AS deal_id, d.title,
               d.category_id AS funnel,
               COALESCE(u.name, '') AS broker,
               COALESCE(s.name, d.stage_id) AS stage,
               r.promised, r.promised_at, r.wait_until, r.refused,
               r.refused_why, r.ready, r.terms, r.prompt_version, r.read_at,
               ROUND(julianday('now')
                     - julianday(MAX(c.created_at))) AS quiet_days,
               ROUND(julianday('now')
                     - julianday(r.promised_at)) AS overdue_days
        FROM v_comment_read r
        JOIN v_deal d ON d.deal_id = r.entity_id
        LEFT JOIN v_user u ON u.user_id = d.assigned_by_id
        LEFT JOIN dim_stage s
               ON s.stage_id = d.stage_id AND s.category_id = d.category_id
        LEFT JOIN v_comment c
               ON c.entity_type = 'deal' AND c.entity_id = r.entity_id
              AND c.is_auto = 0
        WHERE r.entity_type = 'deal' AND d.is_closed = 0
        GROUP BY r.entity_id
        ORDER BY overdue_days DESC, quiet_days DESC
        """
    ).fetchall()
    out = []
    for row in found:
        card = dict(row)
        card["notes"] = " | ".join(
            f"[{note['created_at'][:10]}] {hide(note['body'])}"
            for note in conn.execute(
                """
                SELECT created_at, body FROM fact_comment
                WHERE entity_type = 'deal' AND entity_id = ? AND is_auto = 0
                ORDER BY created_at DESC LIMIT 4
                """,
                (card["deal_id"],),
            )
        )
        card["sign"] = _sign(card, late)
        out.append(card)
    return out


def _sign(card: dict, late: set[int]) -> str:
    """Чем эта карточка интересна — одним словом.

    Порядок важен: просроченное обещание сильнее всего остального, ради
    него разбор и делался. Отказ идёт раньше готовности, потому что
    карточка с отказом на живой стадии — это ошибка воронки, а не работа.

    Просрочку спрашиваем у work.promises() — у того же кода, из которого
    её берут советы. Здесь была своя копия правила, и она дважды разошлась
    с оригиналом: сперва не вычитала договорённое молчание, потом считала
    обещанием пустую фразу с датой. Выгрузка показывала 92 против 37 в
    сводке, и каждая починка была новым поводом разойтись снова.
    Диагностический счёт, спорящий с рабочим, хуже, чем никакой: по нему
    делают выводы, которых система не подтверждает.
    """
    if card["deal_id"] in late:
        return "просрочено"
    if card["promised"]:
        return "обещано"
    if card["refused"]:
        return "отказ"
    if card["wait_until"]:
        return "ждём"
    if card["ready"] or card["terms"]:
        return "есть факты"
    return "пусто"


def summary(cards: list[dict]) -> None:
    """Форма разбора целиком: перекос виден только на всём портфеле."""
    print(f"# Прочитано карточек: {len(cards)}\n")

    versions: dict[str, int] = {}
    signs: dict[str, int] = {}
    for card in cards:
        version = card["prompt_version"] or "(нет)"
        versions[version] = versions.get(version, 0) + 1
        signs[card["sign"]] = signs.get(card["sign"], 0) + 1

    print("## Версия промпта")
    for version, count in sorted(versions.items()):
        print(f"   v{version:<4} {count}")

    print("\n## Что вынуто")
    for sign in ("просрочено", "обещано", "отказ", "ждём", "есть факты", "пусто"):
        print(f"   {sign:<12} {signs.get(sign, 0)}")

    dated = sum(1 for c in cards if c["promised"] and c["promised_at"])
    undated = sum(1 for c in cards if c["promised"] and not c["promised_at"])
    print(f"\n   обещаний со сроком {dated}, без срока {undated}")

    late = [c for c in cards if c["sign"] == "просрочено"]
    if late:
        # Разрез по воронкам не украшение: советы смотрят только на
        # продавцов и воронки с планом, и без этой строки разница между
        # выгрузкой и сводкой выглядит расхождением, а не настройкой.
        by_funnel: dict[Any, int] = {}
        for card in late:
            by_funnel[card["funnel"]] = by_funnel.get(card["funnel"], 0) + 1
        parts = ", ".join(f"воронка {key}: {value}"
                          for key, value in sorted(by_funnel.items()))
        print(f"\n## Просрочено по воронкам\n   {parts}")

        print(f"\n## Просрочено по брокерам ({len(late)} карточек)")
        by_broker: dict[str, int] = {}
        for card in late:
            name = card["broker"] or "(не назначен)"
            by_broker[name] = by_broker.get(name, 0) + 1
        for name, count in sorted(by_broker.items(), key=lambda p: -p[1])[:15]:
            print(f"   {count:>4}  {name}")
        print("\n## Самое давнее просроченное")
        for card in late[:10]:
            print(f"   {int(card['overdue_days'])} дн · {card['deal_id']} · "
                  f"{card['broker']} · {card['promised'][:60]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="/app/data/export",
                        help="куда положить CSV")
    parser.add_argument("--only", default="all",
                        choices=("all", "просрочено", "обещано", "отказ",
                                 "ждём", "есть факты", "пусто"),
                        help="выгрузить только карточки этого вида")
    parser.add_argument("--limit", type=int, default=0,
                        help="ограничить выгрузку (0 — без ограничения)")
    args = parser.parse_args()

    # Область видимости — вся компания: выгрузку смотрит тот, кто отвечает
    # за портфель целиком. Соединение суженное, потому что просрочку даёт
    # work.promises(), а она читает только представления.
    with scoped_session(Scope.everything()) as conn:
        # Те же воронки, что у дайджеста. None здесь означало бы не «все»,
        # а «несущие план», и продавцы выпали бы целиком.
        funnels = [int(get_settings().sellers_category_id),
                   *plan_category_ids()]
        late = {int(row["deal_id"])
                for row in overdue_promises(conn, funnels)}
        cards = rows(conn, late)
    summary(cards)

    chosen = [c for c in cards if args.only in ("all", c["sign"])]
    if args.limit:
        chosen = chosen[:args.limit]

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    name = "read_cards.csv" if args.only == "all" else f"read_{args.only}.csv"
    path = out / name
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS,
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(chosen)
    print(f"\nCSV: {path} — {len(chosen)} строк")


if __name__ == "__main__":
    main()
