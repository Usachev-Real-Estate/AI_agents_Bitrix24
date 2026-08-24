#!/usr/bin/env python3
"""Пилот QC состояния клиента: N сделок на воронку → отчёт в чат.

Формат отчёта живёт в src/client_state_report.py, а не здесь: тот же текст
должен уходить и из регулярного задания, и из ручного прогона.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from client_state import (  # noqa: E402
    STAGE_ENTRY_SELECT,
    _stage_hours,
    run_client_state,
)
from client_state_report import format_card, format_summary  # noqa: E402
from config import get_settings, setup_logging  # noqa: E402
from db import init_db  # noqa: E402
from funnel_profiles import BUYER_PROFILE, SELLER_PROFILE, FunnelProfile  # noqa: E402
from notify import send_chat_message_chunked  # noqa: E402
from tools import _as_list, _bx_get_all_sync, _clean_str, _coerce_int  # noqa: E402

MSK = ZoneInfo("Europe/Moscow")
OUT_PATH = Path("data/client_state_qc_pilot.json")


def pick_deals(
    profile: FunnelProfile,
    category_id: int,
    limit: int,
    order: str = "random",
) -> list[dict]:
    """Открытые сделки воронки.

    Этапы вне контроля качества («Переговоры», «Поиск клиента», «Задаток»,
    «Агент» и прочие, снятые агентством) отсеиваются во всех режимах: по ним
    вердикта не будет в любом случае, а место в выборке они занимают. В
    прошлом прогоне так ушло 6 слотов из 10 у продавцов.

    Порядок задаёт, какую часть оставшейся воронки увидит прогон, и каждый
    режим отвечает на свой вопрос. Крайности лгут по-разному:

    * "random" (по умолчанию) — представительная выборка среди карточек под
      контролем качества. Отвечает на «как обстоят дела». Единственный режим,
      по которому можно судить о распределении.
    * "judgeable" — дольше всего на этапе. Отвечает на «где хуже всего»;
      по построению набирает худшее, поэтому доля «плохо» в нём ни о чём
      не говорит.
    * "newest" — самые свежие. Почти все моложе отсрочки, поэтому годится
      только для отладки свежих лидов, не для оценки качества.

    UF-поля квалификации запрашиваются наравне с остальными: без них прямая
    проверка бюджета и района не видит данных и объявляет поля незаполненными.
    Поля даты нужны отсрочке: без них свежий лид судится как застоявшийся.
    """
    raw = _bx_get_all_sync(
        "crm.deal.list",
        {
            "filter": {"CATEGORY_ID": category_id, "CLOSED": "N"},
            "select": [
                "ID", "TITLE", "STAGE_ID", "ASSIGNED_BY_ID", "CONTACT_ID",
                *STAGE_ENTRY_SELECT,
                *[code for code, _name in profile.qualification_fields],
            ],
        },
    )
    deals = [
        d for d in _as_list(raw)
        if isinstance(d, dict)
        and _clean_str(d.get("STAGE_ID")) not in profile.stages_out_of_qc
    ]
    if order == "newest":
        deals.sort(key=lambda d: _coerce_int(d.get("ID")), reverse=True)
        return deals[:limit]

    if order == "random":
        # Сид от ID сделок: выборка воспроизводима, пока воронка не изменилась,
        # поэтому повторный прогон можно сравнить с предыдущим.
        pool = sorted(deals, key=lambda d: _coerce_int(d.get("ID")))
        rng = random.Random(sum(_coerce_int(d.get("ID")) for d in pool))
        rng.shuffle(pool)
        return pool[:limit]

    now = datetime.now(timezone.utc)
    # Карточки без известного возраста ставим в конец: по ним отсрочка не
    # применяется, и они дали бы завышенную строгость на ровном месте.
    deals.sort(
        key=lambda d: (_stage_hours(d, now) is None, -(_stage_hours(d, now) or 0.0)),
    )
    return deals[:limit]


def format_report(
    sections: list[tuple[dict[str, Any], dict[int, str]]],
    webhook_url: str,
) -> str:
    """Полный текст отчёта: шапка прогона, затем воронка за воронкой."""
    now = datetime.now(MSK).strftime("%d.%m.%Y %H:%M")
    total_cost = sum(float(stats.get("cost_rub") or 0.0) for stats, _ in sections)
    parts = [
        "🧪 [B]QC-агент: состояние клиентов[/B]",
        f"📅 {now} МСК · всего {total_cost:.2f} ₽",
        "",
    ]
    for stats, titles in sections:
        parts.append(format_summary(stats))
        parts.append("")
        for result in stats.get("results", []):
            deal_id = _coerce_int(result.get("deal_id"))
            parts.append(format_card(result, titles.get(deal_id, ""), webhook_url))
            parts.append("")
    return "\n".join(parts).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Client-state QC pilot report")
    parser.add_argument("--limit", type=int, default=10, help="Сделок на воронку")
    parser.add_argument(
        "--chat-id", type=int, default=0, help="Чат (0 → REPORT_CHAT_ID)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Не отправлять в чат")
    parser.add_argument(
        "--force", action="store_true",
        help="Перечитать карточки моделью, даже если новых событий нет",
    )
    parser.add_argument(
        "--order", choices=("random", "judgeable", "newest"), default="random",
        help="random — представительная выборка (по умолчанию, только по ней "
             "можно судить о воронке); judgeable — дольше всего на этапах, "
             "которые QC судит (где хуже всего); newest — самые свежие "
             "(отладка, почти все моложе отсрочки)",
    )
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.log_level)
    init_db()

    chat_id = args.chat_id or settings.report_chat_id
    sections: list[tuple[dict[str, Any], dict[int, str]]] = []
    for profile, category_id in (
        (BUYER_PROFILE, settings.buyers_category_id),
        (SELLER_PROFILE, settings.sellers_category_id),
    ):
        deals = pick_deals(profile, category_id, args.limit, args.order)
        titles = {
            _coerce_int(d.get("ID")): _clean_str(d.get("TITLE")) for d in deals
        }
        print(f"{profile.label}: {[d.get('ID') for d in deals]}")
        stats = run_client_state(
            profile, deals, settings=settings, force=args.force,
        )
        sections.append((stats, titles))

    report = format_report(sections, settings.b24_webhook_url)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "chat_id": chat_id,
                "funnels": [
                    {k: v for k, v in stats.items() if k != "results"}
                    for stats, _titles in sections
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("\n" + report)

    if settings.dry_run or args.dry_run:
        print(f"\nDRY_RUN: отчёт не отправлен ({len(report)} символов)")
        return

    chunks = send_chat_message_chunked(chat_id, report)
    print(f"\nОтчёт отправлен в чат {chat_id} ({chunks} сообщ.)")


if __name__ == "__main__":
    main()
