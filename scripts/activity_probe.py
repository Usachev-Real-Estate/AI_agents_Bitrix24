"""Замер объёма активностей в Битриксе перед их заливкой в витрину.

Витрина знает сделки, лиды и переходы по стадиям — и ни одной строки о том,
что человек СДЕЛАЛ. Из-за этого на вопрос «с какими карточками работают, а с
какими нет» ответить нечем: движение по стадиям его не заменяет. Объект в
рекламе месяцами стоит на одной стадии, пока брокер по нему звонит и
показывает, и «карточка не двигалась 60 дней» одинаково верно и для честной
работы, и для забытого собственника.

Действия это чинят, но сначала нужно знать объём. Заливка на полмиллиона
строк и на двадцать тысяч — это разные решения по хранению, по времени
первого прогона и по тому, за какой срок вообще имеет смысл тянуть историю.

Скрипт ТОЛЬКО СЧИТАЕТ. Ни одной активности он не выгружает и ничего не
пишет: у crm.activity.list в конверте есть total, и его достаточно.

Запуск на сервере:

    docker run --rm --env-file .env b24-ai-auditor:latest \\
        python scripts/activity_probe.py
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

# Клиент Битрикса живёт в src/analytics, а настройки — в src. Кладём оба:
# один только src давал «No module named client» уже после того, как скрипт
# прочитал конфиг, — то есть ломался на середине и выглядел как отказ портала.
_SRC = Path(__file__).resolve().parent.parent / "src"
for _path in (_SRC, _SRC / "analytics"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from config import get_settings  # noqa: E402

# Что за сущность держит активность. Числа заданы Битриксом, не нами.
OWNERS = ((1, "лиды"), (2, "сделки"), (3, "контакты"), (4, "компании"))

# Тип провайдера: звонок, встреча, письмо, задача. Пусто — всё подряд.
PROVIDERS = (
    ("CALL", "звонки"),
    ("MEETING", "встречи"),
    ("EMAIL", "письма"),
    ("TASK", "задачи"),
)

MONTHS = (1, 3, 12)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Сколько активностей в портале")
    parser.add_argument("--months", type=int, default=12,
                        help="за сколько месяцев считать подробности")
    args = parser.parse_args(argv)

    settings = get_settings()
    try:
        from client import BitrixClient
    except Exception as exc:  # pragma: no cover — зависит от окружения сервера
        print(f"Клиент Bitrix недоступен: {exc}")
        return 1

    print("ЗАМЕР АКТИВНОСТЕЙ")
    print("=" * 60)
    with BitrixClient(
        settings.b24_webhook_url, rps=settings.analytics_rate_limit_rps,
    ) as client:
        print("\nВсего за период (все типы, все сущности):")
        total_all = 0
        for months in MONTHS:
            since = (date.today() - timedelta(days=30 * months)).isoformat()
            total = _total(client, {">=CREATED": since})
            if months == args.months:
                total_all = total
            print(f"  за {months:>2} мес.: {total:>9,}".replace(",", " "))

        since = (date.today() - timedelta(days=30 * args.months)).isoformat()
        print(f"\nЗа {args.months} мес. по сущностям:")
        for owner_id, label in OWNERS:
            total = _total(client, {">=CREATED": since, "OWNER_TYPE_ID": owner_id})
            print(f"  {label:<12} {total:>9,}".replace(",", " "))

        # Подпись раньше врала «сделки и лиды»: фильтра по сущности здесь нет,
        # и числа идут по всем владельцам сразу. На боевом портале это важно —
        # большая часть звонков висит на контактах, а не на сделках.
        print(f"\nЗа {args.months} мес. по типам (все сущности):")
        for provider, label in PROVIDERS:
            total = _total(client, {
                ">=CREATED": since, "PROVIDER_TYPE_ID": provider,
            })
            print(f"  {label:<12} {total:>9,}".replace(",", " "))

        named = sum(
            _total(client, {">=CREATED": since, "PROVIDER_TYPE_ID": provider})
            for provider, _ in PROVIDERS
        )
        done = _total(client, {">=CREATED": since, "COMPLETED": "Y"})
        planned = _total(client, {">=CREATED": since, "COMPLETED": "N"})

    print(f"\nЗа {args.months} мес.: завершено {done:,}, запланировано {planned:,}"
          .replace(",", " "))
    print(f"Типов, не попавших в разбивку выше: {max(total_all - named, 0):,}"
          .replace(",", " "))
    print()
    print("Что с этим делать дальше:")
    print("  до ~100 тыс. строк — заливаем всю историю, витрина не заметит;")
    print("  до ~500 тыс.       — заливаем, но окном, как сделки;")
    print("  больше            — тянем только сделки и лиды, и только год.")
    return 0


def _total(client, activity_filter: dict) -> int:
    """Число активностей под фильтром. Берётся из конверта, а не счётом строк.

    Выгружать ради счёта нельзя: у портала с сотней тысяч активностей это
    десятки минут и мегабайты, а ответ — одно число, которое Битрикс и так
    кладёт в total рядом с первой страницей.
    """
    envelope = client.call_envelope(
        "crm.activity.list",
        {"filter": activity_filter, "select": ["ID"], "start": 0},
    )
    if isinstance(envelope, dict) and "total" in envelope:
        return int(envelope["total"] or 0)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    sys.exit(main())
