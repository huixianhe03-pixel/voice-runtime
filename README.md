# 可打断、可回放的实时语音交互引擎

单会话最小 Voice Runtime：`Audio In → VAD/Endpointing → ASR → LLM → TTS → Playback`。
没有 UI、WebRTC、SIP、真实模型和部署——全部重量放在 runtime 的状态、时序、
并发、取消和可观测性上。

设计和取舍在 [DESIGN.md](DESIGN.md)。这里只讲怎么跑、做到了什么、没做什么。

## 环境

Python 3.10+，零运行时依赖（只用标准库）。测试要 pytest。

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install pytest
```

不需要 pytest-asyncio：每个测试自己 `asyncio.run`。少一个依赖，而且 `run()`
退出时事件循环被销毁，任何泄漏的任务都会在 "Task was destroyed but it is pending"
里暴露出来——这正好是我们想要的。

## 运行

```bash
# 全部测试（58 个，约 0.9 秒）
PYTHONPATH=src pytest

# 跑全部场景，重新生成 sample-output/
PYTHONPATH=src python3 -m voice_runtime.report sample-output
```

0.9 秒跑完 58 个测试，是因为核心测试全部跑在可注入的虚拟时钟上——场景 C 那
4 秒播放不真的等 4 秒。同一份输入永远产生同一条 trace。

## 架构概览

两条通路，同时活着，互不阻塞：

- **Ingress（听）** `AudioSource → frame_bus → VAD → {Endpointer, BargeInDetector}`，
  以及 `frame_bus → ASR`。全部是 session 生命周期，永不因 generation 取消而停。
- **Egress（说）** `LLM → text_q → TTS → Player.write() → play_q → Sink`。
  LLM 和 TTS 是 generation 生命周期，cancel 只撕这一层。

`Player` 是唯一跨格的组件：数据方向上属于 Egress，寿命和 Ingress 一样长。
它持有 fence、持有输出流、是 `played_samples` 的唯一写入方。

分两条路的唯一原因是 barge-in。按一条直线写，VAD 就只在用户轮运行，机器人
说话时没人盯着麦克风，打断在架构层面就检测不到。

```
src/voice_runtime/
  clock.py        可注入时钟。VirtualClock 的推进语义是"先跑干、再跳表"
  taskscope.py    结构化并发。没有裸 create_task，所以孤儿任务结构上不可能出现
  audio.py        PCM16 / 16kHz / 20ms 帧（320 samples / 640 bytes）
  queues.py       两种有界队列：block 生产者 / 丢最旧。没有无界队列
  events.py       JSONL 事件日志
  ledger.py       Playback Truth 账本 + sample→文本映射
  player.py       fence 门禁 + pause/resume/flush + sink 循环
  endpointer.py   多信号动态阈值
  bargein.py      两阶段打断检测
  watchdog.py     provider 超时（跑在注入的 clock 上，不用 wait_for）
  session.py      编排器，生命周期的唯一 owner
  metrics.py      指标（从日志算，不从对象状态读）
  mermaid.py      从 trace 自动生成时序图
  providers/      Fake VAD / ASR / LLM / TTS，全部可配置
  scenarios.py    A～E 的驱动
  report.py       产出 sample-output/
```

## 五个场景的实测结果

| 场景 | 要求 | 实测 |
| --- | --- | --- |
| A 完整一句 | 无明显额外排队 | turn-end → first-audio **360ms**，等于 provider 固有延迟之和（60+180+120）。一层排队都没有 |
| B 犹豫 700ms | 停顿期不开播，且不靠大固定阈值 | 生效阈值在同一次会话里取过 400 和 1100。停顿期 `for` 命中虚词表 → incomplete → 1100ms，700ms 静音不 commit |
| C barge-in | 用户开口到停播 ≤250ms | 重叠 **40ms**，逻辑打断 **180ms**。三段延迟分别 20 / 0 / 20ms |
| D 80ms 噪声 | 不得永久中断 | rejected @80ms，resume，终态 completed，`heard == generated`。pause→resume 的 hiccup 60ms |
| E 迟到 chunk | 不播、不重现、关闭后不写 | stale 收到 2 / **播放 0**。两个迟到 chunk 落在 3.44/3.50s，新回复 5.52s 才开始 |

完整数字见 [sample-output/metrics.md](sample-output/metrics.md)，分析见
[sample-output/analysis.md](sample-output/analysis.md)。

一次 barge-in 的完整 trace：`sample-output/trace_bargein.jsonl`（含每一帧）和
`trace_bargein.readable.jsonl`（去掉帧事件，人读的）。
`bargein_sequence.mmd` / `.html` 是**从那条 trace 自动生成的**时序图——
图和日志不可能不一致，这是"只看日志就能解释这次执行"最直接的证明。

被打断那一代的账本在 `sample-output/ledger_bargein.json`：

```
生成       Sure, I have booked the meeting room for Wednesday afternoon at three.  (70 字符 / 4060ms)
入队       同上                                                                     (70 字符 / 4060ms)
听到       Sure, I have booked                                                      (20 字符 / 1234ms)
没听到     the meeting room for Wednesday afternoon at three.
```

chunk #1 播了 1102ms 里的 480ms，被截在词中间，`heard` 只认 `'booked '`——
播了一半的下一个词不算。进下一轮上下文的只有"听到"那 20 个字符。

## 已完成

- 两条通路 + 2×2 生命周期模型；Player 跨 generation 存活
- 单调 fence（用 `<` 比较），两道门禁（closed / superseded），两个检查点（write / dequeue）
- 账本四态 + 写入权隔离；word-mark 主路径和比例降级路径都实现并测试
- 多信号动态阈值 endpointing（VAD 静音 + partial 稳定时长 + 完整度规则表）
- 两阶段 barge-in（phase 1 pause 不等确认，phase 2 才 cancel/flush）
- 四个有界队列，两种溢出策略；题目问的四种情况都有对应处理和测试
- 结构化并发 + 固定拆除顺序；七种生命周期情况都有测试
- 可注入虚拟时钟（加分项）
- 从日志自动生成 Mermaid 时序图（加分项）
- 58 个测试，覆盖 A～E 全部必须断言

## 未完成

按重要性排：

1. **pause / resume 处没做淡入淡出。** PCM16 直接切会在波形上产生不连续，
   听起来是一声 click。需要各加 5~10ms 线性 fade。知道怎么做，时间没排上。
2. **完整度规则表是英语特化的。** 语序自由的语言要重写整套规则——日语的
   句末助词、中文的语气词，判据完全不同。生产环境该换成轻量分类器，或者
   直接用 ASR 自带的 endpoint 置信度（我的 Fake ASR 没模拟这个信号）。
3. **阈值三档（400/700/1100）是拍的。** 没有真实语料可以调，只保证了场景 B
   的 700ms 落在 incomplete 档内并留了余量。
4. **虚拟时钟看不见事件循环被阻塞。** 真实系统里这是 barge-in 延迟爆预算的
   头号原因（音频重采样、同步日志落盘都能干出这事），而虚拟时钟下时间只在
   所有人 idle 时推进，这类问题在测试里根本不会发生。这是明确接受的盲区。
5. **没有真实时钟的 smoke test。** 上一条的直接后果：现在没有任何用例能证明
   代码不依赖虚拟时钟的特殊行为。加一个就能补上，是最该先做的下一件事。
6. **降级文本映射的精度没量化。** 实现了也测了，但没给出和 word-mark 路径的
   误差分布。
7. **单会话。** 没有多会话资源竞争、跨会话背压、全局并发上限。
8. **没做的加分项：** 真实 WAV/麦克风输入、真实 VAD、离线 ASR、
   jitter/丢帧/乱序模拟（配置项留了 `jitter_ms`，但没写对应用例）、
   属性测试、并发压测、8kHz G.711 电话信道适配。

## 开放问题

代码里做了选择但我不确定是最优的，列在这里而不是假装没有：

1. **`turn_id` 在打断之后 ++ 了。** 实现选择是：每次 endpoint commit 都是新
   一轮，`generation_id` 在 session 内单调、和 turn 独立。理由是用户说的确实
   是新的一句话。反面是：如果用户打断之后说的是同一件事的补充，把它算成新
   一轮会让日志归因变散。这个决定影响日志怎么查，所以写在这里。
2. **barge-in rejected 之后没有重说被截断的半个词。** 实现复杂度不值，但如果
   截在数字中间（"three-hun—"）体验会很差。
3. **连续 rejected 没有动态提高确认门槛。** 连续被噪声打断说明环境确实吵，
   继续用 150ms 会一直误判。倾向做，但会让"确认窗口"变成状态相关的量，
   日志和指标都得跟着改。

## 实际耗时


- 设计（架构、状态机、fence 机制、账本模型）：2 小时
- 实现：1.5 小时
- 测试与调试：1 小时
- 文档：1 小时
- 合计：5.5 小时

## AI 工具使用说明

**用了什么、用在哪**

用 Claude（Cowork 模式）做了架构讨论和代码实现。完整对话记录在
`docs/agent-chat.md`。

**我自己做的设计决策**

- 打断手段选 pause 而不是 duck，理由是题目要 overlap duration 这个指标
- 完整度判定用规则表 + 动态阈值，不用纯 partial 稳定性也不加韵律特征
- 时钟方案：只做虚拟时钟，不做真实时钟双跑

**实现过程中发现并修掉的真实 bug**

这两个不是设计阶段想到的，是跑起来才暴露的：

1. **打断之后新一轮永远起不来。** `Endpointer.arm()` 把 `_saw_speech` 重置为
   False，但确认打断时用户**正在说话**——那段语音就是新一轮的开头。结果
   后续的 `speech_end` 不触发任何评估，机器人从此沉默。场景 C 的 turn 2
   直接不存在。修法是 `arm()` 接管 VAD 的当前状态（`already_speaking` 参数）。
2. **`play_q` 容量那条测试测不到点子上。** 整段回复只有 4 个 chunk，容量 5
   和 100 都装得下，队列从没满过，所以"缓冲越大灰色地带越大"这个论断在
   两个配置下结果一样。得把小缓冲压到 1 个 chunk 才测得出来。
