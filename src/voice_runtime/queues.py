"""有界队列。没有无界队列，一个都没有。

两种溢出策略，分别用在两种地方：

  BoundedQueue（满了 block 生产者）—— 用于 text_q 和 play_q。
    音频不能有洞，丢一段就是可听见的断裂，没法插值补救；而播放器以实时
    速率消费，所以 block 本身就是精确的限流，不需要再写 rate limiter。

  DropOldestQueue（满了丢最旧）—— 用于 frame_bus 和 asr_input。
    麦克风不等人，入口绝不能 block。陈旧的 partial 没有价值，
    丢失优于延迟。

两个类都记 high-water mark，因为"最大队列长度"是要输出的指标；
也都暴露 capacity，好让测试能枚举所有队列断言有上限。
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class QueueBase(Generic[T]):
    kind = "base"

    def __init__(self, name: str, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError(f"队列 {name} 必须有正的容量上限")
        self.name = name
        self.capacity = capacity
        self.max_depth = 0
        self.dropped = 0
        self.total_put = 0
        self.total_get = 0

    def _touch(self, depth: int) -> None:
        if depth > self.max_depth:
            self.max_depth = depth

    def stats(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "capacity": self.capacity,
            "max_depth": self.max_depth,
            "dropped": self.dropped,
            "put": self.total_put,
            "get": self.total_get,
        }


class BoundedQueue(QueueBase[T]):
    """满了就 block 生产者。背压。"""

    kind = "blocking"

    def __init__(self, name: str, capacity: int) -> None:
        super().__init__(name, capacity)
        self._q: asyncio.Queue[T] = asyncio.Queue(maxsize=capacity)
        self.blocked_count = 0

    async def put(self, item: T) -> None:
        if self._q.full():
            self.blocked_count += 1
        await self._q.put(item)
        self.total_put += 1
        self._touch(self._q.qsize())

    async def get(self) -> T:
        item = await self._q.get()
        self.total_get += 1
        return item

    def get_nowait(self) -> T:
        item = self._q.get_nowait()
        self.total_get += 1
        return item

    def qsize(self) -> int:
        return self._q.qsize()

    def empty(self) -> bool:
        return self._q.empty()

    def flush(self) -> list[T]:
        """清空并返回被清掉的东西。打断时要用，而且要能报出清了多少。"""
        out: list[T] = []
        while True:
            try:
                out.append(self._q.get_nowait())
            except asyncio.QueueEmpty:
                break
        return out

    def stats(self) -> dict[str, Any]:
        d = super().stats()
        d["blocked_count"] = self.blocked_count
        return d


class DropOldestQueue(QueueBase[T]):
    """满了丢最旧。put 永不 block。"""

    kind = "drop_oldest"

    def __init__(self, name: str, capacity: int) -> None:
        super().__init__(name, capacity)
        self._dq: deque[T] = deque()
        self._waiters: deque[asyncio.Future] = deque()

    def put_nowait(self, item: T) -> T | None:
        """返回被丢掉的那一项（没丢就是 None），好让调用方记日志。"""
        dropped: T | None = None
        if len(self._dq) >= self.capacity:
            dropped = self._dq.popleft()
            self.dropped += 1
        self._dq.append(item)
        self.total_put += 1
        self._touch(len(self._dq))
        while self._waiters:
            fut = self._waiters.popleft()
            if not fut.done():
                fut.set_result(None)
                break
        return dropped

    async def get(self) -> T:
        while not self._dq:
            fut = asyncio.get_running_loop().create_future()
            self._waiters.append(fut)
            await fut
        self.total_get += 1
        return self._dq.popleft()

    def qsize(self) -> int:
        return len(self._dq)

    def empty(self) -> bool:
        return not self._dq

    def flush(self) -> list[T]:
        out = list(self._dq)
        self._dq.clear()
        return out
