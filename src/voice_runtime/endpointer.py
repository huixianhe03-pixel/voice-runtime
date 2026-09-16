"""Endpointing。多信号动态阈值。

三个信号：VAD 静音时长（计时基准）、ASR partial 稳定时长（还在变说明人还在
说）、句子完整度（决定用哪一档阈值）。

完整度是规则表不是模型（题目明说不要求语义模型）。局限很明确：这套规则是
英语特化的，语序自由的语言要重写——日语的句末助词、中文的语气词，判据
完全不同。生产环境应该换成轻量分类器或者直接用 ASR 自带的 endpoint 置信度。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .audio import FRAME_MS
from .clock import Clock
from .events import E, EventLog
from .providers.config import EndpointConfig
from .providers.vad import VadResult, VadState, VadTransition

EVALUATE_LOG_EVERY_FRAMES = 5  # 100ms 记一次，不然 trace 被 evaluated 淹掉

# 落在这张表里的尾词意味着话没说完。
TRAILING_FUNCTION_WORDS = frozenset(
    {
        "a", "an", "and", "as", "at", "because", "but", "by", "for", "from",
        "how", "i", "if", "in", "into", "is", "it", "its", "my", "of", "on",
        "or", "our", "so", "than", "that", "the", "their", "then", "there",
        "this", "to", "was", "we", "were", "what", "when", "which", "while",
        "who", "with", "would", "your",
        # 填充词
        "um", "uh", "er", "erm", "hmm", "like", "well",
    }
)

SENTENCE_FINAL = ".!?"


class Completeness(Enum):
    COMPLETE = "complete"
    AMBIGUOUS = "ambiguous"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class CompletenessVerdict:
    verdict: Completeness
    reason: str


def classify(text: str) -> CompletenessVerdict:
    t = text.strip()
    if not t:
        return CompletenessVerdict(Completeness.INCOMPLETE, "empty_transcript")
    if t[-1] in SENTENCE_FINAL:
        return CompletenessVerdict(Completeness.COMPLETE, "sentence_final_punctuation")
    words = t.split()
    last = words[-1].lower().strip(",;:\"'")
    if last in TRAILING_FUNCTION_WORDS:
        return CompletenessVerdict(
            Completeness.INCOMPLETE, f"trailing_function_word:{last}"
        )
    if len(words) <= 2:
        return CompletenessVerdict(Completeness.AMBIGUOUS, "too_few_words")
    return CompletenessVerdict(Completeness.AMBIGUOUS, "content_word_tail_no_punctuation")


@dataclass
class EndpointDecision:
    committed: bool
    silence_ms: float
    threshold_ms: float
    verdict: CompletenessVerdict
    partial_stable_ms: float
    partial_text: str


class Endpointer:
    """常驻组件，靠 arm / disarm 切换武装状态——你不能"取消听"。"""

    def __init__(
        self,
        cfg: EndpointConfig,
        clock: Clock,
        log: EventLog,
        asr,
    ) -> None:
        self.cfg = cfg
        self.clock = clock
        self.log = log
        self.asr = asr

        self._armed = False
        self.turn_id = 0
        self._saw_speech = False
        self._silence_since: float | None = None
        self._frames_since_log = 0

    def arm(self, turn_id: int, *, already_speaking: bool = False) -> None:
        """already_speaking 不是可选的细节，是 barge-in 之后的正确性前提。

        确认打断时用户**正在说话**，那段语音就是新一轮的开头。如果这里不
        接管 VAD 的当前状态，_saw_speech 会一直是 False，后面的 speech_end
        不触发任何评估，新一轮就永远 commit 不了——机器人从此沉默。
        我第一版漏了这个参数，场景 C 的 turn 2 直接不存在。
        """
        self._armed = True
        self.turn_id = turn_id
        self._saw_speech = already_speaking
        self._silence_since = None
        self._frames_since_log = 0

    def disarm(self) -> None:
        self._armed = False
        self._silence_since = None

    @property
    def armed(self) -> bool:
        return self._armed

    def _threshold_for(self, verdict: Completeness) -> float:
        if verdict is Completeness.COMPLETE:
            return self.cfg.threshold_complete_ms
        if verdict is Completeness.AMBIGUOUS:
            return self.cfg.threshold_ambiguous_ms
        return self.cfg.threshold_incomplete_ms

    def on_vad(self, result: VadResult) -> EndpointDecision | None:
        if not self._armed:
            return None

        if result.transition is VadTransition.SPEECH_START:
            self._saw_speech = True
            self._silence_since = None
        elif result.transition is VadTransition.SPEECH_END:
            self._silence_since = self.clock.now()

        if not self._saw_speech or self._silence_since is None:
            return None
        if result.state is VadState.SPEECH:
            return None

        now = self.clock.now()
        silence_ms = (now - self._silence_since) * 1000.0
        partial = self.asr.partial_text
        stable_ms = self.asr.partial_stable_ms()
        verdict = classify(partial)
        threshold = self._threshold_for(verdict.verdict)

        committed = (
            silence_ms >= threshold and stable_ms >= self.cfg.min_partial_stable_ms
        )

        self._frames_since_log += 1
        should_log = committed or self._frames_since_log >= EVALUATE_LOG_EVERY_FRAMES
        if should_log:
            self._frames_since_log = 0
            # 这一条日志就把场景 B 解释完了：为什么 700ms 静音没有 commit。
            self.log.emit(
                E.ENDPOINT_EVALUATED,
                turn_id=self.turn_id,
                silence_ms=round(silence_ms, 1),
                effective_threshold_ms=threshold,
                partial_text=partial,
                partial_stable_ms=round(stable_ms, 1),
                completeness={
                    "verdict": verdict.verdict.value,
                    "reason": verdict.reason,
                },
                decision="commit" if committed else "wait",
                threshold_source=f"dynamic:{verdict.verdict.value}",
            )

        if not committed:
            return EndpointDecision(
                committed=False,
                silence_ms=silence_ms,
                threshold_ms=threshold,
                verdict=verdict,
                partial_stable_ms=stable_ms,
                partial_text=partial,
            )

        self.log.emit(
            E.ENDPOINT_COMMITTED,
            turn_id=self.turn_id,
            silence_ms=round(silence_ms, 1),
            effective_threshold_ms=threshold,
            completeness=verdict.verdict.value,
            reason=verdict.reason,
            partial_text=partial,
        )
        self.disarm()
        return EndpointDecision(
            committed=True,
            silence_ms=silence_ms,
            threshold_ms=threshold,
            verdict=verdict,
            partial_stable_ms=stable_ms,
            partial_text=partial,
        )

    def speech_end_ts(self) -> float | None:
        return self._silence_since
