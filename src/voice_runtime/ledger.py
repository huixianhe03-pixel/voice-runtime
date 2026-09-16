"""Playback Truth：账本。

四个状态，关键是写入权：

  generated    LLM 出了文本          写入者 LLM 任务      意图
  synthesized  TTS 出了音频          写入者 TTS 任务      意图
  enqueued     过了 fence 进了队列    写入者 Player.write  意图
  played       Sink 实际消费的 sample 写入者 **只有 Sink**  现实

played_samples 只有 Sink 写，其他组件只读。这条纪律靠代码结构保证，
不靠注释：ChunkRecord 只暴露 mark_played()，而调用它的地方只有
player.py 的 sink 循环。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .audio import samples_to_ms


@dataclass(frozen=True)
class GenerationHandle:
    session_id: str
    turn_id: int
    generation_id: int  # session 内单调递增

    def __str__(self) -> str:
        return f"{self.session_id}/t{self.turn_id}/g{self.generation_id}"


@dataclass(frozen=True)
class WordMark:
    """词级时间标记。真实 provider（Azure / ElevenLabs 等）普遍提供，
    Fake TTS 顺手吐出几乎免费。有它就不用靠比例去猜听到了哪几个字。"""

    word: str
    start_sample: int  # 相对本 chunk
    end_sample: int


@dataclass(frozen=True)
class TtsChunk:
    generation_id: int
    chunk_index: int
    text: str
    text_span: tuple[int, int]  # 在完整生成文本中的字符区间
    pcm: bytes
    word_marks: tuple[WordMark, ...] = ()

    @property
    def n_samples(self) -> int:
        return len(self.pcm) // 2

    @property
    def duration_ms(self) -> float:
        return samples_to_ms(self.n_samples)


@dataclass
class ChunkRecord:
    chunk: TtsChunk
    synthesized_at: float
    enqueued: bool = False
    enqueued_at: float | None = None
    _played_samples: int = 0
    truncated: bool = False
    mapping_method: str = "none"

    # --- 只有 Sink 调这两个 ---

    def mark_played(self, n_samples: int) -> None:
        self._played_samples = min(self.chunk.n_samples, self._played_samples + n_samples)

    def mark_truncated(self) -> None:
        self.truncated = True

    # --- 其余组件只读 ---

    @property
    def played_samples(self) -> int:
        return self._played_samples

    @property
    def played_ms(self) -> float:
        return samples_to_ms(self._played_samples)

    @property
    def fully_played(self) -> bool:
        return self._played_samples >= self.chunk.n_samples

    def heard_text(self) -> str:
        """本 chunk 里用户实际听到的文本。

        两条路都选择"向下"、少认。误判代价不对称：少认最多让下一轮重复
        一点内容，是体验瑕疵；多认会让 LLM 引用用户从没听到的信息
        （"您的确认号是 ABC123" 而用户只听到 "您的确认号是"），
        那是正确性错误，而且不会自我修复、会随轮次累积。
        """
        if not self.enqueued or self._played_samples <= 0:
            self.mapping_method = "not_played"
            return ""
        if self.fully_played:
            self.mapping_method = "full_chunk"
            return self.chunk.text

        if self.chunk.word_marks:
            # 主路径：只要 end_sample 完整落在已播范围内的词。
            # 播了一半的尾词不算。
            self.mapping_method = "word_marks"
            words = [w.word for w in self.chunk.word_marks if w.end_sample <= self._played_samples]
            return "".join(words)

        # 降级路径：provider 不给 timing mark 时按比例换算，再向下对齐到词边界。
        # 真实 provider 不一定提供 word-level timing，所以这条路必须保留并测试。
        self.mapping_method = "proportional_snapped"
        frac = self._played_samples / self.chunk.n_samples
        cut = int(len(self.chunk.text) * frac)
        head = self.chunk.text[:cut]
        if cut < len(self.chunk.text) and not self.chunk.text[cut].isspace():
            sp = head.rfind(" ")
            head = head[: sp + 1] if sp >= 0 else ""
        return head

    def to_dict(self) -> dict[str, Any]:
        heard = self.heard_text()
        return {
            "chunk_index": self.chunk.chunk_index,
            "text": self.chunk.text,
            "text_span": list(self.chunk.text_span),
            "n_samples": self.chunk.n_samples,
            "duration_ms": round(self.chunk.duration_ms, 2),
            "enqueued": self.enqueued,
            "played_samples": self._played_samples,
            "played_ms": round(self.played_ms, 2),
            "truncated": self.truncated,
            "heard_text": heard,
            "mapping_method": self.mapping_method,
        }


@dataclass
class GenerationLedger:
    handle: GenerationHandle
    generated_text: str = ""
    records: list[ChunkRecord] = field(default_factory=list)
    was_interrupted: bool = False
    terminal_state: str = "active"

    def add(self, record: ChunkRecord) -> None:
        self.records.append(record)

    # --- 题目 3.3 要求能输出的四个量 ---

    def enqueued_text(self) -> str:
        return "".join(r.chunk.text for r in self.records if r.enqueued)

    def heard_text(self) -> str:
        return "".join(r.heard_text() for r in self.records)

    def heard_span(self) -> tuple[int, int]:
        """听到的文本在 generated_text 里的字符区间。

        chunk 的 text_span 是连续切片，所以终点 = 最后一个有听到内容的
        chunk 的起点 + 该 chunk 内听到的字符数。
        """
        end = 0
        for r in self.records:
            heard = r.heard_text()
            if heard:
                end = r.chunk.text_span[0] + len(heard)
        return (0, end)

    def played_duration_ms(self) -> float:
        return sum(r.played_ms for r in self.records)

    # --- 结算 ---

    def unheard_suffix(self) -> str:
        return self.generated_text[self.heard_span()[1] :]

    def context_content(self) -> str:
        """进下一轮上下文的内容。

        被打断时只放听到的部分。不能无条件把 generated_text 当作已被听到。
        """
        return self.heard_text() if self.was_interrupted else self.generated_text

    def summary(self) -> dict[str, Any]:
        heard = self.heard_text()
        return {
            "generation": str(self.handle),
            "terminal_state": self.terminal_state,
            "was_interrupted": self.was_interrupted,
            "generated_text": self.generated_text,
            "enqueued_text": self.enqueued_text(),
            "heard_text": heard,
            "heard_span": list(self.heard_span()),
            "unheard_suffix": self.unheard_suffix(),
            "generated_chars": len(self.generated_text),
            "heard_chars": len(heard),
            "played_duration_ms": round(self.played_duration_ms(), 2),
            "synthesized_duration_ms": round(
                sum(r.chunk.duration_ms for r in self.records), 2
            ),
            "enqueued_duration_ms": round(
                sum(r.chunk.duration_ms for r in self.records if r.enqueued), 2
            ),
            "chunks": [r.to_dict() for r in self.records],
        }
