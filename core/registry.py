# -*- coding: utf-8 -*-
"""``@lazy_tool`` 装饰器与全量注册表。

本模块承担两件事，顺序不能颠倒：

1. **先按 AstrBot 官方方式注册**（``filter.llm_tool``）。这是路线 A 的前提：
   工具必须真的进入 AstrBot 的全局 ``llm_tools``，per-LLM 请求的 ``func_tool``
   里才会出现它，上游的 persona 工具选择、会话级插件过滤（``_plugin_tool_fix``）、
   工具启用开关（``FunctionTool.active``）才会对它生效。我们随后在
   ``on_llm_request`` 里做的只是「按许可池裁剪」，绝不重新发明权限判定。
2. **再记录检索元数据**。标签、示例、TTL、风险等级这些 AstrBot 不认识的信息
   存在本插件的注册表里，只用于本地检索和激活决策。
"""

from __future__ import annotations

import functools
import inspect
import logging
from contextvars import ContextVar
from typing import Any, Callable, Iterable, Iterator

try:  # 允许在没有 AstrBot 的环境里单独导入本模块做纯逻辑测试
    from .models import RISK_NORMAL, ToolMeta
except ImportError:  # pragma: no cover - AstrBot 直接以模块方式载入 main.py 时
    from core.models import RISK_NORMAL, ToolMeta

logger = logging.getLogger("astrbot_plugin_neko_halflife")

#: 结果裁剪的全局默认值。装饰器在导入期执行，那时还读不到插件配置，
#: 因此这里放一个可变槽位，由插件 ``__init__`` 从配置写入。
RESULT_LIMITS: dict[str, int | None] = {"default": None}

_TRIM_NOTICE = "\n…（结果已裁剪，原文共 {total} 字符）"

#: 子插件加载器在导入子插件模块前设置它，使 ``@lazy_tool`` 能记录来源。
_CURRENT_SOURCE: ContextVar[str] = ContextVar("lazy_tool_source", default="main")


def parse_description(fn: Callable[..., Any]) -> str:
    """从函数 docstring 中取出描述。

    与 AstrBot 的 ``register_llm_tool`` 保持一致：优先用 ``docstring_parser``
    解析出的 description，解析器不可用时退回 docstring 首段。
    """
    raw = inspect.getdoc(fn) or ""
    if not raw:
        return ""
    try:
        import docstring_parser

        parsed = docstring_parser.parse(raw)
        if parsed.description:
            return parsed.description.strip()
    except Exception:  # pragma: no cover - docstring_parser 缺失或解析异常
        pass
    lines: list[str] = []
    for line in raw.splitlines():
        if line.strip().lower().startswith(("args:", "arguments:", "returns:", "yields:")):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _trim(value: Any, meta: ToolMeta) -> Any:
    """按配置裁剪字符串返回值；非字符串原样返回。"""
    if not isinstance(value, str):
        return value
    cap = meta.max_result_chars
    if cap is None:
        cap = RESULT_LIMITS.get("default")
    if not cap or cap <= 0 or len(value) <= cap:
        return value
    return value[:cap] + _TRIM_NOTICE.format(total=len(value))


def _wrap_result(fn: Callable[..., Any], meta: ToolMeta) -> Callable[..., Any]:
    """包一层结果裁剪。

    * 协程版工具裁剪返回的字符串；
    * 异步生成器版工具（用 ``yield`` 直接发消息、返回 ``MessageEventResult``）
    原样透传——裁剪消息段没有意义，也不该动它的生命周期。
    """
    if inspect.isasyncgenfunction(fn):

        @functools.wraps(fn)
        async def agen_wrapper(*args: Any, **kwargs: Any) -> Any:
            async for item in fn(*args, **kwargs):
                yield item

        return agen_wrapper

    @functools.wraps(fn)
    async def coro_wrapper(*args: Any, **kwargs: Any) -> Any:
        return _trim(await fn(*args, **kwargs), meta)

    return coro_wrapper


class ToolRegistry:
    """全量工具注册表。

    注册表是全局单例：所有 bot、所有会话共享同一套工具定义（说明书是全局的），
    会话维度的状态只在 :mod:`core.activation` 里。
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolMeta] = {}
        self._disabled_sources: set[str] = set()
        self._retired_sources: set[str] = set()

    # ---- 写入 ----------------------------------------------------------

    def register(self, meta: ToolMeta) -> None:
        previous = self._tools.get(meta.name)
        if previous is not None:
            if previous.module == meta.module:
                # 同一个模块被重新导入（插件重载、子插件启停）——覆盖即可，不算冲突。
                logger.debug("[neko-halflife] 重载工具元数据：%s", meta.name)
            else:
                logger.warning(
                    "[neko-halflife] 工具名冲突，%s 覆盖了 %s 的同名工具：%s",
                    meta.module,
                    previous.module,
                    meta.name,
                )
        self._tools[meta.name] = meta

    def retire_source(self, source: str) -> int:
        """把一个来源标记为「已退休」：保留工具定义，但保证它们再也不会被注入。

        为什么保留定义而不是删掉：``injector.plan_pruning`` 把「注册表里查不到」
        当成「不是本插件的工具」而**原样保留**。所以删掉定义反而会让工具继续注入。
        保留定义 + 永不进入保留集 = 每轮必被裁掉，这才是安全的兜底。

        用于删除子插件时：万一无法从 AstrBot 全局 ``llm_tools`` 移除，
        也不能让这些工具因为「查不到」而被当成别人的工具照常注入。
        """
        self._retired_sources.add(source)
        self._disabled_sources.add(source)
        return sum(1 for meta in self._tools.values() if meta.source == source)

    def is_source_retired(self, source: str) -> bool:
        return source in self._retired_sources

    def set_source_enabled(self, source: str, enabled: bool) -> None:
        """启用/停用来源。

        启用会**解除退休**：管理员重新上传同名子插件并启用，是明确的授权动作，
        如果继续按住退休标记，工具会被永久裁掉、看起来像「上传了但没生效」。
        """
        if enabled:
            self._disabled_sources.discard(source)
            self._retired_sources.discard(source)
        else:
            self._disabled_sources.add(source)

    def forget_source(self, source: str) -> int:
        """移除某个来源的全部工具元数据，返回移除数量。"""
        doomed = [n for n, m in self._tools.items() if m.source == source]
        for name in doomed:
            self._tools.pop(name, None)
        self._disabled_sources.discard(source)
        self._retired_sources.discard(source)
        return len(doomed)

    # ---- 读取 ----------------------------------------------------------

    def get(self, name: str) -> ToolMeta | None:
        return self._tools.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def all(self) -> tuple[ToolMeta, ...]:
        return tuple(self._tools.values())

    def candidates(self) -> tuple[ToolMeta, ...]:
        """参与检索与注入的工具：排除已禁用的子插件、排除常驻元工具。"""
        return tuple(
            meta
            for meta in self._tools.values()
            if meta.source not in self._disabled_sources and not meta.always_active
        )

    def always_active(self) -> tuple[ToolMeta, ...]:
        """常驻工具（元工具）。禁用子插件不影响它们。"""
        return tuple(meta for meta in self._tools.values() if meta.always_active)

    def sources(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for meta in self._tools.values():
            if meta.source != "main":
                seen.setdefault(meta.source, None)
        return tuple(seen)

    def is_source_enabled(self, source: str) -> bool:
        return source not in self._disabled_sources

    def clear(self) -> None:
        self._tools.clear()
        self._disabled_sources.clear()
        self._retired_sources.clear()


#: 全局注册表单例。
REGISTRY = ToolRegistry()


def lazy_tool(
    name: str | None = None,
    *,
    tags: Iterable[str] = (),
    examples: Iterable[str] = (),
    group: str | None = None,
    ttl_turns: int | None = None,
    ttl_seconds: float | None = None,
    always_active: bool = False,
    risk: str = RISK_NORMAL,
    max_result_chars: int | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """把一个普通 AstrBot LLM 工具标记为「懒加载」。

    用法与 ``@filter.llm_tool`` 完全一致（描述取 docstring、参数取 ``Args:``
    段），额外多出检索所需的标签与过期策略::

        @lazy_tool(name="get_weather", tags=("天气", "weather"), ttl_turns=5)
        async def get_weather(self, event: AstrMessageEvent, city: str) -> str:
            '''查询指定城市的天气。

            Args:
                city(string): 城市名
            '''
            ...

    Args:
        name: 工具名，缺省用函数名。
        tags: 检索标签，权重最高，建议写用户可能说的词。
        examples: 示例说法，把口语映射到工具。
        group: 工具分组名，按组加权。
        ttl_turns: 覆盖默认存活轮次。
        ttl_seconds: 覆盖默认存活秒数。
        always_active: 常驻不裁剪（元工具用）。
        risk: ``"normal"`` 或 ``"high"``；高风险工具不会被预检索自动激活。
        max_result_chars: 覆盖全局结果裁剪长度。
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        meta = ToolMeta(
            name=name or fn.__name__,
            description=parse_description(fn),
            module=getattr(fn, "__module__", "") or "",
            source=_CURRENT_SOURCE.get(),
            tags=tuple(str(t) for t in tags),
            examples=tuple(str(e) for e in examples),
            group=group,
            ttl_turns=ttl_turns,
            ttl_seconds=ttl_seconds,
            always_active=always_active,
            risk=risk,
            max_result_chars=max_result_chars,
        )

        wrapped = _wrap_result(fn, meta)

        # 延迟导入：让本模块在没有 AstrBot 的环境里也能被导入（纯逻辑测试用）。
        from astrbot.api.event import filter as event_filter

        registered = event_filter.llm_tool(name=name)(wrapped)
        REGISTRY.register(meta)
        logger.debug("[neko-halflife] 已注册懒加载工具：%s", meta.name)
        return registered

    return decorator


def current_source() -> str:
    """返回当前正在导入的来源名（供子插件加载器与调试使用）。"""
    return _CURRENT_SOURCE.get()


def push_source(source: str) -> Any:
    """设置当前来源，返回可用于 :func:`pop_source` 的 token。"""
    return _CURRENT_SOURCE.set(source)


def pop_source(token: Any) -> None:
    _CURRENT_SOURCE.reset(token)


def iter_tools() -> Iterator[ToolMeta]:
    """遍历全量注册表。"""
    return iter(REGISTRY.all())
