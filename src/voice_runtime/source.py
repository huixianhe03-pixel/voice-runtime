"""音频输入源。按 20ms 一帧往 frame_bus 上推。

脚本是"推进去"的而不是一次性给定的，因为场景 C 需要在"播放已经开始 1.2s"
这个条件成立之后才注入用户的打断语音——那个时刻要等事件才知道。

脚本空了就发静音（麦克风一直在开着），只有显式 finish() 之后才停。
不这样的话虚拟时钟会因为"没有 timer 了"提前收工。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .audio import FRAME_MS, AudioFrame, FrameFactory
from .clock import Clock
from .events import E, EventLog
from .providers.asr import TranscriptOracle
from .queues import DropOldestQueue

FRAME_LOG_EVERY = 1  # 每帧都记。trace 里 5 秒约 250 条，可以接受；
# 要瘦身就调大这个数，读的时候用 skip_frames=True 过滤。


@dataclass
class _Seg:
    kind: str  # silence | speech | noise
    n_frames: int
    text: str | None = None


class ScriptedAudioSource:
    def __init__(
        self,
        clock: Clock,
        log: EventLog,
        oracle: TranscriptOracle,
        out: DropOldestQueue[AudioFrame],
    ) -> None:
        self.clock = clock
        self.log = log
        self.oracle = oracle
        self.out = out
        self._script: deque[_Seg] = deque()
        self._current: _Seg | None = None
        self._current_left = 0
        self._seq = 0
        self._finished = False
        self.frames_emitted = 0
        self.frames_dropped = 0

    # --- 脚本 ---

    @staticmethod
    def _n(ms: float) -> int:
        return max(1, int(round(ms / FRAME_MS)))

    def enqueue_silence(self, ms: float) -> "ScriptedAudioSource":
        self._script.append(_Seg("silence", self._n(ms)))
        return self

    def enqueue_speech(self, ms: float, text: str) -> "ScriptedAudioSource":
        self._script.append(_Seg("speech", self._n(ms), text))
        return self

    def enqueue_noise(self, ms: float) -> "ScriptedAudioSource":
        """高能量但没有转写文本。VAD 会看到能量，ASR 什么都不会出。
        场景 D 的 80ms 噪声走这条路。"""
        self._script.append(_Seg("noise", self._n(ms)))
        return self

    def finish(self) -> None:
        self._finished = True

    @property
    def script_empty(self) -> bool:
        return not self._script and self._current is None

    # --- 主循环 ---

    async def run(self) -> None:
        while True:
            if self._current is None or self._current_left <= 0:
                self._current = None
                if self._script:
                    self._current = self._script.popleft()
                    self._current_left = self._current.n_frames
                    if self._current.kind == "speech" and self._current.text:
                        # 登记给 Fake ASR 的作弊通道
                        self.oracle.register(
                            self._seq, self._current.text, self._current.n_frames
                        )
                elif self._finished:
                    return

            kind = self._current.kind if self._current else "silence"
            ts = self.clock.now()
            if kind == "speech":
                frame = FrameFactory.speech(self._seq, ts)
            elif kind == "noise":
                frame = FrameFactory.noise(self._seq, ts)
            else:
                frame = FrameFactory.silence(self._seq, ts)

            dropped = self.out.put_nowait(frame)
            if dropped is not None:
                self.frames_dropped += 1
                self.log.emit(
                    E.QUEUE_OVERFLOW,
                    queue="frame_bus",
                    policy="drop_oldest",
                    dropped_seq=dropped.seq,
                    capacity=self.out.capacity,
                )

            if self._seq % FRAME_LOG_EVERY == 0:
                self.log.emit(
                    E.AUDIO_FRAME,
                    seq=frame.seq,
                    kind=kind,
                    rms=round(frame.rms(), 4),
                )

            self._seq += 1
            self.frames_emitted += 1
            if self._current is not None:
                self._current_left -= 1
            await self.clock.sleep(FRAME_MS / 1000.0)
