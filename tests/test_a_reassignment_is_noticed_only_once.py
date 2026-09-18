"""Журнал смены ответственного — единственный способ узнать, кто вёл карточку.

В REST истории переназначений нет вовсе. Без журнала нельзя отличить
«брокер не работал» от «карточку передали ему вчера, а до этого её месяц
вёл другой» — и первый вывод, вынесенный вместо второго, это обвинение
человека в чужом бездействии.

Восстановить историю задним числом невозможно: портал её не хранит. Журнал
копится только вперёд, снимок за снимком, и поэтому обязан быть аккуратен в
двух местах. Первое: новая карточка — не переназначение. Второе: снимок
обновляется слиянием, а не перезаписью, иначе прогон с --limit 20 сотрёт
тысячу карточек, и на следующий день они все отрапортуют смену
ответственного, которой не было.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import dossier  # noqa: E402

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
USERS = {7: "Галина Шарипова", 9: "Владислав Ветров", 11: "Антон Кретов"}


@pytest.fixture(autouse=True)
def dossier_dir(tmp_path, monkeypatch):
    """Свой каталог выгрузки на тест: снимок и журнал — файлы на диске."""
    from config import get_settings

    monkeypatch.setenv("DOSSIER_DIR", str(tmp_path / "dossier"))
    get_settings.cache_clear()
    yield tmp_path / "dossier"
    get_settings.cache_clear()


# ── Диф снимков ────────────────────────────────────────────────────────
def test_a_changed_assignee_is_written_down():
    events = dossier.diff_assignees({16722: 7}, {16722: 9}, USERS, NOW)

    assert len(events) == 1
    assert events[0]["deal_id"] == 16722
    assert events[0]["from_id"] == 7 and events[0]["to_id"] == 9
    assert events[0]["from_name"] == "Галина Шарипова"
    assert events[0]["to_name"] == "Владислав Ветров"
    assert events[0]["detected_at"] == NOW.isoformat()


def test_an_unchanged_assignee_writes_nothing():
    assert dossier.diff_assignees({16722: 7}, {16722: 7}, USERS, NOW) == []


def test_a_brand_new_card_is_not_a_reassignment():
    """Карточки не было в прошлом снимке — её завели, а не передали.

    Без этой проверки первый же прогон объявил бы переназначением весь
    портфель, и журнал начался бы с тысячи выдуманных событий.
    """
    assert dossier.diff_assignees({}, {16722: 7}, USERS, NOW) == []


def test_a_card_that_vanished_is_not_a_reassignment():
    """Сделку закрыли или удалили — это не передача другому брокеру."""
    assert dossier.diff_assignees({16722: 7}, {}, USERS, NOW) == []


def test_an_unknown_person_keeps_his_number():
    """Уволенного может не быть в справочнике — номер честнее пустоты."""
    events = dossier.diff_assignees({1: 7}, {1: 999}, USERS, NOW)
    assert events[0]["to_id"] == 999 and events[0]["to_name"] == ""


# ── Снимок на диске ────────────────────────────────────────────────────
def test_the_snapshot_survives_a_round_trip():
    dossier.save_snapshot({16722: 7, 16723: 9})
    assert dossier.load_snapshot() == {16722: 7, 16723: 9}


def test_a_missing_snapshot_reads_as_empty():
    assert dossier.load_snapshot() == {}


def test_a_broken_snapshot_does_not_kill_the_run():
    """Оборванная запись не повод уронить прогон — журнал просто начнётся заново."""
    path = dossier._snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{не json", encoding="utf-8")

    assert dossier.load_snapshot() == {}


def test_a_partial_run_does_not_erase_the_rest_of_the_portfolio():
    """Главная ловушка: --limit 20 не должен стирать снимок тысячи карточек.

    Слияние, а не перезапись. Иначе назавтра девятьсот восемьдесят карточек
    исчезнут из снимка, а послезавтра вернутся как «новые» — и журнал
    наполнится переназначениями, которых не было.
    """
    dossier.save_snapshot({1: 7, 2: 7, 3: 7})

    seen_this_run = {2: 9}
    dossier.save_snapshot({**dossier.load_snapshot(), **seen_this_run})

    assert dossier.load_snapshot() == {1: 7, 2: 9, 3: 7}


# ── Журнал ─────────────────────────────────────────────────────────────
def test_the_log_keeps_every_handover_in_order():
    dossier.append_log(dossier.diff_assignees({1: 7}, {1: 9}, USERS, NOW))
    dossier.append_log(dossier.diff_assignees({1: 9}, {1: 11}, USERS, NOW))

    history = dossier.load_log()[1]
    assert [(e["from_id"], e["to_id"]) for e in history] == [(7, 9), (9, 11)]


def test_an_empty_diff_does_not_touch_the_log():
    dossier.append_log([])
    assert not dossier._log_path().exists()


def test_a_damaged_line_does_not_hide_the_good_ones():
    """Журнал дописывается построчно, и обрыв записи не должен стоить истории."""
    path = dossier._log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"deal_id": 1, "from_id": 7, "to_id": 9}, ensure_ascii=False)
        + "\n{оборвано\n"
        + json.dumps({"deal_id": 2, "from_id": 9, "to_id": 11}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    log = dossier.load_log()
    assert sorted(log) == [1, 2]
