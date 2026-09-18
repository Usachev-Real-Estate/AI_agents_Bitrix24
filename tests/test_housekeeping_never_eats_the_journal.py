"""Уборка старых выгрузок не имеет права трогать то, что не восстановить.

В каталоге выгрузки рядом с ежедневными файлами лежат две вещи, которых
больше нигде нет: снимок ответственных и журнал переназначений. Журнал
копится только вперёд — в REST истории смены ответственного не существует,
и стёртый журнал не восстанавливается ничем, кроме ожидания в месяцы.

Первая версия уборки искала выгрузки маской «*_*.jsonl». Под неё попадал
assignee_log.jsonl, и при сортировке по убыванию он оказывался последним —
то есть первым кандидатом на удаление, как только выгрузок накопится
тридцать. Прогон при этом отчитался бы успехом. Поэтому уборка знает имена
выгрузок наперечёт, а этот файл проверяет, что знает.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import dossier  # noqa: E402


def _names(days: list[str], mode: str = "delta") -> list[str]:
    out: list[str] = []
    for day in days:
        out += [f"{mode}_{day}.jsonl", f"{mode}_{day}.meta.json",
                f"queue_{day}.json"]
    return out


# ── Что нельзя удалять ─────────────────────────────────────────────────
def test_the_reassignment_log_is_never_a_candidate():
    """Тот самый дефект: журнал .jsonl, но он не выгрузка."""
    names = _names([f"2026-09-{d:02d}" for d in range(1, 10)])
    names += ["assignee_log.jsonl", "assignee_snapshot.json", "README.md"]

    doomed = dossier.doomed_names(names, keep=2)

    assert "assignee_log.jsonl" not in doomed
    assert "assignee_snapshot.json" not in doomed
    assert "README.md" not in doomed


def test_the_field_cache_survives_housekeeping():
    names = _names(["2026-09-01", "2026-09-02"]) + ["dossier_fields.json"]
    assert "dossier_fields.json" not in dossier.doomed_names(names, keep=1)


# ── Что удаляется ──────────────────────────────────────────────────────
def test_the_oldest_export_goes_whole():
    """Строки без меты — выгрузка, о которой ничего не известно."""
    names = _names(["2026-09-01", "2026-09-02", "2026-09-03"])

    doomed = dossier.doomed_names(names, keep=2)

    assert doomed == [
        "delta_2026-09-01.jsonl",
        "delta_2026-09-01.meta.json",
        "queue_2026-09-01.json",
    ]


def test_nothing_goes_while_there_is_room():
    names = _names(["2026-09-01", "2026-09-02"])
    assert dossier.doomed_names(names, keep=30) == []


def test_zero_keep_disables_housekeeping_instead_of_deleting_all():
    """Ноль — это «не убирать», а не «убрать всё». Разница в целой выгрузке."""
    assert dossier.doomed_names(_names(["2026-09-01"]), keep=0) == []


def test_the_newest_exports_are_the_ones_kept():
    names = _names([f"2026-09-{d:02d}" for d in range(1, 6)])

    doomed = dossier.doomed_names(names, keep=2)

    assert "delta_2026-09-05.jsonl" not in doomed
    assert "delta_2026-09-04.jsonl" not in doomed
    assert "delta_2026-09-03.jsonl" in doomed


# ── Очередь общая на день ──────────────────────────────────────────────
def test_a_queue_shared_with_a_living_export_stays():
    """Полный прогон идёт в понедельник, ежедневный — тоже.

    Очередь именуется по дню, и оба прогона пишут в одну. Удалив её вместе
    с ежедневной выгрузкой, мы обезглавили бы недельную, которая осталась.
    """
    names = (
        ["full_2026-09-14.jsonl", "full_2026-09-14.meta.json"]
        + ["delta_2026-09-14.jsonl", "delta_2026-09-14.meta.json"]
        + ["queue_2026-09-14.json"]
        + _names(["2026-09-15", "2026-09-16"])
    )

    # keep=3 оставляет full_14, delta_16, delta_15 — и убирает delta_14.
    doomed = dossier.doomed_names(names, keep=3)

    assert "delta_2026-09-14.jsonl" in doomed
    assert "queue_2026-09-14.json" not in doomed, "очередь нужна полной выгрузке"


def test_a_queue_of_a_fully_expired_day_goes_too():
    """Иначе очереди копились бы вечно: их уборка не ловила вовсе."""
    names = _names(["2026-09-01", "2026-09-02", "2026-09-03"])

    assert "queue_2026-09-01.json" in dossier.doomed_names(names, keep=2)


# ── На диске ───────────────────────────────────────────────────────────
def test_the_files_really_disappear(tmp_path):
    for name in _names(["2026-09-01", "2026-09-02"]) + ["assignee_log.jsonl"]:
        (tmp_path / name).write_text("x", encoding="utf-8")

    removed = dossier.prune_old(tmp_path, keep=1)

    assert sorted(removed) == [
        "delta_2026-09-01.jsonl",
        "delta_2026-09-01.meta.json",
        "queue_2026-09-01.json",
    ]
    assert (tmp_path / "assignee_log.jsonl").exists()
    assert (tmp_path / "delta_2026-09-02.jsonl").exists()


def test_a_missing_directory_is_not_an_error(tmp_path):
    assert dossier.prune_old(tmp_path / "нет-такого", keep=5) == []
