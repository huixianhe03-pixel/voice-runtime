# 各场景指标汇总

由 `python -m voice_runtime.report` 生成。全部跑在虚拟时钟上，
同一份输入永远产生同一条 trace，所以这些数字是可复现的，不是某次运行的快照。

### A · 完整一句

| 指标 | 值 |
| --- | --- |
| turn 1 · endpoint latency | 400.0 ms |
| turn 1 · 生效阈值 / 完整度 | 400.0 ms / complete |
| turn 1 · turn-end → first-audio | 360.0 ms |
| turn 1 · 听到 / 生成 字符 | 70 / 70 |
| turn 1 · 终态 | completed |
| speech-start detection | — ms |
| interruption → playback-stop | — ms |
| overlap duration | — ms |
| 逻辑打断完成 | — ms |
| false interruption count | 0 |
| stale chunk received | 0 |
| **stale chunk played** | **0** |
| duplicate cancel | 0 |
| provider timeout | 0 |
| queue overflow | 0 |
| queue `frame_bus` max_depth / capacity | 1 / 50 (dropped 0) |
| queue `asr_input` max_depth / capacity | 1 / 25 (dropped 0) |
| queue `play_q` max_depth / capacity | 3 / 20 (dropped 0) |
| 关闭后残留任务 | — |

### B · 犹豫 700ms

| 指标 | 值 |
| --- | --- |
| turn 1 · endpoint latency | 400.0 ms |
| turn 1 · 生效阈值 / 完整度 | 400.0 ms / complete |
| turn 1 · turn-end → first-audio | 360.0 ms |
| turn 1 · 听到 / 生成 字符 | 70 / 70 |
| turn 1 · 终态 | completed |
| speech-start detection | — ms |
| interruption → playback-stop | — ms |
| overlap duration | — ms |
| 逻辑打断完成 | — ms |
| false interruption count | 0 |
| stale chunk received | 0 |
| **stale chunk played** | **0** |
| duplicate cancel | 0 |
| provider timeout | 0 |
| queue overflow | 0 |
| queue `frame_bus` max_depth / capacity | 1 / 50 (dropped 0) |
| queue `asr_input` max_depth / capacity | 1 / 25 (dropped 0) |
| queue `play_q` max_depth / capacity | 3 / 20 (dropped 0) |
| 关闭后残留任务 | — |

### C · barge-in

| 指标 | 值 |
| --- | --- |
| turn 1 · endpoint latency | 400.0 ms |
| turn 1 · 生效阈值 / 完整度 | 400.0 ms / complete |
| turn 1 · turn-end → first-audio | 360.0 ms |
| turn 1 · 听到 / 生成 字符 | 20 / 70 |
| turn 1 · 终态 | cancelled |
| turn 2 · endpoint latency | 420.0 ms |
| turn 2 · 生效阈值 / 完整度 | 400.0 ms / complete |
| turn 2 · turn-end → first-audio | 360.0 ms |
| turn 2 · 听到 / 生成 字符 | 57 / 57 |
| turn 2 · 终态 | completed |
| speech-start detection | 20.0 ms |
| interruption → playback-stop | 20.0 ms |
| overlap duration | 40.0 ms |
| 逻辑打断完成 | 180.0 ms |
| false interruption count | 0 |
| stale chunk received | 2 |
| **stale chunk played** | **0** |
| duplicate cancel | 0 |
| provider timeout | 0 |
| queue overflow | 0 |
| queue `frame_bus` max_depth / capacity | 1 / 50 (dropped 0) |
| queue `asr_input` max_depth / capacity | 1 / 25 (dropped 0) |
| queue `play_q` max_depth / capacity | 3 / 20 (dropped 0) |
| 关闭后残留任务 | — |

### D · 80ms 噪声

| 指标 | 值 |
| --- | --- |
| turn 1 · endpoint latency | 400.0 ms |
| turn 1 · 生效阈值 / 完整度 | 400.0 ms / complete |
| turn 1 · turn-end → first-audio | 360.0 ms |
| turn 1 · 听到 / 生成 字符 | 70 / 70 |
| turn 1 · 终态 | completed |
| speech-start detection | 20.0 ms |
| interruption → playback-stop | 20.0 ms |
| overlap duration | 40.0 ms |
| 逻辑打断完成 | — ms |
| false interruption count | 1 |
| stale chunk received | 0 |
| **stale chunk played** | **0** |
| duplicate cancel | 0 |
| provider timeout | 0 |
| queue overflow | 0 |
| queue `frame_bus` max_depth / capacity | 1 / 50 (dropped 0) |
| queue `asr_input` max_depth / capacity | 1 / 25 (dropped 0) |
| queue `play_q` max_depth / capacity | 3 / 20 (dropped 0) |
| 关闭后残留任务 | — |

### E · 迟到 chunk

| 指标 | 值 |
| --- | --- |
| turn 1 · endpoint latency | 400.0 ms |
| turn 1 · 生效阈值 / 完整度 | 400.0 ms / complete |
| turn 1 · turn-end → first-audio | 360.0 ms |
| turn 1 · 听到 / 生成 字符 | 20 / 70 |
| turn 1 · 终态 | cancelled |
| turn 2 · endpoint latency | 420.0 ms |
| turn 2 · 生效阈值 / 完整度 | 400.0 ms / complete |
| turn 2 · turn-end → first-audio | 360.0 ms |
| turn 2 · 听到 / 生成 字符 | 57 / 57 |
| turn 2 · 终态 | completed |
| speech-start detection | 20.0 ms |
| interruption → playback-stop | 20.0 ms |
| overlap duration | 40.0 ms |
| 逻辑打断完成 | 180.0 ms |
| false interruption count | 0 |
| stale chunk received | 2 |
| **stale chunk played** | **0** |
| duplicate cancel | 0 |
| provider timeout | 0 |
| queue overflow | 0 |
| queue `frame_bus` max_depth / capacity | 1 / 50 (dropped 0) |
| queue `asr_input` max_depth / capacity | 1 / 25 (dropped 0) |
| queue `play_q` max_depth / capacity | 3 / 20 (dropped 0) |
| 关闭后残留任务 | — |
