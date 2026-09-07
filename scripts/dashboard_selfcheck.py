"""Самопроверка дашборда после правок аудита. Только чтение, только числа.

Запуск в контейнере (витрина монтируется как обычно):

    docker run --rm --env-file .env \
      -v $(pwd)/data:/app/data -v $(pwd)/scripts:/app/scripts:ro \
      b24-ai-auditor:latest python scripts/dashboard_selfcheck.py

В вывод не попадают ни названия сделок, ни имена людей — только счётчики,
суммы и вердикты. Витрина открывается в режиме только для чтения.
"""

import sys
from datetime import datetime
from pathlib import Path

# Скрипт запускают и из репозитория, и примонтированным в контейнер одним
# файлом — ищем src по всем разумным местам, а не по одному.
_HERE = Path(__file__).resolve()
for _candidate in (_HERE.parent.parent / "src", _HERE.parent / "src",
                   Path("/app/src"), Path.cwd() / "src"):
    if _candidate.is_dir():
        sys.path.insert(0, str(_candidate))
        break

from analytics import metrics, plans  # noqa: E402
from analytics import pulse as pulse_metrics  # noqa: E402
from analytics.scope import Scope, scoped_session  # noqa: E402

# Правки аудита добавили эти имена. Если их нет — крутится старый код, и
# дальше идти незачем: вывод будет про образ, который вы уже заменили.
_REQUIRED = ("base_currency", "MAX_EXPORT_ROWS", "funnel_norm_days")
_MISSING = [name for name in _REQUIRED if not hasattr(metrics, name)]

OK, BAD, INFO = "  ✔", "  ✘", "  ·"


def head(title):
    print()
    print(title)
    print("-" * len(title))


def money(value):
    return f"{value:,.0f}".replace(",", " ")


def main():
    print("САМОПРОВЕРКА ДАШБОРДА")
    print("=" * 60)
    if _MISSING:
        print()
        print(f"{BAD} В образе старый код: нет {', '.join(_MISSING)}.")
        print("     Правки аудита не доехали — пересоберите образ и повторите.")
        return 1

    month = metrics.resolve_period("prev_month")
    days30 = metrics.resolve_period("30d")

    head("1. Границы периодов (правка: московский календарь)")
    print(f"{INFO} прошлый месяц: {month['since']} → {month['until']}")
    print(f"{INFO} 30 дней:       {days30['since']} → {days30['until']}")
    msk_ok = month["since"].endswith("21:00:00+00:00")
    print(f"{OK if msk_ok else BAD} начало периода = полночь по Москве"
          f"{'' if msk_ok else ' — ПЕРИОДЫ ВСЁ ЕЩЁ ПО UTC, код не обновился'}")

    with scoped_session(Scope.everything()) as conn:
        pipelines = metrics.pipelines(conn)
        base = metrics.base_currency()

        head("2. Объём витрины")
        status = metrics.etl_status(conn)
        counts = status["counts"]
        print(f"{INFO} сделок {counts['deals']}, лидов {counts['leads']}, "
              f"событий стадий {counts['stage_events']}")
        print(f"{INFO} окно витрины с {status['window_since']}")
        lag = status["lag_minutes"]
        print(f"{OK if lag is not None and lag < 120 else BAD} свежесть данных: "
              f"{'нет успешных прогонов' if lag is None else str(lag) + ' мин назад'}")

        head("3. Журнал прогонов ETL (правка: падение оставляет след)")
        runs = status["runs"]
        errors = [r for r in runs if r["status"] == "error"]
        for run in runs[:8]:
            print(f"{INFO} {run['kind']:<12} {run['status']:<8} "
                  f"строк {run['rows_upserted']:<7} {run['finished_at'] or 'не завершён'}")
        print(f"{INFO} всего записей в журнале: {len(runs)}, из них с ошибкой: {len(errors)}")
        if errors:
            print(f"{BAD} есть упавшие прогоны — раньше они были невидимы, теперь видны:")
            for run in errors[:3]:
                print(f"      {run['kind']}: {run['error'][:120]}")
        full_runs = [r for r in runs if r["kind"] == "full"]
        print(f"{OK if full_runs else BAD} ночная полная сверка в журнале: "
              f"{'есть' if full_runs else 'НЕТ — либо ещё не отрабатывала, либо не запускается'}")

        head(f"4. Валюты (базовая: {base})")
        foreign = metrics._rows(
            conn,
            "SELECT COALESCE(NULLIF(currency_id, ''), '(пусто)') AS cur, COUNT(*) AS n,"
            " SUM(is_won) AS won FROM v_deal"
            " WHERE currency_id <> '' AND upper(currency_id) <> :base"
            " GROUP BY currency_id ORDER BY n DESC",
            {"base": base},
        )
        if not foreign:
            print(f"{OK} нерублёвых сделок нет — денежные цифры не изменились")
        else:
            print(f"{BAD} есть сделки в другой валюте, в суммы они не входят:")
            for row in foreign:
                print(f"      {row['cur']}: {row['n']} шт, из них выиграно {row['won'] or 0}")

        head("5. Норма стадий считается по сделкам, не по лидам")
        mix = metrics._one(
            conn,
            "SELECT SUM(CASE WHEN entity_type = 'lead' THEN 1 ELSE 0 END) AS leads,"
            " SUM(CASE WHEN entity_type = 'deal' THEN 1 ELSE 0 END) AS deals"
            " FROM fact_stage_event WHERE category_id = 0 AND duration_sec IS NOT NULL",
        )
        print(f"{INFO} завершённых интервалов с category_id=0: "
              f"сделок {mix['deals'] or 0}, лидов {mix['leads'] or 0}")
        norm_rows = metrics._one(
            conn, "SELECT COUNT(*) AS n FROM v_stage_norm WHERE category_id = 0")
        expected = mix["deals"] or 0
        norm_ok = int(norm_rows["n"] or 0) == expected
        print(f"{OK if norm_ok else BAD} в норме воронки 0 участвует {norm_rows['n']} интервалов "
              f"(ожидалось {expected} — только сделки)")

        for pipeline in pipelines:
            cat = pipeline["category_id"]
            name = pipeline["name"]
            head(f"6. Воронка «{name}» (id {cat})")

            for label, period in (("прошлый месяц", month), ("30 дней", days30)):
                wins = metrics.win_rate(conn, cat, period["since"], period["until"])
                mon = metrics.money(conn, cat, period["since"], period["until"])
                ppl = metrics.people(conn, period["since"], period["until"], cat)
                people_sum = sum(r["won_amount"] or 0 for r in ppl)
                people_won = sum(r["won"] or 0 for r in ppl)
                same = abs(people_sum - mon["won_amount"]) < 1 and people_won == mon["won_deals"]
                print(f"{INFO} {label}: выиграно {wins['won']} шт "
                      f"на {money(wins['won_amount'])} {base}, "
                      f"доля побед {wins['win_rate']}%")
                verdict = "=" if same else "≠"
                print(f"{OK if same else BAD} сумма по сотрудникам "
                      f"{money(people_sum)} ({people_won} шт) {verdict} "
                      f"итог {money(mon['won_amount'])} ({mon['won_deals']} шт)")
                cover_ok = mon["won_filled"] <= mon["won_deals"]
                tail = "" if cover_ok else " — числитель больше знаменателя, старый код"
                print(f"{OK if cover_ok else BAD} покрытие "
                      f"{mon['won_filled']} из {mon['won_deals']}{tail}")
                print(f"{INFO} средний чек {money(wins['avg_check'])} {base} "
                      f"по {wins['avg_check_base']} сделкам с заполненной суммой")
                if wins.get("won_foreign"):
                    print(f"{INFO} из выигранных {wins['won_foreign']} в другой валюте — "
                          f"в сумму не вошли")

            forecast = metrics.weighted_forecast(conn, cat)
            priced = [s for s in forecast["by_stage"] if s["probability"] is not None]
            print(f"{INFO} прогноз: {money(forecast['expected'])} {base} по {len(priced)} стадиям "
                  f"из {len(forecast['by_stage'])}")
            if forecast["unpriced_deals"]:
                print(f"{INFO} не оценено {money(forecast['unpriced_amount'])} {base} "
                      f"по {forecast['unpriced_deals']} сделкам (мало закрытых на стадии)")
            if not priced and forecast["by_stage"]:
                print(f"{BAD} ни одна стадия не набрала {forecast['min_closed']} закрытых сделок — "
                      f"прогноз показывать нечем, карточку лучше снять")

            stuck = metrics.stuck_deals(conn, cat)
            ids = [row["deal_id"] for row in stuck]
            dupes = len(ids) - len(set(ids))
            by_funnel = sum(1 for row in stuck if row.get("threshold_source") == "воронка")
            print(f"{INFO} зависших: {len(stuck)}"
                  f"{' (список обрезан по лимиту 50)' if len(stuck) >= 50 else ''}, "
                  f"из них порог по воронке у {by_funnel}")
            print(f"{OK if dupes == 0 else BAD} дублей карточек в списке: {dupes}")
            # Два набора, а не подмножество: норма считается по истории
            # переходов, а список стадий — это воронка сегодня. Стадию
            # переименовали или убрали — её интервалы в истории остались, и
            # числитель без этой поправки оказывался больше знаменателя.
            # «Норма есть у 10 стадий из 8» читается как поломка, хотя это
            # два разных вопроса.
            norms = metrics.stage_norms(conn, cat)
            stages_all = metrics.stages(conn, cat)
            current = {row["stage_id"] for row in stages_all}
            covered = sum(1 for stage_id in norms if stage_id in current)
            retired = len(norms) - covered
            print(f"{INFO} своя норма есть у {covered} стадий из {len(stages_all)}"
                  + (f"; ещё у {retired} — стадии, которых в воронке уже нет"
                     if retired else ""))

        head("7. Лиды: скорость первой обработки")
        first = metrics.lead_first_move_days(conn, month["since"], month["until"])
        if not first["supported"]:
            print(f"{INFO} портал не отдаёт историю статусов лидов — метрика не считается")
        else:
            print(f"{INFO} за прошлый месяц в расчёте {first['count']} лидов, "
                  f"из них ещё не обработаны {first['waiting']}")
            print(f"{INFO} медиана {first['median']} дн, 90-й перцентиль {first['p90']} дн")
            print(f"{OK if first['count'] else BAD} необработанные учитываются "
                  f"{'(медиана — оценка снизу)' if first['waiting'] else ''}")

        head("8. Выгрузка")
        page = metrics.entity_table(conn, entity="deal", page_size=10_000)
        export = metrics.entity_table(conn, entity="deal", page_size=10_000,
                                      max_rows=metrics.MAX_EXPORT_ROWS)
        print(f"{INFO} всего сделок под фильтром: {page['total']}")
        print(f"{INFO} потолок страницы {metrics.MAX_PAGE_SIZE}, "
              f"потолок выгрузки {metrics.MAX_EXPORT_ROWS}")
        print(f"{INFO} страница отдаёт {len(page['rows'])}, выгрузка отдаёт {len(export['rows'])}")
        if page["total"] <= metrics.MAX_PAGE_SIZE:
            print(f"{INFO} сделок меньше потолка страницы — разницу видно не будет, "
                  f"это нормально")
        else:
            wider = len(export["rows"]) > len(page["rows"])
            print(f"{OK if wider else BAD} выгрузка шире страницы"
                  f"{'' if wider else ' — обрезка осталась, код старый'}")

        head("9. Пульс: план и факт человека в одном отделе")
        code = plans.quarter_code(datetime.now(metrics.BUSINESS_TZ).date())
        data = pulse_metrics.pulse(conn, code)
        bounds = plans.period(conn, code)
        print(f"{INFO} {plans.quarter_label(code)}: план {money(data['plan'])} {base}, "
              f"факт {money(data['fact'])} {base}")

        by_dept = sum(row["fact"] for row in data["departments"])
        same = abs(by_dept - data["fact"]) < 1
        print(f"{OK if same else BAD} сумма по отделам {money(by_dept)} "
              f"= итог {money(data['fact'])}")

        split = all(
            abs(row["fact_on_plan"] + row["others_fact"] - row["fact"]) < 1
            for row in data["departments"]
        )
        print(f"{OK if split else BAD} в каждом отделе «с нормой» + «без нормы» = факт")

        # Деньги вне плановых отделов. Исключённый из плана отдел продолжает
        # работать и закрывать сделки — в «Пульс» они не входят, и это
        # осознанное решение. Но разница между кассой агентства и суммой на
        # экране обязана быть названа числом: молчащая, она однажды всплывёт
        # как «дашборд врёт», и доверия к остальным цифрам не останется.
        #
        # Условие по валюте берётся у метрик (_money_of), а не пишется здесь
        # заново: два ответа на вопрос «какие суммы можно складывать» разошлись
        # бы молча, и самопроверка врала бы убедительнее проверяемого.
        whole = metrics._one(
            conn,
            f"""
            SELECT COALESCE(SUM(CASE WHEN {metrics._money_of()}
                                     THEN opportunity ELSE 0 END), 0) AS amount
            FROM v_deal
            WHERE is_won = 1 AND closedate IS NOT NULL
              AND closedate >= :since AND closedate < :until
            """,
            {"since": bounds["starts_at"], "until": bounds["ends_at"],
             "base": base},
        )
        outside = float(whole.get("amount") or 0) - data["fact"]
        print(f"{INFO} выиграно за квартал всего {money(float(whole['amount']))} {base}; "
              f"вне отделов, несущих план: {money(outside)} {base}")

        # Ростер решает, чей человек. Перенос в отдел, которого нет в списке
        # плановых, тихо выносит его деньги из отчёта целиком — это не
        # «занижено на процент», это вычеркнутый человек.
        allowed = plans.sales_department_ids()
        moved = metrics._rows(
            conn,
            "SELECT user_id, department_id FROM v_plan_roster "
            "WHERE department_id IS NOT NULL AND period_code IN (:code, :any)",
            {"code": code, "any": plans.ANY_PERIOD},
        )
        print(f"{INFO} ростер переносит людей между отделами: {len(moved)}")
        if not allowed:
            # Пустой список — это не «переносить некуда нельзя», а «сверять не
            # с чем». Печатать здесь галочку значит отчитаться о проверке,
            # которая не выполнялась, — а ей поверят ровно так же, как
            # настоящей.
            print(f"{BAD} список плановых отделов пуст: OWNER_SALES_DEPT_IDS_JSON "
                  f"не прочитан, перенос сверять не с чем")
        else:
            lost = [row for row in moved if row["department_id"] not in allowed]
            print(f"{OK if not lost else BAD} все перенесены в отделы, несущие план"
                  + ("" if not lost
                     else f" — {len(lost)} в отделы вне плана, "
                          f"их деньги в отчёт не войдут"))

    print()
    print("=" * 60)
    print("Готово. Пришлите этот вывод целиком.")


if __name__ == "__main__":
    raise SystemExit(main() or 0)
