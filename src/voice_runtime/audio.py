"""音频约定与 PCM 生成。

单声道 / PCM16 / 16kHz / 20ms 一帧 = 320 samples = 640 bytes，50 帧/秒。
每帧带单调递增的 seq 和时间戳。

帧里装的是真的 PCM 采样，不是"这一帧是不是语音"的标注位。原因是想让 VAD
真的去算 RMS，而不是读一个预言机——这样才有可能把它换成 Silero，
也才能把输入导出成 WAV。
"""

from __future__ import annotations

import array
import math
from dataclasses import dataclass
from typing import Iterator

SAMPLE_RATE = 16_000
FRAME_MS = 20
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_MS // 1000  # 320
BYTES_PER_FRAME = SAMPLES_PER_FRAME * 2  # 640
FULL_SCALE = 32767

# 20ms 是 Player 停止粒度的硬下界：不做 sub-frame 切分的话，
# playback_stop 这个指标永远不会小于一帧。看日志时别当成 bug。
MIN_STOP_GRANULARITY_MS = FRAME_MS


def ms_to_samples(ms: float) -> int:
    return int(round(ms * SAMPLE_RATE / 1000))


def samples_to_ms(n: int) -> float:
    return n * 1000.0 / SAMPLE_RATE


def _tone(seq: int, amplitude: float, freq: float = 220.0) -> bytes:
    """确定性的正弦帧。相位由 seq 推出，所以帧之间连续且可重放。"""
    buf = array.array("h")
    base = seq * SAMPLES_PER_FRAME
    k = 2 * math.pi * freq / SAMPLE_RATE
    peak = int(amplitude * FULL_SCALE)
    for i in range(SAMPLES_PER_FRAME):
        buf.append(int(peak * math.sin(k * (base + i))))
    return buf.tobytes()


_SILENCE = b"\x00" * BYTES_PER_FRAME

SPEECH_AMPLITUDE = 0.25
NOISE_AMPLITUDE = 0.55


@dataclass(frozen=True)
class AudioFrame:
    seq: int
    ts: float
    pcm: bytes

    def rms(self) -> float:
        """归一化 RMS，0.0~1.0。VAD 的输入。"""
        samples = array.array("h")
        samples.frombytes(self.pcm)
        if not samples:
            return 0.0
        acc = 0
        for s in samples:
            acc += s * s
        return math.sqrt(acc / len(samples)) / FULL_SCALE

    @property
    def duration_ms(self) -> float:
        return samples_to_ms(len(self.pcm) // 2)


class FrameFactory:
    """按 seq 造帧。silence / speech / noise 三种，都是真 PCM。"""

    @staticmethod
    def silence(seq: int, ts: float) -> AudioFrame:
        return AudioFrame(seq=seq, ts=ts, pcm=_SILENCE)

    @staticmethod
    def speech(seq: int, ts: float) -> AudioFrame:
        return AudioFrame(seq=seq, ts=ts, pcm=_tone(seq, SPEECH_AMPLITUDE))

    @staticmethod
    def noise(seq: int, ts: float) -> AudioFrame:
        return AudioFrame(seq=seq, ts=ts, pcm=_tone(seq, NOISE_AMPLITUDE, freq=1400.0))


def pcm_silence(n_samples: int) -> bytes:
    return b"\x00" * (n_samples * 2)


def pcm_tone(n_samples: int, amplitude: float = 0.3, freq: float = 180.0) -> bytes:
    """给 Fake TTS 造合成音频。内容不重要，长度和可切分性才重要。"""
    buf = array.array("h")
    k = 2 * math.pi * freq / SAMPLE_RATE
    peak = int(amplitude * FULL_SCALE)
    for i in range(n_samples):
        buf.append(int(peak * math.sin(k * i)))
    return buf.tobytes()


def iter_frames(pcm: bytes) -> Iterator[bytes]:
    """把一段 PCM 切成 20ms 帧。最后一帧可能不满，不补零——
    补零会让 played_samples 多算，直接破坏 Playback Truth。"""
    for off in range(0, len(pcm), BYTES_PER_FRAME):
        yield pcm[off : off + BYTES_PER_FRAME]
