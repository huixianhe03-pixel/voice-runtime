"""场景 A～E 的驱动。测试和 sample-output 都从这里跑，保证两边是同一条路径。"""

from __future__ import annotations

from dataclasses import dataclass

from .clock import VirtualClock
from .events import E, Event, EventLog
from .metrics import SessionMetrics, compute
from .providers.config import RuntimeConfig
from .session import Session

TURN1 = "Please book it for Wednesday afternoon."
TURN1_PART_A = "Please book it for"
TURN1_PART_B = "Wednesday afternoon."
TURN2 = "Stop. Make it Friday instead."


@dataclass
class ScenarioResult:
    name: str
    session: Session
    clock: VirtualClock
    metrics: SessionMetrics

    @property
    def log(self) -> EventLog:
        return self.session.log


async def wait_for(clock: VirtualClock, log: EventLog, *types: str, limit_s: float = 30.0) -> Event | None:
    """推进虚拟时钟直到出现一个**新的**指定事件。"""
    baseline = log.count(*types)
    start = clock.now()
    while clock.now() - start < limit_s:
        if log.count(*types) > baseline:
            return log.of(*types)[-1]
        if not await clock.advance_to_next():
            break
    return log.of(*types)[-1] if log.count(*types) > baseline else None


def _new(cfg: RuntimeConfig | None = None) -> tuple[VirtualClock, Session]:
    clock = VirtualClock()
    return clock, Session(cfg or RuntimeConfig(), clock)


def _finish(name: str, clock: VirtualClock, session: Session) -> ScenarioResult:
    m = compute(session.log, player=session.player, session=session)
    session.log.emit(E.METRICS_SUMMARY, **{"scenario": name, **m.as_dict()})
    return ScenarioResult(name=name, session=session, clock=clock, metrics=m)


# --------------------------------------------------------------------- A

async def scenario_a(cfg: RuntimeConfig | None = None) -> ScenarioResult:
    """完整一句：判定 endpoint → 开始播放，除 provider 固有延迟外无额外排队。"""
    clock, session = _new(cfg)
    await session.start()
    session.source.enqueue_silence(200)
    session.source.enqueue_speech(1000, TURN1)
    session.source.enqueue_silence(1500)
    session.source.finish()
    await clock.run_until_idle()
    await session.close(reason="scenario_end")
    return _finish("A · 完整一句", clock, session)


# --------------------------------------------------------------------- B

async def scenario_b(cfg: RuntimeConfig | None = None) -> ScenarioResult:
    """犹豫：停顿 700ms 期间不得开播，且不靠一个大的固定静音阈值。"""
    clock, session = _new(cfg)
    await session.start()
    session.source.enqueue_silence(200)
    session.source.enqueue_speech(900, TURN1_PART_A)
    session.source.enqueue_silence(700)
    session.source.enqueue_speech(900, TURN1_PART_B)
    session.source.enqueue_silence(1400)
    session.source.finish()
    await clock.run_until_idle()
    await session.close(reason="scenario_end")
    return _finish("B · 犹豫 700ms", clock, session)


# --------------------------------------------------------------------- C

async def scenario_c(cfg: RuntimeConfig | None = None) -> ScenarioResult:
    """barge-in：播放 1.2s 时用户插话。停播 + 取消 + 清队列 + 起新一轮。"""
    clock, session = _new(cfg)
    await session.start()
    session.source.enqueue_silence(200)
    session.source.enqueue_speech(1000, TURN1)

    await wait_for(clock, session.log, E.PLAYBACK_STARTED)
    await clock.run_for(1.2)

    session.source.enqueue_speech(1500, TURN2)
    session.source.enqueue_silence(1400)
    session.source.finish()
    await clock.run_until_idle()
    await session.close(reason="scenario_end")
    return _finish("C · barge-in", clock, session)


# --------------------------------------------------------------------- D

async def scenario_d(cfg: RuntimeConfig | None = None) -> ScenarioResult:
    """80ms 噪声：不得因此永久中断当前回复。"""
    clock, session = _new(cfg)
    await session.start()
    session.source.enqueue_silence(200)
    session.source.enqueue_speech(1000, TURN1)

    await wait_for(clock, session.log, E.PLAYBACK_STARTED)
    await clock.run_for(1.0)

    session.source.enqueue_noise(80)
    session.source.enqueue_silence(4000)
    session.source.finish()
    await clock.run_until_idle()
    await session.close(reason="scenario_end")
    return _finish("D · 80ms 噪声", clock, session)


# --------------------------------------------------------------------- E

async def scenario_e(cfg: RuntimeConfig | None = None) -> ScenarioResult:
    """cancel 后仍吐 2 个旧 chunk：记为 stale、不得播放、不得在新回复后再出现。"""
    if cfg is None:
        cfg = RuntimeConfig()
        cfg.tts.late_chunks_after_cancel = 2  # 默认值已经是 2，写出来是为了点明本场景的关键配置
    clock, session = _new(cfg)
    await session.start()
    session.source.enqueue_silence(200)
    session.source.enqueue_speech(1000, TURN1)

    await wait_for(clock, session.log, E.PLAYBACK_STARTED)
    await clock.run_for(1.2)

    session.source.enqueue_speech(1500, TURN2)
    session.source.enqueue_silence(1400)
    session.source.finish()
    await clock.run_until_idle()
    await session.close(reason="scenario_end")
    return _finish("E · 迟到 chunk", clock, session)


ALL = {
    "A": scenario_a,
    "B": scenario_b,
    "C": scenario_c,
    "D": scenario_d,
    "E": scenario_e,
}
