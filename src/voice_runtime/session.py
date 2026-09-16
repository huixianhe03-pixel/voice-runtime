"""Session：编排器，也是生命周期的唯一 owner。

任务拓扑：

    session_scope  (TaskScope)
    ├── source_task   ─┐
    ├── vad_task       │  session 生命周期：cancel generation 时不受影响
    ├── asr_task       │  （否则打断之后就聋了，下一句话没人接）
    ├── player_task   ─┘
    └── gen_task       ─ generation 生命周期：cancel 只撕这一层
        └── gen_scope (TaskScope)
            ├── llm / tts
            └── 两个 watchdog

关闭顺序是固定的：cancel → await 任务（带超时）→ 关 provider → 关 Player
→ 发 session_closed。Player 最后关，而且带 _closed 硬门禁。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .audio import FRAME_MS, AudioFrame
from .bargein import BargeInDetector, BargeInOutcome
from .clock import Clock
from .endpointer import Endpointer
from .events import E, EventLog
from .ledger import GenerationHandle, GenerationLedger
from .player import Player
from .providers.asr import FakeAsr, TranscriptOracle
from .providers.config import Jitter, RuntimeConfig
from .providers.llm import FakeLlm
from .providers.tts import FakeTts
from .providers.vad import FakeVad, VadState, VadTransition
from .queues import BoundedQueue, DropOldestQueue
from .source import ScriptedAudioSource
from .taskscope import TaskScope
from .watchdog import Watchdog


class SessionState(Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    SPEAKING_PAUSED = "speaking_paused"
    CLOSING = "closing"
    CLOSED = "closed"


class GenState(Enum):
    """终态全是吸收态：进去之后针对该 generation 的任何事件一律归 stale，
    不区分它为什么迟到。这顺带把"多个 cancel 几乎同时到达"变成天然幂等。"""

    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    TIMED_OUT = "timed_out"

    @property
    def terminal(self) -> bool:
        return self in (
            GenState.COMPLETED,
            GenState.CANCELLED,
            GenState.FAILED,
            GenState.TIMED_OUT,
        )


@dataclass
class ActiveGeneration:
    handle: GenerationHandle
    ledger: GenerationLedger
    state: GenState = GenState.PENDING
    started_at: float = 0.0
    text_q: BoundedQueue | None = None
    watchdogs: list[Watchdog] = field(default_factory=list)


@dataclass
class BargeInTimings:
    """场景 C 要求分别记录的三段延迟 + 实际重叠时长。"""

    first_loud_at: float | None = None
    speech_start_detected_at: float | None = None
    decision_at: float | None = None
    playback_stopped_at: float | None = None
    confirmed_at: float | None = None

    def as_dict(self) -> dict:
        def ms(a, b):
            if a is None or b is None:
                return None
            return round((b - a) * 1000, 2)

        return {
            "speech_start_detection_ms": ms(self.first_loud_at, self.speech_start_detected_at),
            "interruption_decision_ms": ms(self.speech_start_detected_at, self.decision_at),
            "playback_stop_ms": ms(self.decision_at, self.playback_stopped_at),
            "speech_start_to_playback_stop_ms": ms(self.first_loud_at, self.playback_stopped_at),
            "overlap_duration_ms": ms(self.first_loud_at, self.playback_stopped_at),
            "logical_interruption_ms": ms(self.first_loud_at, self.confirmed_at),
        }


class Session:
    def __init__(
        self,
        cfg: RuntimeConfig,
        clock: Clock,
        *,
        session_id: str = "s1",
    ) -> None:
        self.cfg = cfg
        self.clock = clock
        self.session_id = session_id
        self.log = EventLog(clock=clock, session_id=session_id)
        self.state = SessionState.IDLE

        jitter = Jitter(cfg.rng())
        self.oracle = TranscriptOracle()
        self.frame_bus: DropOldestQueue[AudioFrame] = DropOldestQueue(
            "frame_bus", cfg.queues.frame_bus
        )
        self.asr_input: DropOldestQueue[AudioFrame] = DropOldestQueue(
            "asr_input", cfg.queues.asr_input
        )
        self.source = ScriptedAudioSource(clock, self.log, self.oracle, self.frame_bus)
        self.vad = FakeVad(cfg.vad)
        self.asr = FakeAsr(cfg.asr, clock, self.log, self.oracle, jitter)
        self.llm = FakeLlm(cfg.llm, clock, self.log, jitter)
        self.tts = FakeTts(cfg.tts, clock, self.log, jitter)
        self.endpointer = Endpointer(cfg.endpoint, clock, self.log, self.asr)
        self.bargein = BargeInDetector(cfg.bargein, clock, self.log)
        self.player = Player(
            clock,
            self.log,
            play_q_ms=cfg.queues.play_q_ms,
            close_drain_ms=cfg.close_drain_ms,
        )
        self.player.on_playback_started = self._on_playback_started

        self._scope = TaskScope(f"session:{session_id}")
        self._gen_scope: TaskScope | None = None
        self._gen_task: asyncio.Task | None = None

        self._turn_id = 0
        self._gen_counter = 0
        self.active: ActiveGeneration | None = None
        self.ledgers: dict[int, GenerationLedger] = {}
        self.context: list[dict] = []
        self.timings: list[BargeInTimings] = []
        self._pending_timing: BargeInTimings | None = None
        self.turn_end_ts: dict[int, float] = {}
        self.first_audio_ts: dict[int, float] = {}
        self.duplicate_cancels = 0
        self.closed_task_leftovers: list[str] = []

    # ------------------------------------------------------------- 启动 / 关闭

    async def start(self) -> None:
        self.state = SessionState.LISTENING
        self._turn_id = 1
        self.endpointer.arm(self._turn_id)
        self.asr.reset_for_next_turn(self._turn_id)
        self._scope.create_task(self.source.run(), name="ingress")
        self._scope.create_task(self._vad_loop(), name="vad")
        self._scope.create_task(self.asr.run(self.asr_input), name="asr")
        self._scope.create_task(self.player.run(), name="player")

    async def close(self, reason: str = "explicit") -> None:
        if self.state in (SessionState.CLOSED, SessionState.CLOSING):
            return
        self.state = SessionState.CLOSING

        # 1. 先取消 generation
        if self.active is not None and not self.active.state.terminal:
            self._transition(self.active, GenState.CANCELLED, reason=f"session_close:{reason}")
            self._settle(self.active, interrupted=True)
        if self._gen_task is not None and not self._gen_task.done():
            self._gen_task.cancel()
        if self._gen_scope is not None:
            await self._gen_scope.aclose(timeout=self.cfg.task_join_timeout_ms / 1000.0)

        # 2. 再带上界地等 session 级任务释放
        leftovers = await self._scope.aclose(
            timeout=self.cfg.task_join_timeout_ms / 1000.0
        )
        self.closed_task_leftovers = leftovers

        # 3. Player 最后关。_closed 门禁和"先 await 任务"是两道独立的保险。
        await self.player.close()

        self.state = SessionState.CLOSED
        self.log.emit(
            E.SESSION_CLOSED,
            reason=reason,
            pending_tasks=leftovers,
            residual_chunks=self.player.residual_chunks,
            residual_samples=self.player.residual_samples,
        )

    # ------------------------------------------------------------- ingress

    async def _vad_loop(self) -> None:
        """常驻。永不因 generation 取消而停——否则打断之后就聋了。"""
        while True:
            frame = await self.frame_bus.get()

            dropped = self.asr_input.put_nowait(frame)
            if dropped is not None:
                self.log.emit(
                    E.QUEUE_OVERFLOW,
                    queue="asr_input",
                    policy="drop_oldest",
                    dropped_seq=dropped.seq,
                    capacity=self.asr_input.capacity,
                )

            result = self.vad.push(frame)
            if result.transition is VadTransition.SPEECH_START:
                self.log.emit(
                    E.SPEECH_START,
                    turn_id=self._turn_id,
                    seq=frame.seq,
                    rms=round(result.rms, 4),
                    consecutive_loud=result.consecutive_loud,
                )
            elif result.transition is VadTransition.SPEECH_END:
                self.log.emit(
                    E.SPEECH_END, turn_id=self._turn_id, seq=frame.seq
                )

            self._handle_bargein(result)

            decision = self.endpointer.on_vad(result)
            if decision is not None and decision.committed:
                self.turn_end_ts[self._turn_id] = self.clock.now()
                self._scope.create_task(
                    self._begin_turn(decision.partial_text), name=f"begin-t{self._turn_id}"
                )

    # ------------------------------------------------------------- barge-in

    def _handle_bargein(self, result) -> None:
        d = self.bargein.on_vad(result)

        if d.outcome is BargeInOutcome.CANDIDATE:
            # phase 1：廉价可逆，立刻做，不等确认。
            t = BargeInTimings(
                first_loud_at=d.first_loud_at,
                speech_start_detected_at=d.candidate_at,
                decision_at=self.clock.now(),
            )
            self._pending_timing = t
            self.player.pause()
            t.playback_stopped_at = self.clock.now() + FRAME_MS / 1000.0
            if self.state is SessionState.SPEAKING:
                self.state = SessionState.SPEAKING_PAUSED

        elif d.outcome is BargeInOutcome.REJECTED:
            # 80ms 噪声走这条路。不得因此永久中断当前回复。
            self.player.resume()
            if self.state is SessionState.SPEAKING_PAUSED:
                self.state = SessionState.SPEAKING
            self._pending_timing = None

        elif d.outcome is BargeInOutcome.CONFIRMED:
            self._commit_barge_in()

    def _commit_barge_in(self) -> None:
        gen = self.active
        if gen is None or gen.state.terminal:
            return

        # 顺序有要求：fence++ 必须在 cancel 之前。反过来的话，中间那个窗口里
        # 到达的 in-flight chunk 会合法通过 fence 检查，然后被播出去。
        self.player.set_fence(gen.handle.generation_id + 1)
        flushed = self.player.flush()

        self._transition(gen, GenState.CANCELLED, reason="barge_in", flushed_chunks=flushed)
        # 迟到 chunk 挂在 session scope 上：它们来自 provider 传输层，
        # 不属于被取消的那一代的任务树。
        self.tts.schedule_late(self._scope, gen.handle, self.player.write)
        self._settle(gen, interrupted=True)

        if self._gen_task is not None and not self._gen_task.done():
            self._gen_task.cancel()

        # 必须解除 pause，否则 sink 永远卡着，下一代也放不出来。
        # fence 已经把旧音频拦死了，所以这里解除是安全的。
        self.player.release_pause()

        if self._pending_timing is not None:
            self._pending_timing.confirmed_at = self.clock.now()
            self.timings.append(self._pending_timing)
            self._pending_timing = None

        self._next_turn()

    # ------------------------------------------------------------- turn / 生成

    def _next_turn(self) -> None:
        self._turn_id += 1
        self.state = SessionState.LISTENING
        self.asr.reset_for_next_turn(self._turn_id)
        # 打断确认时用户正在说话，那段语音就是新一轮的开头。
        # 不把 VAD 的当前状态交给 endpointer，新一轮就永远 commit 不了。
        self.endpointer.arm(
            self._turn_id, already_speaking=self.vad.state is VadState.SPEECH
        )
        self.bargein.disarm()

    async def _begin_turn(self, partial: str) -> None:
        transcript = await self.asr.finalize()
        if not transcript:
            transcript = partial
        self._gen_task = self._scope.create_task(
            self._run_generation(transcript), name=f"gen-t{self._turn_id}"
        )

    async def _run_generation(self, transcript: str) -> None:
        self._gen_counter += 1
        handle = GenerationHandle(self.session_id, self._turn_id, self._gen_counter)
        ledger = GenerationLedger(handle)
        self.ledgers[handle.generation_id] = ledger
        gen = ActiveGeneration(handle=handle, ledger=ledger, started_at=self.clock.now())
        self.active = gen
        self.state = SessionState.THINKING

        self.log.emit(
            E.GENERATION_STARTED,
            turn_id=handle.turn_id,
            generation_id=handle.generation_id,
            transcript=transcript,
            fence_before=self.player.active_fence,
        )
        # 用户可能在机器人还没出声时就插话，所以这里就武装，不等 SPEAKING。
        self.bargein.arm(turn_id=handle.turn_id, generation_id=handle.generation_id)

        text_q: BoundedQueue = BoundedQueue("text_q", self.cfg.queues.text_q)
        gen.text_q = text_q
        scope = TaskScope(f"gen{handle.generation_id}")
        self._gen_scope = scope

        wd_llm = Watchdog(
            self.clock, self.log, label="llm",
            first_timeout_ms=self.cfg.llm.first_chunk_timeout_ms,
            chunk_timeout_ms=self.cfg.llm.chunk_timeout_ms,
            on_timeout=lambda *_: self._request_cancel("llm_timeout", GenState.TIMED_OUT),
            turn_id=handle.turn_id, generation_id=handle.generation_id,
        )
        wd_tts = Watchdog(
            self.clock, self.log, label="tts",
            first_timeout_ms=self.cfg.tts.first_chunk_timeout_ms,
            chunk_timeout_ms=self.cfg.tts.chunk_timeout_ms,
            on_timeout=lambda *_: self._request_cancel("tts_timeout", GenState.TIMED_OUT),
            turn_id=handle.turn_id, generation_id=handle.generation_id,
        )
        gen.watchdogs = [wd_llm, wd_tts]

        def on_text(piece: str) -> None:
            ledger.generated_text += piece

        gen.state = GenState.ACTIVE
        try:
            llm_t = scope.create_task(
                self.llm.run(transcript, handle, text_q, wd_llm.feed, on_text),
                name="llm",
            )
            tts_t = scope.create_task(
                self.tts.run(handle, text_q, self.player.write, ledger, wd_tts.feed),
                name="tts",
            )
            scope.create_task(wd_llm.run(), name="wd-llm")
            scope.create_task(wd_tts.run(), name="wd-tts")

            # FIRST_EXCEPTION 不是可选的优化。默认的 ALL_COMPLETED 下，
            # LLM 抛异常之后 TTS 会永远阻塞在 inq.get()（等一个永远不会来的
            # SENTINEL），这个 await 就再也不返回。最后是 TTS 的看门狗把它
            # 兜住的——代价是默认配置下 2 秒静默，而且终态被记成 timed_out
            # 而不是 failed，日志归因直接错了。探针跑出来虚拟时间 101 秒。
            await asyncio.wait([llm_t, tts_t], return_when=asyncio.FIRST_EXCEPTION)
            wd_llm.stop()
            wd_tts.stop()
            if scope.errors:
                raise scope.errors[0]

            await self._await_drained(gen)
            if not gen.state.terminal:
                self._transition(gen, GenState.COMPLETED, reason="natural")
                self._settle(gen, interrupted=False)
                self._next_turn()
        except asyncio.CancelledError:
            # 取消路径的结算由 _commit_barge_in / close 负责（它们手上有
            # 精确的暂停位置）。这里只保证不留下未结算的账本。
            if not gen.state.terminal:
                self._transition(gen, GenState.CANCELLED, reason="cancelled")
                self._settle(gen, interrupted=True)
            raise
        except Exception as exc:  # 后台任务抛异常：记录后上抛，不静默吞掉
            self._transition(gen, GenState.FAILED, reason=type(exc).__name__)
            self._settle(gen, interrupted=True)
            raise
        finally:
            wd_llm.stop()
            wd_tts.stop()
            await scope.aclose(timeout=self.cfg.task_join_timeout_ms / 1000.0)

    async def _await_drained(self, gen: ActiveGeneration) -> None:
        """等播放真的放完。

        用轮询而不是事件，是因为虚拟时钟下轮询是零成本且完全确定的。
        生产实现应该由 Sink 主动发信号——这里的选择是为了少一个同步原语。
        """
        while True:
            if self.player.is_closed or gen.state.terminal:
                return
            pending = [
                r
                for r in gen.ledger.records
                if r.enqueued and not (r.fully_played or r.truncated)
            ]
            if not pending and self.player.queue.empty():
                return
            await self.clock.sleep(FRAME_MS / 1000.0)

    # ------------------------------------------------------------- 取消 / 结算

    def _request_cancel(self, reason: str, target: GenState = GenState.CANCELLED) -> None:
        gen = self.active
        if gen is None:
            return
        if gen.state.terminal:
            # 吸收态天然幂等。多个 cancel 几乎同时到达时，只有第一个生效。
            self.duplicate_cancels += 1
            self.log.emit(
                E.DUPLICATE_CANCEL,
                turn_id=gen.handle.turn_id,
                generation_id=gen.handle.generation_id,
                reason=reason,
                current_state=gen.state.value,
            )
            return
        self.player.set_fence(gen.handle.generation_id + 1)
        self.player.flush()
        self._transition(gen, target, reason=reason)
        self._settle(gen, interrupted=True)
        if self._gen_task is not None and not self._gen_task.done():
            self._gen_task.cancel()
        self.player.release_pause()
        self._next_turn()

    def _transition(self, gen: ActiveGeneration, to: GenState, **payload) -> None:
        gen.state = to
        gen.ledger.terminal_state = to.value
        event = E.GENERATION_COMPLETED if to is GenState.COMPLETED else E.GENERATION_CANCELLED
        self.log.emit(
            event,
            turn_id=gen.handle.turn_id,
            generation_id=gen.handle.generation_id,
            terminal_state=to.value,
            **payload,
        )

    def _settle(self, gen: ActiveGeneration, *, interrupted: bool) -> None:
        """结算账本，并且只把"听到的"放进下一轮上下文。"""
        led = gen.ledger
        led.was_interrupted = interrupted
        summary = led.summary()
        self.context.append(
            {
                "role": "assistant",
                "content": led.context_content(),
                "metadata": {
                    "interrupted": interrupted,
                    "generation_id": gen.handle.generation_id,
                    "unheard_chars": len(led.generated_text) - len(led.context_content()),
                },
            }
        )
        self.log.emit(
            E.TURN_SETTLED,
            turn_id=gen.handle.turn_id,
            generation_id=gen.handle.generation_id,
            was_interrupted=interrupted,
            generated_text=summary["generated_text"],
            enqueued_text=summary["enqueued_text"],
            heard_text=summary["heard_text"],
            heard_span=summary["heard_span"],
            unheard_suffix=summary["unheard_suffix"],
            played_duration_ms=summary["played_duration_ms"],
            context_content=led.context_content(),
        )

    # ------------------------------------------------------------- 回调

    def _on_playback_started(self, generation_id: int) -> None:
        """Sink 报告首帧真的播出了才转 SPEAKING，不是 TTS 入队就转。"""
        self.first_audio_ts.setdefault(generation_id, self.clock.now())
        if self.state is SessionState.THINKING:
            self.state = SessionState.SPEAKING

    # ------------------------------------------------------------- 观测

    def all_queues(self) -> list:
        qs = [self.frame_bus, self.asr_input, self.player.queue]
        if self.active is not None and self.active.text_q is not None:
            qs.append(self.active.text_q)
        return qs

    def trace_to(self, path: str | Path, *, skip_frames: bool = False) -> Path:
        return self.log.to_jsonl(path, skip_frames=skip_frames)
