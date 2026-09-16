"""生命周期与取消传播。七种情况各来一个。"""

from __future__ import annotations

import asyncio

from conftest import run

from voice_runtime.clock import VirtualClock
from voice_runtime.events import E
from voice_runtime.providers.config import RuntimeConfig
from voice_runtime.scenarios import scenario_a, scenario_c, wait_for
from voice_runtime.session import GenState, Session
from voice_runtime.taskscope import ScopeClosed, TaskScope


def _live_tasks():
    cur = asyncio.current_task()
    return [t for t in asyncio.all_tasks() if t is not cur and not t.done()]


def test_no_background_tasks_survive_close():
    """底线之一：session 关闭后没有仍在运行的后台任务。
    靠断言而不是靠 review —— 代码里没有裸 create_task，所以孤儿任务
    在结构上不可能出现，但结构论证需要一个可执行的证明。"""

    async def body():
        clock = VirtualClock()
        session = Session(RuntimeConfig(), clock)
        await session.start()
        session.source.enqueue_speech(1000, "Please book it for Wednesday afternoon.")
        await wait_for(clock, session.log, E.PLAYBACK_STARTED)
        assert _live_tasks(), "跑起来的时候本该有后台任务"
        await session.close(reason="test")
        assert _live_tasks() == [], f"关闭后还有任务活着：{_live_tasks()}"
        assert session.closed_task_leftovers == []

    run(body())


def test_close_reports_no_pending_tasks_in_the_log():
    r = run(scenario_c())
    closed = r.log.last(E.SESSION_CLOSED)
    assert closed is not None
    assert closed.payload["pending_tasks"] == []


def test_close_while_speaking_settles_and_counts_residual():
    """客户端断开 / 主动关闭：播放中途关掉，账本要结算，残留要计数。"""

    async def body():
        clock = VirtualClock()
        session = Session(RuntimeConfig(), clock)
        await session.start()
        session.source.enqueue_speech(1000, "Please book it for Wednesday afternoon.")
        await wait_for(clock, session.log, E.PLAYBACK_STARTED)
        await clock.run_for(0.5)
        assert session.active is not None
        await session.close(reason="client_disconnect")

        assert session.active.state is GenState.CANCELLED
        settled = session.log.last(E.TURN_SETTLED)
        assert settled is not None
        assert settled.payload["was_interrupted"] is True
        # 播放中途关掉，听到的必然少于生成的
        assert len(settled.payload["heard_text"]) < len(
            settled.payload["generated_text"]
        )
        closed = session.log.last(E.SESSION_CLOSED)
        assert closed.payload["residual_chunks"] >= 1, "队列里应该还有没播的 chunk"
        assert _live_tasks() == []

    run(body())


def test_close_is_idempotent():
    async def body():
        clock = VirtualClock()
        session = Session(RuntimeConfig(), clock)
        await session.start()
        await clock.run_for(0.1)
        await session.close(reason="first")
        await session.close(reason="second")
        assert session.log.count(E.SESSION_CLOSED) == 1

    run(body())


def test_provider_timeout_is_a_first_class_event():
    """Provider 长时间无返回：分级超时 → TIMED_OUT → 取消，不静默挂死。"""
    cfg = RuntimeConfig()
    cfg.llm.hang = True
    cfg.llm.first_chunk_timeout_ms = 400.0
    r = run(scenario_a(cfg))

    timeouts = r.log.of(E.PROVIDER_TIMEOUT)
    assert timeouts, "provider 卡死了但没有超时事件"
    assert timeouts[0].payload["provider"] == "llm"
    assert timeouts[0].payload["stage"] == "first_chunk"
    cancelled = r.log.of(E.GENERATION_CANCELLED)
    assert cancelled
    assert cancelled[0].payload["terminal_state"] == "timed_out"
    assert r.metrics.stale_chunk_played_count == 0


def test_first_chunk_and_interval_timeouts_are_distinguishable():
    """两个超时数分开，是为了能从日志里区分"首包没来"和"流中间卡死"。"""
    cfg = RuntimeConfig()
    cfg.tts.hang = True
    cfg.tts.first_chunk_timeout_ms = 300.0
    r = run(scenario_a(cfg))
    t = r.log.first(E.PROVIDER_TIMEOUT)
    assert t.payload["provider"] == "tts"
    assert "stage" in t.payload
    assert t.payload["budget_ms"] == 300.0


def test_concurrent_cancels_are_idempotent():
    """多个 cancel 几乎同时到达：终态是吸收态，只有第一个生效。"""

    async def body():
        clock = VirtualClock()
        session = Session(RuntimeConfig(), clock)
        await session.start()
        session.source.enqueue_speech(1000, "Please book it for Wednesday afternoon.")
        await wait_for(clock, session.log, E.PLAYBACK_STARTED)

        gen = session.active
        session._request_cancel("first")
        session._request_cancel("second")
        session._request_cancel("third")

        assert session.duplicate_cancels == 2
        assert session.log.count(E.DUPLICATE_CANCEL) == 2
        # 只有一次真正的取消
        real = [
            e
            for e in session.log.of(E.GENERATION_CANCELLED)
            if e.generation_id == gen.handle.generation_id
        ]
        assert len(real) == 1
        assert real[0].payload["reason"] == "first"
        await session.close(reason="test")
        assert _live_tasks() == []

    run(body())


def test_background_exception_is_not_swallowed():
    """后台任务抛异常：上抛给 owner，不 catch-all 吞掉。"""

    async def body():
        scope = TaskScope("t")

        async def boom():
            raise RuntimeError("provider exploded")

        scope.create_task(boom(), name="boom")
        try:
            async with scope:
                pass
        except RuntimeError as exc:
            assert "exploded" in str(exc)
            return
        raise AssertionError("异常被吞掉了")

    run(body())


def test_closed_scope_refuses_new_tasks():
    """关闭之后还能 create_task 的话，就会重新长出孤儿任务。"""

    async def body():
        scope = TaskScope("t")
        await scope.aclose(timeout=0.1)

        async def noop():
            return None

        try:
            scope.create_task(noop(), name="late")
        except ScopeClosed:
            return
        raise AssertionError("关闭后的 scope 还接受新任务")

    run(body())


def test_ingress_survives_generation_cancel():
    """取消 generation 不能把 Ingress 撕掉 —— 否则打断之后就聋了。
    场景 C 里 turn 2 能起来，本身就是这条的证明。"""
    r = run(scenario_c())
    starts = r.log.of(E.GENERATION_STARTED)
    assert len(starts) == 2
    assert r.log.of(E.SPEECH_START)[-1].ts > r.log.first(E.GENERATION_CANCELLED).ts - 1.0
    # 取消之后 VAD / ASR / source 仍在产出
    frames_after = [
        e
        for e in r.log.of(E.AUDIO_FRAME)
        if e.ts > r.log.first(E.GENERATION_CANCELLED).ts
    ]
    assert len(frames_after) > 10
