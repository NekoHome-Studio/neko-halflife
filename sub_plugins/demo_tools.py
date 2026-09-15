# -*- coding: utf-8 -*-
"""示例子插件：三个纯函数工具。

写法约定（很重要）：

* 子插件里的工具是**普通函数**，不是方法，第一个参数是 ``event``。
  AstrBot 只会把插件实例绑定到模块路径等于插件主模块的 handler 上，
  子插件不满足该条件，写成 ``self`` 会缺少实参。
* 直接用 ``@lazy_tool(...)`` 即可，加载器会把该名字注入模块命名空间，无需 import。
* 描述取 docstring，参数取 ``Args:`` 段，与 ``@filter.llm_tool`` 完全一致。
"""

from __future__ import annotations

from astrbot.api.event import AstrMessageEvent


@lazy_tool(
    name="demo_echo",
    tags=("示例", "回显", "复述", "echo", "demo"),
    examples=("把我的话重复一遍", "原样返回这段文字"),
    ttl_turns=2,
)
async def demo_echo(event: AstrMessageEvent, text: str) -> str:
    """把给定文本原样返回，用于验证懒加载链路是否打通。

    Args:
        text(string): 需要回显的内容
    """
    return f"echo: {text}"


@lazy_tool(
    name="demo_word_count",
    tags=("示例", "字数", "统计", "word count", "demo"),
    examples=("这段文字有多少字", "统计一下字数"),
    group="demo",
)
async def demo_word_count(event: AstrMessageEvent, text: str) -> str:
    """统计给定文本的字符数与词数，演示「多轮里保持激活」的场景。

    Args:
        text(string): 需要统计的文本
    """
    words = len(text.split())
    return f"字符数 {len(text)}，词数 {words}"


@lazy_tool(
    name="demo_dangerous_reset",
    tags=("示例", "危险操作", "重置", "reset", "demo"),
    examples=("重置全部数据",),
    risk="high",
)
async def demo_dangerous_reset(event: AstrMessageEvent, confirm: bool) -> str:
    """演示高风险工具：不会被预检索自动激活，只能由模型显式激活后调用。

    Args:
        confirm(boolean): 是否确认执行
    """
    if not confirm:
        return "已取消：confirm 为 false。"
    return "（示例）高风险操作已执行。真实工具请在这里做真正的校验与幂等保护。"
