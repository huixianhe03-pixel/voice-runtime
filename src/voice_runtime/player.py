"""Player + Sink。播放真相的唯一写入方，也是 fence 的持有者。

Player 在数据方向上属于 Egress，但寿命和 Ingress 一样长——它不随
generation 死。三个原因：

  1. 它持有 fence。fence 是单调的 high-water mark，跟着 generation 死就归零。
  2. 它持有输出流。真实环境音频设备初始化有几十毫秒，每轮重建纯浪费。
  3. 打断时要由它报告精确的 played_samples。跟着被取消的那一代一起死，
     这个数就丢了，账本没法结算。

fence 检查放在 write() 里，不在 Orchestrator：编排器手上的状态永远可能
是过期的，Player 是进程内最后一跳，是最后有机会拦住声音的地方。

一个真实系统的坑：这里 Sink 就是最后一站，所以 flush 掉 play_q 就等于真的
停了。真实实现下 PortAudio / ALSA 还有一层驱动缓冲，那一层才是真正的
最后一跳，flush 不掉。生产上 fence 检查要跟着往后挪。
"""

from __future__ import annotations

import asyncio
from enum import Enum

from .audio import BYTES_PER_FRAME, FRAME_MS, SAMPLES_PER_FRAME, iter_frames
from .clock import Clock
from .events import E, EventLog
from .ledger import ChunkRecord
from .queues import BoundedQueue

PROGRESS_EVERY_FRAMES = 10  # 200ms 记一次，不然 trace 里全是 progress


class WriteResult(Enum):
    ENQUEUED = "enqueued"
    DROPPED_STALE = "dropped_stale"
    DROPPED_CLOSED = "dropped_closed"


class Player:
    def __init__(
        self,
        clock: Clock,
        log: EventLog,
        *,
        play_q_ms: float,
        close_drain_ms: float = 0.0,
    ) -> None:
        self.clock = clock
        self.log = log
        capacity = max(1, int(play_q_ms / FRAME_MS))
        self.queue: BoundedQueue[ChunkRecord] = BoundedQueue("play_q", capacity)
        self._close_drain_s = close_drain_ms / 1000.0

        self._fence = 0
        self._closed = False
        self._paused = False
        self._pause_requested_at: float | None = None
        self._pause_became_effective = False
        self._wake = asyncio.Event()

        self._played_samples_total = 0
        self._started_generations: set[int] = set()
        # Session 靠这个回调把 THINKING → SPEAKING 的转移挂在"首帧真的播出"上，
        # 而不是"TTS chunk 入队"。在状态机层面钉死 Playback Truth 纪律。
        self.on_playback_started = None
        self.stale_received = 0
        self.stale_played = 0
        self.residual_chunks = 0
        self.residual_samples = 0

    # ------------------------------------------------------------------ fence

    @property
    def active_fence(self) -> int:
        return self._fence

    def set_fence(self, value: int) -> None:
        """只增不减。用 < 比较而不是 != ，所以单调性是正确性的前提。"""
        if value < self._fence:
            raise ValueError(f"fence 不能回退：{self._fence} -> {value}")
        self._fence = value
        self._wake.set()

    def _is_stale(self, record: ChunkRecord) -> bool:
        return record.chunk.generation_id < self._fence

    # ------------------------------------------------------------------ 写入

    async def write(self, record: ChunkRecord) -> WriteResult:
        """两道门禁。顺序不能换：closed 是比 fence 更强的条件。"""
        chunk = record.chunk
        if self._closed:
            self.stale_received += 1
            self.log.emit(
                E.STALE_EVENT_DROPPED,
                generation_id=chunk.generation_id,
                reason="session_closed",
                chunk_index=chunk.chunk_index,
                n_samples=chunk.n_samples,
                stage="write",
            )
            return WriteResult.DROPPED_CLOSED

        if self._is_stale(record):
            self.stale_received += 1
            self.log.emit(
                E.STALE_EVENT_DROPPED,
                generation_id=chunk.generation_id,
                reason="superseded_generation",
                chunk_index=chunk.chunk_index,
                n_samples=chunk.n_samples,
                active_fence=self._fence,
                stage="write",
            )
            return WriteResult.DROPPED_STALE

        # 有界队列，满了 block 生产者。这是有意的背压，不是疏忽。
        await self.queue.put(record)
        record.enqueued = True
        record.enqueued_at = self.clock.now()
        self.log.emit(
            E.AUDIO_ENQUEUED,
            generation_id=chunk.generation_id,
            chunk_index=chunk.chunk_index,
            n_samples=chunk.n_samples,
            queue_depth=self.queue.qsize(),
            queue_capacity=self.queue.capacity,
        )
        return WriteResult.ENQUEUED

    # ------------------------------------------------------------------ 指令

    def pause(self) -> None:
        """barge-in phase 1。廉价且可逆，所以无条件立刻做，不等确认。

        这里就记 stage="requested"，不等 sink 确认。因为"命令发出"和
        "真的不出声了"是两个不同的事实，日志里都得有：

          - 只记 requested：拿不到真实停播延迟（下界是一帧）
          - 只记 effective：pause 和 resume 落在同一帧内时 sink 根本没观察到，
            日志里就会出现一个孤立的 playback_resumed，trace 自相矛盾

        第二种情况我是靠探针发现的，不是想出来的。
        """
        if self._paused:
            return
        self._paused = True
        self._pause_requested_at = self.clock.now()
        self._pause_became_effective = False
        self._wake.set()
        self.log.emit(
            E.PLAYBACK_PAUSED,
            stage="requested",
            played_samples_total=self._played_samples_total,
        )

    def resume(self) -> None:
        """barge-in 被判为噪声时走这条路：从暂停位置继续。"""
        if not self._paused:
            return
        self._paused = False
        became = self._pause_became_effective
        self._pause_requested_at = None
        self._pause_became_effective = False
        self._wake.set()
        self.log.emit(
            E.PLAYBACK_RESUMED,
            played_samples_total=self._played_samples_total,
            # False 表示这次暂停没撑过一帧，用户其实没听出停顿。
            # 记下来，否则读日志的人会把它当成漏了一条 paused。
            pause_became_effective=became,
        )

    def release_pause(self) -> None:
        """barge-in 确认时走这条路：只解除暂停标志，不记 RESUMED。

        必须解除，否则 sink 会一直卡在 pause 上，下一代的音频也放不出来。
        不记 RESUMED 是因为这一代的音频已经被 fence 判死了，"恢复播放"
        会让日志读者误解成真的又出声了。
        """
        if not self._paused:
            return
        self._paused = False
        self._pause_requested_at = None
        self._pause_became_effective = False
        self._wake.set()

    @property
    def is_paused(self) -> bool:
        return self._paused

    def flush(self) -> int:
        """清掉未播队列。返回清掉的 chunk 数。"""
        dropped = self.queue.flush()
        n_samples = sum(r.chunk.n_samples for r in dropped)
        if dropped:
            self.log.emit(
                E.PLAYBACK_STOPPED,
                reason="flush",
                flushed_chunks=len(dropped),
                flushed_samples=n_samples,
            )
        return len(dropped)

    # ------------------------------------------------------------------ sink

    async def run(self) -> None:
        """Sink 循环。**唯一**写 played_samples 的地方。"""
        while True:
            record = await self.queue.get()

            # 出队时再校验一次：入队时有效的 chunk，在队列里排队期间可能过期了。
            # 这是 fence 检查的第二个点，不是冗余——play_q 的容量越大，
            # 两个检查点之间的时间窗越长。
            if self._is_stale(record):
                self.stale_received += 1
                self.log.emit(
                    E.STALE_EVENT_DROPPED,
                    generation_id=record.chunk.generation_id,
                    reason="superseded_generation",
                    chunk_index=record.chunk.chunk_index,
                    active_fence=self._fence,
                    stage="dequeue",
                )
                continue
            if self._closed:
                self.residual_chunks += 1
                self.residual_samples += record.chunk.n_samples
                continue

            await self._play(record)

    async def _play(self, record: ChunkRecord) -> None:
        chunk = record.chunk
        gen = chunk.generation_id

        if gen not in self._started_generations:
            self._started_generations.add(gen)
            self.log.emit(
                E.PLAYBACK_STARTED,
                generation_id=gen,
                chunk_index=chunk.chunk_index,
            )
            if self.on_playback_started is not None:
                self.on_playback_started(gen)

        frames_done = 0
        for frame_bytes in iter_frames(chunk.pcm):
            # 暂停等待。set_fence() 和 resume() 都会 wake，醒来后重新判断。
            while self._paused and not self._closed:
                if self._is_stale(record):
                    break
                if self._pause_requested_at is not None:
                    # sink 真的停了才记 effective。这才是"用户听不到机器人"
                    # 的时刻，指标里的 playback_stop 用的是这一条。
                    now = self.clock.now()
                    self.log.emit(
                        E.PLAYBACK_PAUSED,
                        generation_id=gen,
                        stage="effective",
                        chunk_index=chunk.chunk_index,
                        played_samples_in_chunk=record.played_samples,
                        played_samples_total=self._played_samples_total,
                        requested_at=round(self._pause_requested_at, 6),
                        effective_lag_ms=round((now - self._pause_requested_at) * 1000, 3),
                    )
                    self._pause_requested_at = None
                    self._pause_became_effective = True
                self._wake.clear()
                await self._wake.wait()

            if self._is_stale(record):
                record.mark_truncated()
                self.log.emit(
                    E.PLAYBACK_STOPPED,
                    generation_id=gen,
                    reason="superseded_generation",
                    chunk_index=chunk.chunk_index,
                    played_samples_in_chunk=record.played_samples,
                )
                return
            if self._closed:
                record.mark_truncated()
                return

            await self.clock.sleep(FRAME_MS / 1000.0)
            n = len(frame_bytes) // 2
            record.mark_played(n)
            self._played_samples_total += n
            if gen < self._fence:
                self.stale_played += 1
            frames_done += 1
            if frames_done % PROGRESS_EVERY_FRAMES == 0:
                self.log.emit(
                    E.PLAYBACK_PROGRESS,
                    generation_id=gen,
                    chunk_index=chunk.chunk_index,
                    played_samples_in_chunk=record.played_samples,
                    played_ms_in_chunk=round(record.played_ms, 2),
                )

        if not record.fully_played:
            record.mark_truncated()

    # ------------------------------------------------------------------ 关闭

    async def close(self) -> None:
        """关闭顺序里 Player 是最后一个。_closed 门禁和"先 await 任务"是
        两道独立的保险，两个都要有：只靠前者，任何一个 await 超时都会漏；
        只靠后者，任务泄漏就检测不出来。"""
        if self._closed:
            return
        if self._close_drain_s > 0:
            await self.clock.sleep(self._close_drain_s)
        self._closed = True
        self._wake.set()
        leftover = self.queue.flush()
        self.residual_chunks += len(leftover)
        self.residual_samples += sum(r.chunk.n_samples for r in leftover)

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def played_samples_total(self) -> int:
        return self._played_samples_total

    def stats(self) -> dict:
        return {
            "active_fence": self._fence,
            "closed": self._closed,
            "played_samples_total": self._played_samples_total,
            "stale_chunk_received_count": self.stale_received,
            "stale_chunk_played_count": self.stale_played,
            "residual_chunks_at_close": self.residual_chunks,
            "residual_samples_at_close": self.residual_samples,
            "queue": self.queue.stats(),
        }
