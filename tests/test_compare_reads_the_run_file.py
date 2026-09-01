"""Сравнение прогонов должно читать тот файл, который прогон пишет.

С переходом на отчёт каждому РОПу лично воронки уехали на уровень глубже:
было `{"funnels": [...]}`, стало `{"deliveries": [{"funnels": [...]}]}`.
Обход в scripts/compare_qc_runs.py остался прежним и стал возвращать ноль
карточек — молча, без единой ошибки: `.get("funnels")` на новом файле
просто не находит ключа, а ноль карточек печатался как «сравнивать нечего».

Инструмент, которым проверяют правки, сам показывал бы это неограниченно
долго — и первым делом соврал бы на опыте с размышлениями, ради которого
его и открыли.

Тест держит форму файла и форму скрипта вместе. Порознь они уже разошлись
однажды: JSON правился в пилоте, обход — в скрипте, и связывала их только
память.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "compare_qc_runs", _ROOT / "scripts" / "compare_qc_runs.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


compare = _load_script()


def _card(deal_id: int, reason: str = "no_trace_in_window") -> dict[str, Any]:
    return {
        "deal_id": deal_id, "verdict": "poor", "temperature": "warm",
        "work_reason": reason, "recoverable": True, "counterparty": "client",
        "facts_present": 3, "facts_needed": 4,
    }


def _funnel(cards: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "funnel": "sellers", "cost_rub": 12.5, "llm_calls": len(cards),
        "errors": 0, "evidence_dropped": 1,
        "usage": {
            "input_tokens": 4000, "output_tokens": 1500,
            "reasoning_tokens": 900,
        },
        "cards": cards,
    }


def _new_shape(ids: list[int]) -> dict[str, Any]:
    """Как пишет пилот с 01.09: воронки внутри адресатов."""
    return {
        "generated_at": "2026-09-01T12:00:00+00:00",
        "chat_id": 22358,
        "deliveries": [
            {"kind": "user", "to": "42", "name": "Кретов",
             "funnels": [_funnel([_card(i) for i in ids])]},
        ],
    }


def _old_shape(ids: list[int]) -> dict[str, Any]:
    """Как писал пилот до перехода на личную рассылку."""
    return {"funnels": [_funnel([_card(i) for i in ids])]}


def test_the_current_file_shape_is_read():
    cards = compare.cards_by_id(_new_shape([101, 102, 103]))
    assert sorted(cards) == [101, 102, 103]


def test_the_old_file_shape_still_reads():
    """Прогоны до 01.09 лежат на диске, и сравнивать их с новыми — смысл скрипта."""
    cards = compare.cards_by_id(_old_shape([101, 102]))
    assert sorted(cards) == [101, 102]


def test_totals_are_summed_from_the_current_shape():
    """Не только карточки: деньги и токены брались тем же обходом."""
    totals = compare.totals(_new_shape([101, 102]))
    assert totals["cost_rub"] == 12.5
    assert totals["reasoning_tokens"] == 900
    assert totals["output_tokens"] == 1500


def test_cards_from_every_recipient_are_collected():
    """Карточки лежат у пяти адресатов, а сравнение — по всему прогону."""
    run = {
        "deliveries": [
            {"name": "Кретов", "funnels": [_funnel([_card(1), _card(2)])]},
            {"name": "Шпырная", "funnels": [_funnel([_card(3)])]},
            {"name": "чат 22358", "funnels": [_funnel([_card(4)])]},
        ],
    }
    assert sorted(compare.cards_by_id(run)) == [1, 2, 3, 4]


def test_an_unreadable_file_is_named_not_silently_empty(tmp_path, monkeypatch):
    """Ноль карточек — это сломанный файл, а не «выборки не совпали».

    Ровно эта путаница и прятала дефект: скрипт печатал «сравнивать
    нечего» и выходил с нулевым кодом, то есть выглядел работающим.
    """
    good = tmp_path / "good.json"
    bad = tmp_path / "bad.json"
    good.write_text(json.dumps(_new_shape([1, 2])), encoding="utf-8")
    bad.write_text(json.dumps({"deliveries": []}), encoding="utf-8")
    monkeypatch.setattr(
        sys, "argv", ["compare_qc_runs.py", str(good), str(bad)],
    )
    with pytest.raises(SystemExit) as exc:
        compare.main()
    assert exc.value.code != 0


def test_two_good_runs_compare_without_error(tmp_path, monkeypatch, capsys):
    """Сквозная проверка: два файла текущей формы доходят до отчёта."""
    a = tmp_path / "run_A.json"
    b = tmp_path / "run_B.json"
    a.write_text(json.dumps(_new_shape([1, 2, 3])), encoding="utf-8")
    b.write_text(json.dumps(_new_shape([2, 3, 4])), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["compare_qc_runs.py", str(a), str(b)])
    compare.main()
    out = capsys.readouterr().out
    assert "Общих карточек: 2" in out
    # Доля размышлений — метрика опыта с reasoning_effort, ради которого
    # скрипт и чинили.
    assert "доля размышлений" in out
