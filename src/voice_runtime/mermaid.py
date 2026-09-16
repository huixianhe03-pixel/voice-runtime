"""从 trace 自动生成 Mermaid 时序图。

这件事值得做的理由不是"图好看"，而是：图是**日志生成的**，所以图和日志
不可能不一致。"能只看日志就解释清楚这一次执行"这个标准，最强的证明就是
拿日志喂给一个没有额外输入的渲染器，出来的东西能读。
"""

from __future__ import annotations

from .events import E, Event, EventLog

ACTORS = [
    ("USER", "User"),
    ("VAD", "VAD"),
    ("ORC", "Orchestrator"),
    ("TTS", "TTS"),
    ("PLR", "Player"),
    ("SINK", "Sink"),
]

# event_type → (from, to, 箭头, 标签模板)。不在表里的事件不画。
WIRE: dict[str, tuple[str, str, str, str]] = {
    E.SPEECH_START: ("USER", "VAD", "->>", "语音能量上升"),
    E.SPEECH_END: ("USER", "VAD", "-->>", "静音开始"),
    E.ENDPOINT_COMMITTED: ("VAD", "ORC", "->>", "endpoint committed ({silence_ms}ms / 阈值 {effective_threshold_ms}ms)"),
    E.GENERATION_STARTED: ("ORC", "TTS", "->>", "start gen={generation_id} (fence={fence_before})"),
    E.TTS_CHUNK: ("TTS", "PLR", "->>", "chunk #{chunk_index} ({duration_ms}ms)"),
    E.AUDIO_ENQUEUED: ("PLR", "SINK", "->>", "enqueue #{chunk_index} (depth {queue_depth}/{queue_capacity})"),
    E.PLAYBACK_STARTED: ("SINK", "USER", "->>", "开始出声 gen={generation_id}"),
    E.BARGE_IN_CANDIDATE: ("VAD", "ORC", "->>", "barge-in candidate (检测滞后 {detection_lag_ms}ms)"),
    E.PLAYBACK_PAUSED: ("ORC", "SINK", "->>", "pause() 生效，滞后 {effective_lag_ms}ms"),
    E.BARGE_IN_REJECTED: ("VAD", "ORC", "-->>", "rejected：{reason}"),
    E.PLAYBACK_RESUMED: ("ORC", "SINK", "->>", "resume()"),
    E.BARGE_IN_CONFIRMED: ("VAD", "ORC", "->>", "confirmed ({elapsed_ms}ms ≥ {confirm_window_ms}ms)"),
    E.GENERATION_CANCELLED: ("ORC", "TTS", "->>", "cancel gen={generation_id}（flush 前已 fence++）"),
    E.STALE_EVENT_DROPPED: ("TTS", "PLR", "--)", "迟到 chunk gen={generation_id} → DROPPED"),
    E.PROVIDER_TIMEOUT: ("TTS", "ORC", "-->>", "{provider} 超时 {stage}"),
}

NOTES = {
    E.TURN_SETTLED: "ORC",
    E.SESSION_CLOSED: "ORC",
}


def _label(ev: Event, template: str) -> str:
    data = dict(ev.payload)
    data.setdefault("generation_id", ev.generation_id)
    data.setdefault("turn_id", ev.turn_id)
    try:
        return template.format(**data)
    except (KeyError, IndexError):
        return template


def _sanitize(text: str) -> str:
    return text.replace("\n", " ").replace(";", "，").replace("#", "＃")


def render(log: EventLog, *, title: str = "", max_wires: int = 70) -> str:
    out = ["sequenceDiagram", "    autonumber"]
    if title:
        out.insert(0, f"%% {_sanitize(title)}")
    for key, name in ACTORS:
        out.append(f"    participant {key} as {name}")

    wires = 0
    for ev in log.events:
        if ev.event_type in NOTES:
            actor = NOTES[ev.event_type]
            if ev.event_type == E.TURN_SETTLED:
                p = ev.payload
                note = (
                    f"结算 gen={ev.generation_id}：听到 {len(p.get('heard_text') or '')}"
                    f"/{len(p.get('generated_text') or '')} 字符，"
                    f"播出 {p.get('played_duration_ms')}ms，"
                    f"interrupted={p.get('was_interrupted')}"
                )
            else:
                note = f"session closed（残留任务 {ev.payload.get('pending_tasks')}）"
            out.append(f"    Note over {actor}: {_sanitize(note)}")
            continue

        spec = WIRE.get(ev.event_type)
        if spec is None or wires >= max_wires:
            continue
        src, dst, arrow, template = spec
        out.append(
            f"    {src}{arrow}{dst}: {ev.ts:.3f}s · {_sanitize(_label(ev, template))}"
        )
        wires += 1

    return "\n".join(out) + "\n"


def render_html(mermaid_src: str, *, title: str) -> str:
    """包成一个自包含 HTML，双击就能看。仓库里同时留 .mmd 源文件。"""
    return (
        "<!doctype html>\n<html><head><meta charset='utf-8'>"
        f"<title>{title}</title>"
        "<script src='https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js'></script>"
        "</head><body style=\"font-family:system-ui;margin:2rem\">"
        f"<h1 style='font-size:1.1rem'>{title}</h1>"
        f"<pre class='mermaid'>\n{mermaid_src}\n</pre>"
        "<script>mermaid.initialize({startOnLoad:true,maxTextSize:200000});</script>"
        "</body></html>\n"
    )
