"""Утренний дайджест «Пульса»: план, факт и темп — в Битрикс.

Экран есть, но открывать его каждое утро никто не будет. Сообщение прочитают,
поэтому расчёт тот же самый, а меняется только доставка: ``pulse.pulse``
возвращает данные, здесь они превращаются в текст и адресуются людям.

**Дайджест ведёт со вчерашнего дня, а не с итога.** Квартальный план меняется
медленно, и сообщение «14,5 из 127,5, отстаём» будет одинаковым девяносто
дней подряд — такое перестают читать на третий раз. Новость — это то, что
случилось со вчера: сколько закрыли и на сколько. Итог идёт следом, одной
строкой, как опора.

**Каждому РОПу считается своё, на суженном соединении.** Можно было бы взять
общий расчёт и отфильтровать отделы в Python, но тогда правильность держалась
бы на внимательности: одна ошибка в условии — и РОП получает в сообщении
чужой отдел. Здесь чужих данных нет в том, из чего собрано сообщение.

**Отдел без опознанного РОПа не теряется молча.** Он попадает в сводку
директору отдельной строкой — как в рассылке QC, по тем же соображениям:
отчёт, не дошедший ни до кого, выглядит точно так же, как отчёт, в котором
всё хорошо.

**Часть отделов разбирает не их руководитель.** Кто именно — берётся из
рассылки QC (``ROP_TO_CHAT``), а не заводится здесь заново: вопрос «кто из
РОПов не получает отчёт лично» агентство уже решило, и второй ответ на него
означал бы, что две рассылки одного проекта адресуют по-разному. Отчёт такого
отдела уходит владельцу отчёта отдельным сообщением — не строкой в сводке,
чтобы его можно было переслать как есть.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent
for extra in (_SRC, _SRC / "analytics", _SRC / "web"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import events as funnel  # noqa: E402
import metrics  # noqa: E402
import plans  # noqa: E402
import pulse as pulse_metrics  # noqa: E402
from config import get_settings, setup_logging  # noqa: E402
from notify import send_user_chat_message_chunked  # noqa: E402
from qc_delivery import ROP_TO_CHAT  # noqa: E402
from scope import Scope, scoped_session  # noqa: E402

logger = logging.getLogger(__name__)

MILLION = 1_000_000


# --------------------------------------------------------------------------
# форматирование
# --------------------------------------------------------------------------

def money(value: float | None) -> str:
    """Сумма в миллионах. В сообщении рубли до копейки не нужны никому."""
    if value is None:
        return "—"
    text = f"{value / MILLION:.1f}".replace(".", ",")
    return f"{text} млн ₽"


def percent(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1f}".rstrip("0").rstrip(".").replace(".", ",") + "%"


def verdict(data: dict[str, Any]) -> str:
    """Одно слово о темпе. Без него два процента рядом надо сравнивать в уме."""
    if data.get("plan_share") is None:
        return "плана нет"
    if data["behind"]:
        return "отстаём"
    return "идём с опережением"


def format_company(data: dict[str, Any], yesterday: dict[str, Any], url: str) -> str:
    lines = [
        f"📊 Пульс{_funnel_note(data)} · {data['period']['label']}",
        "",
        _yesterday_line(yesterday),
        f"Квартал: {money(data['fact'])} из {money(data['plan'])} — "
        f"{percent(data['plan_share'])} плана при {percent(data['time_share'])} "
        f"срока ({verdict(data)})",
    ]
    if data.get("projection"):
        lines.append(f"Если темп не изменится: {money(data['projection'])}")
    if data["others"]["fact"]:
        lines.append(
            f"Из факта у людей с нормой: {money(data['fact_on_plan'])}, "
            f"остальное у тех, кому норму не ставили"
        )
    lines += _breakeven_lines(data) + _stuck_lines(data, company=True)
    lines += _event_lines(data.get("events"))

    # Отдел без плана и без факта в сводку не попадает: строка «0,0 из —»
    # ничего не сообщает и только удлиняет сообщение. Если план есть, отдел
    # показывается всегда — ноль при живом плане это и есть новость.
    lines += ["", "По отделам:"]
    for row in data["departments"]:
        if not row["plan"] and not row["fact"]:
            continue
        target = money(row["plan"]) if row["plan"] else "плана нет"
        lines.append(
            f"  {row['name']}: {money(row['fact'])} из {target} — "
            f"{percent(row['plan_share'])}"
        )

    missing = data.get("departments_without_rop") or []
    if missing:
        lines += [
            "",
            f"Без опознанного РОПа: {', '.join(missing)} — "
            f"эти отделы отчёт лично не получили.",
        ]
    return "\n".join(lines + _tail(data, url))


def format_department(data: dict[str, Any], yesterday: dict[str, Any], url: str) -> str:
    """Сообщение РОПу. Данные уже сужены соединением, фильтровать нечего."""
    row = data["departments"][0] if data["departments"] else None
    if row is None:
        return ""
    lines = [
        f"📊 Пульс отдела «{row['name']}»{_funnel_note(data)} "
        f"· {data['period']['label']}",
        "",
        _yesterday_line(yesterday),
        f"Квартал: {money(row['fact'])} из {money(row['plan'])} — "
        f"{percent(row['plan_share'])} плана при {percent(row['time_share'])} "
        f"срока ({verdict(row)})",
    ]
    if data.get("projection"):
        lines.append(f"Если темп не изменится: {money(data['projection'])}")
    if row["others_fact"]:
        lines.append(
            f"Из них {money(row['others_fact'])} закрыли люди без нормы — "
            f"в план отдела это входит, но обещали не они"
        )
    lines.append(
        f"Норму несут {row['on_plan']} чел., без нормы ещё {row['without_norm']}"
    )
    lines += _stuck_lines(data, company=False)
    lines += _event_lines(data.get("events"))
    if not row["rop_known"]:
        lines += [
            "",
            "В составе отдела не опознан руководитель: если вы план не несёте, "
            "цель отдела завышена на одну норму. Скажите админу — поправим.",
        ]
    return "\n".join(lines + _tail(data, url))


def _breakeven_lines(data: dict[str, Any]) -> list[str]:
    """Рубеж безубыточности словами. Пусто, если расходы не заданы.

    Процент выполнения планки красный весь квартал по замыслу, и по нему
    нельзя понять, идём мы к нулю или под него. Эта строка отвечает именно
    на этот вопрос — и меняется от каждой сделки, в отличие от процента.
    """
    edge = data.get("breakeven")
    if not edge:
        return []
    if edge["gap"] is None:
        return [
            f"Рубеж безубыточности периода {money(edge['gross'])}, "
            f"пройдено {percent(edge['share'])}"
        ]
    if edge["reaches"]:
        return [
            f"Безубыточность: при нынешнем темпе пройдём с запасом "
            f"{money(edge['gap'])}"
        ]
    return [
        f"⚠ Безубыточность: при нынешнем темпе НЕ дотянем "
        f"{money(-edge['gap'])} до {money(edge['gross'])}"
    ]


def _stuck_lines(data: dict[str, Any], company: bool) -> list[str]:
    """Где остановились деньги. Ответ на «куда поднажать».

    Не отдел с худшим процентом, а сделки, стоящие на стадии дольше нормы
    этой стадии. Процент говорит, что уже случилось; остановившиеся карточки —
    что можно сделать сегодня.

    Впереди стоит КОЛИЧЕСТВО, а сумма названа «проставлено в карточках».
    Первая формулировка — «стоят без движения 572,6 млн» — на боевых данных
    читалась как «у нас полмиллиарда на столе», хотя означала другое: в
    двухстах двадцати четырёх карточках, которые никто не двигает, кем-то
    когда-то проставлены суммы. Это разные новости, и вторая проверяема, а
    первая нет: сумма открытой сделки — намерение, а не деньги.
    """
    stuck = data.get("stuck")
    if not stuck or not stuck["deals"]:
        return []
    head = (
        f"Не двигаются {stuck['deals']} {_deals_word(stuck['deals'])}; "
        f"в карточках проставлено {money(stuck['amount'])}"
    )
    if not company:
        return [head]
    worst = max(
        (row for row in data["departments"] if row.get("stuck")),
        key=lambda row: row["stuck"]["deals"],
        default=None,
    )
    if worst and worst["stuck"]["deals"]:
        head += (
            f"; больше всего у отдела «{worst['name']}» "
            f"({worst['stuck']['deals']} {_deals_word(worst['stuck']['deals'])})"
        )
    return [head]


def _on_plan_ids(data: dict[str, Any]) -> set[int]:
    """Кто несёт норму. Молчание спрашивается только с них.

    С новичка без нормы спрашивать нечего — он и заведён затем, чтобы
    учиться. Строка «не двигал карточки неделю» про такого человека
    обесценила бы весь блок: в нём стало бы поровну тех, с кого спрос, и тех,
    с кого нет.
    """
    return {
        man["user_id"]
        for row in data["departments"]
        for man in row.get("brokers") or []
        if man.get("plan") is not None
    }


def _event_lines(events: dict[str, Any] | None) -> list[str]:
    """Что случилось с воронкой за окно. Пустые блоки не печатаются.

    Отчёт, ежедневно сообщающий «ничего не произошло», перестают открывать
    раньше, чем в нём появится что-то важное. Поэтому здесь нет ни одной
    строки-заглушки: нечего сказать — блока нет.
    """
    if not events:
        return []
    lines: list[str] = []

    stalled = events["stalled"]
    if stalled["deals"]:
        lines.append(
            f"\n🟠 {_verb(stalled['deals'], 'Встала', 'Встали')} "
            f"{stalled['deals']} {_deals_word(stalled['deals'])} "
            f"на {money(stalled['amount'])}:"
        )
        lines += [
            f"  · {row['title'][:44]} — {money(row.get('opportunity') or 0)}, "
            f"«{row['stage_name']}», {round(row['days_in_stage'])} дн"
            + (f" ({row['assignee']})" if row.get("assignee") else "")
            for row in stalled["top"]
        ]

    advanced = events["advanced"]
    if advanced["deals"]:
        lines.append(
            f"\n🟢 {_verb(advanced['deals'], 'Сдвинулась', 'Сдвинулись')} вперёд "
            f"{advanced['deals']} {_deals_word(advanced['deals'])} "
            f"на {money(advanced['amount'])}:"
        )
        lines += [
            f"  · {row['title'][:44]} — {money(row.get('opportunity') or 0)}, "
            f"«{row['from_name']}» → «{row['to_name']}»"
            + (f" ({row['assignee']})" if row.get("assignee") else "")
            for row in advanced["top"]
        ]

    returned = events["returned"]
    if returned["deals"]:
        lines.append(
            f"\n🔴 {_verb(returned['deals'], 'Вернулась', 'Вернулись')} назад "
            f"{returned['deals']} {_deals_word(returned['deals'])} — обычно это "
            f"неверная квалификация на входе:"
        )
        lines += [
            f"  · {row['title'][:44]}, «{row['from_name']}» → «{row['to_name']}»"
            + (f" ({row['assignee']})" if row.get("assignee") else "")
            for row in returned["top"]
        ]

    silent = events["silent"]["people"]
    if silent:
        worst = silent[0]
        lines.append(
            f"\nНе двигали ни одной карточки дольше "
            f"{events['silent']['idle_days']} дней: {len(silent)} чел. "
            f"Дольше всех — {worst['name']} ({worst['quiet_days']} дн, "
            f"{worst['deals']} {_deals_word(worst['deals'])})"
        )

    quality = events["quality"]
    if quality["won_without_amount"]:
        names = ", ".join(row["title"][:30] for row in quality["won_without_amount"][:2])
        lines.append(
            f"\n⚠ {_verb(len(quality['won_without_amount']), 'Закрыта', 'Закрыто')} "
            f"без суммы: "
            f"{len(quality['won_without_amount'])} "
            f"{_deals_word(len(quality['won_without_amount']))} — {names}"
        )
    if quality["without_assignee"]:
        lines.append(
            f"\n⚠ {_verb(len(quality['without_assignee']), 'Заведена', 'Заведено')} "
            f"без ответственного: "
            f"{len(quality['without_assignee'])} "
            f"{_deals_word(len(quality['without_assignee']))}"
        )
    return lines


def _funnel_note(data: dict[str, Any]) -> str:
    """Воронка плана — в заголовке, а не в сноске.

    Число в сводке меняется в тот день, когда меняется список воронок, и
    объяснение обязано стоять рядом с числом. В сноске его прочитают после
    того, как решат, что отчёт сломался.
    """
    names = [row["name"] for row in data.get("funnels") or []]
    return f" · {', '.join('«' + name + '»' for name in names)}" if names else ""


def _yesterday_line(yesterday: dict[str, Any]) -> str:
    if not yesterday["deals"]:
        return f"За {yesterday['label']} закрытых сделок нет."
    return (
        f"За {yesterday['label']} закрыто {yesterday['deals']} "
        f"{_deals_word(yesterday['deals'])} на {money(yesterday['amount'])}."
    )


def _verb(count: int, one: str, many: str) -> str:
    """Глагол под число сделок: «встала 1» против «встали 3».

    Мелочь, но она читается: сообщение, которое не согласует слова, выглядит
    машинным, а машинному отчёту верят меньше, чем он заслуживает.
    """
    return one if count % 10 == 1 and count % 100 != 11 else many


def _deals_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "сделка"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "сделки"
    return "сделок"


def _tail(data: dict[str, Any], url: str) -> list[str]:
    tail = []
    coverage = data.get("coverage") or {}
    # Покрытие показывается, только когда оно способно изменить решение.
    # Строка «заполнено 97%» каждый день — это шум; «заполнено 68%» — повод
    # не верить сумме.
    if coverage.get("deals") and coverage.get("share", 100) < 90:
        tail.append(
            f"\n⚠ Сумма заполнена у {percent(coverage['share'])} выигранных сделок — "
            f"факт занижен на невнесённые комиссии."
        )
    if url:
        tail.append(f"\n{url}")
    return tail


# --------------------------------------------------------------------------
# сбор и доставка
# --------------------------------------------------------------------------

def yesterday_window(now: datetime | None = None) -> dict[str, Any]:
    """Прошлый РАБОЧИЙ день по московскому календарю.

    В понедельник «вчера» — это пятница: сообщение про воскресенье, в котором
    закономерно ничего не закрыто, обесценивает всю рассылку. Выходные при
    этом не теряются — в понедельник окно накрывает их целиком.
    """
    now = now or datetime.now(metrics.BUSINESS_TZ)
    end = datetime(now.year, now.month, now.day, tzinfo=metrics.BUSINESS_TZ)
    start = end - timedelta(days=1)
    while start.weekday() >= 5:
        start -= timedelta(days=1)
    label = (
        "вчера" if (end - start).days == 1
        else f"{start:%d.%m}–{end - timedelta(days=1):%d.%m}"
    )
    return {"since": _iso(start), "until": _iso(end), "label": label}


def _iso(moment: datetime) -> str:
    from datetime import timezone

    return moment.astimezone(timezone.utc).isoformat()


def closed_in(conn, window: dict[str, Any]) -> dict[str, Any]:
    """Сколько закрыто за окно. Область видимости приходит соединением.

    Воронки те же, что и у квартала. Иначе первая строка сообщения считала
    бы одно, а вторая другое: «вчера закрыто 3 сделки» из всех воронок над
    кварталом, собранным по одной, — это два разных отчёта в одном письме, и
    несовпадение заметят раньше, чем поймут причину.
    """
    from metrics import _money_of, _one, base_currency

    where, params = plans.category_filter("v_deal")
    row = _one(
        conn,
        f"""
        SELECT COUNT(*) AS deals,
               COALESCE(SUM(CASE WHEN {_money_of()} THEN opportunity ELSE 0 END), 0)
                   AS amount
        FROM v_deal
        WHERE is_won = 1 AND closedate IS NOT NULL
          AND closedate >= :since AND closedate < :until
          AND {where}
        """,
        {"since": window["since"], "until": window["until"],
         "base": base_currency(), **params},
    )
    return {**window, "deals": int(row.get("deals") or 0),
            "amount": float(row.get("amount") or 0)}


def build(period_code: str, url: str, now: datetime | None = None) -> list[dict[str, Any]]:
    """Собрать все сообщения. Ничего не отправляет — это делает вызывающий."""
    window = yesterday_window(now)
    deliveries: list[dict[str, Any]] = []
    settings = get_settings()

    with scoped_session(Scope.everything()) as conn:
        company = pulse_metrics.pulse(conn, period_code, with_stuck=True)
        yesterday = closed_in(conn, window)
        company["events"] = funnel.funnel_events(
            conn, window["since"], window["until"],
            on_plan_ids=_on_plan_ids(company),
        )

    # Владелец отчёта: своя настройка, с откатом на администратора.
    director = int(settings.pulse_digest_to or settings.admin_user_id or 0)
    if director:
        deliveries.append({
            "user_id": director,
            "name": "Директор",
            "text": format_company(company, yesterday, url),
        })

    for row in company["departments"]:
        # РОП берётся из состава плана, а не из портала напрямую. Портал
        # считает руководителем того, кто в отделе ЧИСЛИТСЯ; ростер знает,
        # кто за отдел ОТВЕЧАЕТ. Спросив портал, рассылка не увидела бы
        # руководителя, переставленного ростером, — и отдел молча остался бы
        # без отчёта при исправленном составе на экране.
        rop = row["rops"][0] if row["rops"] else None
        if not rop:
            logger.info(
                "Отдел %s без опознанного РОПа — только в сводке директору",
                row["name"],
            )
            continue
        # Своё соединение на отдел: чужих данных нет в том, из чего собрано
        # сообщение, а не отфильтровано из общего расчёта.
        with scoped_session(Scope.departments([row["department_id"]])) as conn:
            data = pulse_metrics.pulse(conn, period_code, with_stuck=True)
            # Область видимости приходит соединением: тот же вызов в сессии
            # РОПа возвращает события его отдела и ничьи больше.
            data["events"] = funnel.funnel_events(
                conn, window["since"], window["until"],
                on_plan_ids=_on_plan_ids(data),
            )
            own_yesterday = closed_in(conn, window)
        text = format_department(data, own_yesterday, url)
        if not text:
            continue

        # Часть отделов разбирает не их руководитель, а владелец отчёта. Список
        # взят из рассылки QC, а не заведён заново: вопрос «кто из РОПов не
        # получает отчёт лично» уже решён агентством, и второй ответ на него
        # означал бы, что две рассылки одного проекта адресуют по-разному.
        redirected = (rop.get("surname") or "").strip().lower() in ROP_TO_CHAT
        if redirected and not director:
            logger.warning(
                "Отдел %s адресован директору, но ADMIN_USER_ID не задан — "
                "сообщение никуда не уйдёт", row["name"],
            )
            continue
        deliveries.append({
            "user_id": director if redirected else int(rop["user_id"]),
            "name": (f"Отдел «{row['name']}» → директору" if redirected
                     else f"РОП {row['name']}"),
            "text": text,
        })
    return deliveries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Утренний дайджест «Пульса»")
    parser.add_argument("--period", default="", help="код квартала, по умолчанию текущий")
    parser.add_argument("--dry-run", action="store_true",
                        help="показать сообщения, но не отправлять")
    args = parser.parse_args(argv)

    settings = get_settings()
    setup_logging(settings.log_level)

    # Выключатель гасит ОТПРАВКУ, а не показ. Раньше он стоял выше и обрывал
    # прогон целиком — посмотреть текст было нельзя, пока не включишь
    # рассылку, то есть ровно тем действием, от которого --dry-run и должен
    # был уберечь. Флаг ничего никому не шлёт, запрещать его нечего.
    preview = settings.dry_run or args.dry_run
    if not preview and not settings.pulse_digest_enabled:
        logger.info(
            "PULSE_DIGEST_ENABLED=false — дайджест выключен. "
            "Посмотреть текст, ничего не отправляя: --dry-run"
        )
        return 0

    period_code = args.period or plans.quarter_code(
        datetime.now(metrics.BUSINESS_TZ).date()
    )
    deliveries = build(period_code, settings.pulse_digest_url)
    if not deliveries:
        logger.warning(
            "Ни одного адресата: проверьте PULSE_DIGEST_TO (или ADMIN_USER_ID) "
            "и список РОПов"
        )
        return 0

    if preview:
        for item in deliveries:
            print(f"\n{'=' * 60}\n{item['name']} (user_id={item['user_id']})\n{'=' * 60}")
            print(item["text"])
        print(f"\nDRY_RUN: {len(deliveries)} сообщений НЕ отправлено")
        return 0

    for item in deliveries:
        chunks = send_user_chat_message_chunked(item["user_id"], item["text"])
        logger.info("Отправлено: %s (%s сообщ.)", item["name"], chunks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
