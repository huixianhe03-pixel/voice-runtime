"""场景 D：播放中一次 80ms 高能量噪声，不得因此永久中断当前回复。"""

from __future__ import annotations

from conftest import run

from voice_runtime.events import E
from voice_runtime.providers.config import RuntimeConfig
from voice_runtime.scenarios import scenario_c, scenario_d


def test_noise_does_not_permanently_interrupt():
    r = run(scenario_d())
    completed = [
        e
        for e in r.log.of(E.GENERATION_COMPLETED)
        if e.generation_id == 1
    ]
    assert completed, "回复被噪声永久中断了"
    assert completed[0].payload["terminal_state"] == "completed"
    assert r.log.count(E.GENERATION_CANCELLED) == 0


def test_entire_reply_is_still_heard():
    r = run(scenario_d())
    p = r.log.last(E.TURN_SETTLED).payload
    assert p["was_interrupted"] is False
    assert p["heard_text"] == p["generated_text"]
    assert p["unheard_suffix"] == ""


def test_noise_was_rejected_not_confirmed():
    r = run(scenario_d())
    assert r.log.count(E.BARGE_IN_CANDIDATE) == 1, "应该进入 candidate（phase 1）"
    assert r.log.count(E.BARGE_IN_REJECTED) == 1
    assert r.log.count(E.BARGE_IN_CONFIRMED) == 0
    assert r.metrics.false_interruption_count == 1
    rejected = r.log.first(E.BARGE_IN_REJECTED).payload
    assert rejected["reason"] == "speech_stopped_before_confirmation"
    assert rejected["elapsed_ms"] < rejected["confirm_window_ms"]


def test_playback_paused_then_resumed():
    """两阶段的价值就在这里：phase 1 已经 pause 了（所以真打断时体感够快），
    但因为没确认，播放又恢复了（所以噪声不造成永久中断）。"""
    r = run(scenario_d())
    paused = r.log.first(E.PLAYBACK_PAUSED)
    resumed = r.log.first(E.PLAYBACK_RESUMED)
    assert paused is not None and resumed is not None
    assert resumed.ts > paused.ts
    hiccup_ms = (resumed.ts - paused.ts) * 1000
    # 这就是 pause-then-resume 的代价：用户听到一小段停顿再续播。
    # 上界是确认窗口 + 一帧。
    assert hiccup_ms <= r.session.cfg.bargein.confirm_ms + 20.0


def test_phase1_pause_is_as_fast_as_a_real_bargein():
    """抗噪保护不消耗体感停播延迟——这是两阶段设计的唯一理由。
    所以场景 D 的停播延迟必须和场景 C（真打断）一样快。"""
    d = run(scenario_d())
    c = run(scenario_c())
    assert d.metrics.overlap_duration_ms[0] == c.metrics.overlap_duration_ms[0]


def test_longer_confirmation_window_still_rejects_noise():
    cfg = RuntimeConfig()
    cfg.bargein.confirm_ms = 300.0
    r = run(scenario_d(cfg))
    assert r.log.count(E.BARGE_IN_REJECTED) == 1
    assert r.log.count(E.GENERATION_CANCELLED) == 0


def test_zero_tolerance_still_works():
    """一帧静音就判噪声。更激进，但 80ms 噪声照样被拒。"""
    cfg = RuntimeConfig()
    cfg.bargein.tolerated_silence_frames = 0
    r = run(scenario_d(cfg))
    assert r.log.count(E.BARGE_IN_REJECTED) == 1
    assert r.log.count(E.GENERATION_CANCELLED) == 0
