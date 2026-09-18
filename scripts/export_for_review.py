#!/usr/bin/env python3
"""Обезличенная выгрузка агрегатов для внешнего разбора.

Читает data/violations.db и data/analytics.db ТОЛЬКО на чтение и пишет
один markdown-файл с агрегатами. В файл не попадают: ФИО, телефоны,
названия карточек, тексты комментариев, id карточек. Брокеры заменены
псевдонимами «Б-01…» (порядок случайный на каждый запуск).

Только стандартная библиотека, зависимостей нет.

    python3 scripts/export_for_review.py                 # data/ → data/review_export.md
    python3 scripts/export_for_review.py --months 6      # глубина окна
    python3 scripts/export_for_review.py --hide-departments

Перед отправкой файл стоит пролистать глазами.
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import statistics
import traceback
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

MSK = timezone(timedelta(hours=3))
OWNER_LEAD, OWNER_DEAL = 1, 2
DIR_IN, DIR_OUT = 1, 2


# ---------------------------------------------------------------- утилиты

def parse_dt(value) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace(" ", "T", 1)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:  # в базах хранится UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MSK)


def week_key(dt: datetime) -> str:
    return (dt - timedelta(days=dt.weekday())).strftime("%Y-%m-%d")


def month_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


def pct(part, whole) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "—"


def quantile(values, q):
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, max(0, round(q * (len(s) - 1))))
    return s[idx]


def fmt_hours(h) -> str:
    if h is None:
        return "—"
    if h < 1:
        return f"{h * 60:.0f} мин"
    if h < 48:
        return f"{h:.1f} ч"
    return f"{h / 24:.1f} дн"


def table(headers, rows) -> str:
    if not rows:
        return "_нет данных_\n"
    out = ["| " + " | ".join(headers) + " |",
           "|" + "---|" * len(headers)]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out) + "\n"


def open_ro(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def has_table(conn, name) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


class Pseudo:
    """Стабильные в пределах запуска псевдонимы: id → «Б-07»."""

    def __init__(self, ids):
        ids = sorted({i for i in ids if i})
        random.shuffle(ids)
        self.map = {i: f"Б-{n + 1:02d}" for n, i in enumerate(ids)}

    def __call__(self, i):
        return self.map.get(i, "Б-??")


# ------------------------------------------------------- раздел: нарушения

def section_violations(conn, since, hide_dept) -> list[str]:
    out = ["## 1. Нарушения (violations.db)\n"]
    if not has_table(conn, "violation_states"):
        return out + ["_таблицы violation_states нет_\n"]

    rows = [dict(r) for r in conn.execute(
        "SELECT entity_type, rule, responsible_id, department, severity, "
        "first_detected_at, last_seen_at, resolved_at, times_seen "
        "FROM violation_states")]
    for r in rows:
        r["first"] = parse_dt(r["first_detected_at"])
        r["resolved"] = parse_dt(r["resolved_at"])
    rows = [r for r in rows if r["first"] and r["first"] >= since]
    now = datetime.now(MSK)

    # 1.1 динамика по неделям
    appeared, resolved = Counter(), Counter()
    for r in rows:
        appeared[week_key(r["first"])] += 1
        if r["resolved"]:
            resolved[week_key(r["resolved"])] += 1
    weeks = sorted(set(appeared) | set(resolved))
    trend = []
    for w in weeks:
        w_end = datetime.fromisoformat(w).replace(tzinfo=MSK) + timedelta(days=7)
        open_at_end = sum(
            1 for r in rows
            if r["first"] < w_end and (not r["resolved"] or r["resolved"] >= w_end)
        )
        trend.append((w, appeared[w], resolved[w], open_at_end))
    out.append("### 1.1. По неделям (неделя = понедельник, МСК)\n")
    out.append(table(["Неделя", "Появилось", "Закрыто", "Открыто на конец недели"], trend))

    # 1.2 по правилам
    by_rule = defaultdict(list)
    for r in rows:
        by_rule[r["rule"]].append(r)
    rule_rows = []
    for rule, items in sorted(by_rule.items(), key=lambda kv: -len(kv[1])):
        fixed = [i for i in items if i["resolved"]]
        hours = [(i["resolved"] - i["first"]).total_seconds() / 3600 for i in fixed]
        open_now = [i for i in items if not i["resolved"]]
        open_age = [(now - i["first"]).total_seconds() / 86400 for i in open_now]
        within_24 = sum(1 for h in hours if h <= 24)
        brokers = len({i["responsible_id"] for i in items})
        rule_rows.append((
            rule, len(items), brokers, pct(len(fixed), len(items)),
            fmt_hours(statistics.median(hours)) if hours else "—",
            pct(within_24, len(items)), len(open_now),
            f"{statistics.median(open_age):.0f} дн" if open_age else "—",
        ))
    out.append("### 1.2. По правилам\n")
    out.append(table(
        ["Правило", "Всего", "Брокеров", "Закрыто", "Медиана до закрытия",
         "Закрыто за 24 ч", "Открыто сейчас", "Медианный возраст открытых"],
        rule_rows))

    # 1.3 повторяемость: одни и те же брокеры или разные
    pseudo = Pseudo(r["responsible_id"] for r in rows)
    per_broker_week = defaultdict(Counter)
    for r in rows:
        per_broker_week[week_key(r["first"])][r["responsible_id"]] += 1
    weeks_sorted = sorted(per_broker_week)
    top_share, overlap = [], []
    prev_top = None
    for w in weeks_sorted:
        c = per_broker_week[w]
        total = sum(c.values())
        top5 = {b for b, _ in c.most_common(5)}
        top_share.append(sum(c[b] for b in top5) / total if total else 0)
        if prev_top is not None:
            overlap.append(len(top5 & prev_top))
        prev_top = top5
    out.append("### 1.3. Концентрация\n")
    active = len({r["responsible_id"] for r in rows})
    out.append(
        f"- Брокеров хотя бы с одним нарушением за окно: {active}\n"
        f"- Доля новых нарушений у топ-5 брокеров недели (медиана по неделям): "
        f"{pct(statistics.median(top_share), 1) if top_share else '—'}\n"
        f"- Сколько брокеров из топ-5 остаются в топ-5 на следующей неделе "
        f"(медиана): {statistics.median(overlap) if overlap else '—'} из 5\n")

    totals = Counter(r["responsible_id"] for r in rows)
    out.append("\nТоп-15 брокеров по числу нарушений за окно (псевдонимы):\n\n")
    top_rows = []
    for b, n in totals.most_common(15):
        items = [r for r in rows if r["responsible_id"] == b]
        dept = "" if hide_dept else (items[0]["department"] or "")
        top_rows.append((pseudo(b), dept, n,
                         sum(1 for i in items if not i["resolved"]),
                         len({i["rule"] for i in items})))
    out.append(table(["Брокер", "Отдел", "Нарушений", "Открыто", "Разных правил"], top_rows))

    # 1.4 по отделам по неделям
    if not hide_dept:
        dept_week = defaultdict(Counter)
        for r in rows:
            dept_week[r["department"] or "(без отдела)"][week_key(r["first"])] += 1
        last_weeks = weeks_sorted[-8:]
        out.append("### 1.4. Новые нарушения по отделам, последние 8 недель\n")
        out.append(table(
            ["Отдел"] + [w[5:] for w in last_weeks],
            [[d] + [dept_week[d][w] for w in last_weeks] for d in sorted(dept_week)]))

    # 1.5 перенос лидов в общий пул и чистые дни
    if has_table(conn, "broker_shared_leads"):
        moved = Counter()
        for r in conn.execute("SELECT moved_at FROM broker_shared_leads"):
            dt = parse_dt(r["moved_at"])
            if dt and dt >= since:
                moved[week_key(dt)] += 1
        out.append("### 1.5. Лиды, ушедшие в «Общие лиды», по неделям\n")
        out.append(table(["Неделя", "Лидов"], sorted(moved.items())))

    if has_table(conn, "broker_daily_metrics"):
        clean = defaultdict(lambda: [0, 0])
        for r in conn.execute(
                "SELECT metric_date, clean_day, had_crm_visit FROM broker_daily_metrics"):
            dt = parse_dt(r["metric_date"])
            if dt and dt >= since:
                k = week_key(dt)
                clean[k][0] += r["clean_day"]
                clean[k][1] += 1
        out.append("### 1.6. Доля «чистых дней» (брокеро-дни без нарушений)\n")
        out.append(table(["Неделя", "Чистых", "Всего брокеро-дней", "Доля"],
                         [(k, c, t, pct(c, t)) for k, (c, t) in sorted(clean.items())]))

    if has_table(conn, "broker_ratings"):
        avg = defaultdict(list)
        for r in conn.execute("SELECT snapshot_date, score FROM broker_ratings"):
            dt = parse_dt(r["snapshot_date"])
            if dt and dt >= since:
                avg[week_key(dt)].append(r["score"])
        out.append("### 1.7. Средний балл рейтинга по неделям\n")
        out.append(table(["Неделя", "Средний балл", "Медиана", "Замеров"],
                         [(k, f"{statistics.mean(v):.1f}", f"{statistics.median(v):.1f}", len(v))
                          for k, v in sorted(avg.items())]))
    return out


# -------------------------------------------------------- раздел: воронки

def section_funnels(conn, since) -> list[str]:
    out = ["## 2. Воронки и скорость (analytics.db)\n"]
    need = ["fact_lead", "fact_deal"]
    if not all(has_table(conn, t) for t in need):
        return out + ["_нет таблиц fact_lead / fact_deal_\n"]

    sources = {}
    if has_table(conn, "dim_source"):
        sources = {r["source_id"]: r["name"] for r in conn.execute(
            "SELECT source_id, name FROM dim_source")}
    lead_sem = {}
    if has_table(conn, "dim_lead_status"):
        lead_sem = {r["status_id"]: (r["name"], r["semantic"]) for r in conn.execute(
            "SELECT status_id, name, semantic FROM dim_lead_status")}
    pipelines = {}
    if has_table(conn, "dim_pipeline"):
        pipelines = {r["category_id"]: r["name"] for r in conn.execute(
            "SELECT category_id, name FROM dim_pipeline")}

    leads = []
    for r in conn.execute(
            "SELECT lead_id, status_id, source_id, assigned_by_id, date_create, "
            "is_converted, converted_deal_id FROM fact_lead WHERE is_deleted = 0"):
        dt = parse_dt(r["date_create"])
        if dt and dt >= since:
            d = dict(r)
            d["created"] = dt
            leads.append(d)

    deals = {}
    for r in conn.execute(
            "SELECT deal_id, category_id, stage_id, source_id, opportunity, date_create, "
            "closedate, moved_time, is_closed, is_won, is_lost, lead_id "
            "FROM fact_deal WHERE is_deleted = 0"):
        d = dict(r)
        d["created"] = parse_dt(r["date_create"])
        d["closed"] = parse_dt(r["closedate"]) if r["is_closed"] else None
        if d["is_closed"] and r["moved_time"]:
            d["closed"] = parse_dt(r["moved_time"]) or d["closed"]
        deals[r["deal_id"]] = d

    # 2.1 лиды по месяцам и статусам
    by_month = defaultdict(Counter)
    for lead in leads:
        m = month_key(lead["created"])
        by_month[m]["всего"] += 1
        sem = lead_sem.get(lead["status_id"], ("", ""))[1]
        by_month[m][sem or "?"] += 1
        if lead["is_converted"]:
            by_month[m]["в сделку"] += 1
    out.append("### 2.1. Лиды по месяцам создания\n")
    out.append(table(
        ["Месяц", "Лидов", "В работе", "Успех (статус)", "Провал (статус)",
         "Сконвертировано в сделку"],
        [(m, c["всего"], c["in_progress"], c["won"], c["lost"],
          f"{c['в сделку']} ({pct(c['в сделку'], c['всего'])})")
         for m, c in sorted(by_month.items())]))

    status_counts = Counter(
        lead_sem.get(lead["status_id"], (lead["status_id"], ""))[0] for lead in leads)
    out.append("\nРаспределение лидов окна по текущему статусу:\n\n")
    out.append(table(["Статус", "Лидов", "Доля"],
                     [(s, n, pct(n, len(leads))) for s, n in status_counts.most_common()]))

    # 2.2 источники: лид → сделка → выигрыш
    src_stat = defaultdict(lambda: Counter())
    src_money = defaultdict(float)
    for lead in leads:
        s = sources.get(lead["source_id"], lead["source_id"] or "(пусто)")
        src_stat[s]["leads"] += 1
        if lead["is_converted"]:
            src_stat[s]["conv"] += 1
            d = deals.get(lead["converted_deal_id"])
            if d and d["is_won"]:
                src_stat[s]["won"] += 1
                src_money[s] += d["opportunity"] or 0
            if d and d["is_lost"]:
                src_stat[s]["lost"] += 1
    out.append("### 2.2. Источники лидов: путь до выигрыша (когорта лидов окна)\n")
    out.append(table(
        ["Источник", "Лидов", "В сделку", "Сделок выиграно", "Сделок проиграно",
         "Лид → победа", "Комиссия по победам, млн ₽"],
        [(s, c["leads"], f"{c['conv']} ({pct(c['conv'], c['leads'])})", c["won"], c["lost"],
          pct(c["won"], c["leads"]), f"{src_money[s] / 1e6:.2f}")
         for s, c in sorted(src_stat.items(), key=lambda kv: -kv[1]["leads"])]))

    # 2.3 скорость первого контакта по лидам
    out.extend(_first_contact(conn, leads, sources))

    # 2.4 сделки по воронкам и месяцам
    cat_month = defaultdict(lambda: defaultdict(Counter))
    cycle = defaultdict(list)
    cover = defaultdict(lambda: [0, 0])
    for d in deals.values():
        cat = pipelines.get(d["category_id"], str(d["category_id"]))
        if d["created"] and d["created"] >= since:
            cat_month[cat][month_key(d["created"])]["created"] += 1
        if d["closed"] and d["closed"] >= since:
            k = "won" if d["is_won"] else "lost" if d["is_lost"] else "closed"
            cat_month[cat][month_key(d["closed"])][k] += 1
            if d["is_won"]:
                cat_month[cat][month_key(d["closed"])]["money"] += d["opportunity"] or 0
                cover[cat][1] += 1
                if d["opportunity"]:
                    cover[cat][0] += 1
                if d["created"]:
                    cycle[cat].append((d["closed"] - d["created"]).days)
    out.append("### 2.4. Сделки по воронкам (создано — по дате создания; "
               "выиграно/проиграно — по дате закрытия)\n")
    for cat in sorted(cat_month):
        months = cat_month[cat]
        out.append(f"\n**{cat}** — медианный цикл выигранной сделки: "
                   f"{statistics.median(cycle[cat]) if cycle[cat] else '—'} дн; "
                   f"сумма заполнена у {pct(*cover[cat])} побед\n\n")
        out.append(table(
            ["Месяц", "Создано", "Выиграно", "Проиграно", "Win rate закрытых", "Комиссия, млн ₽"],
            [(m, c["created"], c["won"], c["lost"],
              pct(c["won"], c["won"] + c["lost"]), f"{c['money'] / 1e6:.2f}")
             for m, c in sorted(months.items())]))

    # 2.5 прохождение этапов (когорта сделок окна)
    out.extend(_stage_reach(conn, deals, since, pipelines))

    # 2.6 нагрузка
    per_broker_month = defaultdict(set)
    for lead in leads:
        per_broker_month[month_key(lead["created"])].add(lead["assigned_by_id"])
    out.append("### 2.6. Лидов на брокера\n")
    out.append(table(
        ["Месяц", "Брокеров с лидами", "Лидов на брокера (среднее)"],
        [(m, len(b), f"{by_month[m]['всего'] / len(b):.1f}" if b else "—")
         for m, b in sorted(per_broker_month.items())]))
    return out


def _first_contact(conn, leads, sources) -> list[str]:
    out = ["### 2.3. Скорость первого контакта по лидам\n"]
    if not has_table(conn, "fact_activity"):
        return out + ["_таблицы fact_activity нет_\n"]

    ptypes = Counter(r["provider_type_id"] for r in conn.execute(
        "SELECT provider_type_id FROM fact_activity WHERE owner_type_id = ?", (OWNER_LEAD,)))
    out.append("Типы дел по лидам в витрине (для проверки, что звонки распознаны): "
               + ", ".join(f"`{k or '(пусто)'}`: {v}" for k, v in ptypes.most_common(8)) + "\n\n")

    calls = defaultdict(list)
    for r in conn.execute(
            "SELECT owner_id, direction, created_at, start_time, completed "
            "FROM fact_activity WHERE owner_type_id = ? AND UPPER(provider_type_id) = 'CALL'",
            (OWNER_LEAD,)):
        dt = parse_dt(r["start_time"]) or parse_dt(r["created_at"])
        if dt:
            calls[r["owner_id"]].append((dt, r["direction"], r["completed"]))

    inbound_origin = 0
    no_call = 0
    delays = []
    by_src = defaultdict(list)
    by_bucket = defaultdict(list)
    for lead in leads:
        created = lead["created"]
        lc = sorted(calls.get(lead["lead_id"], []), key=lambda c: c[0])
        first_in = next((c for c in lc if c[1] == DIR_IN), None)
        if first_in and abs((first_in[0] - created).total_seconds()) <= 180:
            inbound_origin += 1  # лид родился из входящего звонка
            continue
        first_out = next(
            (c for c in lc
             if c[1] == DIR_OUT and c[0] >= created - timedelta(minutes=1)), None)
        if not first_out:
            no_call += 1
            continue
        h = max(0.0, (first_out[0] - created).total_seconds() / 3600)
        delays.append(h)
        by_src[sources.get(lead["source_id"], lead["source_id"] or "(пусто)")].append(h)
        work = created.weekday() < 5 and 9 <= created.hour < 19
        by_bucket["рабочее время (пн–пт 9–19)" if work else "нерабочее время"].append(h)

    total = len(leads)
    out.append(
        f"- Лидов в окне: {total}\n"
        f"- Лид создан входящим звонком (контакт уже состоялся): "
        f"{inbound_origin} ({pct(inbound_origin, total)})\n"
        f"- Исходящего звонка по лиду нет вообще: {no_call} ({pct(no_call, total)})\n"
        f"- С исходящим звонком: {len(delays)}\n\n")

    def row(name, vals):
        return (name, len(vals), fmt_hours(quantile(vals, 0.5)), fmt_hours(quantile(vals, 0.75)),
                pct(sum(1 for v in vals if v <= 0.25), len(vals)),
                pct(sum(1 for v in vals if v <= 1), len(vals)),
                pct(sum(1 for v in vals if v > 24), len(vals)))

    hdr = ["Срез", "Лидов", "Медиана до звонка", "75-й перцентиль", "≤ 15 мин", "≤ 1 ч", "> 24 ч"]
    rows = [row("Все лиды с исходящим", delays)]
    rows += [row(k, v) for k, v in sorted(by_bucket.items())]
    rows += [row(f"Источник: {k}", v)
             for k, v in sorted(by_src.items(), key=lambda kv: -len(kv[1]))
             if len(v) >= 10]
    out.append(table(hdr, rows))
    out.append("\n_Звонком считается дело с PROVIDER_TYPE_ID = CALL; "
               "направление 1 — входящий, 2 — исходящий._\n")
    return out


def _stage_reach(conn, deals, since, pipelines) -> list[str]:
    out = ["### 2.5. Доходимость до этапов (сделки, созданные в окне)\n"]
    if not (has_table(conn, "fact_stage_event") and has_table(conn, "dim_stage")):
        return out + ["_нет fact_stage_event / dim_stage_\n"]
    stage_meta = {(r["category_id"], r["stage_id"]): (r["name"], r["sort"], r["semantic"])
                  for r in conn.execute(
                      "SELECT category_id, stage_id, name, sort, semantic FROM dim_stage")}
    cohort = {i for i, d in deals.items() if d["created"] and d["created"] >= since}
    reached = defaultdict(lambda: defaultdict(set))
    for r in conn.execute(
            "SELECT entity_id, category_id, stage_id FROM fact_stage_event "
            "WHERE entity_type = 'deal'"):
        if r["entity_id"] in cohort:
            reached[r["category_id"]][r["stage_id"]].add(r["entity_id"])
    for cat in sorted(reached):
        size = sum(1 for i in cohort if deals[i]["category_id"] == cat)
        rows = []
        for stage, ids in sorted(reached[cat].items(),
                                 key=lambda kv: stage_meta.get((cat, kv[0]), ("", 999, ""))[1]):
            name, _, sem = stage_meta.get((cat, stage), (stage, 0, ""))
            rows.append((name, sem, len(ids), pct(len(ids), size)))
        out.append(f"\n**{pipelines.get(cat, cat)}** — когорта {size} сделок\n\n")
        out.append(table(["Этап", "Тип", "Дошло сделок", "Доля когорты"], rows))
    return out


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data", help="каталог с базами (по умолчанию data)")
    ap.add_argument("--months", type=int, default=6, help="глубина окна в месяцах")
    ap.add_argument("--out", default=None,
                    help="путь к результату (по умолчанию <data>/review_export.md)")
    ap.add_argument("--hide-departments", action="store_true", help="не выводить названия отделов")
    args = ap.parse_args()

    data = Path(args.data)
    since = datetime.now(MSK) - timedelta(days=30 * args.months)
    out_path = Path(args.out) if args.out else data / "review_export.md"

    parts = [f"# Выгрузка для разбора\n\nСформировано: {datetime.now(MSK):%Y-%m-%d %H:%M} МСК. "
             f"Окно: с {since:%Y-%m-%d} ({args.months} мес.). Персональных данных нет.\n"]

    for name, fn in (("violations.db",
                      lambda c: section_violations(c, since, args.hide_departments)),
                     ("analytics.db", lambda c: section_funnels(c, since))):
        conn = open_ro(data / name)
        if conn is None:
            # Без пути: файл уезжает наружу, а путь каталога данных —
            # это устройство сервера, которое читающей модели не нужно.
            parts.append(f"\n_{name} не найден_\n")
            continue
        try:
            parts.extend(fn(conn))
        except Exception as exc:  # раздел упал — остальное всё равно нужно
            # В файл — только тип и текст ошибки: он уезжает наружу, а трейсбек
            # несёт пути каталогов сервера. Разбирать падение всё равно тому,
            # кто запускал, — ему трейсбек печатается на stderr.
            parts.append(
                f"\n**Раздел {name} не собран: "
                f"{type(exc).__name__}: {exc}**\n"
            )
            traceback.print_exc()
        finally:
            conn.close()

    out_path.write_text("\n".join(parts), encoding="utf-8")
    print(f"Готово: {out_path} ({out_path.stat().st_size // 1024} КБ)")


if __name__ == "__main__":
    main()
