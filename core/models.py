# -*- coding: utf-8 -*-
"""懒加载工具插件的数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 工具的风险等级。``high`` 的工具不会被预检索自动激活，
#: 只能由模型通过元工具显式激活（见 §十 提示注入 / 误调用应对）。
RISK_NORMAL = "normal"
RISK_HIGH = "high"


@dataclass
class ToolMeta:
    """一个懒加载工具的检索元数据。

    这里只保存「说明书」，真正的可调用对象由 AstrBot 自己的
    ``llm_tools`` 注册表持有——本插件不复制 handler，避免执行路径分叉。
    """

    name: str
    """工具名，必须与注册进 AstrBot 的 ``FunctionTool.name`` 一致。"""

    description: str
    """工具描述，取自函数 docstring 的首段。"""

    module: str
    """定义该工具的模块路径，用于子插件启用/禁用时批量定位。"""

    source: str = "main"
    """来源：``main`` 表示主插件自身，其它值为 sub_plugins/ 下的子插件名。"""

    tags: tuple[str, ...] = ()
    """检索标签，权重最高。"""

    examples: tuple[str, ...] = ()
    """示例说法，用于把用户口语映射到工具。"""

    learned: tuple[str, ...] = ()
    """从实际使用中归纳出来的查询样例（见 :mod:`core.learning`）。

    与 ``examples`` 的区别：``examples`` 是作者**猜**用户会怎么说；
    ``learned`` 是"用户真的这么说、而且模型真的选了这个工具"的**实证**，
    所以检索权重比 ``examples`` 高。
    """

    group: str | None = None
    """工具分组名，检索时按组加权，也便于将来做「一次激活整组」。"""

    ttl_turns: int | None = None
    """覆盖默认存活轮次；``None`` 表示用插件配置的默认值。"""

    ttl_seconds: float | None = None
    """覆盖默认存活秒数；``None`` 表示用插件配置的默认值。"""

    always_active: bool = False
    """常驻工具（三个元工具），永不参与裁剪，也不参与检索排名。"""

    risk: str = RISK_NORMAL
    """风险等级，见 :data:`RISK_HIGH`。"""

    max_result_chars: int | None = None
    """覆盖 ``max_result_chars`` 配置；``None`` 表示用全局默认值。"""

    def to_index_text(self) -> dict[str, str]:
        """返回参与建索引的字段，键名与 :data:`core.retriever.FIELD_WEIGHTS` 对应。"""
        fields = {
            "name": self.name,
            "description": self.description,
            "tags": " ".join(self.tags),
            "examples": " ".join(self.examples),
        }
        if self.learned:
            fields["learned"] = " ".join(self.learned)
        if self.group:
            fields["group"] = self.group
        return fields


@dataclass
class TurnContext:
    """一次 LLM 请求的快照，供元工具在同一轮内补注入使用。

    为什么需要它：``on_llm_request`` 每个用户回合只触发一次，但一次回合里
    模型可能发起多轮工具调用。模型中途用 ``lazy_activate_tool`` 激活的工具，
    只有写回**同一个** ``ProviderRequest.func_tool`` 才能在下一轮 LLM 调用里
    被看到（runner 的 ``_func_tool_for_provider`` 每次调用都实时读取该字段）。
    """

    umo: str
    """会话标识（``event.unified_msg_origin``）。"""

    request: Any
    """本轮 ``ProviderRequest``，元工具补注入时直接改它的 ``func_tool``。"""

    allowed: frozenset[str] = field(default_factory=frozenset)
    """钩子入口处 ``req.func_tool`` 的全量快照，即「上游许可池」。"""

    lazy_allowed: frozenset[str] = field(default_factory=frozenset)
    """许可池中属于本插件的工具名，是所有注入决策的唯一合法来源。"""

    pruned: dict[str, Any] = field(default_factory=dict)
    """本轮被裁掉的工具对象（``name -> FunctionTool``），供元工具原样补回。"""

    query: str = ""
    """本轮用户的原始输入。归纳学习要用它把"这句话"与"被调用的工具"关联起来。"""

    created_at: float = 0.0
    """快照创建时间，用于清理陈旧的会话快照。"""
