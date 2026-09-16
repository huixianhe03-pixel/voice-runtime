"""场景 A：完整一句 → 判定 endpoint → 开始播放，无明显额外排队。"""

from __future__ import annotations

from conftest import provider_floor_ms, run

from voice_runtime.audio import FRAME_MS
from voice_runtime.events import E
from voice_runtime.providers.config import RuntimeConfig
from voice_runtime.scenarios import scenario_a


def test_endpoint_committed_and_playback_happens():
    r = run(scenario_a())
    assert r.log.count(E.ENDPOINT_COMMITTED) == 1
    assert r.log.count(E.PLAYBACK_STARTED) == 1


def test_no_extra_queueing_beyond_provider_latency():
    """"除 Provider 固有延迟外无明显额外排队"。

    这条断言的重点是它会**失败**：只要把流水线改成"等 LLM 全部生成完再送
    TTS"，turn-end → first-audio 就会涨到整段文本的合成时间，立刻超标。
    """
    cfg = RuntimeConfig()
    r = run(scenario_a(cfg))
    turn = r.metrics.turns[0]
    floor = provider_floor_ms(cfg)
    assert turn.turn_end_to_first_audio_ms is not None
    # 容差一帧：endpoint 判定发生在帧边界上
    assert turn.turn_end_to_first_audio_ms <= floor + FRAME_MS, (
        f"first-audio 用了 {turn.turn_end_to_first_audio_ms}ms，"
        f"provider 固有延迟只有 {floor}ms —— 中间有额外排队"
    )


def test_full_reply_is_heard_when_not_interrupted():
    r = run(scenario_a())
    settled = r.log.last(E.TURN_SETTLED)
    assert settled is not None
    p = settled.payload
    assert p["was_interrupted"] is False
    assert p["heard_text"] == p["generated_text"]
    assert p["unheard_suffix"] == ""


def test_context_carries_full_reply():
    r = run(scenario_a())
    assert len(r.session.context) == 1
    entry = r.session.context[0]
    assert entry["metadata"]["interrupted"] is False
    assert entry["metadata"]["unheard_chars"] == 0


def test_no_stale_and_no_false_interruption():
    r = run(scenario_a())
    assert r.metrics.stale_chunk_received_count == 0
    assert r.metrics.stale_chunk_played_count == 0
    assert r.metrics.false_interruption_count == 0
