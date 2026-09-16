"""流式 ASR（fake）。渐进吐 partial，endpoint commit 时吐 final。

partial 的"稳定时长"是 endpointing 的三个信号之一，所以 ASR 必须暴露
partial_text 和 last_change_ts 给 Endpointer 读。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..audio import FRAME_MS, AudioFrame
from ..clock import Clock
from ..events import E, EventLog
from ..queues import DropOldestQueue
from .config import AsrConfig, Jitter


@dataclass
class _Segment:
    start_seq: int
    text: str
    n_frames: int
    end_seq: int | None = None


class TranscriptOracle:
    """Fake ASR 的作弊通道。

    真实 ASR 从 PCM 解码；这个 fake 需要 ground truth，所以 AudioSource 把
    （帧区间 → 文本）登记在这里。这是整个实现里唯一一处彻底"假"的地方，
    单独放一个类，是为了让读代码的人一眼看到假的边界在哪——
    其余组件（VAD / Endpointer / Player）都在处理真数据。
    """

    def __init__(self) -> None:
        self._segments: list[_Segment] = []

    def register(self, start_seq: int, text: str, n_frames: int) -> None:
        self._segments.append(_Segment(start_seq=start_seq, text=text, n_frames=n_frames))

    def lookup(self, seq: int) -> tuple[_Segment, int] | None:
        """返回 (段, 该段内已过帧数)。不在任何语音段里返回 None。"""
        for seg in self._segments:
            if seg.start_seq <= seq < seg.start_seq + seg.n_frames:
                return seg, seq - seg.start_seq + 1
        return None


class FakeAsr:
    def __init__(
        self,
        cfg: AsrConfig,
        clock: Clock,
        log: EventLog,
        oracle: TranscriptOracle,
        jitter: Jitter,
    ) -> None:
        self.cfg = cfg
        self.clock = clock
        self.log = log
        self.oracle = oracle
        self.jitter = jitter

        self._committed = ""  # 已结束语音段累积的文本
        self._current = ""  # 当前段已揭示的部分
        self._partial = ""
        self._last_change_ts = clock.now()
        self._last_emit_ts = -1e9
        self._segment_start_ts: float | None = None
        self._active_seg: _Segment | None = None
        self.turn_id = 0

    # --- Endpointer 读这三个 ---

    @property
    def partial_text(self) -> str:
        return self._partial

    @property
    def last_change_ts(self) -> float:
        return self._last_change_ts

    def partial_stable_ms(self) -> float:
        return (self.clock.now() - self._last_change_ts) * 1000.0

    # --- 主循环 ---

    async def run(self, inq: DropOldestQueue[AudioFrame]) -> None:
        while True:
            frame = await inq.get()
            self._consume(frame)
            await self._maybe_emit_partial()

    def _consume(self, frame: AudioFrame) -> None:
        hit = self.oracle.lookup(frame.seq)
        if hit is None:
            # 语音段结束：把当前段的文本落到 committed
            if self._active_seg is not None:
                self._committed = (self._committed + " " + self._active_seg.text).strip()
                self._current = ""
                self._active_seg = None
                self._segment_start_ts = None
            return

        seg, frames_in = hit
        if self._active_seg is not seg:
            self._active_seg = seg
            self._segment_start_ts = self.clock.now()
            self._current = ""

        words = seg.text.split()
        n = int(len(words) * frames_in / max(1, seg.n_frames))
        revealed = " ".join(words[:n])
        if revealed != self._current:
            self._current = revealed

    def _compose(self) -> str:
        if self._current:
            return (self._committed + " " + self._current).strip()
        return self._committed

    async def _maybe_emit_partial(self) -> None:
        now = self.clock.now()
        if self._segment_start_ts is not None:
            elapsed_ms = (now - self._segment_start_ts) * 1000.0
            if elapsed_ms < self.cfg.first_partial_delay_ms:
                return
        if (now - self._last_emit_ts) * 1000.0 < self.cfg.partial_interval_ms:
            return

        text = self._compose()
        if text == self._partial:
            self._last_emit_ts = now
            return

        self._partial = text
        self._last_change_ts = now
        self._last_emit_ts = now
        self.log.emit(
            E.ASR_PARTIAL,
            turn_id=self.turn_id,
            text=text,
            n_words=len(text.split()),
        )

    async def finalize(self) -> str:
        """endpoint commit 之后拿 final。会把当前段剩下的词补齐。"""
        await self.clock.sleep(self.jitter.apply(self.cfg.final_delay_ms, self.cfg.jitter_ms))
        if self._active_seg is not None:
            self._committed = (self._committed + " " + self._active_seg.text).strip()
        text = self._committed.strip()
        self.log.emit(E.ASR_FINAL, turn_id=self.turn_id, text=text)
        return text

    def reset_for_next_turn(self, turn_id: int) -> None:
        self._committed = ""
        self._current = ""
        self._partial = ""
        self._active_seg = None
        self._segment_start_ts = None
        self._last_change_ts = self.clock.now()
        self._last_emit_ts = -1e9
        self.turn_id = turn_id
