"""可注入时钟。

核心测试全部跑 VirtualClock。推进语义是"先跑干、再跳表"：
先让所有可运行的任务跑到没得跑（事件循环 ready 队列空），再把时间跳到
最近一个 timer 的到期点。时间只在所有人都在等的时候前进。

这样得到三件事：
  - 测试瞬间跑完，场景 C 那 4 秒播放不真的等 4 秒
  - 同一份输入永远产生同一条 trace，日志可以拿来对比
  - "≤250ms" 这种断言不会因为 CI 机器负载而 flaky

代价写在 DESIGN.md「时钟」一节：看不见事件循环被同步任务阻塞导致的超标。
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now(self) -> float: ...
    async def sleep(self, seconds: float) -> None: ...


class RealClock:
    """给 smoke test 用。生产环境也是这个。"""

    def now(self) -> float:
        return asyncio.get_running_loop().time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class VirtualClock:
    def __init__(self, start: float = 0.0) -> None:
        self._now = start
        self._heap: list[tuple[float, int, asyncio.Future]] = []
        self._seq = itertools.count()

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._heap, (self._now + seconds, next(self._seq), fut))
        try:
            await fut
        except asyncio.CancelledError:
            # 被取消的 sleeper 留在堆里没关系，唤醒时会跳过已取消的 future。
            raise

    @property
    def pending_timers(self) -> int:
        return sum(1 for _, _, f in self._heap if not f.done())

    async def _drain_ready(self, guard: int = 10_000) -> None:
        """把当前可运行的任务全部跑完。

        asyncio.sleep(0) 让出控制权后，其他就绪回调会跑一轮；我们恢复执行时
        自己已经被移出 ready 队列，所以此刻 loop._ready 里剩下的就是"还有人
        想跑"。用私有属性是有意的：这是唯一能精确表达"没人可跑了"的信号，
        用固定次数的 sleep(0) 去猜会在管线变长时漏掉任务。
        """
        loop = asyncio.get_running_loop()
        for _ in range(guard):
            await asyncio.sleep(0)
            if not loop._ready:
                return
        raise RuntimeError("ready 队列一直不空，可能有 busy-loop")

    async def advance_to_next(self) -> bool:
        """跳到下一个到期点。没有待到期 timer 时返回 False。"""
        await self._drain_ready()
        while self._heap and self._heap[0][2].done():
            heapq.heappop(self._heap)
        if not self._heap:
            return False
        self._now = self._heap[0][0]
        while self._heap and self._heap[0][0] <= self._now:
            _, _, fut = heapq.heappop(self._heap)
            if not fut.done():
                fut.set_result(None)
        return True

    async def run_until_idle(self, max_steps: int = 200_000) -> None:
        """一直推进到没有任何待到期 timer。

        注意：返回时可能还有任务阻塞在队列上（比如 Player 在等更多音频），
        那是正确的——"没有 timer"就意味着不会再有事情自己发生了。
        """
        for _ in range(max_steps):
            if not await self.advance_to_next():
                return
        raise RuntimeError("超过 max_steps，虚拟时钟里大概有死循环")

    async def run_for(self, seconds: float) -> None:
        """推进至多 seconds 的虚拟时间。用于"跑到某个时刻再断言"。"""
        deadline = self._now + seconds
        while True:
            await self._drain_ready()
            while self._heap and self._heap[0][2].done():
                heapq.heappop(self._heap)
            if not self._heap or self._heap[0][0] > deadline:
                self._now = deadline
                await self._drain_ready()
                return
            await self.advance_to_next()
