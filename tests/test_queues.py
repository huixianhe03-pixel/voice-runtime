"""队列。所有队列都有容量上限，没有例外。"""

from __future__ import annotations

import asyncio

import pytest
from conftest import run

from voice_runtime.clock import VirtualClock
from voice_runtime.events import E
from voice_runtime.providers.config import RuntimeConfig
from voice_runtime.queues import BoundedQueue, DropOldestQueue
from voice_runtime.scenarios import scenario_a, scenario_c
from voice_runtime.session import Session


def test_every_queue_in_the_session_has_a_capacity():
    r = run(scenario_c())
    qs = r.session.all_queues()
    assert qs
    for q in qs:
        assert q.capacity > 0, f"队列 {q.name} 没有上限"
        assert q.max_depth <= q.capacity, (
            f"队列 {q.name} 的 high-water {q.max_depth} 超过了容量 {q.capacity}"
        )


def test_unbounded_queue_is_impossible_to_construct():
    for cls in (BoundedQueue, DropOldestQueue):
        with pytest.raises(ValueError):
            cls("bad", 0)
        with pytest.raises(ValueError):
            cls("bad", -1)


def test_drop_oldest_drops_the_oldest_and_counts_it():
    q: DropOldestQueue[int] = DropOldestQueue("t", 3)
    for i in range(5):
        dropped = q.put_nowait(i)
        if i < 3:
            assert dropped is None
    assert q.qsize() == 3
    assert q.dropped == 2
    assert [run(_drain(q)) for _ in range(3)] == [2, 3, 4]


async def _drain(q):
    return await q.get()


def test_tts_faster_than_player_blocks_the_producer():
    """"TTS 快于播放器"的处理：阻塞生产者。

    验证两件事：队列 high-water 不超过容量（所以没有无界增长），
    以及生产者真的被 block 过（所以背压真的在起作用，不是碰巧没满）。
    """
    cfg = RuntimeConfig()
    cfg.tts.chunk_interval_ms = 0.0  # TTS 尽可能快
    cfg.tts.first_chunk_delay_ms = 0.0
    cfg.llm.chunk_interval_ms = 0.0
    cfg.llm.words_per_chunk = 1  # 更多更小的 chunk
    cfg.queues.play_q_ms = 60.0  # 只有 3 帧的缓冲
    r = run(scenario_a(cfg))
    pq = r.session.player.queue
    assert pq.capacity == 3
    assert pq.max_depth <= pq.capacity
    assert pq.blocked_count > 0, "play_q 从没满过，这个用例没测到背压"
    # 音频一个都不能丢
    assert pq.dropped == 0


def test_audio_ingress_never_blocks_and_drops_instead():
    """"ASR 慢于输入"的处理：丢最旧，绝不阻塞入口。麦克风不等人。"""

    async def body():
        clock = VirtualClock()
        cfg = RuntimeConfig()
        cfg.queues.asr_input = 2
        session = Session(cfg, clock)
        # 故意不启动 ASR：让 asr_input 一直满着
        session._scope.create_task(session.source.run(), name="ingress")
        session._scope.create_task(session._vad_loop(), name="vad")
        session._scope.create_task(session.player.run(), name="player")
        session.state = session.state
        session.source.enqueue_speech(600, "Please book it for Wednesday afternoon.")
        session.source.finish()
        await clock.run_until_idle()
        overflow = [
            e for e in session.log.of(E.QUEUE_OVERFLOW)
            if e.payload.get("queue") == "asr_input"
        ]
        assert overflow, "ASR 卡死时 asr_input 应该开始丢帧并记录"
        assert session.asr_input.qsize() <= session.asr_input.capacity
        assert session.source.frames_emitted >= 30, "入口被阻塞了，帧没发出来"
        await session.close(reason="test")

    run(body())


def test_play_queue_capacity_is_a_latency_knob():
    """play_q 容量不只是内存参数：缓冲越大，打断时要 flush 掉的越多，
    "已入队但用户没听到"的灰色地带就越大。"""
    # 小缓冲必须压到比"管线自然填充量"更小，否则队列根本不会满，
    # 两边都装得下全部 chunk，这条论断就测不出来。
    small = RuntimeConfig()
    small.queues.play_q_ms = 20.0  # 1 个 chunk
    big = RuntimeConfig()
    big.queues.play_q_ms = 2000.0  # 装得下整段回复

    rs = run(scenario_c(small))
    rb = run(scenario_c(big))

    def gray_zone(r):
        p = [e for e in r.log.of(E.TURN_SETTLED) if e.generation_id == 1][0]
        return len(p.payload["enqueued_text"]) - len(p.payload["heard_text"])

    assert gray_zone(rb) > gray_zone(rs), (
        "缓冲变大之后灰色地带没有变大，这个论断就不成立了"
    )
