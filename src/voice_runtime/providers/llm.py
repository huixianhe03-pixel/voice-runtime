"""流式 LLM（fake）。按 words_per_chunk 吐文本块。

回复是写死的脚本，因为核心测试必须可重复、不依赖真人也不依赖真模型。
长度是按场景需要挑的：turn 1 的回复约 70 字符，在 58ms/字符下约 4.06s，
正好对上场景 C 的"机器人播放约 4s 的回复"。
"""

from __future__ import annotations

from ..clock import Clock
from ..events import E, EventLog
from ..ledger import GenerationHandle
from ..queues import BoundedQueue
from .config import Jitter, LlmConfig

SENTINEL = None

SCRIPTED_REPLIES: dict[str, str] = {
    "Please book it for Wednesday afternoon.": (
        "Sure, I have booked the meeting room for Wednesday afternoon at three."
    ),
    "Stop. Make it Friday instead.": (
        "No problem, I have moved it to Friday afternoon at three."
    ),
    "Please book it for Wednesday afternoon": (
        "Sure, I have booked the meeting room for Wednesday afternoon at three."
    ),
}

DEFAULT_REPLY = "Got it, I have noted that down and will follow up shortly."


def reply_for(transcript: str) -> str:
    key = transcript.strip()
    if key in SCRIPTED_REPLIES:
        return SCRIPTED_REPLIES[key]
    # 容忍标点差异：ASR 的 final 可能少一个句点
    stripped = key.rstrip(".!?")
    for k, v in SCRIPTED_REPLIES.items():
        if k.rstrip(".!?") == stripped:
            return v
    return DEFAULT_REPLY


class FakeLlm:
    def __init__(self, cfg: LlmConfig, clock: Clock, log: EventLog, jitter: Jitter) -> None:
        self.cfg = cfg
        self.clock = clock
        self.log = log
        self.jitter = jitter

    async def run(
        self,
        prompt: str,
        handle: GenerationHandle,
        out: BoundedQueue,
        feed,
        on_text,
    ) -> None:
        """把回复切块写进 out。

        feed()   —— 喂看门狗，证明流还活着
        on_text  —— 把块累加到 ledger.generated_text（generated 状态的写入方）
        """
        text = reply_for(prompt)
        words = text.split(" ")
        chunks: list[str] = []
        for i in range(0, len(words), self.cfg.words_per_chunk):
            group = words[i : i + self.cfg.words_per_chunk]
            piece = " ".join(group)
            # 除最后一块外都补回分隔空格，保证拼起来等于原文
            if i + self.cfg.words_per_chunk < len(words):
                piece += " "
            chunks.append(piece)

        await self.clock.sleep(
            self.jitter.apply(self.cfg.first_chunk_delay_ms, self.cfg.jitter_ms)
        )
        if self.cfg.hang:
            # 模拟长时间无返回：睡到看门狗把我们取消
            await self.clock.sleep(3600.0)
            return
        feed()

        for idx, piece in enumerate(chunks):
            if idx > 0:
                await self.clock.sleep(
                    self.jitter.apply(self.cfg.chunk_interval_ms, self.cfg.jitter_ms)
                )
                feed()
            on_text(piece)
            self.log.emit(
                E.LLM_CHUNK,
                turn_id=handle.turn_id,
                generation_id=handle.generation_id,
                chunk_index=idx,
                text=piece,
            )
            await out.put(piece)

        self.log.emit(
            E.LLM_COMPLETED,
            turn_id=handle.turn_id,
            generation_id=handle.generation_id,
            total_chars=len(text),
            n_chunks=len(chunks),
        )
        await out.put(SENTINEL)
