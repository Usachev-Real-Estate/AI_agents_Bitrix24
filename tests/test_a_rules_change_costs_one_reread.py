"""Смена правил обязана стоить одного повторного разбора карточки.

Это обещание записано в docstring `compute_content_hash` с самого начала —
и не выполнялось. Отпечаток был слитный: промпт менялся, хэш расходился, а
новых событий в карточке не появлялось. Следующая же ветка читала это как
«событие удалили из таймлайна, прошлое состояние остаётся верным» — модель
не звали, хэш переписывали на новый, и правило тихо не доезжало ни до одной
старой карточки.

Прогон 31.08 12:55, через три минуты после смены промпта: десять продавцов
из десяти пришли из кэша, 0.00 ₽. Отказ клиента на #14992 остался
непрочитанным не потому, что правило узкое, а потому, что карточку по
новому правилу никто не читал.

Теперь отпечаток составной — «правила:события», — и ветка спрашивает, ЧТО
именно разошлось.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import db  # noqa: E402
from client_state import (  # noqa: E402
    analyze_buyer_deal,
    build_evidence_events,
    compute_content_hash,
)
from funnel_profiles import BUYER_PROFILE  # noqa: E402
from masking import build_mask_map  # noqa: E402


class _CountingLLM:
    """Модель, которая считает, сколько раз её позвали."""

    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, messages: list[Any]) -> Any:
        self.calls += 1

        class _Resp:
            content = json.dumps({
                "client_goal": "квартира",
                "situation": "разговор был",
                "last_event": {"what": "звонок", "when": "2026-08-20"},
                "next_step": {"what": "показ", "when": "2026-08-22",
                              "who": "broker"},
                "blockers": [], "risk": "low", "recoverable": True,
                "missing": [], "confidence": 0.8, "evidence": ["бюджет"],
            }, ensure_ascii=False)

        return _Resp()


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "violations.db")
    db.init_db()
    yield


def _deal() -> dict[str, Any]:
    return {
        "ID": 14992,
        "TITLE": "ЖК «Шуваловский» Косинцев Александр устал",
        "STAGE_ID": "C18:PREPARATION",
        "timeline": [{
            "id": 1, "created": "2026-08-18T10:00:00+03:00",
            "comment": "Собственник отказывается оплачивать комиссию",
        }],
        "activities": [], "transcripts": [], "contacts": [],
        "evidence_incomplete": False,
    }


def _store(deal: dict[str, Any], content_hash: str) -> None:
    db.save_client_state(
        deal_id=int(deal["ID"]),
        state_json=json.dumps({
            "client_goal": "квартира", "confidence": 0.8, "recoverable": True,
        }, ensure_ascii=False),
        confidence=0.8,
        content_hash=content_hash,
        # Ватермарка позже всех событий: новых событий в карточке нет.
        analyzed_at="2026-08-30T09:00:00+00:00",
        model="test",
    )


def _events_of(deal: dict[str, Any]) -> list[dict[str, Any]]:
    return build_evidence_events(
        deal["timeline"], [], [], build_mask_map(deal),
    )


def _hash_with(deal: dict[str, Any], rules: str) -> str:
    """Отпечаток тех же событий, но по другим правилам."""
    from dataclasses import replace

    other = replace(BUYER_PROFILE, prompt=rules)
    return compute_content_hash(_events_of(deal), other, deal["STAGE_ID"])


def _run(deal: dict[str, Any], monkeypatch) -> tuple[dict[str, Any], int]:
    monkeypatch.setenv("DRY_RUN", "true")
    from config import get_settings

    get_settings.cache_clear()
    llm = _CountingLLM()
    result = analyze_buyer_deal(deal, prepared=True, llm=llm)
    return result, llm.calls


def test_a_changed_prompt_sends_the_card_back_to_the_model(temp_db, monkeypatch):
    """#14992: правило про отказ переписали — карточку надо перечитать."""
    deal = _deal()
    _store(deal, _hash_with(deal, "старый промпт без правила об отказе"))
    result, calls = _run(deal, monkeypatch)
    assert calls == 1, "правила сменились, а модель не позвали"
    assert result["skipped"] is False
    assert result.get("reason") != "no_new_events"


def test_a_card_analyzed_before_the_split_is_reread_once(temp_db, monkeypatch):
    """Строка из БД без двоеточия — разбор по правилам, которых мы не знаем."""
    deal = _deal()
    _store(deal, "3df524ace164b0dc6f9ac5e4ebeb5e3b76c19b97ff06039656da5ae8e715eb56")
    _result, calls = _run(deal, monkeypatch)
    assert calls == 1


def test_the_same_rules_and_no_new_events_still_skip(temp_db, monkeypatch):
    """Правила те же, событие из таймлайна удалили — звать модель не за чем.

    Ради этого случая ветка и писалась, и потерять её нельзя: иначе каждое
    удаление комментария оплачивается разбором всей карточки.
    """
    deal = _deal()
    same_rules = compute_content_hash(
        _events_of(deal), BUYER_PROFILE, deal["STAGE_ID"],
    ).split(":")[0]
    _store(deal, f"{same_rules}:отпечаток-событий-которых-больше-нет")
    result, calls = _run(deal, monkeypatch)
    assert calls == 0
    assert result["skipped"] is True
    assert result["reason"] == "no_new_events"


def test_an_untouched_card_is_still_served_from_cache(temp_db, monkeypatch):
    """Ничего не менялось — ни правил, ни событий: платить не за что."""
    deal = _deal()
    _store(deal, compute_content_hash(
        _events_of(deal), BUYER_PROFILE, deal["STAGE_ID"],
    ))
    result, calls = _run(deal, monkeypatch)
    assert calls == 0
    assert result["skipped"] is True
    assert result["reason"] == "unchanged"
