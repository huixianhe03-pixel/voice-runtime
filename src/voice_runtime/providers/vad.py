"""VAD。带 hysteresis 的 RMS 门限。

这个是真的在算：从帧里的 PCM 求 RMS，不读任何标注位。所以它可以被换成
Silero / WebRTC VAD 而上面的代码不用动（换了不加分，但架构上不该被挡住）。

hysteresis 的两个方向不对称，是有意的：
  进入语音要 2 帧（40ms）—— 滤掉单帧毛刺
  退出语音要 3 帧（60ms）—— 词与词之间的微停顿不该被当成说完
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..audio import AudioFrame
from .config import VadConfig


class VadState(Enum):
    SILENCE = "silence"
    SPEECH = "speech"


class VadTransition(Enum):
    NONE = "none"
    SPEECH_START = "speech_start"
    SPEECH_END = "speech_end"


@dataclass
class VadResult:
    state: VadState
    transition: VadTransition
    rms: float
    frame_is_loud: bool
    # 连续语音帧数。BargeInDetector 的确认窗口直接用这个，
    # 所以它必须由 VAD 维护而不是各消费者自己数。
    consecutive_loud: int
    consecutive_quiet: int


class FakeVad:
    def __init__(self, cfg: VadConfig) -> None:
        self.cfg = cfg
        self.state = VadState.SILENCE
        self._loud = 0
        self._quiet = 0

    def push(self, frame: AudioFrame) -> VadResult:
        rms = frame.rms()
        loud = rms >= self.cfg.rms_threshold
        if loud:
            self._loud += 1
            self._quiet = 0
        else:
            self._quiet += 1
            self._loud = 0

        transition = VadTransition.NONE
        if self.state is VadState.SILENCE and self._loud >= self.cfg.frames_to_speech:
            self.state = VadState.SPEECH
            transition = VadTransition.SPEECH_START
        elif self.state is VadState.SPEECH and self._quiet >= self.cfg.frames_to_silence:
            self.state = VadState.SILENCE
            transition = VadTransition.SPEECH_END

        return VadResult(
            state=self.state,
            transition=transition,
            rms=rms,
            frame_is_loud=loud,
            consecutive_loud=self._loud,
            consecutive_quiet=self._quiet,
        )
