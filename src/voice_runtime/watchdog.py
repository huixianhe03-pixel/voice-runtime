"""Provider 看门狗。

不用 asyncio.wait_for，因为它走的是真实事件循环时钟，在虚拟时钟下会立刻
超时或者永不超时。超时必须和其他一切一样跑在注入的 clock 上。

首包超时和 chunk 间隔超时是两个数：首包迟迟不来（连接/排队）和流中间卡死
（provider 内部故障）成因不同，混成一个数就没法从日志定位。
"""

from __future__ import annotations

from typing import Callable

from .clock import Clock
from .events import E, EventLog


class Watchdog:
    def __init__(
        self,
        clock: Clock,
        log: EventLog,
        *,
        label: str,
        first_timeout_ms: float,
        chunk_timeout_ms: float,
        on_timeout: Callable[[str, float], None],
        turn_id: int | None = None,
        generation_id: int | None = None,
    ) -> None:
        self.clock = clock
        self.log = log
        self.label = label
        self.first_timeout_s = first_timeout_ms / 1000.0
        self.chunk_timeout_s = chunk_timeout_ms / 1000.0
        self.on_timeout = on_timeout
        self.turn_id = turn_id
        self.generation_id = generation_id

        self._last_feed = clock.now()
        self._fed_once = False
        self._stopped = False
        self.fired = False

    def feed(self) -> None:
        self._last_feed = self.clock.now()
        self._fed_once = True

    def stop(self) -> None:
        self._stopped = True

    async def run(self) -> None:
        budget = self.first_timeout_s
        while not self._stopped:
            await self.clock.sleep(budget)
            if self._stopped:
                return
            need = self.chunk_timeout_s if self._fed_once else self.first_timeout_s
            idle = self.clock.now() - self._last_feed
            if idle >= need - 1e-9:
                self.fired = True
                stage = "chunk_interval" if self._fed_once else "first_chunk"
                self.log.emit(
                    E.PROVIDER_TIMEOUT,
                    turn_id=self.turn_id,
                    generation_id=self.generation_id,
                    provider=self.label,
                    stage=stage,
                    idle_ms=round(idle * 1000, 1),
                    budget_ms=round(need * 1000, 1),
                )
                self.on_timeout(self.label, idle * 1000)
                return
            budget = max(1e-6, need - idle)
