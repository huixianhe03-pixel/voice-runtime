"""两阶段打断检测。

单阶段有个死结：要抗噪就得等确认，等确认就直接吃掉停播延迟预算——150ms
确认窗口意味着用户开口后还要多听 150ms 机器人说话。

拆成两阶段就没这个矛盾：
  phase 1（第一个 speech_start 就做）Player.pause()。廉价、可逆，不等确认。
                                     此刻用户已经听不到机器人，重叠结束。
  phase 2（confirm_ms 之后）         才做不可逆的事：fence++ / cancel / flush。

抗噪保护的延迟成本因此是零。这是引入两阶段的唯一理由；如果不要求 250ms，
单阶段更简单。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .audio import FRAME_MS
from .clock import Clock
from .events import E, EventLog
from .providers.config import BargeInConfig
from .providers.vad import VadResult, VadTransition


class BargeInState(Enum):
    IDLE = "idle"
    CANDIDATE = "candidate"


class BargeInOutcome(Enum):
    NONE = "none"
    CANDIDATE = "candidate"  # → phase 1：pause
    CONFIRMED = "confirmed"  # → phase 2：cancel / flush
    REJECTED = "rejected"  # → resume


@dataclass
class BargeInDecision:
    outcome: BargeInOutcome
    candidate_at: float | None = None
    first_loud_at: float | None = None
    loud_frames: int = 0
    silence_frames: int = 0
    elapsed_ms: float = 0.0


class BargeInDetector:
    def __init__(self, cfg: BargeInConfig, clock: Clock, log: EventLog) -> None:
        self.cfg = cfg
        self.clock = clock
        self.log = log
        self.state = BargeInState.IDLE
        self._armed = False
        self._candidate_at: float | None = None
        self._first_loud_at: float | None = None
        self._loud = 0
        self._silence = 0
        self.false_interruption_count = 0
        self.generation_id: int | None = None
        self.turn_id: int | None = None

    def arm(self, *, turn_id: int, generation_id: int) -> None:
        """只在播放中武装。常驻活着，切状态而不是被创建销毁。"""
        self._armed = True
        self.turn_id = turn_id
        self.generation_id = generation_id
        self._reset()

    def disarm(self) -> None:
        self._armed = False
        self._reset()

    def _reset(self) -> None:
        self.state = BargeInState.IDLE
        self._candidate_at = None
        self._first_loud_at = None
        self._loud = 0
        self._silence = 0

    @property
    def armed(self) -> bool:
        return self._armed

    def on_vad(self, result: VadResult) -> BargeInDecision:
        if not self._armed:
            return BargeInDecision(BargeInOutcome.NONE)

        if self.state is BargeInState.IDLE:
            if result.transition is not VadTransition.SPEECH_START:
                return BargeInDecision(BargeInOutcome.NONE)
            now = self.clock.now()
            self.state = BargeInState.CANDIDATE
            self._candidate_at = now
            # VAD 要连续 frames_to_speech 帧才判语音，所以真正"能量上升"的
            # 时刻在这之前。这个差值就是 speech-start detection 延迟。
            self._first_loud_at = now - (result.consecutive_loud - 1) * FRAME_MS / 1000.0
            self._loud = result.consecutive_loud
            self._silence = 0
            self.log.emit(
                E.BARGE_IN_CANDIDATE,
                turn_id=self.turn_id,
                generation_id=self.generation_id,
                first_loud_at=round(self._first_loud_at, 6),
                detection_lag_ms=round((now - self._first_loud_at) * 1000, 3),
                confirm_window_ms=self.cfg.confirm_ms,
                action="pause_playback",
            )
            return BargeInDecision(
                BargeInOutcome.CANDIDATE,
                candidate_at=now,
                first_loud_at=self._first_loud_at,
                loud_frames=self._loud,
            )

        # CANDIDATE：等确认窗口
        now = self.clock.now()
        assert self._candidate_at is not None
        elapsed_ms = (now - self._candidate_at) * 1000.0

        if result.frame_is_loud:
            self._loud += 1
            self._silence = 0
        else:
            self._silence += 1

        if self._silence > self.cfg.tolerated_silence_frames:
            # 不是真打断。80ms 噪声走这条路。
            self.false_interruption_count += 1
            self.log.emit(
                E.BARGE_IN_REJECTED,
                turn_id=self.turn_id,
                generation_id=self.generation_id,
                loud_frames=self._loud,
                silence_frames=self._silence,
                elapsed_ms=round(elapsed_ms, 1),
                confirm_window_ms=self.cfg.confirm_ms,
                reason="speech_stopped_before_confirmation",
                action="resume_playback",
            )
            self._reset()
            return BargeInDecision(
                BargeInOutcome.REJECTED,
                loud_frames=self._loud,
                silence_frames=self._silence,
                elapsed_ms=elapsed_ms,
            )

        if elapsed_ms >= self.cfg.confirm_ms:
            self.log.emit(
                E.BARGE_IN_CONFIRMED,
                turn_id=self.turn_id,
                generation_id=self.generation_id,
                loud_frames=self._loud,
                elapsed_ms=round(elapsed_ms, 1),
                confirm_window_ms=self.cfg.confirm_ms,
                first_loud_at=round(self._first_loud_at or 0.0, 6),
                action="fence_bump_cancel_flush",
            )
            decision = BargeInDecision(
                BargeInOutcome.CONFIRMED,
                candidate_at=self._candidate_at,
                first_loud_at=self._first_loud_at,
                loud_frames=self._loud,
                elapsed_ms=elapsed_ms,
            )
            self.disarm()
            return decision

        return BargeInDecision(
            BargeInOutcome.NONE,
            candidate_at=self._candidate_at,
            first_loud_at=self._first_loud_at,
            loud_frames=self._loud,
            elapsed_ms=elapsed_ms,
        )
