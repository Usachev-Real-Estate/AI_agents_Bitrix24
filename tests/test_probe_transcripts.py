"""Tests for the pre-flight reconnaissance script."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from probe_transcripts import summarize  # noqa: E402


def _deal(context: int) -> dict:
    return {"deal_id": 1, "calls": 0, "calls_with_transcript": 0,
            "transcript_chars": 0, "comment_chars": 0, "context_chars": context}


def test_coverage_is_share_of_calls_with_text():
    stats = summarize([_deal(300)], calls_total=10, calls_with_text=7)
    assert stats["transcript_coverage_pct"] == 70.0


def test_no_calls_does_not_divide_by_zero():
    stats = summarize([_deal(0)], calls_total=0, calls_with_text=0)
    assert stats["transcript_coverage_pct"] == 0.0


def test_empty_probe_is_handled():
    stats = summarize([], calls_total=0, calls_with_text=0)
    assert stats["deals_probed"] == 0
    assert stats["context_chars_median"] == 0


def test_median_and_max_context():
    stats = summarize(
        [_deal(300), _deal(900), _deal(30_000)],
        calls_total=3, calls_with_text=3,
    )
    assert stats["context_chars_median"] == 900
    assert stats["context_chars_max"] == 30_000
    # ~3 символа на токен
    assert stats["context_tokens_median"] == 300
    assert stats["context_tokens_max"] == 10_000


def test_counts_cards_without_any_text():
    stats = summarize(
        [_deal(0), _deal(0), _deal(500)], calls_total=1, calls_with_text=0,
    )
    assert stats["deals_without_any_text"] == 2
