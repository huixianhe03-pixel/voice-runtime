"""Trace 完整性与失败路径。

这一组测试对应的两个 bug 都不是设计时想到的，是写完之后拿探针捅出来的。
留在这里是因为它们都能被"看起来正常"的代码悄悄放过——回归测试是唯一的防线。
"""

from __future__ import annotations

import asyncio

from conftest import run

from voice_runtime.audio import FRAME_MS, ms_to_samples, pcm_tone
from voice_runtime.clock import VirtualClock
from voice_runtime.events import E, EventLog
from voice_runtime.ledger import ChunkRecord, TtsChunk
from voice_runtime.player import Player
from voice_runtime.providers.config import RuntimeConfig
from voice_runtime.scenarios import scenario_c, scenario_d, wait_for
from voice_runtime.session import GenState, Session
from voice_runtime.taskscope import TaskScope


def _stages(log, event_type):
    return [e.payload.get("stage") for e in log.of(event_type)]


# --------------------------------------------------------------------------
# bug 1：LLM 抛异常 → TTS 永远阻塞 → asyncio.wait(ALL_COMPLETED) 不返回
# --------------------------------------------------------------------------


def test_llm_exception_fails_fast_and_is_attributed_correctly():
    """LLM 抛异常时，TTS 还阻塞在 inq.get() 上等一个永远不会来的 SENTINEL。

    用默认的 ALL_COMPLETED，这个 await 再也不返回，最后是 TTS 看门狗兜住的：
    默认配置下 2 秒静默，而且终态被记成 timed_out 而不是 failed——
    日志归因直接错了。探针跑出来虚拟时间 101 秒（因为我把超时调大了）。

    FIRST_EXCEPTION 之后：立刻失败，终态是 failed，归因正确。
    """

    async def body():
        clock = VirtualClock()
        cfg = RuntimeConfig()
        # 把看门狗设得很大，好让这条测试只检验异常路径本身，
        # 而不是"看门狗最终会救场"。
        for c in (cfg.llm, cfg.tts):
            c.first_chunk_timeout_ms = 60_000.0
            c.chunk_timeout_ms = 60_000.0
        session = Session(cfg, clock)

        async def boom(*_a, **_k):
            await clock.sleep(0.05)
            raise RuntimeError("LLM exploded")

        session.llm.run = boom

        await session.start()
        session.source.enqueue_speech(1000, "Please book it for Wednesday afternoon.")
        session.source.enqueue_silence(800)
        session.source.finish()
        await clock.run_until_idle()

        assert session.active is not None
        assert session.active.state is GenState.FAILED, (
            f"终态是 {session.active.state.value}，说明不是异常路径收的尾，"
            "而是被看门狗兜住的 —— 归因错了"
        )
        assert session.log.count(E.PROVIDER_TIMEOUT) == 0, "不该退化成超时"
        # 异常路径应该秒收，不该拖到看门狗的 60 秒
        assert clock.now() < 10.0, f"虚拟时间跑到 {clock.now():.1f}s，卡住了"
        await session.close(reason="test")

    run(body())


def test_failed_generation_still_settles_the_ledger():
    """失败也要结算：账本不能留在半空中，上下文不能留着没听到的内容。"""

    async def body():
        clock = VirtualClock()
        cfg = RuntimeConfig()
        for c in (cfg.llm, cfg.tts):
            c.first_chunk_timeout_ms = 60_000.0
            c.chunk_timeout_ms = 60_000.0
        session = Session(cfg, clock)

        async def boom(*_a, **_k):
            await clock.sleep(0.05)
            raise RuntimeError("LLM exploded")

        session.llm.run = boom
        await session.start()
        session.source.enqueue_speech(1000, "Please book it for Wednesday afternoon.")
        session.source.enqueue_silence(800)
        session.source.finish()
        await clock.run_until_idle()

        settled = session.log.last(E.TURN_SETTLED)
        assert settled is not None, "失败的 generation 没有结算账本"
        assert settled.payload["was_interrupted"] is True
        assert settled.payload["heard_text"] == "", "什么都没合成出来，不该认为听到了东西"
        assert session.context[-1]["content"] == ""
        await session.close(reason="test")

    run(body())


# --------------------------------------------------------------------------
# bug 2：pause 和 resume 落在同一帧内 → sink 没观察到 → trace 自相矛盾
# --------------------------------------------------------------------------


def test_pause_shorter_than_one_frame_keeps_trace_symmetric():
    """暂停没撑过一帧时，sink 根本没机会观察到暂停标志。

    修之前：日志里只有一个孤立的 playback_resumed，没有对应的 playback_paused。
    读日志的人会以为漏了一条。而"只看日志就能解释这一次执行"是这道题的标准，
    所以这是实打实的观测性 bug，不是洁癖。

    修之后：pause() 自己记 stage="requested"，resume() 带上
    pause_became_effective=False 说明它没撑过一帧。
    """

    async def body():
        clock = VirtualClock()
        log = EventLog(clock=clock, session_id="s1")
        player = Player(clock, log, play_q_ms=400.0)
        scope = TaskScope("t")
        scope.create_task(player.run(), name="sink")

        n = ms_to_samples(400)
        chunk = TtsChunk(
            generation_id=1, chunk_index=0, text="hello world now",
            text_span=(0, 15), pcm=pcm_tone(n),
        )
        await player.write(ChunkRecord(chunk=chunk, synthesized_at=0.0))

        await scope.create_task(clock.run_for(0.030), name="d1")
        player.pause()
        await scope.create_task(clock.run_for(0.005), name="d2")  # 不足一帧
        player.resume()
        await scope.create_task(clock.run_for(0.200), name="d3")

        paused = log.of(E.PLAYBACK_PAUSED)
        resumed = log.of(E.PLAYBACK_RESUMED)
        assert len(paused) >= 1, "pause 命令发出了却没有任何记录"
        assert len(resumed) == 1
        assert "requested" in _stages(log, E.PLAYBACK_PAUSED)
        # 没撑过一帧，所以不该有 effective
        assert "effective" not in _stages(log, E.PLAYBACK_PAUSED)
        assert resumed[0].payload["pause_became_effective"] is False

        await scope.aclose(timeout=0.1)

    run(body())


def test_real_pause_records_both_stages():
    """撑过一帧的暂停：两个 stage 都有，effective_lag 的下界是一帧。"""

    async def body():
        clock = VirtualClock()
        log = EventLog(clock=clock, session_id="s1")
        player = Player(clock, log, play_q_ms=400.0)
        scope = TaskScope("t")
        scope.create_task(player.run(), name="sink")

        n = ms_to_samples(400)
        chunk = TtsChunk(
            generation_id=1, chunk_index=0, text="hello world now",
            text_span=(0, 15), pcm=pcm_tone(n),
        )
        await player.write(ChunkRecord(chunk=chunk, synthesized_at=0.0))

        await scope.create_task(clock.run_for(0.030), name="d1")
        player.pause()
        await scope.create_task(clock.run_for(0.150), name="d2")
        player.resume()
        await scope.create_task(clock.run_for(0.200), name="d3")

        stages = _stages(log, E.PLAYBACK_PAUSED)
        assert stages.count("requested") == 1
        assert stages.count("effective") == 1
        eff = [e for e in log.of(E.PLAYBACK_PAUSED) if e.payload.get("stage") == "effective"][0]
        assert eff.payload["effective_lag_ms"] <= FRAME_MS + 1e-6, (
            "停播生效滞后超过一帧，说明 sink 的暂停检查放错了位置"
        )
        assert log.of(E.PLAYBACK_RESUMED)[0].payload["pause_became_effective"] is True
        await scope.aclose(timeout=0.1)

    run(body())


def test_scenario_traces_have_no_orphan_resume():
    """跨全部场景的不变量：每个 playback_resumed 前面都有 playback_paused。"""
    for runner in (scenario_c, scenario_d):
        r = run(runner())
        depth = 0
        for ev in r.log.events:
            if ev.event_type == E.PLAYBACK_PAUSED and ev.payload.get("stage") == "requested":
                depth += 1
            elif ev.event_type == E.PLAYBACK_RESUMED:
                depth -= 1
                assert depth >= 0, (
                    f"{r.name}：@{ev.ts:.3f}s 出现了没有对应 paused 的 resumed"
                )


def test_metrics_stop_latency_comes_from_effective_stage():
    """指标里的 playback_stop 必须取 effective，取 requested 就永远是 0。"""
    r = run(scenario_c())
    assert r.metrics.interruption_to_playback_stop_ms
    v = r.metrics.interruption_to_playback_stop_ms[0]
    assert v is not None
    assert v >= FRAME_MS - 1e-6, f"停播延迟 {v}ms 小于一帧，说明取错了 stage"
