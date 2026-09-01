#!/usr/bin/env python3
"""Пилот QC состояния клиента: N сделок на воронку → отчёт в чат.

Формат отчёта живёт в src/client_state_report.py, а не здесь: тот же текст
должен уходить и из регулярного задания, и из ручного прогона.
"""

from __future__ import annotations

import argparse
import json
import logging
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
    SOURCE_SELECT,
    card_digest,
    STAGE_ENTRY_SELECT,
    _stage_hours,
    run_client_state,
)
from client_state_report import (  # noqa: E402
    format_sections,
    format_summary,
    set_source_names,
)
from config import get_settings, setup_logging  # noqa: E402
from db import init_db, list_client_state_deal_ids  # noqa: E402
from funnel_profiles import BUYER_PROFILE, SELLER_PROFILE, FunnelProfile  # noqa: E402
from buyer_commission_reminder import build_commission_rop_map  # noqa: E402
from notify import (  # noqa: E402
    send_chat_message_chunked,
    send_user_chat_message_chunked,
)
from qc_delivery import (  # noqa: E402
    ROP_SURNAMES,
    Rop,
    build_rop_directory,
    group_deals_by_rop,
)
from tools import (  # noqa: E402
    _build_broker_dept_map,
    _as_list,
    _bx_get_all_sync,
    _clean_str,
    _coerce_int,
    fetch_source_names,
)

logger = logging.getLogger(__name__)

MSK = ZoneInfo("Europe/Moscow")
OUT_PATH = Path("data/client_state_qc_pilot.json")


def describe_run_mode(cache_off: bool, sending_off: bool) -> str:
    """Что этот прогон делает и чего не делает — одной строкой.

    Два выключателя называются похоже и работают врозь: флаг `--dry-run`
    отключает только отправку, а запись разбора в кэш висит на переменной
    окружения DRY_RUN. Имя флага при этом обещает «ничего не делаю
    по-настоящему» — и человек, запустивший прогон, не знает, сохранится ли
    сотня разборов, за которые он заплатил.

    Связать их было бы хуже, а не лучше: тогда `--dry-run` перестал бы
    наполнять кэш, и следующий за ним боевой прогон разобрал бы всё заново.
    На выборке из 676 карточек это ~310 ₽ на ровном месте. Поэтому не
    связываем, а проговариваем.
    """
    cache = "кэш НЕ пишется (DRY_RUN=true)" if cache_off else "разбор пишется в кэш"
    sending = "отправка выключена" if sending_off else "отправка включена"
    return f"Режим прогона: {cache}, {sending}"


def load_rop_directory() -> dict[int, Rop]:
    """Справочник РОПов из портала по списку фамилий агентства.

    Один запрос на прогон. Пустой справочник — не «РОПов нет», а «мы их не
    прочитали»: тогда все карточки уйдут в общий чат, и это видно в логе, а
    не выглядит как тихий успех.
    """
    try:
        users = _bx_get_all_sync("user.get", {"FILTER": {"ACTIVE": True}})
    except Exception:
        logger.exception(
            "Не удалось прочитать пользователей — отчёт уйдёт целиком в чат",
        )
        return {}
    directory = build_rop_directory(
        [u for u in _as_list(users) if isinstance(u, dict)],
    )
    logger.info(
        "РОПов найдено %d из %d: %s",
        len(directory), len(ROP_SURNAMES),
        ", ".join(sorted(r.full_name for r in directory.values())) or "—",
    )
    return directory


def pick_deals(
    profile: FunnelProfile,
    category_id: int,
    limit: int,
    order: str = "random",
    deal_ids: tuple[int, ...] = (),
    cached_deal_ids: set[int] | None = None,
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
    * "uncached" — открытые QC-сделки, которых ещё нет в client_states.
      Сначала с большим ID (обычно свежее). Для живого прогона модели без
      повторения закэшированной десятки random.

    deal_ids отменяет и порядок, и лимит: берутся ровно названные сделки той
    воронки, к которой они относятся. Нужно, чтобы проверить правку на той
    самой карточке, из-за которой она делалась, не дожидаясь, пока сделка
    выпадет в случайную выборку. Этап вне контроля качества при этом не
    отсеивается: если карточку спросили по номеру, ответить надо про неё, а
    не промолчать.

    cached_deal_ids — только для uncached в тестах; иначе читается из БД.

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
                *SOURCE_SELECT,
                *[code for code, _name in profile.qualification_fields],
            ],
        },
    )
    if deal_ids:
        wanted = set(deal_ids)
        named = [
            d for d in _as_list(raw)
            if isinstance(d, dict) and _coerce_int(d.get("ID")) in wanted
        ]
        found = {_coerce_int(d.get("ID")) for d in named}
        missing = sorted(wanted - found)
        if missing:
            # Молча вернуть меньше — значит выдать «карточки нет» за «мы её
            # не искали». Сделка может лежать в другой воронке или быть
            # закрыта; и то и другое надо сказать вслух.
            logger.warning(
                "%s: не найдены среди открытых сделок воронки: %s",
                profile.label,
                ", ".join(f"#{i}" for i in missing),
            )
        return sorted(named, key=lambda d: _coerce_int(d.get("ID")))

    # Охват задаётся списком этапов, а не вычитанием снятых с контроля
    # (решение агентства от 31.08). Вычитание отвечало на вопрос «что мы
    # решили не смотреть», и в выборку молча попадали «Отложенный спрос»,
    # «Отложенная продажа» и «Сделка проиграна» — просто потому, что их
    # никто не вычел. Список отвечает на настоящий вопрос: что РОП увидит.
    deals = [
        d for d in _as_list(raw)
        if isinstance(d, dict)
        and _clean_str(d.get("STAGE_ID")) in profile.audited_stages
    ]
    if order == "all":
        # Весь отдел целиком — решение агентства от 31.08: «надо брать в
        # аудит все карточки, которые есть у отдела этого РОПа». Лимит тут
        # не применяется: срезав выборку, мы бы решили за РОПа, каких его
        # брокеров он сегодня не проверит.
        return sorted(deals, key=lambda d: _coerce_int(d.get("ID")))

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

    if order == "uncached":
        cached = (
            cached_deal_ids
            if cached_deal_ids is not None
            else list_client_state_deal_ids()
        )
        fresh = [
            d for d in deals
            if _coerce_int(d.get("ID")) not in cached
        ]
        fresh.sort(key=lambda d: _coerce_int(d.get("ID")), reverse=True)
        if len(fresh) < limit:
            logger.info(
                "%s: без кэша только %s QC-сделок (запрошено %s)",
                profile.label,
                len(fresh),
                limit,
            )
        return fresh[:limit]

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
        parts.append(
            format_sections(stats.get("results", []), titles, webhook_url),
        )
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
        "--order",
        choices=("all", "random", "judgeable", "newest", "uncached"),
        default="all",
        help="all — все карточки на этапах аудита (боевой режим: РОП должен "
             "видеть весь свой отдел, --limit не применяется); random — "
             "представительная выборка, по ней можно судить о воронке; "
             "judgeable — дольше всего на этапах, которые QC судит (где "
             "хуже всего); newest — самые свежие (отладка, почти все моложе "
             "отсрочки); uncached — QC-сделки, которых ещё нет в "
             "client_states (живой прогон модели)",
    )
    parser.add_argument(
        "--deal-id", type=int, action="append", default=[], metavar="ID",
        help="Разобрать ровно эти сделки (можно повторять). Отменяет --order "
             "и --limit; удобно проверить правку на той карточке, из-за "
             "которой она делалась",
    )
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.log_level)
    init_db()

    mode = describe_run_mode(
        cache_off=settings.dry_run,
        sending_off=settings.dry_run or args.dry_run,
    )
    logger.info("%s", mode)
    print(mode)

    chat_id = args.chat_id or settings.report_chat_id
    # Один запрос на прогон: «источник 26» РОПу ничего не говорит.
    set_source_names(fetch_source_names())

    # Кому что уходит — решается до разбора: отчёт РОПа собирается из его
    # карточек, а не режется из общего постфактум.
    directory = load_rop_directory()
    rop_map = build_commission_rop_map()

    picked: list[tuple[FunnelProfile, list[dict[str, Any]]]] = []
    for profile, category_id in (
        (BUYER_PROFILE, settings.buyers_category_id),
        (SELLER_PROFILE, settings.sellers_category_id),
    ):
        deals = pick_deals(
            profile, category_id, args.limit, args.order,
            deal_ids=tuple(args.deal_id),
        )
        print(f"{profile.label}: {len(deals)} карточек")
        picked.append((profile, deals))

    broker_ids = {
        _coerce_int(d.get("ASSIGNED_BY_ID"))
        for _profile, deals in picked for d in deals
    }
    broker_dept_map = _build_broker_dept_map({b for b in broker_ids if b})

    # Адресат → воронка → его карточки. Оба списка вместе: РОП получает одно
    # сообщение про обе воронки, а не два про одну.
    plan: dict[int, dict[str, list[dict[str, Any]]]] = {}
    for profile, deals in picked:
        groups = group_deals_by_rop(deals, broker_dept_map, rop_map, directory)
        for key, group in groups.items():
            plan.setdefault(key, {})[profile.key] = group

    personal_keys = sorted(
        (k for k in plan if k in directory and directory[k].personal),
        key=lambda k: directory[k].surname,
    )
    chat_keys = [k for k in plan if k not in personal_keys]

    def _report_for(
        keys: list[int],
    ) -> tuple[str, list[tuple[dict[str, Any], dict[int, str]]]]:
        """Отчёт по карточкам этих адресатов и разбор, из которого он собран."""
        sections: list[tuple[dict[str, Any], dict[int, str]]] = []
        for profile, _all_deals in picked:
            deals = [
                d for key in keys for d in plan[key].get(profile.key, [])
            ]
            if not deals:
                continue
            titles = {
                _coerce_int(d.get("ID")): _clean_str(d.get("TITLE"))
                for d in deals
            }
            stats = run_client_state(
                profile, deals, settings=settings, force=args.force,
            )
            sections.append((stats, titles))
            if stats.get("aborted"):
                # Прогон оборвался: модель перестала отвечать. Дальше по
                # остальным адресатам будет то же самое, а собранное
                # отправлять нельзя — см. проверку перед отправкой.
                break
        if not sections:
            return "", []
        return format_report(sections, settings.b24_webhook_url), sections

    Delivery = tuple[str, str, str, list[tuple[dict[str, Any], dict[int, str]]]]
    deliveries: list[Delivery] = []
    for key in personal_keys:
        rop = directory[key]
        text, sections = _report_for([key])
        if text:
            deliveries.append(
                ("user", str(rop.user_id), rop.full_name, sections),
            )
    if chat_keys:
        # Подразделение Волковой и всё, чей РОП не определился, — одним
        # сообщением: в чате их распределяют руками, и делить их между собой
        # незачем.
        text, sections = _report_for(chat_keys)
        if text:
            deliveries.append(("chat", str(chat_id), f"чат {chat_id}", sections))

    texts: dict[str, str] = {}
    for kind, addr, name, sections in deliveries:
        text = format_report(sections, settings.b24_webhook_url)
        texts[f"{kind}:{addr}"] = text
        cards = sum(len(stats.get("results") or []) for stats, _t in sections)
        print(f"{name}: {cards} карточек")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "chat_id": chat_id,
                "deliveries": [
                    {
                        "kind": kind,
                        "to": addr,
                        "name": name,
                        "funnels": [
                            {
                                # results выкидываем: там развёрнутые
                                # состояния с именами и суммами. Вместо них —
                                # построчная выжимка из кодов и чисел: по ней
                                # два прогона сравниваются машиной, а
                                # персональных данных в файле не остаётся.
                                **{
                                    k: v for k, v in stats.items()
                                    if k != "results"
                                },
                                "cards": [
                                    card_digest(r)
                                    for r in stats.get("results") or []
                                ],
                            }
                            for stats, _titles in sections
                        ],
                    }
                    for kind, addr, name, sections in deliveries
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    aborted = [
        stats for _kind, _addr, _name, sections in deliveries
        for stats, _titles in sections if stats.get("aborted")
    ]
    if aborted:
        # Отчёт по оборванному прогону — это отчёт, в котором почти всё «не
        # прочитано». РОП увидит короткий список претензий и решит, что в
        # отделе порядок. Сегодня такое письмо не ушло только потому, что за
        # логом смотрел человек; в 12:00 по крону смотреть будет некому.
        #
        # Ненулевой код возврата важен не меньше: cron_job.sh по нему шлёт
        # уведомление админу, и молчание вместо отчёта перестаёт выглядеть
        # как «сегодня нарушений не было».
        print(
            "Прогон оборван: модель не отвечает. Отчёты НЕ отправлены — "
            "в них почти всё «не прочитано». Разобранное сохранено в кэш, "
            "повторный запуск продолжит с этого места.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not deliveries:
        print("Ни одной карточки на этапах аудита — отправлять нечего")
        return

    if settings.dry_run or args.dry_run:
        for kind, addr, name, _sections in deliveries:
            size = len(texts[f"{kind}:{addr}"])
            print(f"DRY_RUN: {name} — {size} символов, не отправлено")
        return

    for kind, addr, name, _sections in deliveries:
        text = texts[f"{kind}:{addr}"]
        if kind == "user":
            chunks = send_user_chat_message_chunked(int(addr), text)
        else:
            chunks = send_chat_message_chunked(int(addr), text)
        print(f"Отправлено: {name} ({chunks} сообщ.)")


if __name__ == "__main__":
    main()
