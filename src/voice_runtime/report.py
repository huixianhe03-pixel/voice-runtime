"""跑全部场景，产出 sample-output/。

    python -m voice_runtime.report [输出目录]
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from . import mermaid, scenarios
from .events import E
from .metrics import render_markdown


async def _run_all(outdir: Path) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)
    results = {}
    md = [
        "# 各场景指标汇总",
        "",
        "由 `python -m voice_runtime.report` 生成。全部跑在虚拟时钟上，",
        "同一份输入永远产生同一条 trace，所以这些数字是可复现的，不是某次运行的快照。",
        "",
    ]

    for key in ("A", "B", "C", "D", "E"):
        r = await scenarios.ALL[key]()
        results[key] = r
        md.append(render_markdown(r.name, r.metrics))

        stem = f"trace_{key.lower()}"
        r.log.to_jsonl(outdir / f"{stem}.jsonl")
        r.log.to_jsonl(outdir / f"{stem}.readable.jsonl", skip_frames=True)

    (outdir / "metrics.md").write_text("\n".join(md), encoding="utf-8")

    # 一次 barge-in 的完整 trace，题目点名要的
    c = results["C"]
    c.log.to_jsonl(outdir / "trace_bargein.jsonl")
    c.log.to_jsonl(outdir / "trace_bargein.readable.jsonl", skip_frames=True)

    src = mermaid.render(c.log, title="场景 C · barge-in（由 trace 自动生成）")
    (outdir / "bargein_sequence.mmd").write_text(src, encoding="utf-8")
    (outdir / "bargein_sequence.html").write_text(
        mermaid.render_html(src, title="场景 C · barge-in 时序（由 trace 自动生成）"),
        encoding="utf-8",
    )

    (outdir / "analysis.md").write_text(_analysis(results), encoding="utf-8")

    ledger = results["C"].session.ledgers[1].summary()
    (outdir / "ledger_bargein.json").write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return results


def _analysis(results: dict) -> str:
    c = results["C"]
    d = results["D"]
    a = results["A"]
    e = results["E"]
    cfg = c.session.cfg

    floor = (
        cfg.asr.final_delay_ms + cfg.llm.first_chunk_delay_ms + cfg.tts.first_chunk_delay_ms
    )
    a_turn = a.metrics.turns[0]
    c_settled = [x for x in c.log.of(E.TURN_SETTLED) if x.generation_id == 1][0].payload
    b_thresholds = sorted(
        {x.payload["effective_threshold_ms"] for x in results["B"].log.of(E.ENDPOINT_EVALUATED)}
    )
    paused = d.log.first(E.PLAYBACK_PAUSED)
    resumed = d.log.first(E.PLAYBACK_RESUMED)
    hiccup = round((resumed.ts - paused.ts) * 1000, 1) if paused and resumed else None

    return f"""# 简短分析

## 场景 A：没有额外排队

turn-end → first-audio 实测 **{a_turn.turn_end_to_first_audio_ms} ms**。
provider 固有延迟之和是 ASR final {cfg.asr.final_delay_ms} + LLM 首包
{cfg.llm.first_chunk_delay_ms} + TTS 首包 {cfg.tts.first_chunk_delay_ms} = **{floor} ms**。
两个数相等，说明中间一层排队都没有——因为第一个 LLM chunk 一出来就送 TTS，
第一个 TTS chunk 一出来就入队播放，没有任何"等全部生成完"的环节。

把流水线改成分段等待，这个数会立刻涨到整段文本的合成时间（约 4s），
`tests/test_scenario_a.py::test_no_extra_queueing_beyond_provider_latency` 会失败。

## 场景 B：阈值真的在动

同一次会话里生效阈值取过 {b_thresholds} 这几个值。
停顿期间 partial 是 "Please book it for"，尾词 `for` 命中虚词表 → incomplete →
阈值 1100ms，所以 700ms 静音没有 commit；后半句说完之后有句末句点 → complete →
阈值降到 400ms，立刻确认。

注意一个细节：停顿期观察到的静音时长上限是 580ms 而不是 700ms。VAD 有 60ms
hangover，评估日志又每 100ms 才记一次。但结论不受影响——580 已经远超
complete 档的 400ms，一个固定 400ms 阈值在这里就会抢话。

## 场景 C：三段延迟分开看

| 段 | 实测 | 预算 |
| --- | --- | --- |
| speech-start detection | {c.metrics.speech_start_detection_ms[0]} ms | ≤40 |
| interruption decision | {c.metrics.interruption_decision_ms[0]} ms | ≤20 |
| playback stop 生效 | {c.metrics.interruption_to_playback_stop_ms[0]} ms | ≤20 |
| **overlap duration** | **{c.metrics.overlap_duration_ms[0]} ms** | ≤250 |
| 逻辑打断完成 | {c.metrics.logical_interruption_ms[0]} ms | ≤250 |

体感停播 {c.metrics.overlap_duration_ms[0]}ms，逻辑打断
{c.metrics.logical_interruption_ms[0]}ms。两个数差了约一个确认窗口
（{cfg.bargein.confirm_ms}ms），这个差值就是两阶段设计的全部价值：
抗噪的 150ms 花在了逻辑取消上，没有花在"用户还能听到机器人"上。

playback stop 那 20ms 是硬下界，等于一帧。不做 sub-frame 切分不可能更快。

### 账本

生成 {len(c_settled['generated_text'])} 字符，入队 {len(c_settled['enqueued_text'])} 字符，
用户实际听到 {len(c_settled['heard_text'])} 字符，播出 {c_settled['played_duration_ms']} ms。

- 听到：`{c_settled['heard_text']}`
- 没听到：`{c_settled['unheard_suffix']}`

中间那 {len(c_settled['enqueued_text']) - len(c_settled['heard_text'])} 个字符
是"已入队但用户没听到"的灰色地带——play_q 里排着队就被 flush 了。
这个数随 play_q 容量变化，所以队列容量不只是内存参数，是延迟/精度旋钮
（`tests/test_queues.py::test_play_queue_capacity_is_a_latency_knob` 验证了这个关系）。

进下一轮上下文的只有"听到"那部分。如果把完整生成文本塞进去，模型会认为
自己已经说过 `{c_settled['unheard_suffix'].strip()}`，而用户从没听到。

## 场景 D：抗噪不花停播延迟

80ms 噪声进入 candidate（phase 1 已经 pause 了），但 {d.log.first(E.BARGE_IN_REJECTED).payload['elapsed_ms']}ms
时语音中断，未达 {cfg.bargein.confirm_ms}ms 确认窗口 → rejected → resume。
generation 终态是 completed，heard_text == generated_text，回复没有被永久中断。

用户听到的是一段 **{hiccup} ms** 的停顿再续播，这就是 pause-then-resume 的代价。
上界是确认窗口加一帧。生产上大概会换成 duck（降音到 20%）让恢复无缝，
这里选 pause 是因为题目要 overlap duration 这个指标，duck 之后"重叠"没有清晰定义。

另外这个实现还没做 pause/resume 处的淡入淡出，PCM16 直接切会有一声 click。

## 场景 E：fence 拦住了迟到 chunk

cancel 之后 TTS 又吐了 {e.metrics.stale_chunk_received_count} 个 chunk，
全部因为 `generation_id(1) < active_fence(2)` 被 `Player.write()` 丢掉，
**stale_chunk_played_count = {e.metrics.stale_chunk_played_count}**。

时序上值得看的一点：两个迟到 chunk 落在 3.44s 和 3.50s，而新回复的
playback_started 在 5.52s。它们被丢弃而不是缓存，所以不会在新回复之后冒出来。

`fence++` 必须排在 `cancel()` 之前。反过来的话，中间那个窗口里到达的
in-flight chunk 会合法通过 fence 检查然后被播出去。
"""


def main() -> int:
    outdir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("sample-output")
    results = asyncio.run(_run_all(outdir))
    print(f"写入 {outdir}/")
    for key, r in results.items():
        m = r.metrics
        print(
            f"  {key}: 事件 {len(r.log.events):4d} | stale 收/播 "
            f"{m.stale_chunk_received_count}/{m.stale_chunk_played_count} | "
            f"误判打断 {m.false_interruption_count} | 残留任务 {m.pending_tasks_at_close}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
