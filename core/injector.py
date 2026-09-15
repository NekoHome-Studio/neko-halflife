# -*- coding: utf-8 -*-
"""许可池快照与 ``ToolSet`` 重建——本插件唯一的安全关键点。

**为什么必须这样做**：拿到 ``req.func_tool`` 时，它已经被 AstrBot 上游筛过三层：

1. persona 显式声明的工具（``astr_main_agent._ensure_persona_and_skills``，
   persona 有 ``tools`` 字段时就只放这些）；
2. 工具启用开关 ``FunctionTool.active``（同一函数里剔除 ``not tool.active``）；
3. 会话级插件选择 ``event.plugins_name``（``astr_main_agent._plugin_tool_fix``，
   在钩子触发**之前**执行，见该函数调用点）。

如果在钩子里「把已激活工具的 Schema 加回去」，就会把 persona 明确排除的工具、
被管理员在本会话禁用的插件工具，重新塞回给模型——这是权限泄露。

因此本模块只做一件事：**在许可池内做减法**。
``allowed`` 快照是唯一合法来源，任何注入决策都必须与它取交集；
``rebuild`` 绝不新增上游没有的工具。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("astrbot_plugin_lazy_tools")


def _new_toolset() -> Any | None:
    """延迟导入 ``ToolSet``，让本模块在没有 AstrBot 的环境里也能被导入测试。"""
    try:
        from astrbot.core.agent.tool import ToolSet

        return ToolSet()
    except Exception:  # pragma: no cover - 仅在 AstrBot 结构变动时触发
        return None


def pool_tools(request: Any) -> list[Any]:
    """取出本轮请求的工具列表，拿不到就返回空表。"""
    pool = getattr(request, "func_tool", None)
    tools = getattr(pool, "tools", None)
    if not tools:
        return []
    return list(tools)


def snapshot_allowed_names(request: Any) -> frozenset[str]:
    """许可池快照：上游允许本轮模型看到的全部工具名。"""
    return frozenset(
        name
        for name in (getattr(tool, "name", "") for tool in pool_tools(request))
        if name
    )


def plan_pruning(
    request: Any,
    registry: Any,
    keep: frozenset[str],
) -> tuple[list[Any], dict[str, Any]]:
    """计算裁剪结果，**不修改** ``request``。

    保留规则：
    * 不在本插件注册表里的工具（别的插件、MCP、内置工具）一律原样保留；
    * 在注册表里且名字位于 ``keep`` 的保留；
    * 其余本插件工具裁掉，并记下对象供元工具同轮补注入。

    Returns:
        ``(kept, pruned)``；``pruned`` 为 ``name -> FunctionTool``。
    """
    kept: list[Any] = []
    pruned: dict[str, Any] = {}
    for tool in pool_tools(request):
        name = getattr(tool, "name", "")
        meta = registry.get(name) if name else None
        if meta is None or name in keep:
            kept.append(tool)
        else:
            pruned[name] = tool
    return kept, pruned


def apply_pruning(request: Any, kept: list[Any], pruned: dict[str, Any]) -> int:
    """把裁剪结果写回请求；返回被裁掉的数量。

    整表替换是一次赋值，不存在「改了一半」的中间态：失败时上游看到的仍是原表。
    """
    if not pruned:
        return 0
    pool = getattr(request, "func_tool", None)
    if pool is None:
        return 0
    new_set = _new_toolset()
    if new_set is None:  # pragma: no cover - 退化路径：原地裁剪
        pool.tools[:] = kept
    else:
        for tool in kept:
            new_set.add_tool(tool)
        request.func_tool = new_set
    return len(pruned)


def restore(request: Any, tool: Any) -> bool:
    """把先前裁掉的工具原样补回本轮请求（元工具同轮生效用）。

    只在工具确实来自本轮的许可池快照时才会被调用——调用方负责先做许可校验。
    """
    pool = getattr(request, "func_tool", None)
    if pool is None or tool is None:
        return False
    name = getattr(tool, "name", "")
    if not name:
        return False
    for existing in getattr(pool, "tools", []) or []:
        if getattr(existing, "name", "") == name:
            return False
    add_tool = getattr(pool, "add_tool", None)
    if callable(add_tool):
        add_tool(tool)
        return True
    pool.tools.append(tool)  # pragma: no cover
    return True


def withdraw(request: Any, name: str) -> bool:
    """把某个工具从本轮请求里摘掉（取消激活时同轮生效用）。"""
    pool = getattr(request, "func_tool", None)
    tools = getattr(pool, "tools", None)
    if pool is None or not tools:
        return False
    if not any(getattr(tool, "name", "") == name for tool in tools):
        return False
    remove_tool = getattr(pool, "remove_tool", None)
    if callable(remove_tool):
        remove_tool(name)
        return True
    pool.tools[:] = [t for t in tools if getattr(t, "name", "") != name]  # pragma: no cover
    return True


def build_keep_set(
    registry: Any,
    *,
    active_names: frozenset[str],
    lazy_allowed: frozenset[str],
    meta_tools_enabled: bool,
) -> frozenset[str]:
    """计算本轮要保留的本插件工具名，并与许可池取交集。

    与许可池取交集这一步是硬要求：``active_names`` 是会话状态，
    ``lazy_allowed`` 是权限状态，两者是「与」而不是「或」。
    """
    keep: set[str] = set(active_names)
    if meta_tools_enabled:
        keep.update(meta.name for meta in registry.always_active())
    return frozenset(keep & lazy_allowed)
