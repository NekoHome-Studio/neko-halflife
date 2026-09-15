# -*- coding: utf-8 -*-
"""per-UMO 激活表与「轮次 + 时间」双过期。

关于并发：本模块**故意不加锁**，理由是可验证的，不是偷懒——

1. asyncio 是单线程协作式调度，模块内所有方法都是同步的、内部不含任何 ``await``，
   因此不存在「读到一半被切走」的中间态；
2. 状态按 UMO（``unified_msg_origin``）分区，跨会话本来就不共享；
3. AstrBot 本身已经按 UMO 串行化了同一会话的事件处理
   （``session_lock_manager.acquire_lock(event.unified_msg_origin)``，见
   ``astrbot/core/pipeline/process_stage/method/agent_sub_stages/internal.py``）。

特别注意：**不要去拿平台那把会话锁**。``on_llm_request`` 是在持有该锁的临界区里
被调用的，``asyncio.Lock`` 不可重入，再获取一次会直接死锁。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger("astrbot_plugin_lazy_tools")


@dataclass
class Activation:
    """单个会话里一个工具的激活状态。"""

    name: str
    remaining_turns: int | None = None
    """剩余轮次；``None`` 表示只受时间过期约束。"""

    expires_at: float | None = None
    """绝对过期时刻（单调时钟）；``None`` 表示只受轮次过期约束。"""

    score: float = 0.0
    """激活时的检索分，用于激活表满员时淘汰。"""

    hits: int = 1
    """本轮内被重复命中的次数，命中越多越不容易被淘汰。"""

    activated_at: float = 0.0

    def is_expired(self, now: float) -> bool:
        return self.expires_at is not None and now >= self.expires_at


class ActivationStore:
    """会话级激活表。

    ``ttl_turns`` 语义：N 表示从激活当轮算起共存活 N 轮。
    激活发生在第 N 轮，第 N+1 轮开始时递减为 N-1……减到 0 的那一轮不再注入。
    """

    def __init__(
        self,
        *,
        default_ttl_turns: int = 3,
        default_ttl_seconds: float = 600.0,
        max_per_session: int = 12,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.default_ttl_turns = max(int(default_ttl_turns), 0)
        self.default_ttl_seconds = max(float(default_ttl_seconds), 0.0)
        self.max_per_session = max(int(max_per_session), 1)
        self._clock = clock
        self._sessions: dict[str, dict[str, Activation]] = {}

    # ---- 每轮入口 ------------------------------------------------------

    def begin_turn(self, umo: str) -> dict[str, Activation]:
        """进入新一轮：先按时间过期，再按轮次衰减，返回衰减后的激活表副本。"""
        now = self._clock()
        table = self._sessions.get(umo)
        if not table:
            return {}
        for name in list(table):
            act = table[name]
            if act.is_expired(now):
                del table[name]
                continue
            if act.remaining_turns is not None:
                act.remaining_turns -= 1
                if act.remaining_turns <= 0:
                    del table[name]
        if not table:
            self._sessions.pop(umo, None)
            return {}
        return dict(table)

    # ---- 增删改查 ------------------------------------------------------

    def activate(
        self,
        umo: str,
        name: str,
        *,
        score: float = 0.0,
        ttl_turns: int | None = None,
        ttl_seconds: float | None = None,
    ) -> Activation:
        """激活一个工具（重复激活会刷新 TTL 并累加命中）。"""
        turns = self.default_ttl_turns if ttl_turns is None else max(int(ttl_turns), 0)
        seconds = (
            self.default_ttl_seconds if ttl_seconds is None else max(float(ttl_seconds), 0.0)
        )
        now = self._clock()
        table = self._sessions.setdefault(umo, {})
        existing = table.get(name)
        if existing is not None:
            existing.remaining_turns = turns if turns > 0 else 0
            existing.expires_at = (now + seconds) if seconds > 0 else None
            existing.score = max(existing.score, score)
            existing.hits += 1
            return existing
        act = Activation(
            name=name,
            remaining_turns=turns if turns > 0 else 0,
            expires_at=(now + seconds) if seconds > 0 else None,
            score=score,
            hits=1,
            activated_at=now,
        )
        table[name] = act
        self._enforce_capacity(umo)
        return act

    def deactivate(self, umo: str, name: str) -> bool:
        table = self._sessions.get(umo)
        if not table or name not in table:
            return False
        del table[name]
        if not table:
            self._sessions.pop(umo, None)
        return True

    def clear(self, umo: str) -> int:
        table = self._sessions.pop(umo, None)
        return len(table) if table else 0

    def names(self, umo: str) -> tuple[str, ...]:
        table = self._sessions.get(umo)
        if not table:
            return ()
        now = self._clock()
        return tuple(name for name, act in table.items() if not act.is_expired(now))

    def snapshot(self, umo: str) -> dict[str, Activation]:
        return dict(self._sessions.get(umo, {}))

    def drop_stale(self, tools: frozenset[str]) -> int:
        """把已不存在的工具从所有会话的激活表里清掉。

        插件重载、子插件禁用后调用，避免激活表里留着永远注入不回来的名字。
        """
        removed = 0
        for umo in list(self._sessions):
            table = self._sessions[umo]
            for name in list(table):
                if name not in tools:
                    del table[name]
                    removed += 1
            if not table:
                self._sessions.pop(umo, None)
        return removed

    @property
    def session_count(self) -> int:
        return len(self._sessions)

    def total_activations(self) -> int:
        return sum(len(t) for t in self._sessions.values())

    def describe(self) -> list[dict[str, Any]]:
        """给 WebUI 用的可序列化快照。

        单调时钟只在本模块内部使用，对外一律换算成「还剩多少秒」，
        避免把 ``time.monotonic()`` 的裸值暴露出去造成误读。
        """
        now = self._clock()
        sessions: list[dict[str, Any]] = []
        for umo, table in self._sessions.items():
            tools = []
            for act in table.values():
                if act.is_expired(now):
                    continue
                tools.append(
                    {
                        "name": act.name,
                        "remaining_turns": act.remaining_turns,
                        "expires_in": (
                            None
                            if act.expires_at is None
                            else round(max(0.0, act.expires_at - now), 1)
                        ),
                        "score": round(act.score, 3),
                        "hits": act.hits,
                    }
                )
            if tools:
                tools.sort(key=lambda item: (-item["score"], item["name"]))
                sessions.append({"umo": umo, "count": len(tools), "tools": tools})
        sessions.sort(key=lambda item: item["umo"])
        return sessions

    # ---- 内部 ----------------------------------------------------------

    def _enforce_capacity(self, umo: str) -> None:
        table = self._sessions.get(umo)
        if not table or len(table) <= self.max_per_session:
            return
        # 优先淘汰「分低、命中少、激活早」的，保证检索分最高的工具留下。
        ordered = sorted(
            table.values(),
            key=lambda a: (a.score, a.hits, a.activated_at),
        )
        for victim in ordered[: len(table) - self.max_per_session]:
            table.pop(victim.name, None)
            logger.debug("[lazy-tools] 激活表满员，淘汰 %s（分 %.3f）", victim.name, victim.score)
