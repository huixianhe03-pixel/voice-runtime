"""测试用不到 pytest-asyncio：每个测试自己 asyncio.run。

少一个依赖，而且 run() 退出时事件循环被销毁，任何泄漏的任务都会在
"Task was destroyed but it is pending" 里暴露出来——这正好是我们想要的。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def run(coro):
    return asyncio.run(coro)


# provider 固有延迟之和：ASR final + LLM 首包 + TTS 首包。
# 场景 A 的"无明显额外排队"就是拿实测值和这个下界比。
def provider_floor_ms(cfg) -> float:
    return (
        cfg.asr.final_delay_ms
        + cfg.llm.first_chunk_delay_ms
        + cfg.tts.first_chunk_delay_ms
    )
