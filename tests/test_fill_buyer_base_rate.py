"""Tests for buyer-deal base rate fill mapping."""

from __future__ import annotations

import pytest

from pathlib import Path

from fill_buyer_base_rate import (
    CsvBrokerRate,
    UF_BASE_RATE,
    build_broker_rate_map,
    load_csv_rates,
    name_key_variants,
    normalize_person_name,
    plan_deal_updates,
)


def test_normalize_person_name_strips_rop_and_yo() -> None:
    assert normalize_person_name("Шпырная Юлия (РОП)") == "шпырная юлия"
    assert normalize_person_name("Алёна Тест") == "алена тест"


def test_name_key_variants_order_independent() -> None:
    keys = name_key_variants("Алевтина Ротшильд")
    assert "алевтина ротшильд" in keys
    assert "ротшильд алевтина" in keys


def test_build_broker_rate_map_prefers_active() -> None:
    csv_rows = [
        CsvBrokerRate(fio="Орешникова Марина", rate="30%"),
        CsvBrokerRate(fio="Алевтина Ротшильд", rate="40%"),
        CsvBrokerRate(fio="Кретов Антон (РОП)", rate="-"),
        CsvBrokerRate(fio="Ципкало Кирилл", rate="30%"),
    ]
    users = [
        {"ID": "160", "LAST_NAME": "Орешникова", "NAME": "Марина", "ACTIVE": False},
        {"ID": "92", "LAST_NAME": "Орешникова", "NAME": "Марина", "ACTIVE": True},
        {
            "ID": "80",
            "LAST_NAME": "Ротшильд",
            "NAME": "Алевтина",
            "SECOND_NAME": "Вадимовна",
            "ACTIVE": True,
        },
        {"ID": "84", "LAST_NAME": "Кретов", "NAME": "Антон", "ACTIVE": False},
        {"ID": "46", "LAST_NAME": "Кретов", "NAME": "Антон", "ACTIVE": True},
    ]
    rates, unmatched, names = build_broker_rate_map(csv_rows, users)
    assert rates[92] == "30%"
    assert rates[80] == "40%"
    assert rates[46] == "-"
    assert 160 not in rates
    assert 84 not in rates
    assert unmatched == ["Ципкало Кирилл"]
    assert names[92] == "Орешникова Марина"


def test_plan_deal_updates_skips_same_and_unknown() -> None:
    rate_by_user = {92: "30%", 46: "-"}
    deals = [
        {"ID": "1", "ASSIGNED_BY_ID": "92", UF_BASE_RATE: ""},
        {"ID": "2", "ASSIGNED_BY_ID": "92", UF_BASE_RATE: "30%"},
        {"ID": "3", "ASSIGNED_BY_ID": "46", UF_BASE_RATE: None},
        {"ID": "4", "ASSIGNED_BY_ID": "16", UF_BASE_RATE: ""},
    ]
    to_update, same, unknown = plan_deal_updates(deals, rate_by_user)
    assert [(p.deal_id, p.new_rate) for p in to_update] == [(1, "30%"), (3, "-")]
    assert [p.deal_id for p in same] == [2]
    assert unknown == [16]


_PROJECT_CSV = Path(
    "data/Мотивация брокеров 3 кв 2026 - Мотивация брокеров 3 кв 2026.csv"
)


@pytest.mark.skipif(
    not _PROJECT_CSV.exists(),
    reason="data/ не в репозитории (gitignored) — проверка идёт только локально",
)
def test_load_csv_rates_from_project_file() -> None:
    path = _PROJECT_CSV
    rows = load_csv_rates(path)
    assert len(rows) == 40
    by_fio = {r.fio: r.rate for r in rows}
    assert by_fio["Логутина Ирина"] == "40%"
    assert by_fio["Трофимова Светлана (РОП)"] == "-"
    assert by_fio["Азизова Ирена"] == "35%"
