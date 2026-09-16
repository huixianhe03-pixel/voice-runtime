"""场景 B：犹豫 700ms。

两条断言，第二条才是重点：
  1. 停顿期间不得开始播放
  2. 不能靠一个很大的固定静音阈值来实现 —— 用"同一次会话里生效阈值变过"
     来证明。固定阈值实现会让这条断言失败。
"""

from __future__ import annotations

from conftest import run

from voice_runtime.endpointer import classify, Completeness
from voice_runtime.events import E
from voice_runtime.scenarios import scenario_b


def test_no_playback_during_hesitation():
    r = run(scenario_b())
    starts = r.log.of(E.SPEECH_START)
    playback = r.log.first(E.PLAYBACK_STARTED)
    assert len(starts) == 2, "脚本应该产生两段语音"
    assert playback is not None
    # 播放必须发生在第二段语音**之后**，而不是在 700ms 停顿里
    assert playback.ts > starts[1].ts, (
        f"停顿期间就开播了：playback @{playback.ts:.3f}, "
        f"第二段语音 @{starts[1].ts:.3f}"
    )


def test_hesitation_was_judged_incomplete_and_waited():
    r = run(scenario_b())
    starts = r.log.of(E.SPEECH_START)
    pause_window = [
        e
        for e in r.log.of(E.ENDPOINT_EVALUATED)
        if starts[0].ts < e.ts < starts[1].ts
    ]
    assert pause_window, "停顿期间应该有 endpoint 评估记录"
    for e in pause_window:
        assert e.payload["decision"] == "wait"
        assert e.payload["completeness"]["verdict"] == "incomplete"
        assert e.payload["completeness"]["reason"].startswith("trailing_function_word")
        assert e.payload["effective_threshold_ms"] == 1100.0
    # 重点在这里：停顿期的静音时长**超过了 complete 档的 400ms**，
    # 却依然 decision=="wait"。一个固定 400ms 阈值会在这里误判抢话。
    # （观察到的上限是 580ms 而不是 700ms：VAD 有 60ms hangover，
    #   评估日志又每 100ms 才记一次。）
    peak = max(e.payload["silence_ms"] for e in pause_window)
    assert peak > 400.0, f"停顿期最大静音只有 {peak}ms，这个用例没测到点子上"


def test_threshold_is_dynamic_not_a_single_big_number():
    """如果实现用的是一个固定静音阈值，这条会失败。"""
    r = run(scenario_b())
    seen = {e.payload["effective_threshold_ms"] for e in r.log.of(E.ENDPOINT_EVALUATED)}
    assert len(seen) >= 2, f"生效阈值从头到尾没变过：{seen}"
    assert 1100.0 in seen and 400.0 in seen


def test_endpoint_confirmed_promptly_once_really_done():
    r = run(scenario_b())
    turn = r.metrics.turns[0]
    assert turn.completeness == "complete"
    # 真正说完之后用的是 complete 档（400ms），不是 incomplete 档
    assert turn.effective_threshold_ms == 400.0
    assert turn.endpoint_latency_ms is not None
    assert turn.endpoint_latency_ms <= 500.0


def test_completeness_rules():
    assert classify("Please book it for").verdict is Completeness.INCOMPLETE
    assert classify("Wednesday afternoon.").verdict is Completeness.COMPLETE
    assert classify("um").verdict is Completeness.INCOMPLETE
    assert classify("").verdict is Completeness.INCOMPLETE
    assert classify("book the room tomorrow").verdict is Completeness.AMBIGUOUS
