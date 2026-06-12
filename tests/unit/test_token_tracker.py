# -*- coding: utf-8 -*-
"""Unit tests for TokenTracker — verbatim accounting with detail buckets.

The contract under test: record() stores what the API reported, summary()
aggregates without deriving, and the pre-detail call signature keeps working
(every existing call site passes only prompt/completion)."""

from docingest.models.token_tracker import TokenTracker


def test_legacy_signature_still_works():
    t = TokenTracker()
    t.record("m", prompt=100, completion=50)
    s = t.summary()
    assert s["total_prompt_tokens"] == 100
    assert s["total_completion_tokens"] == 50
    assert s["total_tokens"] == 150
    assert s["total_calls"] == 1
    # Detail buckets default to zero, not absent.
    assert s["total_reasoning_tokens"] == 0
    assert s["total_audio_seconds"] == 0


def test_detail_buckets_accumulate():
    t = TokenTracker()
    t.record("m", prompt=23, completion=854,
             reasoning=442, cached_prompt=10, total_reported=877)
    t.record("m", prompt=7, completion=146,
             reasoning=58, total_reported=153)
    m = t.summary()["by_model"]["m"]
    assert m["reasoning_tokens"] == 500
    assert m["cached_prompt_tokens"] == 10
    assert m["total_reported"] == 1030
    assert m["calls"] == 2


def test_total_reported_drift_is_visible():
    # Provider reports a total larger than prompt+completion (an unbucketed
    # cost) — the ledger must surface the drift, not hide it.
    t = TokenTracker()
    t.record("m", prompt=10, completion=20, total_reported=45)
    s = t.summary()
    assert s["total_tokens"] == 30          # our sum
    assert s["total_reported_tokens"] == 45  # their figure — drift visible


def test_audio_seconds_dimension():
    t = TokenTracker()
    t.record("dashscope/qwen3-asr-flash", prompt=120, completion=80,
             audio_seconds=93.5)
    t.record("openai/whisper-1", audio_seconds=61.2)
    s = t.summary()
    assert s["total_audio_seconds"] == 154.7
    assert s["by_model"]["openai/whisper-1"]["calls"] == 1


def test_cache_hits_unchanged():
    t = TokenTracker()
    t.record_cache_hit("m")
    s = t.summary()
    assert s["total_cache_hits"] == 1
    assert s["total_calls"] == 0
