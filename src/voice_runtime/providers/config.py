"""Fake Provider 的配置。

题目要求可配置：首包延迟、chunk 间隔、抖动、超时、取消后仍返回 N 个迟到
chunk。全在这里，而且抖动用带 seed 的 PRNG，所以"有抖动"和"可重放"不矛盾。

超时刻意分成 first_chunk_timeout 和 chunk_timeout 两个数：首包迟迟不来
（连接/排队问题）和流中间卡死（provider 内部故障）成因不同，混成一个数
就没法从日志定位。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field


@dataclass
class VadConfig:
    rms_threshold: float = 0.05
    frames_to_speech: int = 2  # 2 帧 = 40ms 才认语音，滤掉单帧毛刺
    frames_to_silence: int = 3  # 60ms hangover，避免词间停顿被当成说完


@dataclass
class AsrConfig:
    first_partial_delay_ms: float = 100.0
    partial_interval_ms: float = 140.0
    final_delay_ms: float = 60.0
    jitter_ms: float = 0.0


@dataclass
class LlmConfig:
    first_chunk_delay_ms: float = 180.0
    chunk_interval_ms: float = 70.0
    words_per_chunk: int = 3
    jitter_ms: float = 0.0
    first_chunk_timeout_ms: float = 3000.0
    chunk_timeout_ms: float = 2000.0
    late_chunks_after_cancel: int = 0
    hang: bool = False  # 模拟"长时间无返回"


@dataclass
class TtsConfig:
    first_chunk_delay_ms: float = 120.0
    chunk_interval_ms: float = 60.0
    ms_per_char: float = 58.0  # 英语约 150wpm ≈ 每字符 58ms
    jitter_ms: float = 0.0
    first_chunk_timeout_ms: float = 3000.0
    chunk_timeout_ms: float = 2000.0
    late_chunks_after_cancel: int = 2  # 场景 E
    late_chunk_delay_ms: float = 60.0
    emit_word_marks: bool = True  # False 走比例降级映射
    hang: bool = False


@dataclass
class EndpointConfig:
    """三档阈值。

    这三个数是拍的。手上没有真实语料可以调，只保证了场景 B 的 700ms 静音
    落在 incomplete 档内并留了 400ms 余量。真实系统必须用线上的抢话率 vs
    平均等待去拟合，而且要按语言、按客户分开调。
    """

    threshold_complete_ms: float = 400.0
    threshold_ambiguous_ms: float = 700.0
    threshold_incomplete_ms: float = 1100.0
    # partial 还在变说明人还在说。这个下界防止"刚出第一个词就判完整"。
    min_partial_stable_ms: float = 120.0


@dataclass
class BargeInConfig:
    """两阶段打断。

    phase 1 在第一个语音帧就 pause，廉价可逆，所以不等确认；
    phase 2 等 confirm_ms 的连续语音才做不可逆的事（cancel / flush / 结算）。
    抗噪保护因此完全不消耗体感停播延迟。
    """

    confirm_ms: float = 150.0
    # 确认窗口内允许的静音帧数。0 = 一帧静音就判为噪声。
    # 留 1 帧是因为真人说话词间会有单帧掉落。
    tolerated_silence_frames: int = 1


@dataclass
class QueueConfig:
    frame_bus: int = 50  # 1s 音频。麦克风不等人，丢最旧
    asr_input: int = 25  # 500ms。丢最旧
    text_q: int = 16  # LLM→TTS，block
    play_q_ms: float = 400.0  # TTS→Sink，block。见 DESIGN.md：这是延迟/精度旋钮


@dataclass
class RuntimeConfig:
    vad: VadConfig = field(default_factory=VadConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    tts: TtsConfig = field(default_factory=TtsConfig)
    endpoint: EndpointConfig = field(default_factory=EndpointConfig)
    bargein: BargeInConfig = field(default_factory=BargeInConfig)
    queues: QueueConfig = field(default_factory=QueueConfig)
    seed: int = 0
    close_drain_ms: float = 0.0  # 关闭时 drain 播放队列的上界。0 = 直接丢弃并计数
    task_join_timeout_ms: float = 500.0

    def rng(self) -> random.Random:
        return random.Random(self.seed)


class Jitter:
    """带 seed 的抖动。同一个 seed 出同一条 trace。"""

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng

    def apply(self, base_ms: float, jitter_ms: float) -> float:
        if jitter_ms <= 0:
            return base_ms / 1000.0
        delta = self._rng.uniform(-jitter_ms, jitter_ms)
        return max(0.0, base_ms + delta) / 1000.0
