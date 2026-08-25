"""Метрики витрины: конверсии, движение, деньги, разложение до карточек."""

import analytics  # noqa: F401  — кладёт src/analytics на sys.path
import metrics
import pytest
from schema import analytics_session


@pytest.fixture
def seeded(analytics_db):
    """Маленькая, но полная витрина: воронка 18 с тремя стадиями."""
    with analytics_session() as conn:
        conn.execute(
            "INSERT INTO dim_pipeline(category_id, name, is_active, sort, synced_at) "
            "VALUES (18, 'Покупатели', 1, 10, '2026-08-01T00:00:00+00:00')"
        )
        for stage_id, name, sort, semantic in (
            ("C18:NEW", "Подбор", 10, "in_progress"),
            ("C18:SHOW", "Показ", 20, "in_progress"),
            ("C18:WON", "Договор закрыт", 90, "won"),
            ("C18:APOLOGY", "Проиграна", 95, "lost"),
        ):
            conn.execute(
                "INSERT INTO dim_stage(stage_id, category_id, name, sort, semantic, synced_at) "
                "VALUES (?, 18, ?, ?, ?, '2026-08-01T00:00:00+00:00')",
                (stage_id, name, sort, semantic),
            )
        conn.execute(
            "INSERT INTO dim_user(user_id, name, department_id, department_name, "
            "is_active, synced_at) VALUES (32, 'Иван Петров', 44, 'Отдел Трофимовой', 1, 'x')"
        )
        conn.execute(
            "INSERT INTO dim_source(source_id, name, synced_at) VALUES ('CALL', 'Звонок', 'x')"
        )
        conn.execute(
            "INSERT INTO dim_lead_status(status_id, name, sort, semantic, synced_at) "
            "VALUES ('NEW', 'Не обработан', 10, 'in_progress', 'x')"
        )
        conn.execute(
            "INSERT INTO dim_lead_status(status_id, name, sort, semantic, synced_at) "
            "VALUES ('CONVERTED', 'Квалифицирован', 20, 'in_progress', 'x')"
        )

        # Сделка 1: дошла до показа и выиграна, сумма есть.
        _deal(conn, 1, "C18:WON", won=True, amount=500000, closed="2026-08-10T00:00:00+00:00")
        _events(conn, 1, [
            ("C18:NEW", "2026-08-01T00:00:00+00:00", "2026-08-03T00:00:00+00:00"),
            ("C18:SHOW", "2026-08-03T00:00:00+00:00", "2026-08-10T00:00:00+00:00"),
            ("C18:WON", "2026-08-10T00:00:00+00:00", None),
        ])
        # Сделка 2: застряла на подборе, сумма НЕ заполнена.
        _deal(conn, 2, "C18:NEW", amount=0)
        _events(conn, 2, [("C18:NEW", "2026-08-01T00:00:00+00:00", None)])
        # Сделка 3: проиграна на показе.
        _deal(conn, 3, "C18:APOLOGY", lost=True, amount=300000,
              closed="2026-08-08T00:00:00+00:00")
        _events(conn, 3, [
            ("C18:NEW", "2026-08-01T00:00:00+00:00", "2026-08-04T00:00:00+00:00"),
            ("C18:SHOW", "2026-08-04T00:00:00+00:00", "2026-08-08T00:00:00+00:00"),
            ("C18:APOLOGY", "2026-08-08T00:00:00+00:00", None),
        ])

        conn.execute(
            "INSERT INTO fact_lead(lead_id, title, status_id, source_id, assigned_by_id, "
            "date_create, is_converted, converted_deal_id, is_deleted, synced_at) "
            "VALUES (10, 'Лид 10', 'CONVERTED', 'CALL', 32, '2026-08-01T00:00:00+00:00', "
            "1, 1, 0, 'x')"
        )
        conn.execute(
            "INSERT INTO fact_lead(lead_id, title, status_id, source_id, assigned_by_id, "
            "date_create, is_converted, is_deleted, synced_at) "
            "VALUES (11, 'Лид 11', 'NEW', 'CALL', 32, '2026-08-01T00:00:00+00:00', 0, 0, 'x')"
        )
    return analytics_db


def _deal(conn, deal_id, stage, *, won=False, lost=False, amount=0, closed=None):
    conn.execute(
        """
        INSERT INTO fact_deal(deal_id, title, category_id, stage_id, assigned_by_id,
            source_id, opportunity, date_create, closedate, is_closed, is_won, is_lost,
            is_deleted, synced_at)
        VALUES (?, ?, 18, ?, 32, 'CALL', ?, '2026-08-01T00:00:00+00:00', ?, ?, ?, ?, 0, 'x')
        """,
        (deal_id, f"Сделка {deal_id}", stage, amount, closed,
         1 if closed else 0, 1 if won else 0, 1 if lost else 0),
    )


def _events(conn, deal_id, intervals):
    for seq, (stage, entered, left) in enumerate(intervals):
        duration = None
        if left:
            from datetime import datetime
            duration = int(
                (datetime.fromisoformat(left) - datetime.fromisoformat(entered)).total_seconds()
            )
        conn.execute(
            "INSERT INTO fact_stage_event(entity_type, entity_id, category_id, stage_id, "
            "entered_at, left_at, duration_sec, seq) VALUES ('deal', ?, 18, ?, ?, ?, ?, ?)",
            (deal_id, stage, entered, left, duration, seq),
        )


AUG = {"since": "2026-08-01T00:00:00+00:00", "until": "2026-09-01T00:00:00+00:00"}


def test_cohort_reach_counts_ever_reached_not_current_stage(seeded):
    """Ключевое различие витрины: «дошёл до стадии» ≠ «стоит на стадии сейчас»."""
    with analytics_session(readonly=True) as conn:
        funnel = metrics.deal_funnel(conn, 18, AUG["since"], AUG["until"])
    by_stage = {s["stage_id"]: s for s in funnel["stages"]}

    assert funnel["cohort_size"] == 3
    # На «Показе» сейчас не стоит никто, но дошли до него две сделки.
    assert by_stage["C18:SHOW"]["count_now"] == 0
    assert by_stage["C18:SHOW"]["reached"] == 2
    assert by_stage["C18:SHOW"]["conversion_from_start"] == pytest.approx(66.7)
    assert by_stage["C18:NEW"]["reached"] == 3


def test_step_conversion_runs_along_the_working_chain(seeded):
    """Цепочка шагов идёт по стадиям «в работе»: WON и LOSE стоят параллельно."""
    with analytics_session(readonly=True) as conn:
        funnel = metrics.deal_funnel(conn, 18, AUG["since"], AUG["until"])
    by_stage = {s["stage_id"]: s for s in funnel["stages"]}
    assert by_stage["C18:NEW"]["conversion_step"] is None  # первая стадия — не из чего
    assert by_stage["C18:SHOW"]["conversion_step"] == pytest.approx(66.7)  # 2 из 3


def test_win_rate_counts_only_closed_deals(seeded):
    with analytics_session(readonly=True) as conn:
        result = metrics.win_rate(conn, 18, AUG["since"], AUG["until"])
    # Закрыты 2 (выиграна 1, проиграна 1); открытая в знаменатель не входит.
    assert result["closed"] == 2
    assert result["win_rate"] == 50.0
    assert result["won_amount"] == 500000
    assert result["avg_check"] == 500000


def test_money_reports_coverage_next_to_amount(seeded):
    """Сумма без покрытия вводит в заблуждение там, где решается вопрос о деньгах."""
    with analytics_session(readonly=True) as conn:
        cash = metrics.money(conn, 18, AUG["since"], AUG["until"])
    # Открытая сделка одна, и сумма у неё не заполнена.
    assert cash["open_deals"] == 1
    assert cash["open_filled"] == 0
    assert cash["open_coverage"] == 0.0
    assert cash["won_coverage"] == 100.0


def test_stage_movement_accounts_flow_not_snapshot_difference(seeded):
    with analytics_session(readonly=True) as conn:
        movement = {m["stage_id"]: m for m in metrics.stage_movement(conn, 18, **AUG)}
    assert movement["C18:NEW"]["entered"] == 3
    assert movement["C18:NEW"]["left_count"] == 2
    assert movement["C18:NEW"]["remaining"] == 1
    # Сделка зашла на «Показ» и ушла с него внутри периода — разность срезов
    # этого не увидела бы вовсе.
    assert movement["C18:SHOW"]["entered"] == 2
    assert movement["C18:SHOW"]["left_count"] == 2
    assert movement["C18:SHOW"]["remaining"] == 0


def test_transitions_detect_backward_moves(analytics_db, seeded):
    with analytics_session() as conn:
        conn.execute(
            "INSERT INTO fact_stage_event(entity_type, entity_id, category_id, stage_id, "
            "entered_at, left_at, duration_sec, seq) "
            "VALUES ('deal', 2, 18, 'C18:SHOW', '2026-08-05T00:00:00+00:00', "
            "'2026-08-06T00:00:00+00:00', 86400, 1)"
        )
        conn.execute(
            "INSERT INTO fact_stage_event(entity_type, entity_id, category_id, stage_id, "
            "entered_at, left_at, duration_sec, seq) "
            "VALUES ('deal', 2, 18, 'C18:NEW', '2026-08-06T00:00:00+00:00', NULL, NULL, 2)"
        )
    with analytics_session(readonly=True) as conn:
        result = metrics.stage_transitions(conn, 18, **AUG)
    assert result["backwards_total"] == 1
    assert result["backwards"][0]["from_stage"] == "C18:SHOW"
    assert result["backwards"][0]["to_stage"] == "C18:NEW"


def test_stage_durations_use_median_not_mean(seeded):
    with analytics_session(readonly=True) as conn:
        durations = {d["stage_id"]: d for d in metrics.stage_durations(conn, 18, **AUG)}
    assert durations["C18:NEW"]["completed_count"] == 2
    assert durations["C18:NEW"]["median_days"] in (2.0, 3.0)
    assert durations["C18:NEW"]["open_count"] == 1


def test_lead_conversion_uses_real_deal_link(seeded):
    with analytics_session(readonly=True) as conn:
        funnel = metrics.lead_funnel(conn, **AUG)
        by_source = metrics.lead_sources(conn, **AUG)
    assert funnel["total"] == 2
    assert funnel["converted"] == 1
    assert funnel["conversion"] == 50.0
    assert by_source[0]["name"] == "Звонок"
    assert by_source[0]["conversion"] == 50.0


def test_weighted_forecast_uses_own_history(seeded):
    with analytics_session(readonly=True) as conn:
        forecast = metrics.weighted_forecast(conn, 18)
    # Из дошедших до «Подбора» закрылись 2, выиграна 1 → вероятность 50%.
    by_stage = {s["stage_id"]: s for s in forecast["by_stage"]}
    assert by_stage["C18:NEW"]["probability"] == 50.0
    # Открытая сделка без суммы — прогноз ноль, и покрытие это показывает.
    assert forecast["coverage"] == 0.0


def test_entity_table_paginates_and_links_rows(seeded):
    with analytics_session(readonly=True) as conn:
        table = metrics.entity_table(conn, entity="deal", category_id=18, page_size=2)
    assert table["total"] == 3
    assert table["pages"] == 2
    assert len(table["rows"]) == 2
    assert table["rows"][0]["stage_name"]
    assert table["rows"][0]["assignee"] == "Иван Петров"


def test_entity_table_rejects_unknown_sort_column(seeded):
    """Имя колонки нельзя параметризовать — принимаем только белый список."""
    with analytics_session(readonly=True) as conn:
        table = metrics.entity_table(
            conn, entity="deal", sort="d.deal_id; DROP TABLE fact_deal--",
        )
    assert table["total"] == 3
    with analytics_session(readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM fact_deal").fetchone()[0] == 3


def test_data_quality_surfaces_missing_amounts(seeded):
    with analytics_session(readonly=True) as conn:
        quality = metrics.data_quality(conn, 18)
    assert quality["deals"]["total"] == 3
    assert quality["deals"]["no_amount"] == 1
    assert quality["deals"]["amount_coverage"] == pytest.approx(66.7)


def test_overview_compares_to_previous_period(seeded):
    period = metrics.resolve_period(start="2026-08-01", end="2026-08-31")
    with analytics_session(readonly=True) as conn:
        result = metrics.overview(conn, period, 18)
    assert result["current"]["deals_created"] == 3
    assert result["current"]["won"] == 1
    assert result["previous"]["deals_created"] == 0
    # Сравнивать не с чем — честный None вместо ложного «+100%».
    assert result["delta"]["deals_created"] is None


@pytest.mark.parametrize("preset", list(metrics.PERIOD_PRESETS))
def test_period_presets_produce_half_open_range(preset):
    period = metrics.resolve_period(preset)
    assert period["since"] < period["until"]
    assert period["preset"] == preset


def test_percentile_is_robust_to_outliers():
    values = [1, 2, 3, 4, 5, 1000]
    assert metrics.percentile(values, 0.5) in (3.0, 4.0)
    assert metrics.percentile([], 0.5) is None


def test_lost_stage_has_no_step_conversion(seeded):
    """Проиграть сделку можно с любой стадии — «доля от предыдущей» тут бессмысленна.

    Раньше проигрыш делился на последнюю рабочую стадию и выдавал 400%:
    цифра, по которой нельзя принять ни одного решения.
    """
    with analytics_session(readonly=True) as conn:
        funnel = metrics.deal_funnel(conn, 18, AUG["since"], AUG["until"])
    by_stage = {s["stage_id"]: s for s in funnel["stages"]}
    assert by_stage["C18:APOLOGY"]["conversion_step"] is None
    assert by_stage["C18:APOLOGY"]["conversion_from_start"] == pytest.approx(33.3)
    # Выигрыш — законный конец цепочки, у него шаг считается.
    assert by_stage["C18:WON"]["conversion_step"] == pytest.approx(50.0)


def test_remaining_on_stage_agrees_with_current_snapshot(seeded):
    """Инвариант: «осталось на стадии» обязано сходиться с текущим срезом.

    Расхождение здесь означает дыру в модели пребывания на стадии — ровно
    такую, из-за которой на стадии «Проиграна» показывалось «осталось 0» при
    сотне реально стоящих там карточек. Числа, противоречащие друг другу на
    одной странице, стоят доверия ко всему дашборду.
    """
    far_future = "2999-01-01T00:00:00+00:00"
    with analytics_session(readonly=True) as conn:
        movement = {
            row["stage_id"]: row["remaining"]
            for row in metrics.stage_movement(conn, 18, "0001-01-01T00:00:00+00:00", far_future)
        }
        funnel = {
            row["stage_id"]: row["count_now"]
            for row in metrics.deal_funnel(conn, 18, AUG["since"], AUG["until"])["stages"]
        }
    for stage_id, snapshot in funnel.items():
        assert movement.get(stage_id, 0) == snapshot, (
            f"стадия {stage_id}: «осталось» {movement.get(stage_id)} "
            f"против среза {snapshot}"
        )


def test_junk_threshold_comes_from_the_data_not_a_guess(analytics_db, seeded):
    """«Плохой источник» — это хуже остальных каналов, а не хуже выдуманных 20%."""
    with analytics_session() as conn:
        conn.execute(
            "INSERT INTO dim_source(source_id, name, synced_at) VALUES ('WEB', 'Сайт', 'x')"
        )
        for lead_id, status in ((20, "JUNK"), (21, "JUNK"), (22, "NEW")):
            conn.execute(
                "INSERT INTO fact_lead(lead_id, title, status_id, source_id, "
                "assigned_by_id, date_create, is_converted, is_deleted, synced_at) "
                "VALUES (?, 'Лид', ?, 'WEB', 32, '2026-08-01T00:00:00+00:00', 0, 0, 'x')",
                (lead_id, status),
            )
    with analytics_session(readonly=True) as conn:
        by_source = {row["source_id"]: row for row in metrics.lead_sources(conn, **AUG)}
    # У «Сайта» мусора 2 из 3, у «Звонка» — 0 из 2: выше среднего только первый.
    assert by_source["WEB"]["junk_above_average"] is True
    assert by_source["CALL"]["junk_above_average"] is False


def test_future_stage_entry_shows_as_zero_and_is_flagged(analytics_db, seeded):
    """«На стадии −482 ч» — не число, а мусор. Аномалия уходит в качество данных."""
    with analytics_session() as conn:
        conn.execute(
            "INSERT INTO fact_stage_event(entity_type, entity_id, category_id, "
            "stage_id, entered_at, left_at, duration_sec, seq) "
            "VALUES ('deal', 2, 18, 'C18:NEW', '2099-01-01T00:00:00+00:00', NULL, NULL, 9)"
        )
    with analytics_session(readonly=True) as conn:
        table = metrics.entity_table(conn, entity="deal", category_id=18)
        quality = metrics.data_quality(conn, 18)
    assert all(
        row["days_in_stage"] is None or row["days_in_stage"] >= 0 for row in table["rows"]
    )
    assert quality["future_stage_events"] >= 1


# --------------------------------------------------------------------------
# учёт потока и фильтр по отделам
# --------------------------------------------------------------------------

def _flow_identity_holds(conn, category_id, since, until):
    """осталось = было + вошло − вышло для каждой стадии."""
    broken = []
    for row in metrics.stage_movement(conn, category_id, since, until):
        expected = row["opening"] + row["entered"] - row["left_count"]
        if expected != row["remaining"]:
            broken.append((row["stage_id"], expected, row["remaining"]))
    return broken


def test_flow_identity_holds_for_every_stage(seeded):
    """Главная проверка страницы «Движение».

    Если тождество не сходится, «чистый поток» перестаёт быть потоком:
    по нему нельзя судить, наполняется стадия или разгружается, а
    руководитель будет искать затор там, где его нет.
    """
    with analytics_session(readonly=True) as conn:
        assert _flow_identity_holds(conn, 18, AUG["since"], AUG["until"]) == []


@pytest.mark.parametrize("since,until", [
    ("2026-08-01T00:00:00+00:00", "2026-08-05T00:00:00+00:00"),   # начало
    ("2026-08-03T00:00:00+00:00", "2026-08-09T00:00:00+00:00"),   # середина
    ("2026-08-09T00:00:00+00:00", "2026-09-01T00:00:00+00:00"),   # хвост
    ("2025-01-01T00:00:00+00:00", "2026-08-02T00:00:00+00:00"),   # период шире данных
    ("2026-08-04T00:00:00+00:00", "2026-08-04T12:00:00+00:00"),   # полдня
])
def test_flow_identity_holds_on_arbitrary_windows(seeded, since, until):
    """Тождество не должно зависеть от того, куда попали границы периода."""
    with analytics_session(readonly=True) as conn:
        assert _flow_identity_holds(conn, 18, since, until) == []


def test_deal_entering_and_leaving_inside_period_is_counted_both_ways(seeded):
    """Именно ради этого случая поток считается по событиям, а не разностью срезов.

    Сделка, зашедшая на стадию и ушедшая с неё внутри периода, в разности
    срезов не видна вовсе — хотя работа по ней шла.
    """
    with analytics_session(readonly=True) as conn:
        rows = {r["stage_id"]: r for r in metrics.stage_movement(conn, 18, **AUG)}
    show = rows["C18:SHOW"]
    assert show["entered"] == 2 and show["left_count"] == 2
    assert show["opening"] == 0 and show["remaining"] == 0
    # Разность срезов показала бы ноль движения — а движение было.
    assert show["entered"] > 0


def test_reentry_counts_as_two_entries(analytics_db, seeded):
    """Сделка, вернувшаяся на стадию, входила дважды.

    Считать «разные сделки» вместо переходов значило бы сломать тождество:
    остаток изменился бы, а «вошло» — нет.
    """
    with analytics_session() as conn:
        conn.execute(
            "INSERT INTO fact_stage_event(entity_type, entity_id, category_id, stage_id, "
            "entered_at, left_at, duration_sec, seq) VALUES "
            "('deal', 2, 18, 'C18:SHOW', '2026-08-05T00:00:00+00:00', "
            "'2026-08-06T00:00:00+00:00', 86400, 1)"
        )
        conn.execute(
            "INSERT INTO fact_stage_event(entity_type, entity_id, category_id, stage_id, "
            "entered_at, left_at, duration_sec, seq) VALUES "
            "('deal', 2, 18, 'C18:SHOW', '2026-08-07T00:00:00+00:00', NULL, NULL, 2)"
        )
    with analytics_session(readonly=True) as conn:
        rows = {r["stage_id"]: r for r in metrics.stage_movement(conn, 18, **AUG)}
        assert _flow_identity_holds(conn, 18, **AUG) == []
    assert rows["C18:SHOW"]["entered"] == 4      # 2 прежних + 2 захода сделки 2
    assert rows["C18:SHOW"]["remaining"] == 1


def test_department_filter_narrows_movement(analytics_db, seeded):
    """Фильтр по отделу считает только сделки этого отдела."""
    with analytics_session() as conn:
        conn.execute(
            "INSERT INTO dim_user(user_id, name, department_id, department_name, "
            "is_active, synced_at) VALUES (99, 'Пётр Сидоров', 50, 'Отдел Волковой', 1, 'x')"
        )
        _deal(conn, 4, "C18:NEW", amount=100)
        conn.execute("UPDATE fact_deal SET assigned_by_id = 99 WHERE deal_id = 4")
        _events(conn, 4, [("C18:NEW", "2026-08-01T00:00:00+00:00", None)])

    with analytics_session(readonly=True) as conn:
        everyone = {r["stage_id"]: r for r in metrics.stage_movement(conn, 18, **AUG)}
        trofimova = {
            r["stage_id"]: r
            for r in metrics.stage_movement(conn, 18, AUG["since"], AUG["until"], 44)
        }
        volkova = {
            r["stage_id"]: r
            for r in metrics.stage_movement(conn, 18, AUG["since"], AUG["until"], 50)
        }

    assert everyone["C18:NEW"]["entered"] == 4
    assert trofimova["C18:NEW"]["entered"] == 3
    assert volkova["C18:NEW"]["entered"] == 1

    # Отделы в сумме дают целое по каждой колонке и каждой стадии: ни одна
    # сделка не потеряна и не задвоена. Иначе РОПы, сложив свои цифры, не
    # получат общую — и перестанут верить обеим.
    for stage_id, whole in everyone.items():
        for column in ("opening", "entered", "left_count", "remaining"):
            parts = trofimova[stage_id][column] + volkova[stage_id][column]
            assert parts == whole[column], f"{stage_id}.{column}: {parts} ≠ {whole[column]}"


def test_flow_identity_holds_under_department_filter(analytics_db, seeded):
    """Фильтр не должен ломать сходимость потока внутри отдела."""
    with analytics_session(readonly=True) as conn:
        for department_id in (44, 50, None):
            broken = []
            rows = metrics.stage_movement(
                conn, 18, AUG["since"], AUG["until"], department_id,
            )
            for row in rows:
                expected = row["opening"] + row["entered"] - row["left_count"]
                if expected != row["remaining"]:
                    broken.append((department_id, row["stage_id"]))
            assert broken == []


def test_department_filter_narrows_transitions_and_stuck(analytics_db, seeded):
    with analytics_session(readonly=True) as conn:
        everyone = metrics.stage_transitions(conn, 18, **AUG)
        other = metrics.stage_transitions(
            conn, 18, AUG["since"], AUG["until"], 999,
        )
        stuck_other = metrics.stuck_deals(conn, 18, department_id=999)
    assert everyone["transitions"]
    assert other["transitions"] == []
    assert stuck_other == []


def test_departments_options_lists_only_departments_with_deals(seeded):
    with analytics_session(readonly=True) as conn:
        options = metrics.departments_options(conn)
    assert [row["name"] for row in options] == ["Отдел Трофимовой"]
    assert options[0]["department_id"] == 44
