"""指标。刻意从事件日志里算，不从对象内部状态里读。

理由是这道题的标准是"只看日志就能解释这一次执行"。如果指标走的是另一条
数据通路，那这个标准就没被真正检验——日志可能缺东西而指标照样漂亮。
唯一的例外是队列统计（high-water mark 是队列对象自己的计数器）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .events import E, Event, EventLog


def _ms(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    return round((b - a) * 1000, 2)


@dataclass
class TurnMetrics:
    turn_id: int
    endpoint_latency_ms: float | None = None
    effective_threshold_ms: float | None = None
    completeness: str | None = None
    turn_end_to_first_audio_ms: float | None = None
    generation_id: int | None = None
    terminal_state: str | None = None
    heard_chars: int | None = None
    generated_chars: int | None = None
    played_duration_ms: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class SessionMetrics:
    turns: list[TurnMetrics] = field(default_factory=list)
    interruption_to_playback_stop_ms: list[float] = field(default_factory=list)
    speech_start_detection_ms: list[float] = field(default_factory=list)
    interruption_decision_ms: list[float] = field(default_factory=list)
    overlap_duration_ms: list[float] = field(default_factory=list)
    logical_interruption_ms: list[float] = field(default_factory=list)
    false_interruption_count: int = 0
    stale_chunk_received_count: int = 0
    stale_chunk_played_count: int = 0
    duplicate_cancel_count: int = 0
    provider_timeout_count: int = 0
    queue_overflow_count: int = 0
    max_queue_depth: dict[str, dict[str, int]] = field(default_factory=dict)
    residual_chunks_at_close: int = 0
    pending_tasks_at_close: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["turns"] = [t.as_dict() for t in self.turns]
        return d


def _last_before(events: list[Event], types: tuple[str, ...], ts: float) -> Event | None:
    best = None
    for e in events:
        if e.event_type in types and e.ts <= ts:
            best = e
    return best


def compute(log: EventLog, *, player=None, session=None) -> SessionMetrics:
    ev = log.events
    m = SessionMetrics()

    # --- 每轮：endpoint 延迟、turn-end → first-audio ---
    starts_by_turn = {e.turn_id: e for e in log.of(E.GENERATION_STARTED)}
    first_audio_by_gen = {}
    for e in log.of(E.PLAYBACK_STARTED):
        first_audio_by_gen.setdefault(e.generation_id, e.ts)
    settled_by_gen = {e.generation_id: e for e in log.of(E.TURN_SETTLED)}
    terminal_by_gen = {}
    for e in log.of(E.GENERATION_CANCELLED, E.GENERATION_COMPLETED):
        terminal_by_gen[e.generation_id] = e.payload.get("terminal_state")

    for committed in log.of(E.ENDPOINT_COMMITTED):
        turn = committed.turn_id or 0
        tm = TurnMetrics(turn_id=turn)
        speech_end = _last_before(ev, (E.SPEECH_END,), committed.ts)
        tm.endpoint_latency_ms = _ms(speech_end.ts if speech_end else None, committed.ts)
        tm.effective_threshold_ms = committed.payload.get("effective_threshold_ms")
        tm.completeness = committed.payload.get("completeness")

        start = starts_by_turn.get(turn)
        if start is not None:
            gid = start.generation_id
            tm.generation_id = gid
            tm.terminal_state = terminal_by_gen.get(gid)
            # turn-end 定义成 endpoint committed 的那一刻：用户说完的判定点。
            tm.turn_end_to_first_audio_ms = _ms(committed.ts, first_audio_by_gen.get(gid))
            s = settled_by_gen.get(gid)
            if s is not None:
                tm.heard_chars = len(s.payload.get("heard_text") or "")
                tm.generated_chars = len(s.payload.get("generated_text") or "")
                tm.played_duration_ms = s.payload.get("played_duration_ms")
        m.turns.append(tm)

    # --- barge-in 三段延迟 + 重叠 ---
    for cand in log.of(E.BARGE_IN_CANDIDATE):
        first_loud = cand.payload.get("first_loud_at")
        m.speech_start_detection_ms.append(cand.payload.get("detection_lag_ms"))
        # 只认 stage=="effective" 的那一条：那才是"用户真的听不到了"。
        # stage=="requested" 是命令发出的时刻，用来算 decision 延迟。
        paused = next(
            (
                e
                for e in log.of(E.PLAYBACK_PAUSED)
                if e.ts >= cand.ts and e.payload.get("stage") == "effective"
            ),
            None,
        )
        if paused is not None:
            # 三段分开算，因为生产环境里三段由不同代码拥有、以不同方式劣化，
            # 混成一个数就没法定位。
            #   decision  = 检测到语音 → pause() 被调用（同一 tick，≈0）
            #   stop      = pause() 被调用 → Sink 真的不出声（下界一帧 20ms）
            #   overlap   = 能量上升 → Sink 真的不出声
            requested_at = paused.payload.get("requested_at")
            m.interruption_decision_ms.append(_ms(cand.ts, requested_at))
            m.interruption_to_playback_stop_ms.append(
                paused.payload.get("effective_lag_ms")
            )
            if first_loud is not None:
                m.overlap_duration_ms.append(round((paused.ts - first_loud) * 1000, 2))

    for conf in log.of(E.BARGE_IN_CONFIRMED):
        fl = conf.payload.get("first_loud_at")
        if fl:
            m.logical_interruption_ms.append(round((conf.ts - fl) * 1000, 2))

    m.false_interruption_count = log.count(E.BARGE_IN_REJECTED)
    m.duplicate_cancel_count = log.count(E.DUPLICATE_CANCEL)
    m.provider_timeout_count = log.count(E.PROVIDER_TIMEOUT)
    m.queue_overflow_count = log.count(E.QUEUE_OVERFLOW)
    m.stale_chunk_received_count = log.count(E.STALE_EVENT_DROPPED)

    # stale_chunk_played_count：从日志里算不出来"播了一个本该丢的 chunk"，
    # 因为那是个不该发生的事件。所以这个数取 Player 的内部计数器——
    # 它是个 tripwire：只要不是 0，就说明 fence 检查漏了。
    if player is not None:
        m.stale_chunk_played_count = player.stale_played
        m.residual_chunks_at_close = player.residual_chunks

    if session is not None:
        for q in [session.frame_bus, session.asr_input, session.player.queue]:
            st = q.stats()
            m.max_queue_depth[st["name"]] = {
                "capacity": st["capacity"],
                "max_depth": st["max_depth"],
                "dropped": st["dropped"],
            }
        closed = log.last(E.SESSION_CLOSED)
        if closed is not None:
            m.pending_tasks_at_close = closed.payload.get("pending_tasks") or []

    return m


def render_markdown(name: str, m: SessionMetrics) -> str:
    def fmt(v):
        if v is None:
            return "—"
        if isinstance(v, list):
            return ", ".join(str(x) for x in v) if v else "—"
        return str(v)

    lines = [f"### {name}", ""]
    lines.append("| 指标 | 值 |")
    lines.append("| --- | --- |")
    for t in m.turns:
        lines.append(f"| turn {t.turn_id} · endpoint latency | {fmt(t.endpoint_latency_ms)} ms |")
        lines.append(
            f"| turn {t.turn_id} · 生效阈值 / 完整度 | "
            f"{fmt(t.effective_threshold_ms)} ms / {fmt(t.completeness)} |"
        )
        lines.append(
            f"| turn {t.turn_id} · turn-end → first-audio | {fmt(t.turn_end_to_first_audio_ms)} ms |"
        )
        lines.append(
            f"| turn {t.turn_id} · 听到 / 生成 字符 | {fmt(t.heard_chars)} / {fmt(t.generated_chars)} |"
        )
        lines.append(f"| turn {t.turn_id} · 终态 | {fmt(t.terminal_state)} |")
    lines.append(f"| speech-start detection | {fmt(m.speech_start_detection_ms)} ms |")
    lines.append(f"| interruption → playback-stop | {fmt(m.interruption_to_playback_stop_ms)} ms |")
    lines.append(f"| overlap duration | {fmt(m.overlap_duration_ms)} ms |")
    lines.append(f"| 逻辑打断完成 | {fmt(m.logical_interruption_ms)} ms |")
    lines.append(f"| false interruption count | {m.false_interruption_count} |")
    lines.append(f"| stale chunk received | {m.stale_chunk_received_count} |")
    lines.append(f"| **stale chunk played** | **{m.stale_chunk_played_count}** |")
    lines.append(f"| duplicate cancel | {m.duplicate_cancel_count} |")
    lines.append(f"| provider timeout | {m.provider_timeout_count} |")
    lines.append(f"| queue overflow | {m.queue_overflow_count} |")
    for qname, st in m.max_queue_depth.items():
        lines.append(
            f"| queue `{qname}` max_depth / capacity | {st['max_depth']} / {st['capacity']}"
            f" (dropped {st['dropped']}) |"
        )
    lines.append(f"| 关闭后残留任务 | {fmt(m.pending_tasks_at_close)} |")
    lines.append("")
    return "\n".join(lines)
