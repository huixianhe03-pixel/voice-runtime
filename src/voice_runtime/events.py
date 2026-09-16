"""结构化事件日志（JSONL）。

标准是"只看日志就能解释这一次执行为什么是这个结果"。所以决策类事件
（endpoint_evaluated / barge_in_*）必须记下**决策依据**，不只是结果——
否则看日志的人只能看到"它 commit 了"，没法知道为什么 700ms 静音没 commit。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .clock import Clock


class E:
    """事件类型常量。放成类是为了 IDE 能补全、拼错能被发现。"""

    AUDIO_FRAME = "audio_frame"
    SPEECH_START = "speech_start"
    SPEECH_END = "speech_end"
    ENDPOINT_EVALUATED = "endpoint_evaluated"
    ENDPOINT_COMMITTED = "endpoint_committed"
    ASR_PARTIAL = "asr_partial"
    ASR_FINAL = "asr_final"
    LLM_CHUNK = "llm_chunk"
    LLM_COMPLETED = "llm_completed"
    TTS_CHUNK = "tts_chunk"
    AUDIO_ENQUEUED = "audio_enqueued"
    PLAYBACK_STARTED = "playback_started"
    PLAYBACK_PROGRESS = "playback_progress"
    PLAYBACK_PAUSED = "playback_paused"
    PLAYBACK_RESUMED = "playback_resumed"
    PLAYBACK_STOPPED = "playback_stopped"
    BARGE_IN_CANDIDATE = "barge_in_candidate"
    BARGE_IN_CONFIRMED = "barge_in_confirmed"
    BARGE_IN_REJECTED = "barge_in_rejected"
    GENERATION_STARTED = "generation_started"
    GENERATION_CANCELLED = "generation_cancelled"
    GENERATION_COMPLETED = "generation_completed"
    DUPLICATE_CANCEL = "duplicate_cancel"
    STALE_EVENT_DROPPED = "stale_event_dropped"
    PROVIDER_TIMEOUT = "provider_timeout"
    QUEUE_OVERFLOW = "queue_overflow"
    TURN_SETTLED = "turn_settled"
    SESSION_CLOSED = "session_closed"
    METRICS_SUMMARY = "metrics_summary"


@dataclass(frozen=True)
class Event:
    ts: float
    session_id: str
    turn_id: int | None
    generation_id: int | None
    event_type: str
    sequence_number: int
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": round(self.ts, 6),
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "generation_id": self.generation_id,
            "event_type": self.event_type,
            "sequence_number": self.sequence_number,
            "payload": self.payload,
        }


@dataclass
class EventLog:
    clock: Clock
    session_id: str
    events: list[Event] = field(default_factory=list)
    _seq: int = 0

    def emit(
        self,
        event_type: str,
        *,
        turn_id: int | None = None,
        generation_id: int | None = None,
        **payload: Any,
    ) -> Event:
        ev = Event(
            ts=self.clock.now(),
            session_id=self.session_id,
            turn_id=turn_id,
            generation_id=generation_id,
            event_type=event_type,
            sequence_number=self._seq,
            payload=payload,
        )
        self._seq += 1
        self.events.append(ev)
        return ev

    # --- 查询接口，给测试和指标用 ---

    def of(self, *types: str) -> list[Event]:
        want = set(types)
        return [e for e in self.events if e.event_type in want]

    def first(self, *types: str) -> Event | None:
        got = self.of(*types)
        return got[0] if got else None

    def last(self, *types: str) -> Event | None:
        got = self.of(*types)
        return got[-1] if got else None

    def count(self, *types: str) -> int:
        return len(self.of(*types))

    def ts_of_first(self, *types: str) -> float | None:
        ev = self.first(*types)
        return ev.ts if ev else None

    # --- 落盘 ---

    def to_jsonl(self, path: str | Path, *, skip_frames: bool = False) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as fh:
            for ev in self.events:
                if skip_frames and ev.event_type == E.AUDIO_FRAME:
                    continue
                fh.write(json.dumps(ev.to_dict(), ensure_ascii=False) + "\n")
        return p

    def dump(self, *, skip_frames: bool = True) -> str:
        lines: Iterable[Event] = self.events
        out = []
        for ev in lines:
            if skip_frames and ev.event_type == E.AUDIO_FRAME:
                continue
            out.append(json.dumps(ev.to_dict(), ensure_ascii=False))
        return "\n".join(out)
