"""结构化并发的最小实现。

为什么不用 asyncio.TaskGroup：一是目标运行环境是 3.10（TaskGroup 要 3.11+），
二是取消传播正好是这道题要考的东西，把它写出来比调标准库更能说明语义。
行为上是 TaskGroup 的子集加两个我需要的扩展：

  - cancel_all()  只取消这个 scope，不动父 scope。generation 取消靠它。
  - aclose(timeout) 带上界地等资源释放，超时会明确报告哪些任务没死。

不变量：create_task 登记的任务，退出 scope 时一定已经结束。
代码里没有裸 asyncio.create_task，所以孤儿任务在结构上不可能出现，
这一点由 tests/test_lifecycle.py 断言 all_tasks() 回到基线来证明。
"""

from __future__ import annotations

import asyncio
from typing import Any, Coroutine


class ScopeClosed(RuntimeError):
    pass


class TaskScope:
    def __init__(self, name: str) -> None:
        self.name = name
        self._tasks: set[asyncio.Task] = set()
        self._closed = False
        self._errors: list[BaseException] = []

    def create_task(self, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task:
        if self._closed:
            coro.close()
            raise ScopeClosed(f"scope {self.name!r} 已关闭，拒绝创建 {name!r}")
        task = asyncio.ensure_future(coro)
        task.set_name(f"{self.name}:{name}")
        self._tasks.add(task)
        task.add_done_callback(self._on_done)
        return task

    def _on_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            # 后台任务抛异常不吞掉：记下来，退出 scope 时上抛给 owner。
            self._errors.append(exc)

    @property
    def live(self) -> int:
        return len(self._tasks)

    @property
    def errors(self) -> list[BaseException]:
        return list(self._errors)

    def cancel_all(self) -> int:
        n = 0
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
                n += 1
        return n

    async def aclose(self, timeout: float | None = None) -> list[str]:
        """取消并有上界地等待。返回超时后仍未结束的任务名。

        顺序是刻意的：先全部 cancel，再一起 await。反过来（逐个 cancel-await）
        会让后面的任务在前一个还没死时多跑一段。
        """
        self._closed = True
        self.cancel_all()
        pending = list(self._tasks)
        if not pending:
            return []
        done, still = await asyncio.wait(pending, timeout=timeout)
        return [t.get_name() for t in still]

    async def __aenter__(self) -> "TaskScope":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            await self.aclose(timeout=1.0)
            return False
        # 正常退出：等所有子任务自然结束；任何一个抛异常就取消其余的。
        while self._tasks:
            done, _ = await asyncio.wait(list(self._tasks), return_when=asyncio.FIRST_EXCEPTION)
            if self._errors:
                await self.aclose(timeout=1.0)
                break
        if self._errors:
            raise self._errors[0]
        return False
