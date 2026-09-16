# -*- coding: utf-8 -*-
"""astrbot_plugin_neko_halflife —— 薛定谔的工具箱
=================================================

名字取自薛定谔的猫：工具既已经注册（存在），又没有进入本轮请求（不可见），
只有在被本地检索「观测」到并激活之后，才坍缩成模型能看见的一整套 Schema。

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

import inspect
import shutil
import time
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request

try:
    from .core import injector, plugin_host, plugin_import, uploads
    from .core.activation import ActivationStore
    from .core.models import RISK_HIGH, ToolMeta, TurnContext
    from .core.registry import REGISTRY, RESULT_LIMITS, lazy_tool
    from .core.retriever import ToolIndex
    from .core.subplugins import SubPluginLoader
    from .core.uploads import UploadError
except ImportError:  # AstrBot 也支持把 main.py 当普通模块载入
    from core import injector, plugin_host, plugin_import, uploads
    from core.activation import ActivationStore
    from core.models import RISK_HIGH, ToolMeta, TurnContext
    from core.registry import REGISTRY, RESULT_LIMITS, lazy_tool
    from core.retriever import ToolIndex
    from core.subplugins import SubPluginLoader
    from core.uploads import UploadError

#: AstrBot 的指令管理 API。它是 core 的内部模块、不在 astrbot.api 里导出，
#: 所以**必须容错导入**：拿不到就把指令面板降级成「此版本不支持」，
#: 而不是让整个插件加载失败。
try:
    from astrbot.core.star import command_management
except Exception:  # noqa: BLE001 - 不同 AstrBot 版本该路径可能变化
    command_management = None

__all__ = ["LazyToolsPlugin", "lazy_tool"]

#: Page 用的后端路由前缀。必须是 metadata.name——dashboard 前端
#: （PluginPagePage.vue 的 buildPluginApiPath）拼的就是
#: ``/api/v1/plugins/extensions/<plugin.name>/<endpoint>``，
#: 而页面里 ``bridge.apiGet("state")`` 不带这个前缀。
PLUGIN_NAME = "astrbot_plugin_neko_halflife"

#: 会话快照的保留时长与数量上限，避免长时间运行后字典无限增长。
_TURN_TTL_SECONDS = 3600.0
_TURN_MAX_ENTRIES = 512


class LazyToolsPlugin(Star):
    """薛定谔的工具箱：懒加载工具注入插件。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.index = ToolIndex()
        self.activation = ActivationStore()
        self.loader = SubPluginLoader(Path(__file__).resolve().parent, REGISTRY)
        self._turns: dict[str, TurnContext] = {}
        #: 宿主模式下的子插件实例（name -> HostResult），删除/卸载时要停掉它们
        self._hosted: dict[str, Any] = {}
        self._apply_config(config)
        self._register_web_apis()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """AstrBot 会在插件实例化后调用。这里加载子插件并建索引。

        索引必须在子插件导入之后建，否则子插件的工具不会进入检索候选。
        """
        self._apply_config(self.config)
        await self._load_sub_plugins()
        self._rebuild_index()
        self.activation.drop_stale(frozenset(REGISTRY.names()))
        logger.info(
            "[neko-halflife] 就绪：懒加载工具 %d 个（其中常驻元工具 %d 个），"
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
        """卸载清理。

        宿主子插件的实例必须在这里停掉并解绑——它们的工具与事件处理器注册在
        AstrBot 全局表里，不主动清理会在本插件卸载后继续被调用，而实例已经不在了。
        """
        for name in list(self._hosted):
            try:
                await self._unhost_async(name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[neko-halflife] 卸载宿主子插件 %s 失败：%s", name, exc)
        self._turns.clear()
        logger.info("[neko-halflife] 已卸载，会话状态与宿主子插件已清理")

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
                "[neko-halflife] 本轮注入失败，保持原工具集不变：%s", exc, exc_info=True
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
                "[neko-halflife] %s 许可 %d 项 | 预检索命中 %s | 本轮激活 %s | 裁掉 %d 项",
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
        self._persist_sub_plugins()
        state = "已启用" if action == "on" else "已停用"
        yield event.plain_result(f"子插件 {name} {state}，索引已重建（{self.index.size} 条）。")

    # ------------------------------------------------------------------
    # Web UI 后端接口（对应 pages/lazy-tools/）
    # ------------------------------------------------------------------

    def _register_web_apis(self) -> None:
        """注册 Page 用的后端接口。

        路由必须带插件名前缀；页面里 ``bridge.apiGet("state")`` 不带前缀，
        由 dashboard 拼成 ``/api/v1/plugins/extensions/<插件名>/state``。
        同路由同方法重复注册会被 ``Context.register_web_api`` 静默替换，
        所以插件重载是幂等的。
        """
        routes = (
            ("state", self.page_state, ["GET"], "懒加载工具总览"),
            ("search", self.page_search, ["POST"], "试跑本地检索"),
            ("sessions/clear", self.page_clear_session, ["POST"], "清空指定会话的激活表"),
            ("sources/toggle", self.page_toggle_source, ["POST"], "启用或停用子插件"),
            ("subplugins/upload", self.page_upload_subplugin, ["POST"], "上传并安装子插件"),
            ("subplugins/delete", self.page_delete_subplugin, ["POST"], "删除子插件"),
            ("subplugins/rescan", self.page_rescan_subplugins, ["POST"], "重新扫描子插件目录"),
            ("plugin-import/candidates", self.page_import_candidates, ["GET"], "列出可导入的已装插件"),
            ("plugin-import/apply", self.page_import_apply, ["POST"], "把已装插件导入为子插件"),
            ("commands", self.page_commands, ["GET"], "列出指令及其别名/权限/启停"),
            ("commands/rename", self.page_rename_command, ["POST"], "重命名指令或改别名"),
            ("commands/toggle", self.page_toggle_command, ["POST"], "启用或停用指令"),
            ("commands/permission", self.page_set_command_permission, ["POST"], "设置指令权限"),
        )
        for suffix, handler, methods, description in routes:
            try:
                self.context.register_web_api(
                    f"/{PLUGIN_NAME}/{suffix}", handler, methods, description
                )
            except Exception as exc:  # noqa: BLE001 - 注册失败不该拖垮插件加载
                logger.error("[neko-halflife] 注册 Web API %s 失败：%s", suffix, exc)

    async def page_state(self):
        """总览：配置、统计、工具清单、子插件与各会话激活表。"""
        sessions = self.activation.describe()

        counts: dict[str, int] = {}
        for meta in REGISTRY.all():
            if meta.source != "main":
                counts[meta.source] = counts.get(meta.source, 0) + 1
        sources = [
            {
                "name": name,
                "enabled": REGISTRY.is_source_enabled(name),
                "tools": counts.get(name, 0),
                "loaded": name in self.loader.loaded,
                "retired": REGISTRY.is_source_retired(name),
                "error": self.loader.errors.get(name),
            }
            for name in sorted(set(counts) | set(self.loader.discover()))
        ]

        tools = [
            {
                "name": meta.name,
                "description": meta.description,
                "source": meta.source,
                "group": meta.group,
                "tags": list(meta.tags),
                "examples": list(meta.examples),
                "risk": meta.risk,
                "always_active": meta.always_active,
                "ttl_turns": self._ttl_turns(meta),
                "ttl_seconds": self._ttl_seconds(meta),
                "max_result_chars": meta.max_result_chars or RESULT_LIMITS.get("default"),
                "enabled": meta.source == "main"
                or REGISTRY.is_source_enabled(meta.source),
            }
            for meta in sorted(REGISTRY.all(), key=lambda m: (m.source, m.name))
        ]

        return json_response(
            {
                "plugin_name": PLUGIN_NAME,
                "enabled": self._enabled,
                "stats": {
                    "tools": len(REGISTRY.candidates()),
                    "meta_tools": len(REGISTRY.always_active()),
                    "all_tools": len(REGISTRY.all()),
                    "indexed": self.index.size,
                    "sessions": len(sessions),
                    "activations": sum(item["count"] for item in sessions),
                    "turn_snapshots": len(self._turns),
                },
                "config": {
                    "prefetch_enabled": self._prefetch_enabled,
                    "top_k": self._top_k,
                    "min_score": round(self._min_score, 3),
                    "default_ttl_turns": self._default_ttl_turns,
                    "default_ttl_seconds": self._default_ttl_seconds,
                    "max_active_per_session": self._max_active,
                    "max_result_chars": RESULT_LIMITS.get("default"),
                    "allow_high_risk_auto": self._allow_high_risk,
                    "meta_tools_enabled": self._meta_enabled,
                    "debug": self._debug,
                },
                "tools": tools,
                "sources": sources,
                "sessions": sessions,
            }
        )

    async def page_search(self):
        """在线试跑检索。

        只读：**不改动激活表**，只回答「这句话会召回什么、够不够阈值」。
        这是调 ``min_score`` / ``top_k`` 时最省事的工具——比改配置、发消息、
        翻日志快得多。
        """
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象。")
        query = str(payload.get("query") or "").strip()
        if not query:
            return error_response("请提供 query。")

        top_k = self._clamp_int(payload.get("top_k"), default=self._search_top_k, low=1, high=50)
        min_score = self._clamp_float(
            payload.get("min_score"), default=self._min_score, low=0.0, high=1.0
        )
        umo = str(payload.get("umo") or "").strip()
        allowed = self._lazy_pool(umo) if umo else frozenset(REGISTRY.names())
        active = set(self.activation.names(umo)) if umo else set()

        hits = []
        for rank, (meta, score) in enumerate(
            self.index.search(query, top_k=50, min_score=0.0), start=1
        ):
            passed = score >= min_score
            hits.append(
                {
                    "rank": rank,
                    "name": meta.name,
                    "score": round(score, 4),
                    "passed": passed,
                    "in_top_k": passed and rank <= top_k,
                    "would_activate": (
                        passed
                        and rank <= top_k
                        and meta.name in allowed
                        and (meta.risk != RISK_HIGH or self._allow_high_risk)
                    ),
                    "description": meta.description,
                    "source": meta.source,
                    "tags": list(meta.tags),
                    "risk": meta.risk,
                    "allowed": meta.name in allowed,
                    "active": meta.name in active,
                }
            )

        return json_response(
            {
                "query": query,
                "top_k": top_k,
                "min_score": min_score,
                "info_tokens": self.index.informative_tokens(query),
                "hits": hits,
            }
        )

    async def page_clear_session(self):
        """清空指定会话的激活表。"""
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象。")
        umo = str(payload.get("umo") or "").strip()
        if not umo:
            return error_response("请提供 umo。")
        previously = self.activation.names(umo)
        cleared = self.activation.clear(umo)
        turn = self._turns.pop(umo, None)
        if turn is not None and turn.request is not None:
            for name in previously:
                injector.withdraw(turn.request, name)
        return json_response({"umo": umo, "cleared": cleared})

    async def page_toggle_source(self):
        """启用或停用子插件，并把结果写回插件配置（重启后保持）。"""
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象。")
        name = str(payload.get("name") or "").strip()
        if not name:
            return error_response("请提供 name。")
        enabled = bool(payload.get("enabled"))

        known = set(REGISTRY.sources()) | set(self.loader.discover())
        if name not in known:
            return error_response(f"没有名为 {name} 的子插件。", status_code=404)

        if enabled and name not in self.loader.loaded and not self.loader.load(name):
            reason = self.loader.errors.get(name, "未知原因")
            return error_response(f"子插件 {name} 加载失败：{reason}")

        self.loader.set_enabled(name, enabled)
        self._rebuild_index()
        self.activation.drop_stale(frozenset(REGISTRY.names()))
        self._persist_sub_plugins()
        return json_response(
            {
                "name": name,
                "enabled": enabled,
                "indexed": self.index.size,
                "persisted": self._sub_disabled,
            }
        )

    async def page_upload_subplugin(self):
        """接收并安装一个子插件包。

        表单字段名固定为 ``file``（bridge 的 ``upload()`` 只发文件，不带别的参数，
        所以「覆盖」必须在页面上拆成「先删除再上传」两步）。

        **这是本插件唯一会写入代码并在进程内执行它的入口**，因此：
        只允许管理员（Page 本身在 dashboard 登录后才可达）、文件名与包结构严格校验、
        体积上限、已存在则拒绝覆盖。校验全部通过后才落盘。
        """
        if not self._allow_upload:
            return error_response(
                "未启用子插件上传。请在插件配置里打开「允许上传子插件」——"
                "上传的子插件会在 AstrBot 进程内执行其中的 Python 代码，"
                "因此默认不开放。"
            )

        files = await request.files()
        upload = files.get("file")
        filename = str(getattr(upload, "filename", "") or "")
        if upload is None or not filename:
            return error_response("没有收到文件（表单字段名必须是 file）。")

        try:
            name = uploads.sanitize_subplugin_name(filename)
        except UploadError as exc:
            return error_response(str(exc))

        max_bytes = self._max_upload_bytes
        declared = getattr(upload, "content_length", None)
        if max_bytes and declared and declared > max_bytes:
            return error_response(
                f"文件过大：{declared} 字节，超过上限 {max_bytes} 字节。"
            )

        is_zip = filename.lower().endswith(".zip")
        staging_dir = self.loader.root / ".uploads"
        staging_dir.mkdir(parents=True, exist_ok=True)
        staged = staging_dir / (f"{name}.zip" if is_zip else f"{name}.py")

        try:
            await upload.save(staged)
            size = staged.stat().st_size
            if max_bytes and size > max_bytes:
                return error_response(
                    f"文件过大：{size} 字节，超过上限 {max_bytes} 字节。"
                )
            if is_zip:
                dest = uploads.install_zip_package(
                    staged, self.loader.root, name, max_total_bytes=max_bytes
                )
            else:
                dest = uploads.install_py_file(staged, self.loader.root, name)
        except UploadError as exc:
            return error_response(str(exc))
        except Exception as exc:  # noqa: BLE001 - 安装失败要变成可读错误，而不是 500
            logger.error("[neko-halflife] 子插件安装失败：%s", exc, exc_info=True)
            return error_response(f"安装失败：{exc}")
        finally:
            staged.unlink(missing_ok=True)

        # 落盘之后才导入并执行——这一步才会真正跑子插件里的代码
        self.loader.set_enabled(name, True)
        before_tools = self._global_tool_names()
        loaded = self.loader.load(name)

        # 与"从已装插件导入"同一套收尾：上传的包里如果是个常规插件
        # （Star 子类 + AstrBot 装饰器），就宿主它；并检测脏注册。
        mode = "native"
        host_result = None
        analysis = None
        if loaded:
            analysis = plugin_import.analyze_plugin_dir(dest)
            mode = analysis.mode
            if analysis.has_star_class:
                plugin_import.write_host_marker(dest, mode="hosted", source_dir=name)
                host_result = self._ensure_hosted(name)
                if host_result is None or not host_result.ok:
                    loaded = False
        stray = sorted(
            tool
            for tool in (self._global_tool_names() - before_tools)
            if REGISTRY.get(tool) is None
        )

        if not loaded or stray:
            for tool in stray:
                self._remove_global_tool(tool)
            await self._unhost_async(name)
            self._forget_source_tools(name)
            uploads.remove_subplugin(self.loader.root, name)
            self.loader.loaded.discard(name)
            self.loader.errors.pop(name, None)
            self._rebuild_index()
            if stray:
                return error_response(
                    f"上传的包注册了 {len(stray)} 个非本插件的工具"
                    f"（{', '.join(stray[:5])}），已清理并回滚文件。"
                )
            return error_response(
                f"子插件 {name} 导入失败，已回滚文件："
                f"{self.loader.errors.get(name, '未知原因')}"
                + (
                    "；宿主失败：" + "；".join(host_result.errors)
                    if host_result is not None and not host_result.ok
                    else ""
                )
            )

        # 「装上了但一个工具都没有」必须当场报错，不能静默成功。
        # 否则用户看到的是「子插件装好了，但工具清单里什么都没有」，无从排查。
        source_tools = [meta.name for meta in REGISTRY.all() if meta.source == name]
        bound_handlers = list(host_result.bound_handlers) if host_result else []
        if not source_tools and not bound_handlers:
            await self._unhost_async(name)
            self._forget_source_tools(name)
            uploads.remove_subplugin(self.loader.root, name)
            self.loader.loaded.discard(name)
            self.loader.errors.pop(name, None)
            self._rebuild_index()
            detail = "；".join(getattr(analysis, "reasons", []) or []) or (
                f"检测到的模式是 {mode}：包里的代码既没有 @lazy_tool 装饰的工具，"
                "也没有可宿主的 Star 子类。子插件的工具请写成模块级普通函数并加上 "
                "@lazy_tool，或直接上传一个常规插件包（含 Star 子类）。"
            )
            return error_response(
                f"子插件 {name} 里没有找到任何工具，已回滚文件。{detail}"
            )

        self._rebuild_index()
        self.activation.drop_stale(frozenset(REGISTRY.names()))
        self._persist_sub_plugins()

        installed = len(source_tools)
        logger.warning(
            "[neko-halflife] 已通过 WebUI 安装子插件 %s（%s，模式 %s，%d 个工具）",
            name,
            dest,
            mode,
            installed,
        )
        return json_response(
            {
                "name": name,
                "path": str(dest),
                "mode": mode,
                "tools": installed,
                "bound_handlers": bound_handlers,
                "indexed": self.index.size,
                "kind": "zip" if is_zip else "single_file",
            }
        )

    async def page_rescan_subplugins(self):
        """重新扫描 ``sub_plugins/``：加载新增的、卸载已消失的、续接宿主、重建索引。

        存在的理由：本插件**只在自己被加载/重载时**才会扫一次目录。如果有人用
        scp / 手写的方式把子插件放进 ``sub_plugins/``，在插件重载之前什么都
        不会发生——而且不报错，看起来就像"没反应"。这个接口让面板上点一下就能
        完成扫描，不必去 AstrBot 的插件管理里重载整个插件。
        """
        before = set(self.loader.loaded)
        await self._load_sub_plugins()
        self._rebuild_index()
        self.activation.drop_stale(frozenset(REGISTRY.names()))

        discovered = sorted(self.loader.discover())
        loaded = sorted(self.loader.loaded)
        errors = {
            name: self.loader.errors[name]
            for name in loaded
            if name in self.loader.errors
        }
        # 仅磁盘上存在、但没加载成功的（含被停用的）也报出来，便于定位
        for name in discovered:
            if name not in self.loader.loaded and name in self.loader.errors:
                errors[name] = self.loader.errors[name]
        tools = {
            source: sum(1 for meta in REGISTRY.all() if meta.source == source)
            for source in sorted(set(REGISTRY.sources()) | set(discovered))
        }
        added = sorted(set(loaded) - before)
        removed = sorted(before - set(loaded))
        logger.info(
            "[neko-halflife] 重新扫描 sub_plugins/：磁盘 %d 个，已加载 %d 个，"
            "新增 %s，消失 %s",
            len(discovered),
            len(loaded),
            added or "无",
            removed or "无",
        )
        return json_response(
            {
                "discovered": discovered,
                "loaded": loaded,
                "added": added,
                "removed": removed,
                "errors": errors,
                "tools": tools,
                "indexed": self.index.size,
            }
        )

    async def page_delete_subplugin(self):
        """删除一个子插件（单文件或目录），并清理它的工具注册。"""
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象。")
        raw = str(payload.get("name") or "").strip()
        if not raw:
            return error_response("请提供 name。")
        try:
            name = uploads.sanitize_subplugin_name(raw)
        except UploadError as exc:
            return error_response(str(exc))

        # 宿主模式要先停掉实例并解绑事件处理器：只删工具不够，
        # 命令/钩子仍挂在全局表里，实例没了以后一触发就报错。
        await self._unhost_async(name)
        removed_tools = self._forget_source_tools(name)
        removed_files = uploads.remove_subplugin(self.loader.root, name)
        self.loader.loaded.discard(name)
        self.loader.errors.pop(name, None)
        self._rebuild_index()
        self.activation.drop_stale(frozenset(REGISTRY.names()))
        self._persist_sub_plugins()

        if not removed_files and not removed_tools:
            return error_response(f"没有找到子插件 {name}。", status_code=404)
        logger.warning(
            "[neko-halflife] 已删除子插件 %s（文件=%s，工具 %d 个）",
            name,
            "已删除" if removed_files else "不存在",
            removed_tools,
        )
        return json_response(
            {
                "name": name,
                "removed_files": removed_files,
                "removed_tools": removed_tools,
                "indexed": self.index.size,
            }
        )

    # ---- 从已装插件导入为子插件 ------------------------------------------

    @staticmethod
    def _plugins_root() -> Path | None:
        """AstrBot 的插件目录（data/plugins）。"""
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_path

            return Path(get_astrbot_plugin_path())
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _read_plugin_metadata(plugin_dir: Path) -> dict:
        for filename in ("metadata.yaml", "metadata.yml"):
            path = plugin_dir / filename
            if not path.is_file():
                continue
            try:
                import yaml

                data = yaml.safe_load(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:  # noqa: BLE001
                return {}
        return {}

    def _global_tool_names(self) -> set[str]:
        try:
            return {tool.name for tool in self.context.get_llm_tool_manager().func_list}
        except Exception:  # noqa: BLE001
            return set()

    def _remove_global_tool(self, name: str) -> bool:
        try:
            self.context.get_llm_tool_manager().remove_func(name)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("[neko-halflife] 清理脏注册工具 %s 失败：%s", name, exc)
            return False

    async def page_import_candidates(self):
        """列出 data/plugins 下已安装的插件，并给出可移植性分析。"""
        plugins_root = self._plugins_root()
        if plugins_root is None or not plugins_root.is_dir():
            return json_response(
                {
                    "supported": False,
                    "reason": "无法定位 AstrBot 插件目录（data/plugins）。",
                    "candidates": [],
                }
            )

        existing = set(self.loader.discover())
        candidates = []
        for entry in sorted(plugins_root.iterdir(), key=lambda p: p.name.lower()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            meta = self._read_plugin_metadata(entry)
            analysis = plugin_import.analyze_plugin_dir(entry)
            item = analysis.to_dict()
            item.update(
                {
                    "plugin_name": meta.get("name") or entry.name,
                    "display_name": meta.get("display_name")
                    or meta.get("name")
                    or entry.name,
                    "version": meta.get("version"),
                    "desc": str(
                        meta.get("short_desc") or meta.get("desc") or ""
                    )[:200],
                }
            )
            try:
                target = uploads.sanitize_subplugin_name(entry.name)
                item["target_name"] = target
                item["already_imported"] = target in existing
            except UploadError as exc:
                item["target_name"] = None
                item["already_imported"] = False
                item["portable"] = False
                item["reasons"] = list(item["reasons"]) + [
                    f"目录名不能作为子插件名：{exc}"
                ]
            candidates.append(item)

        return json_response(
            {"supported": True, "reason": "", "candidates": candidates}
        )

    async def page_import_apply(self):
        """把一个已装插件复制进 sub_plugins 并加载。

        三道关卡：

        1. **静态预检**（``core.plugin_import``）：用 AstrBot 自带工具装饰器的插件
           直接拒绝——导入它们会有全局副作用；
        2. **导入后校验**：比对导入前后的全局工具表，出现「不在本插件注册表里」
           的新工具即判定为脏注册；
        3. **失败回滚**：删掉刚复制的目录、清掉脏注册，并把覆盖前的旧版本还原。
        """
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象。")
        dir_name = str(payload.get("dir_name") or "").strip()
        overwrite = bool(payload.get("overwrite"))
        if not dir_name:
            return error_response("请提供 dir_name。")
        if "/" in dir_name or "\\" in dir_name or dir_name.startswith("."):
            return error_response("非法的插件目录名。")

        plugins_root = self._plugins_root()
        if plugins_root is None:
            return error_response("无法定位 AstrBot 插件目录（data/plugins）。")
        src = plugins_root / dir_name
        if not src.is_dir():
            return error_response(f"插件目录不存在：{dir_name}", status_code=404)

        analysis = plugin_import.analyze_plugin_dir(src)
        if not analysis.portable:
            return error_response(
                "该插件不能作为子插件导入：\n- " + "\n- ".join(analysis.reasons)
            )

        try:
            name = uploads.sanitize_subplugin_name(dir_name)
        except UploadError as exc:
            return error_response(str(exc))

        dest = self.loader.root / name
        if dest.exists() and not overwrite:
            return error_response(
                f"sub_plugins/{name} 已存在。需要替换请勾选「覆盖」后再导入。"
            )

        # 覆盖前先把旧版本挪走，失败时还原
        backup: Path | None = None
        if dest.exists():
            backup = self.loader.root / ".uploads" / f"backup-{name}-{int(time.time())}"
            backup.parent.mkdir(parents=True, exist_ok=True)
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
            shutil.move(str(dest), str(backup))

        before_tools = self._global_tool_names()
        try:
            copied = plugin_import.copy_plugin_tree(src, dest, overwrite=True)
            plugin_import.write_entry(dest, analysis.tool_modules)
            # 先落宿主标记：万一进程在下面中途挂掉，重启后 load_all 也能按标记续接
            # 宿主，而不是留下「装饰器注册了工具、却没人实例化」的坏状态。
            plugin_import.write_host_marker(
                dest, mode=analysis.mode, source_dir=dir_name
            )
        except Exception as exc:  # noqa: BLE001
            await self._rollback_import(dest, backup, name)
            return error_response(f"复制失败，已回滚：{exc}")

        self.loader.set_enabled(name, True)
        loaded = self.loader.load(name)

        host_result = None
        if loaded and analysis.mode == "hosted":
            # 常规插件：实例化它的 Star 类并把工具/事件处理器绑上去，同时把它纳入
            # 本插件注册表 —— 这样它们才会被按需注入。走 _ensure_hosted 而不是直接
            # 调 host_module：它会把实例记进 self._hosted，删除/卸载时才找得到。
            host_result = self._ensure_hosted(name)
            if host_result is None or not host_result.ok:
                loaded = False

        # 脏注册 = 导入后新出现、且不在本插件注册表里的工具
        stray = sorted(
            tool
            for tool in (self._global_tool_names() - before_tools)
            if REGISTRY.get(tool) is None
        )

        if not loaded or stray:
            for tool in stray:
                self._remove_global_tool(tool)
            await self._rollback_import(dest, backup, name)
            if stray:
                return error_response(
                    f"导入失败并已回滚：检测到 {len(stray)} 个非本插件的工具被注册"
                    f"（{', '.join(stray[:5])}），已从全局工具表清理。"
                )
            if host_result is not None and not host_result.ok:
                return error_response(
                    "导入失败并已回滚：宿主该插件失败 —— "
                    + "；".join(host_result.errors)
                )
            return error_response(
                f"导入失败并已回滚：模块无法加载 —— "
                f"{self.loader.errors.get(name, '未知原因')}"
            )

        # 同上：导入完一个工具或处理器都没有，说明这个插件不会带来任何效果，
        # 与其静默成功让人以为"搬进来了"，不如当场说清楚并回滚。
        source_tools = [meta.name for meta in REGISTRY.all() if meta.source == name]
        bound_handlers = (
            list(host_result.bound_handlers) if host_result is not None else []
        )
        if not source_tools and not bound_handlers:
            await self._rollback_import(dest, backup, name)
            return error_response(
                f"导入 {dir_name} 后没有找到任何工具或事件处理器，已回滚。"
                + (
                    "；".join(analysis.reasons)
                    or "该插件的工具/命令没有在导入期注册（可能在 initialize() 里"
                    "动态注册，本插件目前不调用它）。"
                )
            )

        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)

        self._rebuild_index()
        self.activation.drop_stale(frozenset(REGISTRY.names()))
        self._persist_sub_plugins()

        installed = list(source_tools)
        logger.warning(
            "[neko-halflife] 已从插件 %s 导入 %d 个文件为子插件 %s（模式 %s，"
            "%d 个工具：%s）",
            dir_name,
            copied,
            name,
            analysis.mode,
            len(installed),
            ", ".join(installed[:5]) or "无",
        )
        return json_response(
            {
                "name": name,
                "source": dir_name,
                "mode": analysis.mode,
                "files": copied,
                "tools": installed,
                "bound_handlers": list(bound_handlers),
                "warnings": list(analysis.warnings)
                + (list(host_result.warnings) if host_result is not None else []),
                "indexed": self.index.size,
            }
        )

    async def _rollback_import(
        self, dest: Path, backup: Path | None, name: str
    ) -> None:
        """回滚一次导入：先停宿主，再清注册、删新目录、还原旧版本。"""
        await self._unhost_async(name)
        self._forget_source_tools(name)
        self.loader.loaded.discard(name)
        self.loader.errors.pop(name, None)
        shutil.rmtree(dest, ignore_errors=True)
        if backup is not None and backup.exists():
            try:
                shutil.move(str(backup), str(dest))
                self.loader.load(name)
                logger.warning("[neko-halflife] 已还原子插件 %s 的旧版本", name)
            except Exception as exc:  # noqa: BLE001
                logger.error("[neko-halflife] 还原子插件 %s 失败：%s", name, exc)
        self._rebuild_index()
        self.activation.drop_stale(frozenset(REGISTRY.names()))

    # ---- 指令面板：AstrBot 官方 command_management 的薄前端 ----------------

    def _command_panel_unavailable(self) -> str:
        """返回不可用原因；可用时返回空串。"""
        if not self._cmd_panel_enabled:
            return (
                "指令面板未启用。它是 AstrBot 的通用管理功能、与本插件主题无关，"
                "为保持插件定位清晰默认关闭；需要时在插件配置里打开"
                "「启用指令面板」。"
            )
        if command_management is None:
            return (
                "当前 AstrBot 版本未提供 astrbot.core.star.command_management，"
                "指令面板不可用（插件其余功能不受影响）。"
            )
        return ""

    async def page_commands(self):
        """列出全部指令及其别名、权限、启停状态与冲突。"""
        reason = self._command_panel_unavailable()
        if reason:
            return json_response({"supported": False, "reason": reason, "commands": []})
        try:
            commands = await command_management.list_commands()
            conflicts = await command_management.list_command_conflicts()
        except Exception as exc:  # noqa: BLE001
            logger.error("[neko-halflife] 读取指令列表失败：%s", exc, exc_info=True)
            return error_response(f"读取指令列表失败：{exc}")
        return json_response(
            {
                "supported": True,
                "reason": "",
                "commands": commands,
                "conflicts": conflicts,
            }
        )

    async def _command_payload(self) -> tuple[dict[str, Any] | None, Any]:
        """读取并校验指令操作的公共字段。"""
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return None, error_response("请求体必须是 JSON 对象。")
        full_name = str(payload.get("handler_full_name") or "").strip()
        if not full_name:
            return None, error_response("请提供 handler_full_name。")
        return payload, None

    async def page_rename_command(self):
        """重命名指令或修改别名（自带冲突校验）。"""
        reason = self._command_panel_unavailable()
        if reason:
            return error_response(reason)
        payload, bad = await self._command_payload()
        if bad is not None:
            return bad
        fragment = str(payload.get("fragment") or "").strip()
        if not fragment:
            return error_response("请提供新的指令名 fragment。")
        raw_aliases = payload.get("aliases")
        aliases = (
            [str(a).strip() for a in raw_aliases if str(a).strip()]
            if isinstance(raw_aliases, list)
            else None
        )
        try:
            descriptor = await command_management.rename_command(
                str(payload["handler_full_name"]), fragment, aliases
            )
        except ValueError as exc:  # 重名/空名等可预期错误
            return error_response(str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.error("[neko-halflife] 重命名指令失败：%s", exc, exc_info=True)
            return error_response(f"重命名失败：{exc}")
        return json_response(
            {
                "handler_full_name": descriptor.handler_full_name,
                "effective_command": descriptor.effective_command,
                "aliases": descriptor.aliases,
            }
        )

    async def page_toggle_command(self):
        """启用或停用指令。"""
        reason = self._command_panel_unavailable()
        if reason:
            return error_response(reason)
        payload, bad = await self._command_payload()
        if bad is not None:
            return bad
        enabled = bool(payload.get("enabled"))
        try:
            descriptor = await command_management.toggle_command(
                str(payload["handler_full_name"]), enabled
            )
        except ValueError as exc:
            return error_response(str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.error("[neko-halflife] 启停指令失败：%s", exc, exc_info=True)
            return error_response(f"启停失败：{exc}")
        return json_response(
            {
                "handler_full_name": descriptor.handler_full_name,
                "enabled": descriptor.enabled,
            }
        )

    async def page_set_command_permission(self):
        """设置指令权限（admin / member）。

        注意 ``update_command_permission`` 的第二个形参名是 ``permission_type``
        而不是 ``permission``，所以这里**按位置传参**，避免关键字名变化导致 TypeError。
        """
        reason = self._command_panel_unavailable()
        if reason:
            return error_response(reason)
        payload, bad = await self._command_payload()
        if bad is not None:
            return bad
        permission = str(payload.get("permission") or "").strip().lower()
        if permission not in {"admin", "member"}:
            return error_response(
                "权限只能是 admin 或 member（AstrBot 不接受 everyone；"
                "要恢复默认请重新加载插件）。"
            )
        try:
            descriptor = await command_management.update_command_permission(
                str(payload["handler_full_name"]), permission
            )
        except ValueError as exc:
            return error_response(str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.error("[neko-halflife] 设置指令权限失败：%s", exc, exc_info=True)
            return error_response(f"设置权限失败：{exc}")
        return json_response(
            {
                "handler_full_name": descriptor.handler_full_name,
                "permission": descriptor.permission,
            }
        )

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
        self._sub_disabled = list(get("sub_plugins_disabled", []) or [])
        self._allow_upload = bool(get("allow_subplugin_upload", True))
        self._max_upload_bytes = (
            max(int(get("max_subplugin_upload_kb", 2048) or 0), 0) * 1024
        )
        self._cmd_panel_enabled = bool(get("enable_command_panel", False))
        self._debug = bool(get("debug", False))
        self._search_top_k = max(self._top_k, 5) if self._top_k else 5

        raw_limit = int(get("max_result_chars", 4000) or 0)
        RESULT_LIMITS["default"] = raw_limit if raw_limit > 0 else None

        self.activation = ActivationStore(
            default_ttl_turns=self._default_ttl_turns,
            default_ttl_seconds=self._default_ttl_seconds,
            max_per_session=self._max_active,
        )

    async def _load_sub_plugins(self) -> None:
        result = self.loader.load_all(self._sub_disabled)
        for name, ok in result.items():
            if not ok:
                logger.warning(
                    "[neko-halflife] 子插件 %s 加载失败：%s",
                    name,
                    self.loader.errors.get(name, "未知原因"),
                )
                continue
            # 宿主模式的子插件光是导入不够：它的装饰器已经把工具/命令注册进全局表，
            # 但没人实例化它的 Star 类。不续接宿主就会变成「每轮注入但一调用就报错」。
            self._ensure_hosted(name)
        # 注册表里还留着、但目录已经不在的子插件：把工具与事件处理器一并清干净。
        for stale in sorted(set(REGISTRY.sources()) - set(result)):
            await self._unhost_async(stale)
            removed = self._forget_source_tools(stale)
            self.loader.loaded.discard(stale)
            logger.info(
                "[neko-halflife] 子插件 %s 的目录已不存在，清理其 %d 个工具",
                stale,
                removed,
            )

    def _ensure_hosted(self, name: str) -> Any | None:
        """按宿主标记接管一个子插件；返回 HostResult（不需要宿主则为 None）。"""
        dest = self.loader.root / name
        marker = plugin_import.read_host_marker(dest)
        if not marker or marker.get("mode") != "hosted":
            return None
        if not REGISTRY.is_source_enabled(name):
            return None

        module_prefix = self.loader.module_name_for(name)
        result = plugin_host.host_module(
            module_prefix,
            dest,
            str(marker.get("source_dir") or name),
            self.context,
        )
        if not result.ok:
            logger.error(
                "[neko-halflife] 宿主子插件 %s 失败：%s",
                name,
                "；".join(result.errors),
            )
            plugin_host.unhost_module(module_prefix)
            return result

        for meta in result.tool_metas:
            REGISTRY.register(meta)
        self._hosted[name] = result
        self._rebuild_index()
        for warning in result.warnings:
            logger.warning("[neko-halflife] 子插件 %s：%s", name, warning)
        logger.info(
            "[neko-halflife] 已宿主子插件 %s（%s）：绑定工具 %d 个、事件处理器 %d 个",
            name,
            result.class_name,
            len(result.bound_tools),
            len(result.bound_handlers),
        )
        return result

    def _unhost(self, name: str) -> Any:
        """停掉宿主子插件的**同步部分**：解绑、移除工具与事件处理器。

        ``terminate()`` 是协程，所以放在 :meth:`_unhost_async` 里 await。
        """
        result = self._hosted.pop(name, None)
        plugin_host.unhost_module(self.loader.module_name_for(name))
        return result

    async def _unhost_async(self, name: str) -> None:
        """停掉宿主子插件，并尽量 await 它的 ``terminate()``。"""
        result = self._unhost(name)
        instance = getattr(result, "instance", None) if result else None
        if instance is None:
            return
        terminate = getattr(instance, "terminate", None)
        if not callable(terminate):
            return
        try:
            maybe = terminate()
            if inspect.isawaitable(maybe):
                await maybe
        except Exception as exc:  # noqa: BLE001 - 外来 terminate 什么都可能抛
            logger.warning("[neko-halflife] %s.terminate() 报错：%s", name, exc)

    def _forget_source_tools(self, source: str) -> int:
        """移除某个来源的全部工具，并保证它们**再也不会被注入**。

        这里有个很隐蔽的坑：``injector.plan_pruning`` 把「本插件注册表里查不到」
        当作「不是本插件的工具」而**原样保留**。所以如果只从注册表里删掉定义、
        而工具仍留在 AstrBot 全局 ``llm_tools`` 里，结果恰好相反——
        删掉的子插件反而变成每轮都注入。

        因此分两种情况：
        * 全局表移除**全部成功** → 遗忘定义（干净，界面上也不再显示）；
        * 有任何工具没能移除（取不到 llm_tools、或移除抛错）→ **退休**该来源：
          保留定义以便被识别为「本插件的工具」，但永远不进保留集，每轮必被裁掉。
        """
        metas = [meta for meta in REGISTRY.all() if meta.source == source]
        if not metas:
            return 0

        manager = None
        try:
            manager = self.context.get_llm_tool_manager()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[neko-halflife] 取 llm_tools 失败：%s", exc)

        still_registered: list[str] = []
        if manager is None:
            still_registered = [meta.name for meta in metas]
        else:
            for meta in metas:
                try:
                    manager.remove_func(meta.name)
                except Exception as exc:  # noqa: BLE001
                    still_registered.append(meta.name)
                    logger.warning(
                        "[neko-halflife] 从 AstrBot 全局工具表移除 %s 失败：%s",
                        meta.name,
                        exc,
                    )

        if still_registered:
            retired = REGISTRY.retire_source(source)
            logger.warning(
                "[neko-halflife] 有 %d 个工具未能从全局表移除，已退休来源 %s 以确保不再注入：%s",
                len(still_registered),
                source,
                ", ".join(still_registered),
            )
            return retired
        return REGISTRY.forget_source(source)

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

    def _persist_sub_plugins(self) -> None:
        """把子插件启停状态写回插件配置。

        存的是**停用名单**：空列表表示「全部启用」。用停用名单而不是启用名单，
        是因为启用名单里的空列表无法区分「一个都不启用」和「留空 = 全部启用」，
        会把「全停」静默变成「全开」。
        """
        all_sources = sorted(set(REGISTRY.sources()) | set(self.loader.discover()))
        value = [name for name in all_sources if not REGISTRY.is_source_enabled(name)]
        self._sub_disabled = value
        try:
            self.config["sub_plugins_disabled"] = value
            save = getattr(self.config, "save_config", None)
            if callable(save):
                save()
        except Exception as exc:  # noqa: BLE001 - 落盘失败只影响持久化，不影响本次运行
            logger.warning("[neko-halflife] 子插件状态写入配置失败（仅本次运行生效）：%s", exc)

    @staticmethod
    def _clamp_int(value: Any, *, default: int, low: int, high: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return max(low, min(high, number))

    @staticmethod
    def _clamp_float(value: Any, *, default: float, low: float, high: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return max(low, min(high, number))

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
            "【薛定谔的工具箱】",
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
