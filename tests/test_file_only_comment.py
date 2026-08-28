"""Комментарий-вложение без текста — это скриншот, а не пустое место.

Аудит 27.08: комментарий таймлайна с приложенным файлом, но без текста
выбрасывался из доказательств целиком. Брокер прикладывал скриншот
переписки — и получал упрёк «написал клиенту, а подтверждения нет».
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from broker_work import (  # noqa: E402
    GAP_CLAIMED_MESSAGE,
    PROVEN_BY_SCREENSHOT,
    assess_broker_work,
)
from client_state import COMMENT_FILE_ONLY, build_evidence_events  # noqa: E402
from funnel_profiles import BUYER_PROFILE  # noqa: E402
from masking import build_mask_map  # noqa: E402

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
STAGE = "C18:UC_UFPFKK"


def _ago(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).isoformat()


def _timeline() -> list[dict]:
    return [
        {"ID": 1, "CREATED": _ago(2), "COMMENT": "", "has_files": True},
        {"ID": 2, "CREATED": _ago(1), "COMMENT": "Написал клиенту в вотсап"},
    ]


def _events(timeline: list[dict]) -> list[dict]:
    mask = build_mask_map({"ID": 4242, "TITLE": "Сделка"}, [])
    return build_evidence_events(timeline, [], [], mask)


def test_file_only_comment_survives():
    events = _events(_timeline())
    assert len(events) == 2
    with_file = [e for e in events if e.get("has_files")]
    assert len(with_file) == 1
    assert with_file[0]["text"] == COMMENT_FILE_ONLY


def test_screenshot_counts_as_proof():
    verdict = assess_broker_work(
        _events(_timeline()),
        profile=BUYER_PROFILE,
        stage_id=STAGE,
        hours_on_stage=1000.0,
        claims_messaged=True,
        claims_no_answer=False,
        comment_informative=True,
        now=NOW,
    )
    assert verdict["reason"] == PROVEN_BY_SCREENSHOT
    assert verdict["proven"] is True


def test_without_the_attachment_the_gap_still_fires():
    timeline = [{"ID": 2, "CREATED": _ago(1), "COMMENT": "Написал клиенту в вотсап"}]
    verdict = assess_broker_work(
        _events(timeline),
        profile=BUYER_PROFILE,
        stage_id=STAGE,
        hours_on_stage=1000.0,
        claims_messaged=True,
        claims_no_answer=False,
        comment_informative=True,
        now=NOW,
    )
    assert verdict["reason"] == GAP_CLAIMED_MESSAGE


def test_empty_comment_without_files_is_still_dropped():
    timeline = [{"ID": 1, "CREATED": _ago(2), "COMMENT": "   "}]
    assert _events(timeline) == []
