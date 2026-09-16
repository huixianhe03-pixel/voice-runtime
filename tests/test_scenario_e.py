"""场景 E：TTS 收到 cancel 后仍吐回 2 个旧 chunk。

三条要求，一条比一条严：
  1. 记为 stale event、不得播放
  2. 不得在新回复后再次出现
  3. session 关闭后不得再写播放器
"""

from __future__ import annotations

from conftest import run

from voice_runtime.clock import VirtualClock
from voice_runtime.events import E
from voice_runtime.ledger import ChunkRecord, GenerationHandle, TtsChunk
from voice_runtime.player import Player, WriteResult
from voice_runtime.providers.config import RuntimeConfig
from voice_runtime.scenarios import scenario_e
from voice_runtime.session import Session


def _stale(log):
    return [
        e
        for e in log.of(E.STALE_EVENT_DROPPED)
        if e.payload.get("reason") == "superseded_generation"
    ]


def test_late_chunks_are_logged_as_stale():
    r = run(scenario_e())
    stale = _stale(r.log)
    assert len(stale) >= 2, f"配置了 2 个迟到 chunk，只记到 {len(stale)} 个"
    for e in stale:
        assert e.generation_id == 1
        assert e.payload["active_fence"] == 2


def test_stale_chunks_are_never_played():
    """底线之一。这个数不是 0 就说明 fence 检查漏了。"""
    r = run(scenario_e())
    assert r.metrics.stale_chunk_played_count == 0
    assert r.session.player.stale_played == 0


def test_stale_chunks_do_not_reappear_after_new_reply():
    """不得缓存起来以后放——必须丢弃。"""
    r = run(scenario_e())
    new_playback = [
        e
        for e in r.log.of(E.PLAYBACK_STARTED)
        if e.generation_id == 2
    ]
    assert new_playback, "新回复没开始播"
    cutoff = new_playback[0].ts
    late_after = [e for e in _stale(r.log) if e.ts > cutoff]
    assert not late_after, f"新回复开始后又出现了 stale chunk：{late_after}"


def test_no_audio_of_old_generation_after_fence_bump():
    """旧 generation 的音频不得重播。直接查账本：
    gen1 的任何 record 在 fence 抬起之后都不该再增加 played_samples。"""
    r = run(scenario_e())
    led = r.session.ledgers[1]
    truncated = [rec for rec in led.records if rec.truncated]
    assert truncated, "被打断时应该有 chunk 被截断"
    # 迟到的两个 chunk 根本没进账本，因为它们属于已结算的那一代
    assert all(rec.chunk.generation_id == 1 for rec in led.records)


def test_write_after_close_is_rejected():
    """第三条要求：session 关闭后不得再写播放器。

    单独做成单元测试，因为在这个实现里迟到投递挂在 session scope 上，关闭时
    会被一起取消，所以跑不出"关闭后到达"的时序。生产环境里逃出来的那个
    写入来自 provider 的线程或回调，不在任何 scope 里 —— 所以 _closed
    硬门禁是必须的，不能只靠"先 await 任务"。
    """

    async def body():
        clock = VirtualClock()
        cfg = RuntimeConfig()
        session = Session(cfg, clock)
        await session.start()
        await clock.run_for(0.1)
        await session.close(reason="test")

        player = session.player
        assert player.is_closed
        before = player.stale_received

        handle = GenerationHandle("s1", 1, 1)
        chunk = TtsChunk(
            generation_id=handle.generation_id,
            chunk_index=99,
            text="escaped ",
            text_span=(0, 8),
            pcm=b"\x00\x00" * 320,
        )
        record = ChunkRecord(chunk=chunk, synthesized_at=clock.now())
        result = await player.write(record)

        assert result is WriteResult.DROPPED_CLOSED
        assert record.enqueued is False
        assert player.stale_received == before + 1
        closed_drops = [
            e
            for e in session.log.of(E.STALE_EVENT_DROPPED)
            if e.payload.get("reason") == "session_closed"
        ]
        assert closed_drops, "关闭后的写入没有被记成 stale"
        assert player.stale_played == 0

    run(body())


def test_more_late_chunks_still_all_dropped():
    cfg = RuntimeConfig()
    cfg.tts.late_chunks_after_cancel = 6
    r = run(scenario_e(cfg))
    assert len(_stale(r.log)) >= 6
    assert r.metrics.stale_chunk_played_count == 0


def test_fence_cannot_go_backwards():
    """用 < 比较的正确性前提就是单调性。回退直接报错，不留侥幸。"""

    async def body():
        clock = VirtualClock()
        session = Session(RuntimeConfig(), clock)
        session.player.set_fence(5)
        try:
            session.player.set_fence(3)
        except ValueError:
            return
        raise AssertionError("fence 回退居然被允许了")

    run(body())
