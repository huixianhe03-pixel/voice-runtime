"""场景 C：barge-in。停播 + 取消旧 generation + 清空未播队列 + 起新一轮。"""

from __future__ import annotations

from conftest import run

from voice_runtime.events import E
from voice_runtime.scenarios import scenario_c

BUDGET_MS = 250.0


def test_playback_stops_within_budget():
    r = run(scenario_c())
    assert r.metrics.overlap_duration_ms, "没有记录到重叠时长"
    for v in r.metrics.overlap_duration_ms:
        assert v <= BUDGET_MS, f"用户开口到停播 {v}ms，超过 {BUDGET_MS}ms"


def test_three_latencies_recorded_separately():
    """题目要求分别记录三段延迟，而不是给一个总数。"""
    r = run(scenario_c())
    m = r.metrics
    assert m.speech_start_detection_ms and m.speech_start_detection_ms[0] is not None
    assert m.interruption_decision_ms and m.interruption_decision_ms[0] is not None
    assert m.interruption_to_playback_stop_ms
    assert m.interruption_to_playback_stop_ms[0] is not None
    # 停播生效的下界是一帧，不做 sub-frame 切分就不可能更快
    assert m.interruption_to_playback_stop_ms[0] >= 20.0


def test_logical_interruption_also_within_budget():
    r = run(scenario_c())
    assert r.metrics.logical_interruption_ms
    for v in r.metrics.logical_interruption_ms:
        assert v <= BUDGET_MS, f"逻辑打断 {v}ms 超预算"


def test_old_generation_cancelled_and_queue_flushed():
    r = run(scenario_c())
    cancelled = [
        e
        for e in r.log.of(E.GENERATION_CANCELLED)
        if e.payload.get("reason") == "barge_in"
    ]
    assert len(cancelled) == 1
    assert cancelled[0].payload["flushed_chunks"] > 0, "未播队列没被清空"
    assert cancelled[0].payload["terminal_state"] == "cancelled"


def test_fence_bumped_before_cancel():
    """fence++ 必须在 cancel 之前：反过来的话中间窗口的 in-flight chunk
    会合法通过 fence 检查然后被播出去。"""
    r = run(scenario_c())
    gen2 = [
        e for e in r.log.of(E.GENERATION_STARTED) if e.generation_id == 2
    ]
    assert gen2, "打断后没有起新一轮"
    assert gen2[0].payload["fence_before"] == 2, "新一轮启动时 fence 应该已经抬到 2"


def test_new_turn_starts_and_completes():
    r = run(scenario_c())
    completed = [
        e
        for e in r.log.of(E.GENERATION_COMPLETED)
        if e.generation_id == 2
    ]
    assert completed, "新一轮没有正常完成"
    assert len(r.session.context) == 2


def test_only_heard_text_enters_context():
    """generated text 不能无条件当作已被听到。"""
    r = run(scenario_c())
    settled = [
        e for e in r.log.of(E.TURN_SETTLED) if e.generation_id == 1
    ][0].payload

    assert settled["was_interrupted"] is True
    heard, generated = settled["heard_text"], settled["generated_text"]
    assert heard, "应该听到了一部分"
    assert heard != generated, "被打断了却认为整段都听到了"
    assert generated.startswith(heard), "听到的应该是生成文本的前缀"
    assert settled["unheard_suffix"], "应该有没听到的尾巴"
    assert heard + settled["unheard_suffix"] == generated

    ctx = r.session.context[0]
    assert ctx["content"] == heard
    assert ctx["metadata"]["interrupted"] is True
    assert ctx["metadata"]["unheard_chars"] == len(generated) - len(heard)


def test_enqueued_text_sits_between_heard_and_generated():
    """四个量的关系：heard ⊆ enqueued ⊆ generated。
    中间那一层就是"已入队但用户没听到"的灰色地带。"""
    r = run(scenario_c())
    p = [e for e in r.log.of(E.TURN_SETTLED) if e.generation_id == 1][
        0
    ].payload
    assert len(p["heard_text"]) <= len(p["enqueued_text"]) <= len(p["generated_text"])
    assert len(p["heard_text"]) < len(p["enqueued_text"]), (
        "被打断时应该存在已入队但未播出的音频"
    )


def test_played_duration_matches_heard_not_generated():
    r = run(scenario_c())
    led = r.session.ledgers[1]
    assert led.played_duration_ms() > 0
    synthesized = sum(rec.chunk.duration_ms for rec in led.records)
    assert led.played_duration_ms() < synthesized
