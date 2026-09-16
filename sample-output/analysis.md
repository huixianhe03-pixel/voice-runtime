# 简短分析

## 场景 A：没有额外排队

turn-end → first-audio 实测 **360.0 ms**。
provider 固有延迟之和是 ASR final 60.0 + LLM 首包
180.0 + TTS 首包 120.0 = **360.0 ms**。
两个数相等，说明中间一层排队都没有——因为第一个 LLM chunk 一出来就送 TTS，
第一个 TTS chunk 一出来就入队播放，没有任何"等全部生成完"的环节。

把流水线改成分段等待，这个数会立刻涨到整段文本的合成时间（约 4s），
`tests/test_scenario_a.py::test_no_extra_queueing_beyond_provider_latency` 会失败。

## 场景 B：阈值真的在动

同一次会话里生效阈值取过 [400.0, 700.0, 1100.0] 这几个值。
停顿期间 partial 是 "Please book it for"，尾词 `for` 命中虚词表 → incomplete →
阈值 1100ms，所以 700ms 静音没有 commit；后半句说完之后有句末句点 → complete →
阈值降到 400ms，立刻确认。

注意一个细节：停顿期观察到的静音时长上限是 580ms 而不是 700ms。VAD 有 60ms
hangover，评估日志又每 100ms 才记一次。但结论不受影响——580 已经远超
complete 档的 400ms，一个固定 400ms 阈值在这里就会抢话。

## 场景 C：三段延迟分开看

| 段 | 实测 | 预算 |
| --- | --- | --- |
| speech-start detection | 20.0 ms | ≤40 |
| interruption decision | -0.0 ms | ≤20 |
| playback stop 生效 | 20.0 ms | ≤20 |
| **overlap duration** | **40.0 ms** | ≤250 |
| 逻辑打断完成 | 180.0 ms | ≤250 |

体感停播 40.0ms，逻辑打断
180.0ms。两个数差了约一个确认窗口
（150.0ms），这个差值就是两阶段设计的全部价值：
抗噪的 150ms 花在了逻辑取消上，没有花在"用户还能听到机器人"上。

playback stop 那 20ms 是硬下界，等于一帧。不做 sub-frame 切分不可能更快。

### 账本

生成 70 字符，入队 70 字符，
用户实际听到 20 字符，播出 1234.0 ms。

- 听到：`Sure, I have booked `
- 没听到：`the meeting room for Wednesday afternoon at three.`

中间那 50 个字符
是"已入队但用户没听到"的灰色地带——play_q 里排着队就被 flush 了。
这个数随 play_q 容量变化，所以队列容量不只是内存参数，是延迟/精度旋钮
（`tests/test_queues.py::test_play_queue_capacity_is_a_latency_knob` 验证了这个关系）。

进下一轮上下文的只有"听到"那部分。如果把完整生成文本塞进去，模型会认为
自己已经说过 `the meeting room for Wednesday afternoon at three.`，而用户从没听到。

## 场景 D：抗噪不花停播延迟

80ms 噪声进入 candidate（phase 1 已经 pause 了），但 80.0ms
时语音中断，未达 150.0ms 确认窗口 → rejected → resume。
generation 终态是 completed，heard_text == generated_text，回复没有被永久中断。

用户听到的是一段 **80.0 ms** 的停顿再续播，这就是 pause-then-resume 的代价。
上界是确认窗口加一帧。生产上大概会换成 duck（降音到 20%）让恢复无缝，
这里选 pause 是因为题目要 overlap duration 这个指标，duck 之后"重叠"没有清晰定义。

另外这个实现还没做 pause/resume 处的淡入淡出，PCM16 直接切会有一声 click。

## 场景 E：fence 拦住了迟到 chunk

cancel 之后 TTS 又吐了 2 个 chunk，
全部因为 `generation_id(1) < active_fence(2)` 被 `Player.write()` 丢掉，
**stale_chunk_played_count = 0**。

时序上值得看的一点：两个迟到 chunk 落在 3.44s 和 3.50s，而新回复的
playback_started 在 5.52s。它们被丢弃而不是缓存，所以不会在新回复之后冒出来。

`fence++` 必须排在 `cancel()` 之前。反过来的话，中间那个窗口里到达的
in-flight chunk 会合法通过 fence 检查然后被播出去。
