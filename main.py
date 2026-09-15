# -*- coding: utf-8 -*-
"""astrbot_plugin_lazy_tools —— 工具说明书按需注入
=================================================

常规情况下，AstrBot 会把**所有**已注册工具的完整 JSON Schema 一次性放进每轮 LLM
请求里；工具越多、描述越长、会话越长，消耗的提示词 token 就越多。本插件把这件事
拆成两半：

* **全量注册**：所有工具照常通过 ``@filter.llm_tool`` 注册进 AstrBot 的全局
  ``llm_tools``。这一步不能省——只有真的注册了，persona 的工具白名单、
  会话级插件过滤、工具启用开关这些上游权限才会对它生效。
* **按需注入**：在 ``on_llm_request`` 钩子里先取一次「上游许可池」快照，
  再用本地关键词/标签检索决定本轮激活哪些工具，最后把许可池里**未被激活的
  本插件工具裁掉**。未被激活的工具对模型不可见，自然不占提示词预算。

省的是提示词预算、上下文窗口与 prefill 成本，不省工具执行算力；机器人侧会增加
本地检索与状态管理的开销。工具数量越多、描述越长，收益越明显。

与内置 ``provider_settings.tool_schema_mode = skills_like`` 的分工：内置模式首轮
只下发**工具名 + 描述**、选中后再下发**仅参数**的 schema；本插件省的是**名称 + 描述**
本身（整个工具从请求里消失）。两者可叠加，互不冲突。

适用边界：仅在 ``agent_runner_type=local``（内置 Agent Runner）下生效。第三方
runner（dify / coze / dashscope / deerflow）的工具清单来自远端平台，``on_llm_request``
触发时 ``req.func_tool`` 还是空的；cron 与部分 webchat 路径也不经过该钩子。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

try:
    from .core import injector
    from .core.activation import ActivationStore
    from .core.models import RISK_HIGH, ToolMeta, TurnContext
    from .core.registry import REGISTRY, RESULT_LIMITS, lazy_tool
    from .core.retriever import ToolIndex
    from .core.subplugins import SubPluginLoader
except ImportError:  # AstrBot 也支持把 main.py 当普通模块载入
    from core import injector
    from core.activation import ActivationStore
    from core.models import RISK_HIGH, ToolMeta, TurnContext
    from core.registry import REGISTRY, RESULT_LIMITS, lazy_tool
    from core.retriever import ToolIndex
    from core.subplugins import SubPluginLoader

__all__ = ["LazyToolsPlugin", "lazy_tool"]

#: 会话快照的保留时长与数量上限，避免长时间运行后字典无限增长。
_TURN_TTL_SECONDS = 3600.0
_TURN_MAX_ENTRIES = 512


class LazyToolsPlugin(Star):
    """懒加载工具注入插件。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.index = ToolIndex()
        self.activation = ActivationStore()
        self.loader = SubPluginLoader(Path(__file__).resolve().parent, REGISTRY)
        self._turns: dict[str, TurnContext] = {}
        self._apply_config(config)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """AstrBot 会在插件实例化后调用。这里加载子插件并建索引。

        索引必须在子插件导入之后建，否则子插件的工具不会进入检索候选。
        """
        self._apply_config(self.config)
        self._load_sub_plugins()
        self._rebuild_index()
        self.activation.drop_stale(frozenset(REGISTRY.names()))
        logger.info(
            "[lazy-tools] 就绪：懒加载工具 %d 个（其中常驻元工具 %d 个），"
            "子插件 %d 个，索引 %d 条；预检索=%s，阈值=%.2f，top_k=%d",
            len(REGISTRY.candidates()),
            len(REGISTRY.always_active()),
            len(self.loader.loaded),
            self.index.size,
            "开" if self._prefetch_enabled else "关",
            self._min_score,
            self._top_k,
        )

    async def terminate(self) -> None:
        """卸载清理。注册表是模块级单例，这里只清会话状态，不动工具定义。"""
        self._turns.clear()
        logger.info("[lazy-tools] 已卸载，会话状态已清理")

    # ------------------------------------------------------------------
    # 核心：每轮请求前的注入决策
    # ------------------------------------------------------------------

    @filter.on_llm_request()
    async def on_llm_request(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """按需把工具的 Schema 交回给模型。

        本方法体内部没有 ``await``：检索、过期、裁剪全是本地同步计算，
        因此不会在临界区里被切走，也就不需要额外加锁。
        """
        try:
            self._decide(event, req)
        except Exception as exc:  # noqa: BLE001 - 注入失败不能让整轮对话崩掉
            logger.error(
                "[lazy-tools] 本轮注入失败，保持原工具集不变：%s", exc, exc_info=True
            )

    def _decide(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        # 总开关判断放在决策入口而不是钩子外层：这样任何调用路径都绕不过它。
        if not self._enabled:
            return

        # 1) 上游许可池快照。这是本轮所有决策的唯一合法来源。
        allowed = injector.snapshot_allowed_names(req)
        if not allowed:
            return

        umo: str = event.unified_msg_origin
        names = REGISTRY.names()
        lazy_allowed = frozenset(name for name in names if name in allowed)
        if not lazy_allowed:
            # 本插件的工具全部被上游排除（例如管理员在本会话禁用了插件）：
            # 什么都不做，也不暴露元工具。
            self._turns.pop(umo, None)
            return

        # 2) 轮次衰减 + 时间过期。
        self.activation.begin_turn(umo)

        # 3) 本地预检索 -> 自动激活。不调用 LLM。
        hits: list[tuple[ToolMeta, float]] = []
        if self._prefetch_enabled and self.index.size:
            query = (getattr(req, "prompt", None) or event.message_str or "").strip()
            if query:
                for meta, score in self.index.search(
                    query, top_k=self._top_k, min_score=self._min_score
                ):
                    if meta.name not in lazy_allowed:
                        continue
                    if meta.risk == RISK_HIGH and not self._allow_high_risk:
                        continue
                    self.activation.activate(
                        umo,
                        meta.name,
                        score=score,
                        ttl_turns=self._ttl_turns(meta),
                        ttl_seconds=self._ttl_seconds(meta),
                    )
                    hits.append((meta, score))

        # 4) 先算后写：与许可池取交集后重建工具表，一次赋值完成。
        keep = injector.build_keep_set(
            REGISTRY,
            active_names=frozenset(self.activation.names(umo)),
            lazy_allowed=lazy_allowed,
            meta_tools_enabled=self._meta_enabled,
        )
        kept, pruned = injector.plan_pruning(req, REGISTRY, keep)
        removed = injector.apply_pruning(req, kept, pruned)

        # 5) 暂存本轮快照，供元工具在同一轮的后续 LLM 调用里补注入。
        self._turns[umo] = TurnContext(
            umo=umo,
            request=req,
            allowed=allowed,
            lazy_allowed=lazy_allowed,
            pruned=pruned,
            created_at=time.monotonic(),
        )
        self._gc_turns()

        if self._debug:
            active = sorted(self.activation.names(umo))
            logger.info(
                "[lazy-tools] %s 许可 %d 项 | 预检索命中 %s | 本轮激活 %s | 裁掉 %d 项",
                umo,
                len(allowed),
                [f"{m.name}:{s:.2f}" for m, s in hits] or "无",
                active or "无",
                removed,
            )

    # ------------------------------------------------------------------
    # 元工具：预检索没命中时的兜底
    # ------------------------------------------------------------------

    @lazy_tool(
        name="lazy_search_tools",
        always_active=True,
        group="meta",
        tags=("元工具", "搜索工具", "有哪些工具", "能用什么工具", "meta", "search tool"),
    )
    async def lazy_search_tools(self, event: AstrMessageEvent, query: str) -> str:
        """搜索当前会话可用的工具。当你不确定有哪些工具可用，或现有工具不足以完成任务时，先用它搜索一遍。

        Args:
            query(string): 搜索关键词，中英文均可，例如「天气」「下载视频」
        """
        umo: str = event.unified_msg_origin
        pool = self._lazy_pool(umo)
        if not pool:
            return "当前会话没有可用的懒加载工具。"

        candidates = [
            meta
            for meta, _score in self.index.search(
                query, top_k=self._search_top_k, min_score=0.0
            )
            if meta.name in pool
        ]
        if not candidates:
            # 关键词一个都没命中时，退化为按名字/标签做子串匹配，尽量别让模型空手而归。
            needle = (query or "").strip().lower()
            if needle:
                candidates = [
                    meta
                    for meta in REGISTRY.candidates()
                    if meta.name in pool
                    and (
                        needle in meta.name.lower()
                        or any(needle in tag.lower() for tag in meta.tags)
                    )
                ]
        if not candidates:
            return (
                f"没有找到与「{query}」相关的工具。"
                "请换一组更贴近功能的关键词再搜一次，不要臆造工具名。"
            )

        lines = [f"找到 {len(candidates)} 个候选工具（用 lazy_activate_tool 激活后即可调用）："]
        for meta in candidates[: self._search_top_k]:
            desc = (meta.description or "").splitlines()[0][:80]
            lines.append(f"- {meta.name}：{desc}")
        lines.append("如果都不是你要的，换关键词再搜；确认不需要时可用 lazy_deactivate_tool 释放。")
        return "\n".join(lines)

    @lazy_tool(
        name="lazy_activate_tool",
        always_active=True,
        group="meta",
        tags=("元工具", "激活工具", "启用工具", "meta", "activate tool"),
    )
    async def lazy_activate_tool(self, event: AstrMessageEvent, tool_name: str) -> str:
        """激活一个工具，使其在本轮及后续若干轮对话中可用。工具名必须来自 lazy_search_tools 的返回结果。

        Args:
            tool_name(string): 要激活的工具名，例如「get_weather」
        """
        umo: str = event.unified_msg_origin
        meta = REGISTRY.get(tool_name)
        if meta is None:
            return f"没有名为 {tool_name} 的工具。请先用 lazy_search_tools 搜索，不要臆造工具名。"

        turn = self._turns.get(umo)
        if turn is not None and tool_name not in turn.lazy_allowed:
            return (
                f"工具 {tool_name} 在当前会话被管理员的插件或人格设置排除，无法激活。"
            )

        self.activation.activate(
            umo,
            tool_name,
            score=1.0,
            ttl_turns=self._ttl_turns(meta),
            ttl_seconds=self._ttl_seconds(meta),
        )

        same_turn = False
        if turn is not None:
            tool = turn.pruned.pop(tool_name, None)
            if tool is not None:
                same_turn = injector.restore(turn.request, tool)
        span = self._ttl_turns(meta)
        return (
            f"已激活 {tool_name}（有效期 {span} 轮）。"
            + ("本轮后续调用即可直接使用它。" if same_turn else "下一轮对话起可用。")
        )

    @lazy_tool(
        name="lazy_deactivate_tool",
        always_active=True,
        group="meta",
        tags=("元工具", "取消激活", "停用工具", "释放工具", "meta", "deactivate tool"),
    )
    async def lazy_deactivate_tool(self, event: AstrMessageEvent, tool_name: str) -> str:
        """取消激活一个工具，立即释放它占用的提示词预算。

        Args:
            tool_name(string): 要取消激活的工具名
        """
        umo: str = event.unified_msg_origin
        turn = self._turns.get(umo)
        hit = self.activation.deactivate(umo, tool_name)
        withdrawn = False
        if turn is not None:
            withdrawn = injector.withdraw(turn.request, tool_name)
        if not hit and not withdrawn:
            return f"工具 {tool_name} 当前并未处于激活状态。"
        return f"已取消激活 {tool_name}，后续轮次不再向模型下发它的说明书。"

    # ------------------------------------------------------------------
    # 调试与运维命令
    # ------------------------------------------------------------------

    @filter.command("lazy")
    async def lazy_status(self, event: AstrMessageEvent):
        """查看懒加载工具状态：/lazy list 列出工具，/lazy clear 清空本会话激活。"""
        argv = self._argv(event)
        sub = argv[0].lower() if argv else "status"
        umo: str = event.unified_msg_origin

        if sub in {"list", "ls"}:
            yield event.plain_result(self._render_tool_list(umo))
            return
        if sub == "clear":
            previously = self.activation.names(umo)
            count = self.activation.clear(umo)
            turn = self._turns.get(umo)
            if turn is not None and turn.request is not None:
                # 只撤掉原本处于激活态的工具，元工具与未激活工具不动。
                for name in previously:
                    injector.withdraw(turn.request, name)
            yield event.plain_result(f"已清空本会话的激活表（{count} 项）。")
            return
        if sub in {"status", ""}:
            yield event.plain_result(self._render_status(umo, event))
            return
        yield event.plain_result(
            "用法：/lazy status | /lazy list | /lazy clear\n"
            "子插件管理（管理员）：/lazy_sub on|off <子插件名>"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("lazy_sub")
    async def lazy_sub(self, event: AstrMessageEvent):
        """管理员启用/停用子插件：/lazy_sub on|off <子插件名>"""
        argv = self._argv(event)
        if len(argv) < 2 or argv[0].lower() not in {"on", "off"}:
            available = ", ".join(self.loader.discover()) or "（无）"
            yield event.plain_result(
                f"用法：/lazy_sub on|off <子插件名>\n已发现的子插件：{available}"
            )
            return
        action, name = argv[0].lower(), argv[1]
        if name not in self.loader.discover():
            yield event.plain_result(f"没有名为 {name} 的子插件。")
            return
        if not self.loader.load(name):
            reason = self.loader.errors.get(name, "未知原因")
            yield event.plain_result(f"子插件 {name} 加载失败：{reason}")
            return
        self.loader.set_enabled(name, action == "on")
        self._rebuild_index()
        self.activation.drop_stale(frozenset(REGISTRY.names()))
        state = "已启用" if action == "on" else "已停用"
        yield event.plain_result(f"子插件 {name} {state}，索引已重建（{self.index.size} 条）。")

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------

    def _apply_config(self, config: AstrBotConfig | dict[str, Any]) -> None:
        get = getattr(config, "get", None) or (lambda key, default=None: default)
        self._enabled = bool(get("enabled", True))
        self._prefetch_enabled = bool(get("prefetch_enabled", True))
        self._top_k = max(int(get("top_k", 3) or 0), 0)
        self._min_score = max(min(float(get("min_score", 0.35) or 0.0), 1.0), 0.0)
        self._default_ttl_turns = max(int(get("default_ttl_turns", 3) or 0), 0)
        self._default_ttl_seconds = max(float(get("default_ttl_seconds", 600) or 0.0), 0.0)
        self._max_active = max(int(get("max_active_per_session", 12) or 1), 1)
        self._allow_high_risk = bool(get("allow_high_risk_auto", False))
        self._meta_enabled = bool(get("meta_tools_enabled", True))
        self._sub_enabled = list(get("sub_plugins_enabled", []) or [])
        self._debug = bool(get("debug", False))
        self._search_top_k = max(self._top_k, 5) if self._top_k else 5

        raw_limit = int(get("max_result_chars", 4000) or 0)
        RESULT_LIMITS["default"] = raw_limit if raw_limit > 0 else None

        self.activation = ActivationStore(
            default_ttl_turns=self._default_ttl_turns,
            default_ttl_seconds=self._default_ttl_seconds,
            max_per_session=self._max_active,
        )

    def _load_sub_plugins(self) -> None:
        result = self.loader.load_all(self._sub_enabled)
        for name, ok in result.items():
            if not ok:
                logger.warning(
                    "[lazy-tools] 子插件 %s 加载失败：%s",
                    name,
                    self.loader.errors.get(name, "未知原因"),
                )

    def _rebuild_index(self) -> None:
        self.index.build(REGISTRY.candidates())

    def _lazy_pool(self, umo: str) -> frozenset[str]:
        """当前会话真正允许看到的本插件工具名。"""
        turn = self._turns.get(umo)
        if turn is not None:
            return turn.lazy_allowed
        return frozenset(REGISTRY.names())

    def _ttl_turns(self, meta: ToolMeta) -> int:
        return meta.ttl_turns if meta.ttl_turns is not None else self._default_ttl_turns

    def _ttl_seconds(self, meta: ToolMeta) -> float:
        return (
            meta.ttl_seconds
            if meta.ttl_seconds is not None
            else self._default_ttl_seconds
        )

    def _gc_turns(self) -> None:
        if len(self._turns) <= _TURN_MAX_ENTRIES:
            return
        now = time.monotonic()
        stale = [
            umo
            for umo, turn in self._turns.items()
            if now - turn.created_at > _TURN_TTL_SECONDS
        ]
        for umo in stale:
            self._turns.pop(umo, None)
        if len(self._turns) > _TURN_MAX_ENTRIES:
            ordered = sorted(self._turns.items(), key=lambda item: item[1].created_at)
            for umo, _turn in ordered[: len(self._turns) - _TURN_MAX_ENTRIES]:
                self._turns.pop(umo, None)

    @staticmethod
    def _argv(event: AstrMessageEvent) -> list[str]:
        tokens = (event.message_str or "").split()
        for index, token in enumerate(tokens):
            if token.lstrip("/").lower() in {"lazy", "lazy_sub"}:
                return tokens[index + 1 :]
        return tokens[1:] if tokens else []

    def _render_status(self, umo: str, event: AstrMessageEvent) -> str:
        active = self.activation.names(umo)
        pool = self._lazy_pool(umo)
        candidates = REGISTRY.candidates()
        lines = [
            "【懒加载工具注入】",
            f"状态：{'启用' if self._enabled else '已关闭（工具原样注入）'}",
            f"工具：注册 {len(candidates)} 个，本会话许可 {len(pool)} 个",
            f"预检索：{'开' if self._prefetch_enabled else '关'}，"
            f"阈值 {self._min_score:.2f}，top_k {self._top_k}",
            f"过期策略：{self._default_ttl_turns} 轮 / {self._default_ttl_seconds:.0f} 秒"
            f"（先到者生效）",
            f"本会话当前激活：{', '.join(sorted(active)) if active else '无'}",
            f"索引规模：{self.index.size} 条；会话快照：{len(self._turns)} 个",
        ]
        if self.loader.loaded:
            states = [
                f"{name}{'✓' if REGISTRY.is_source_enabled(name) else '✗'}"
                for name in sorted(self.loader.loaded)
            ]
            lines.append(f"子插件：{', '.join(states)}")
        return "\n".join(lines)

    def _render_tool_list(self, umo: str) -> str:
        pool = self._lazy_pool(umo)
        active = set(self.activation.names(umo))
        lines = [f"懒加载工具清单（本会话许可 {len(pool)} 个）："]
        for meta in sorted(REGISTRY.candidates(), key=lambda m: (m.source, m.name)):
            if meta.name not in pool:
                continue
            mark = "★" if meta.name in active else "·"
            source = "" if meta.source == "main" else f" [{meta.source}]"
            desc = (meta.description or "").splitlines()[0][:60]
            lines.append(f"{mark} {meta.name}{source}：{desc}")
        if len(lines) == 1:
            lines.append("（无）")
        lines.append("★ = 本会话已激活")
        return "\n".join(lines)
