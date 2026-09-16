# -*- coding: utf-8 -*-
"""把"外来插件"当作子插件宿主起来。

**为什么需要这一层**：AstrBot 只把 `Star` 实例绑定给**模块路径精确等于插件主模块**
的 handler：

* ``star_manager`` 绑定工具的条件是 ``ft.handler.__module__ == metadata.module_path``
* ``star_handlers_registry.get_handlers_by_module_name`` 也是**精确匹配**

所以搬进 ``sub_plugins/<name>/main.py`` 的东西，AstrBot 永远不会去绑 ``self``。
那些工具虽然会被 ``@filter.llm_tool`` 注册进全局表，但调用时 ``self`` 缺失，
一调就报错。想让它真的能用，只能由我们自己扮演 ``star_manager`` 的角色：

1. 在已导入的模块里找出 ``Star`` 子类；
2. 按 AstrBot 的约定实例化它（``cls(context=..., config=...)``，``TypeError`` 时
   退回 ``cls(context=...)``——与 ``star_manager`` 一致）；
3. 把该模块下的**工具**与**事件处理器**（命令、钩子）都 ``functools.partial``
   到这个实例上。

这一步做完，外来插件才是"真的被搬进来并可用"，同时也才有条件把它纳入懒加载。
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from .models import ToolMeta
except ImportError:  # pragma: no cover
    from core.models import ToolMeta

logger = logging.getLogger("astrbot_plugin_neko_halflife")


@dataclass
class HostResult:
    """宿主一个外来插件的结果。"""

    instance: Any = None
    class_name: str = ""
    tool_metas: list[ToolMeta] = field(default_factory=list)
    bound_tools: list[str] = field(default_factory=list)
    bound_handlers: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# ----------------------------------------------------------------------
# AstrBot 内部对象的容错访问
# ----------------------------------------------------------------------


def _registry() -> Any | None:
    try:
        from astrbot.core.star.star_handler import star_handlers_registry

        return star_handlers_registry
    except Exception as exc:  # noqa: BLE001
        logger.warning("[neko-halflife] 取 star_handlers_registry 失败：%s", exc)
        return None


def _add_func_table() -> Any | None:
    try:
        from astrbot.core.provider.register import llm_tools

        return llm_tools
    except Exception as exc:  # noqa: BLE001
        logger.warning("[neko-halflife] 取 llm_tools 失败：%s", exc)
        return None


def _star_base() -> Any | None:
    try:
        from astrbot.api.star import Star

        return Star
    except Exception:  # noqa: BLE001
        try:
            from astrbot.core.star.base import Star  # type: ignore

            return Star
        except Exception:  # noqa: BLE001
            return None


def _iter_concrete(tool: Any) -> list[Any]:
    """展开 HandoffTool 之类的包装，与 star_manager._iter_concrete_llm_tools 对齐。"""
    inner = getattr(tool, "agent", None)
    if inner is not None and hasattr(tool, "agent") and hasattr(inner, "tools"):
        return [item for item in (inner.tools or []) if hasattr(item, "name")]
    return [tool]


def _tool_module_path(tool: Any) -> str:
    raw = tool.handler
    if isinstance(raw, functools.partial):
        raw = raw.func
    return str(getattr(tool, "handler_module_path", "") or getattr(raw, "__module__", "") or "")


# ----------------------------------------------------------------------
# 查找与实例化
# ----------------------------------------------------------------------


def find_star_classes(module_prefix: str) -> list[type]:
    """在已导入的、位于 ``module_prefix`` 下的模块里找 Star 子类。"""
    base = _star_base()
    if base is None:
        return []
    found: list[type] = []
    for name, module in list(sys.modules.items()):
        if not name.startswith(module_prefix) or module is None:
            continue
        for value in vars(module).values():
            if not inspect.isclass(value) or not issubclass(value, base):
                continue
            if value is base:
                continue
            if value.__module__ != name:
                continue  # 只认在本模块里定义的类，避免把导入进来的基类算进去
            if value not in found:
                found.append(value)
    return found


def build_config(plugin_dir: Path, config_dir_name: str) -> Any | None:
    """按 ``_conf_schema.json`` 构造插件配置；没有 schema 就返回 ``None``。

    配置路径沿用**原插件目录名**，所以宿主的插件读到的还是用户原来的那份配置。
    """
    schema_path = Path(plugin_dir) / "_conf_schema.json"
    if not schema_path.is_file():
        return None
    try:
        from astrbot.core.config.astrbot_config import AstrBotConfig
        from astrbot.core.utils.astrbot_path import get_astrbot_config_path

        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        if not isinstance(schema, dict):
            return None
        return AstrBotConfig(
            config_path=str(
                Path(get_astrbot_config_path()) / f"{config_dir_name}_config.json"
            ),
            schema=schema,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[neko-halflife] 构造插件配置失败（将不带 config 实例化）：%s", exc)
        return None


def instantiate(cls: type, context: Any, config: Any | None) -> Any:
    """按 AstrBot 的约定实例化：``(context, config)``，``TypeError`` 时退回 ``(context)``。"""
    if config is not None:
        try:
            return cls(context=context, config=config)
        except TypeError:
            pass
    return cls(context=context)


# ----------------------------------------------------------------------
# 绑定 / 解绑
# ----------------------------------------------------------------------


def _bind(target: Any, instance: Any) -> bool:
    """把 ``target.handler`` 变成 ``partial(raw, instance)``；已经是本实例则跳过。"""
    current = getattr(target, "handler", None)
    if current is None:
        return False
    if isinstance(current, functools.partial) and current.args and current.args[0] is instance:
        return False
    raw = current.func if isinstance(current, functools.partial) else current
    target.handler = functools.partial(raw, instance)
    return True


def bind_tools(instance: Any, module_prefix: str) -> list[str]:
    """把该模块下的 LLM 工具绑定到实例上，返回绑定的工具名。"""
    table = _add_func_table()
    if table is None:
        return []
    bound: list[str] = []
    for tool in list(getattr(table, "func_list", []) or []):
        for concrete in _iter_concrete(tool):
            path = _tool_module_path(concrete)
            if not path.startswith(module_prefix):
                continue
            if getattr(concrete, "handler", None) is None:
                continue
            if _bind(concrete, instance):
                bound.append(str(getattr(concrete, "name", "")))
            if not getattr(concrete, "handler_module_path", None):
                concrete.handler_module_path = path
    return [name for name in bound if name]


def bind_event_handlers(instance: Any, module_prefix: str) -> list[str]:
    """把该模块下的事件处理器（命令、钩子、监听器）绑定到实例上。

    这一步**必须做**：这些处理器在导入模块时就已注册进全局表，并且会因为模块路径
    落在本插件前缀下而被视为"本插件的处理器"，迟早会被调用；不绑 ``self`` 就会报错。
    """
    registry = _registry()
    if registry is None:
        return []
    bound: list[str] = []
    for handler in list(getattr(registry, "_handlers", []) or []):
        if not str(getattr(handler, "handler_module_path", "") or "").startswith(
            module_prefix
        ):
            continue
        if _bind(handler, instance):
            bound.append(str(getattr(handler, "handler_full_name", "")))
    return [name for name in bound if name]


def collect_tool_metas(module_prefix: str, source: str) -> list[ToolMeta]:
    """为该模块下的工具生成检索元数据，使它们能被本插件按需注入。

    描述直接取自工具自身的 ``description``（即原插件 docstring 的首段），
    所以检索质量取决于原插件写得怎么样——这正是"接管"的代价，也说明
    给工具补 ``tags`` 的价值。
    """
    table = _add_func_table()
    if table is None:
        return []
    metas: list[ToolMeta] = []
    for tool in list(getattr(table, "func_list", []) or []):
        for concrete in _iter_concrete(tool):
            path = _tool_module_path(concrete)
            if not path.startswith(module_prefix):
                continue
            name = str(getattr(concrete, "name", "") or "")
            if not name:
                continue
            metas.append(
                ToolMeta(
                    name=name,
                    description=str(getattr(concrete, "description", "") or ""),
                    module=path,
                    source=source,
                )
            )
    return metas


def unhost_module(module_prefix: str) -> int:
    """解绑并清理某模块留下的一切：事件处理器、工具、以及 ``sys.modules`` 里的模块。

    回滚用。移除事件处理器需要动 ``star_handlers_registry`` 的内部结构——它没有
    公开的删除接口——所以每一步都做了容错。
    """
    removed = 0
    registry = _registry()
    if registry is not None:
        live = getattr(registry, "_handlers", None)
        if isinstance(live, list):
            # 注意：必须从**真正的注册表列表**里删，不能删 list(...) 的副本
            doomed = [
                handler
                for handler in list(live)
                if str(getattr(handler, "handler_module_path", "") or "").startswith(
                    module_prefix
                )
            ]
            for handler in doomed:
                try:
                    live.remove(handler)
                    handler_map = getattr(registry, "star_handlers_map", None)
                    if isinstance(handler_map, dict):
                        handler_map.pop(getattr(handler, "handler_full_name", ""), None)
                    removed += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[neko-halflife] 移除处理器失败：%s", exc)

    table = _add_func_table()
    if table is not None:
        for tool in list(getattr(table, "func_list", []) or []):
            for concrete in _iter_concrete(tool):
                if _tool_module_path(concrete).startswith(module_prefix):
                    name = str(getattr(concrete, "name", "") or "")
                    if name:
                        try:
                            table.remove_func(name)
                            removed += 1
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "[neko-halflife] 移除工具 %s 失败：%s", name, exc
                            )

    for name in [
        key
        for key in list(sys.modules)
        if key == module_prefix or key.startswith(f"{module_prefix}.")
    ]:
        sys.modules.pop(name, None)
    return removed


def host_module(
    module_prefix: str,
    plugin_dir: Path,
    config_dir_name: str,
    context: Any,
) -> HostResult:
    """把已导入的外来插件模块接管为可用状态。"""
    result = HostResult()

    classes = find_star_classes(module_prefix)
    if not classes:
        result.errors.append(
            "在导入的模块里没有找到 Star 子类——无法确定要把工具绑定到哪个实例。"
        )
        return result
    # 取"定义在最外层模块"的那个类作为插件主类
    classes.sort(key=lambda cls: cls.__module__.count("."))
    cls = classes[0]
    result.class_name = cls.__name__
    if len(classes) > 1:
        result.warnings.append(
            f"发现 {len(classes)} 个 Star 子类，使用 {cls.__name__}"
            f"（{cls.__module__}），其余不会实例化。"
        )

    config = build_config(plugin_dir, config_dir_name)
    try:
        result.instance = instantiate(cls, context, config)
    except Exception as exc:  # noqa: BLE001 - 外来构造函数什么都可能抛
        result.errors.append(f"实例化 {cls.__name__} 失败：{type(exc).__name__}: {exc}")
        return result

    try:
        result.bound_handlers = bind_event_handlers(result.instance, module_prefix)
        result.bound_tools = bind_tools(result.instance, module_prefix)
        result.tool_metas = collect_tool_metas(module_prefix, source=config_dir_name)
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"绑定失败：{type(exc).__name__}: {exc}")
        return result

    if not result.tool_metas and not result.bound_handlers:
        result.warnings.append(
            "该插件没有注册任何工具或事件处理器，导入后不会有任何效果。"
        )
    return result
