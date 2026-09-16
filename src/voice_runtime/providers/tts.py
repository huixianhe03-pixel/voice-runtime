"""流式 TTS（fake）。文本块 → 音频 chunk + 词级时间标记。

两个关键行为是这道题的考点：

1. cancel 之后仍吐 N 个迟到 chunk。schedule_late() 刻意把这些投递挂在
   **session scope** 而不是 generation scope 上——因为迟到 chunk 来自
   provider 的传输层，它本来就不在你那一代的任务树里。挂在 generation
   scope 上就会被一起取消，那就测不出场景 E 了。

2. emit_word_marks=False 时不给 timing mark，逼账本走比例降级映射。
   真实 provider 不一定给，所以那条路必须被测到。
"""

from __future__ import annotations

import re

from ..audio import ms_to_samples, pcm_tone
from ..clock import Clock
from ..events import E, EventLog
from ..ledger import ChunkRecord, GenerationHandle, TtsChunk, WordMark
from ..queues import BoundedQueue
from ..taskscope import TaskScope
from .config import Jitter, TtsConfig
from .llm import SENTINEL

_WORD = re.compile(r"\S+\s*")


class FakeTts:
    def __init__(self, cfg: TtsConfig, clock: Clock, log: EventLog, jitter: Jitter) -> None:
        self.cfg = cfg
        self.clock = clock
        self.log = log
        self.jitter = jitter
        self._last_chunk_index = -1
        self._last_span_end = 0

    def _synthesize(
        self, handle: GenerationHandle, index: int, text: str, span_start: int
    ) -> TtsChunk:
        n_samples = ms_to_samples(len(text) * self.cfg.ms_per_char)
        pcm = pcm_tone(n_samples)

        marks: tuple[WordMark, ...] = ()
        if self.cfg.emit_word_marks and n_samples > 0:
            parts = _WORD.findall(text) or [text]
            total_chars = sum(len(p) for p in parts) or 1
            acc = 0
            built = []
            for i, part in enumerate(parts):
                if i == len(parts) - 1:
                    end = n_samples
                else:
                    end = acc + int(n_samples * len(part) / total_chars)
                built.append(WordMark(word=part, start_sample=acc, end_sample=end))
                acc = end
            marks = tuple(built)

        return TtsChunk(
            generation_id=handle.generation_id,
            chunk_index=index,
            text=text,
            text_span=(span_start, span_start + len(text)),
            pcm=pcm,
            word_marks=marks,
        )

    async def run(
        self,
        handle: GenerationHandle,
        inq: BoundedQueue,
        write,
        ledger,
        feed,
    ) -> None:
        """读文本块 → 合成 → 交给 Player.write()。

        write(record) 可能因为 play_q 满而 block，这是有意的背压：
        播放器以实时速率消费，所以 block 本身就是限流。
        """
        index = 0
        span_start = 0
        first = True
        while True:
            piece = await inq.get()
            if piece is SENTINEL:
                break

            delay = (
                self.cfg.first_chunk_delay_ms if first else self.cfg.chunk_interval_ms
            )
            await self.clock.sleep(self.jitter.apply(delay, self.cfg.jitter_ms))
            if self.cfg.hang:
                await self.clock.sleep(3600.0)
                return
            first = False
            feed()

            chunk = self._synthesize(handle, index, piece, span_start)
            span_start = chunk.text_span[1]
            self._last_chunk_index = index
            self._last_span_end = span_start

            record = ChunkRecord(chunk=chunk, synthesized_at=self.clock.now())
            ledger.add(record)
            self.log.emit(
                E.TTS_CHUNK,
                turn_id=handle.turn_id,
                generation_id=handle.generation_id,
                chunk_index=index,
                text=piece,
                text_span=list(chunk.text_span),
                n_samples=chunk.n_samples,
                duration_ms=round(chunk.duration_ms, 2),
                has_word_marks=bool(chunk.word_marks),
            )
            await write(record)
            index += 1

    def schedule_late(
        self,
        scope: TaskScope,
        handle: GenerationHandle,
        write,
    ) -> int:
        """cancel 之后模拟 in-flight chunk 继续到达。

        必须挂在 session scope 上：这些 chunk 来自 provider 传输层，
        不属于被取消的那一代的任务树。挂错了场景 E 就测不出来。
        """
        n = self.cfg.late_chunks_after_cancel
        if n <= 0:
            return 0

        async def _deliver() -> None:
            for i in range(n):
                await self.clock.sleep(
                    self.jitter.apply(self.cfg.late_chunk_delay_ms, self.cfg.jitter_ms)
                )
                idx = self._last_chunk_index + 1 + i
                chunk = self._synthesize(handle, idx, "late ", self._last_span_end)
                record = ChunkRecord(chunk=chunk, synthesized_at=self.clock.now())
                # 不进 ledger：它属于已经作废的那一代，账本已经结算过了
                await write(record)

        scope.create_task(_deliver(), name=f"tts-late-g{handle.generation_id}")
        return n
