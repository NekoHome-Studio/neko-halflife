# -*- coding: utf-8 -*-
"""懒加载工具插件的核心组件。

模块划分：

* :mod:`core.models`      —— 数据结构（工具元数据、单轮上下文快照）
* :mod:`core.registry`    —— ``@lazy_tool`` 装饰器与全量注册表
* :mod:`core.retriever`   —— 关键词/标签本地检索（不调用 LLM）
* :mod:`core.activation`  —— per-UMO 激活表与轮次/时间双过期
* :mod:`core.injector`    —— 许可池快照与 ToolSet 重建（安全关键）
* :mod:`core.subplugins`  —— sub_plugins/ 目录下的私有工具集加载
"""

from __future__ import annotations

__all__ = [
    "activation",
    "injector",
    "models",
    "registry",
    "retriever",
    "subplugins",
]
