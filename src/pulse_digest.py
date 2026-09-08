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
import advice  # noqa: E402
import advice_rules  # noqa: E402
import wording  # noqa: E402
import work  # noqa: E402
from config import get_settings, setup_logging  # noqa: E402
from db import db_session, init_db  # noqa: E402
from notify import (  # noqa: E402
    send_chat_message_chunked,
    send_user_chat_message_chunked,
)
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


def format_events(
    data: dict[str, Any],
    events: dict[str, Any],
    window: dict[str, Any],
    sellers: dict[str, Any] | None = None,
    sellers_work: dict[str, Any] | None = None,
) -> str:
    """Разбор воронок отдельным сообщением в общий чат.

    Пустой возврат означает «в этот день ничего не произошло» — и тогда
    сообщения не будет вовсе. Ежедневная рассылка, сообщающая «событий нет»,
    приучает не открывать себя раньше, чем в ней появится важное.
    """
    lines = _event_lines(events)
    if lines:
        lines = [f"🔎 Воронка за {window['label']}{_funnel_note(data)}"] + lines
    sellers_lines = _sellers_lines(sellers) + _work_lines(sellers_work)
    if sellers_lines:
        lines += ([""] if lines else []) + [
            f"🏠 Продавцы за {window['label']}"
        ] + sellers_lines
    return "\n".join(lines)


def _work_lines(work_data: dict[str, Any] | None) -> list[str]:
    """По каким карточкам собственников вообще не разговаривали.

    Единственный блок сводки, который смотрит не на сутки, а на состояние.
    Карточка, до которой не дошли руки полгода, вчера ничем себя не
    проявила: суточное окно её не покажет никогда, а вопрос «как отработали
    выданные контакты» — ровно про неё.

    Ведущее число — сколько карточек лежит без разговора, а не сколько
    звонков сделано. Звонки складываются в большое число даже когда их все
    сделал один человек по трём карточкам.

    Строка про брокеров называет долю, а не количество: у одного в работе
    52 карточки, у другого 11, и «двадцать молчащих» значит у них разное.
    """
    if not work_data or not work_data["cards"]:
        return []
    lines = [
        f"\n🔕 Не трогали вовсе {work_data['nothing']} "
        f"{_cards_word(work_data['nothing'])} из {work_data['cards']} "
        f"({work_data['nothing_share']:.0f}%) — ни звонка, ни записи, ни отметки"
    ]
    if work_data["silent"]:
        lines.append(
            f"  · ещё {work_data['silent']} — звонили, но дольше "
            f"{work_data['silent_days']} дней назад"
        )
    if work_data["marked"]:
        lines.append(
            f"  · {work_data['marked']} с отметкой без разговора: дело закрыто, "
            "звонка в портале нет"
        )
    worst = [
        row for row in work_data["by_user"]
        if row["cards"] >= _MIN_CARDS and row["cold_share"] >= _COLD_SHARE
    ][:3]
    if worst:
        lines.append("  · больше всего лежит у: " + ", ".join(
            f"{row['name']} {row['cold']}/{row['cards']}" for row in worst
        ))
    stage = max(work_data["by_stage"], key=lambda row: row["nothing"],
                default=None)
    if stage and stage["nothing"]:
        lines.append(
            f"  · чаще всего на стадии «{stage['name']}»: "
            f"{stage['nothing']} из {stage['cards']}"
        )
    lines += _meeting_lines(work_data)
    lines += _pickup_lines(work_data)
    return lines


def _meeting_lines(work_data: dict[str, Any]) -> list[str]:
    """Встречи, у которых срок прошёл, а дело не закрыто.

    Печатается только просроченное. Проведённые копятся за всё окно витрины
    и новостью не бывают; назначенные на будущее вопросов не вызывают.
    Просроченная встреча — вопрос: либо не состоялась, либо о ней не
    отчитались, и оба ответа стоят разговора.

    Встречи без даты названы рядом, а не спрятаны. По ним просрочку не
    отличить вовсе, и молчать о размере слепого пятна значит выдать часть
    картины за всю.
    """
    # Ключей нет вовсе там, где встречи не ведут: спрашивать за отсутствие
    # записи можно только если её положено делать.
    overdue = work_data.get("meetings_overdue") or 0
    undated = work_data.get("meetings_undated") or 0
    if not overdue and not undated:
        return []
    lines = []
    if overdue:
        lines.append(
            f"\n📅 Просрочено встреч: {overdue} — срок прошёл, дело не закрыто"
        )
        # Сортировка своя: by_user разложен по холодным карточкам, и первые
        # три из него могут не иметь просрочек вовсе, пока у четвёртого их
        # десяток. Список, названный «у кого», обязан называть тех, у кого.
        worst = sorted(
            (row for row in work_data["by_user"] if row.get("meetings_overdue")),
            key=lambda row: -row["meetings_overdue"],
        )[:3]
        if worst:
            lines.append("  · у кого: " + ", ".join(
                f"{row['name']} {row['meetings_overdue']}" for row in worst
            ))
        held, planned = work_data.get("meetings") or 0, work_data.get("meetings_planned") or 0
        lines.append(f"  · назначено ещё {planned}, проведено за всё окно {held}")
    if undated:
        lines.append(
            f"  · у {undated} встреч срок не проставлен — просрочку по ним "
            "не отличить"
        )
    return lines


def _pickup_lines(work_data: dict[str, Any]) -> list[str]:
    """Кто не берёт трубку.

    Отдельной строкой, а не вместе с карточками: пропущенный вызов — это
    единственная потеря, где клиент пришёл сам. Остальное в сводке говорит,
    что до человека не дошли руки; это — что он дозванивался и не дозвонился.

    Считается по всем звонкам, а не по карточкам воронки: непринятых по
    порталу 5 169, а по открытым карточкам 63. Потери сидят на входе, до
    того как заводится сделка, и счёт через карточки показал бы процент.

    Называются только люди. Верх этой таблицы на живых данных занимают общая
    линия агентства (929 непринятых из 1286) и уволенный сотрудник, на
    которого всё ещё звонят: обе строки настоящие, обе видны на экране, но в
    ежедневном сообщении им не место. Оно должно звать к действию сегодня, а
    не повторять каждое утро один и тот же структурный факт — иначе его
    перестанут читать раньше, чем в нём появится живой человек.
    """
    people = [row for row in work_data["pickup"]
              if row["person"] and row["missed_share"] >= _MISSED_SHARE][:3]
    if not people:
        return []
    # Свой заголовок с пустой строкой перед ним. Подпунктом эта строка
    # прилипала к блоку выше и читалась как его продолжение: непринятые
    # звонки оказывались частью рассказа про встречи, к которым отношения
    # не имеют вовсе.
    return ["\n📞 Не берут трубку: " + ", ".join(
        f"{row['name'] or 'id ' + str(row['user_id'])} "
        f"{row['missed']}/{row['incoming']}" for row in people
    )]


def _cards_word(count: int) -> str:
    return wording.cards(count)


# Кого называть поимённо. Брокер с тремя карточками, из которых молчат две,
# даёт 67% и возглавил бы список, ничего при этом не значив; порог по числу
# карточек оставляет в списке тех, у кого лежит настоящий объём.
_MIN_CARDS = 10
_COLD_SHARE = 50.0

# Порог доли непринятых, за которым человека называют поимённо. Порог по
# числу входящих уже стоит в work._pickup — здесь отсекается тот, кто берёт
# трубку чаще, чем роняет.
_MISSED_SHARE = 50.0


def _sellers_lines(sellers: dict[str, Any] | None) -> list[str]:
    """Где не дорабатывают с собственниками.

    Отвечает не число потерь, а стадия, С КОТОРОЙ ушли: собственник,
    потерянный на переговорах, и собственник, до которого не доехали на
    встречу, — это две разные недоработки, и разговор с брокером о них
    разный.

    Отложенная продажа считается потерей наравне с проигрышем: в портале у
    неё семантика lost, и выдумывать третье состояние там, где агентство
    завело два, значит спорить с самим агентством.
    """
    if not sellers:
        return []
    lines: list[str] = []
    left = sellers["left_work"]
    if left["deals"]:
        where = ", ".join(
            f"«{stage}» {count}" for stage, count in left["by_stage"]
        )
        lines.append(
            f"\n🔻 {_verb(left['deals'], 'Ушла', 'Ушли')} из работы "
            f"{left['deals']} {_deals_word(left['deals'])} — {where}:"
        )
        lines += [
            f"  · {wording.clip(row['title'], 40)} — "
            f"«{row['from_name']}» → «{row['to_name']}»"
            + (f" ({row['assignee']})" if row.get("assignee") else "")
            for row in left["top"]
        ]
    stalled = sellers["stalled"]
    if stalled["deals"]:
        lines.append(
            f"\n🟠 {_verb(stalled['deals'], 'Встала', 'Встали')} "
            f"{stalled['deals']} {_deals_word(stalled['deals'])}:"
        )
        lines += [
            f"  · {wording.clip(row['title'], 40)} — «{row['stage_name']}», "
            f"{round(row['days_in_stage'])} дн"
            + (f" ({row['assignee']})" if row.get("assignee") else "")
            for row in stalled["top"]
        ]
    return lines


def format_advice(selection: Any, today: str) -> str:
    """Блок «что делать сегодня». Пусто — значит сообщения не будет.

    Стоит первым в личной сводке директора и не идёт в общий чат: совет
    называет конкретного человека худшим в компании, и это разговор
    руководителя с РОПом, а не публичное объявление.

    Пустой блок не печатается вовсе. Сводка, каждое утро сообщающая «всё
    спокойно», приучает не открывать себя раньше, чем в ней появится
    важное — и ровно в то утро её и пролистают.
    """
    if not selection or (not selection.advices and not selection.resolved):
        return ""
    lines = [f"☀️ {today} · что делать сегодня", ""]
    for number, item in enumerate(selection.advices, 1):
        mark = _SLOT_MARK.get(item.slot, "•")
        lines.append(f"{number}. {mark} {item.title}")
        lines.append(f"   → {item.action}")
        lines.append(f"   Почему: {item.proof}")
        note = _advice_note(selection.reasons.get(item.key, ""))
        if note:
            lines.append(f"   {note}")
        lines.append(f"   {item.check}")
        lines.append("")
    for row in selection.resolved[:2]:
        what = row.get("who") or row["subject"]
        lines.append(
            f"✅ Сработало · {what}: было {_round(row['was'])}, "
            f"стало {_round(row['now'])}. Говорил {row['days']} дн назад."
        )
    return "\n".join(lines).rstrip()


# Значок места. Три места закреплены, и значок говорит, о чём строка,
# раньше, чем читатель дойдёт до слов.
_SLOT_MARK = {"money": "💰", "work": "🔕", "acute": "⚡"}

# Жанры сообщений. Сводка отвечает «где мы стоим», совет — «что делать
# сегодня», и обязательства у них разные.
KIND_PULSE = "pulse"
KIND_ADVICE = "advice"


def _advice_note(reason: str) -> str:
    """Отметка о том, что об этом уже говорили.

    Повтор обязан назвать себя повтором. Молча повторённый совет читается
    как новый — и в тот день, когда он повторится третий раз, сводке
    перестанут верить.
    """
    return {
        "хуже": "⚠️ Об этом уже говорил — стало хуже.",
        "вернулось": "⚠️ Считал закрытым, проблема вернулась.",
        "снова": "Говорил об этом неделю назад.",
    }.get(reason, "")


def _round(value: float) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f} млн".replace(".", ",")
    if value >= 10_000:
        return f"{value / 1000:.0f} тыс"
    return f"{value:.0f}"


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
            f"  · {wording.clip(row['title'], 44)} — "
            f"{money(row.get('opportunity') or 0)}, "
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
            f"  · {wording.clip(row['title'], 44)} — "
            f"{money(row.get('opportunity') or 0)}, "
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
            f"  · {wording.clip(row['title'], 44)}, "
            f"«{row['from_name']}» → «{row['to_name']}»"
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
        names = ", ".join(wording.clip(row["title"], 30)
                          for row in quality["won_without_amount"][:2])
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
        company_events = funnel.funnel_events(
            conn, window["since"], window["until"],
            on_plan_ids=_on_plan_ids(company),
        )

    # Разбор воронки уходит в общий чат, а не в личную сводку директора: там
    # его видят все, кого он касается. Чат не задан — разбор остаётся в
    # сводке, чтобы выкатка без настройки не потеряла его молча.
        # Воронка продавцов разбирается отдельно и в план не входит: там
        # другой чек и другая работа. Вопрос к ней один — где брокеры не
        # дорабатывают с собственниками, — и отвечают на него уходы в
        # проигрыш и отложенную продажу, а не деньги.
        sellers = funnel.funnel_events(
            conn, window["since"], window["until"],
            categories=[int(settings.sellers_category_id)],
        )
        # События отвечают «что случилось вчера», работа — «по чему вообще
        # не работают». Второй вопрос не суточный: карточка, до которой не
        # дошли руки полгода, вчера ничем себя не проявила.
        sellers_work = work.card_work(
            conn, [int(settings.sellers_category_id)],
        )

    # Советы отбираются ПОСЛЕ закрытия соединения с витриной: отбор ходит в
    # свою базу памяти, и держать оба соединения открытыми ради этого
    # незачем. Кандидатов правила отдают всех подряд — порог накладывает
    # advice.select(), которому значение нужно и для тех, о ком речь уже
    # шла: иначе проверить «стало лучше» было бы не с чем.
    selection = _advice_for(company, company_events, sellers_work)

    chat_id = int(settings.pulse_events_chat_id or 0)
    if not chat_id:
        company["events"] = company_events

    # Владелец отчёта: своя настройка, с откатом на администратора.
    director = int(settings.pulse_digest_to or settings.admin_user_id or 0)
    if director:
        # Советы — ОТДЕЛЬНОЕ сообщение, а не шапка сводки. Причины две.
        #
        # Своя целостность у каждого. У сводки первая строка обязана назвать
        # воронку, по которой посчитан факт, и новости обязаны стоять раньше
        # итога; приклеенный сверху блок ломает и то и другое, а заодно
        # объединяет два разных вопроса — «что делать» и «где мы стоим».
        #
        # И его читают с телефона. Короткое сообщение из трёх пунктов
        # прочитают целиком; те же три пункта в шапке длинной сводки
        # пролистают вместе с ней.
        #
        # Только директору: совет называет человека худшим в компании, и это
        # разговор руководителя с РОПом, а не объявление в общий чат.
        head = format_advice(selection, window["label"])
        if head:
            deliveries.append({
                "user_id": director,
                "name": "Директор · что делать сегодня",
                # Вид сообщения, а не только имя адресата. Сводка и совет —
                # разные жанры с разными обязательствами: сводка обязана
                # назвать воронку в первой строке, совет — назвать человека
                # и число. Отличать их по тексту имени значит однажды
                # проверить не то.
                "kind": KIND_ADVICE,
                "text": head,
                "advice": selection,
            })
        deliveries.append({
            "user_id": director,
            "name": "Директор",
            "text": format_company(company, yesterday, url),
        })

    if chat_id:
        text = format_events(company, company_events, window, sellers,
                             sellers_work)
        if text:
            deliveries.append({
                "chat_id": chat_id, "name": f"Разбор воронки → чат {chat_id}",
                "text": text,
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
    for item in deliveries:
        item.setdefault("kind", KIND_PULSE)
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
            target = (f"chat_id={item['chat_id']}" if item.get("chat_id")
                      else f"user_id={item['user_id']}")
            print(f"\n{'=' * 60}\n{item['name']} ({target})\n{'=' * 60}")
            print(item["text"])
        print(f"\nDRY_RUN: {len(deliveries)} сообщений НЕ отправлено")
        return 0

    for item in deliveries:
        if item.get("chat_id"):
            chunks = send_chat_message_chunked(item["chat_id"], item["text"])
        else:
            chunks = send_user_chat_message_chunked(item["user_id"], item["text"])
        logger.info("Отправлено: %s (%s сообщ.)", item["name"], chunks)
        # Память пишется ПОСЛЕ отправки, а не до. Сводка, упавшая на
        # отправке, не должна замолчать об этой проблеме на неделю: совет,
        # которого никто не прочитал, сказанным не считается.
        _remember(item.get("advice"))
    return 0


def _advice_for(
    company: dict[str, Any],
    events: dict[str, Any],
    sellers_work: dict[str, Any],
):
    """Кандидаты, отобранные по памяти. Ошибка памяти сводку не роняет.

    База памяти — не витрина: она своя, маленькая и может быть недоступна
    (не создана, заблокирована соседней задачей). Числа при этом верны, и
    отменять из-за этого утреннюю сводку неверно — она теряет только блок
    советов, о чём в журнале остаётся строка.
    """
    candidates = advice_rules.collect(
        pulse=company, events=events, sellers_work=sellers_work,
    )
    candidates = advice.only_named(candidates)
    try:
        init_db()
        with db_session() as conn:
            return advice.select(candidates, advice.load(conn))
    except Exception as error:  # pragma: no cover — база памяти недоступна
        logger.warning("Память советов недоступна, блок пропущен: %s", error)
        return None


def _remember(selection) -> None:
    if selection is None:
        return
    try:
        with db_session() as conn:
            advice.remember(conn, selection)
    except Exception as error:  # pragma: no cover — база памяти недоступна
        logger.warning("Не удалось записать память советов: %s", error)


if __name__ == "__main__":
    sys.exit(main())
