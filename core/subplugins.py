# -*- coding: utf-8 -*-
"""sub_plugins/ 下的私有工具集加载。

子插件不是 AstrBot 插件：不需要继承 ``Star``、不需要 ``metadata.yaml``、
不会出现在 AstrBot 的插件管理面板里。它们只是**工具集**，由本插件扫描、导入，
并把其中的 ``@lazy_tool`` 收进自己的注册表。

两种形态都支持::

    sub_plugins/
    ├── demo_tools.py            # 单文件
    └── weather/                 # 包
        └── __init__.py

约定：子插件里的工具写成**普通函数**（第一个参数是 ``event``），不要写成需要
``self`` 的方法。原因是 AstrBot 的 ``star_manager`` 只在 handler 的 ``__module__``
等于插件主模块路径时才会把插件实例 ``functools.partial`` 绑定进去，子插件的模块
路径不满足该条件，写成方法会缺少 ``self``。加载器会把 ``lazy_tool`` 注入到子插件
的模块命名空间里，所以子插件里直接写 ``@lazy_tool(...)`` 即可，无需 import。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Iterable

try:
    from .registry import REGISTRY, ToolRegistry, lazy_tool, pop_source, push_source
except ImportError:  # pragma: no cover
    from core.registry import REGISTRY, ToolRegistry, lazy_tool, pop_source, push_source

logger = logging.getLogger("astrbot_plugin_lazy_tools")

#: 子插件目录名，相对插件根目录。
SUBDIR_NAME = "sub_plugins"


def _package_prefix() -> str:
    """推导本插件包的完整模块名，用于给子插件模块起名。

    例：``data.plugins.astrbot_plugin_lazy_tools.core`` -> ``data.plugins.astrbot_plugin_lazy_tools``
    """
    pkg = __package__ or ""
    if "." in pkg:
        return pkg.rsplit(".", 1)[0]
    return pkg or "astrbot_plugin_lazy_tools"


class SubPluginLoader:
    """扫描并导入 ``sub_plugins/``。"""

    def __init__(self, plugin_root: Path, registry: ToolRegistry | None = None) -> None:
        self.root = Path(plugin_root) / SUBDIR_NAME
        self.registry = registry or REGISTRY
        self.errors: dict[str, str] = {}
        self.loaded: set[str] = set()

    # ---- 发现 ----------------------------------------------------------

    def discover(self) -> list[str]:
        """列出所有候选子插件名（保持稳定排序）。"""
        if not self.root.is_dir():
            return []
        names: list[str] = []
        for entry in sorted(self.root.iterdir(), key=lambda p: p.name):
            if entry.name.startswith(("_", ".")):
                continue
            if entry.is_file() and entry.suffix == ".py":
                names.append(entry.stem)
            elif entry.is_dir() and (entry / "__init__.py").is_file():
                names.append(entry.name)
        return names

    def entry_path(self, name: str) -> Path | None:
        single = self.root / f"{name}.py"
        if single.is_file():
            return single
        package = self.root / name / "__init__.py"
        if package.is_file():
            return package
        return None

    # ---- 加载 ----------------------------------------------------------

    def load(self, name: str) -> bool:
        """导入单个子插件。成功返回 ``True``，失败记录到 :attr:`errors`。"""
        path = self.entry_path(name)
        if path is None:
            self.errors[name] = "未找到入口文件"
            return False
        module_name = f"{_package_prefix()}.{SUBDIR_NAME}.{name}"
        try:
            if path.parent.name == name:  # 包形态
                spec = importlib.util.spec_from_file_location(
                    module_name,
                    path,
                    submodule_search_locations=[str(path.parent)],
                )
            else:  # 单文件形态
                spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                self.errors[name] = "无法创建模块 spec"
                return False
            module = importlib.util.module_from_spec(spec)
            # 让子插件里可以直接用 @lazy_tool，而不必关心自己是怎么被导入的。
            module.__dict__.setdefault("lazy_tool", lazy_tool)
            module.__dict__.setdefault("lazy_meta", lazy_tool)
            # 先放进 sys.modules：模块内相对导入与 dataclass/pickle 都依赖它。
            sys.modules[module_name] = module
            token = push_source(name)
            try:
                spec.loader.exec_module(module)
            finally:
                pop_source(token)
        except Exception as exc:  # noqa: BLE001 - 子插件出错不能拖垮主插件
            self.errors[name] = f"{type(exc).__name__}: {exc}"
            logger.error("[lazy-tools] 子插件 %s 加载失败：%s", name, exc, exc_info=True)
            return False
        self.errors.pop(name, None)
        self.loaded.add(name)
        logger.info("[lazy-tools] 子插件 %s 已加载（%s）", name, path.name)
        return True

    def load_all(self, only: Iterable[str] | None = None) -> dict[str, bool]:
        """加载全部（或指定）子插件，并同步启用状态。

        Args:
            only: ``sub_plugins_enabled`` 配置；空表示全部启用。

        Returns:
            ``name -> 是否加载成功``。
        """
        wanted = {str(n).strip() for n in (only or ()) if str(n).strip()}
        result: dict[str, bool] = {}
        for name in self.discover():
            ok = self.load(name)
            result[name] = ok
            if not ok:
                continue
            enabled = (not wanted) or (name in wanted)
            self.registry.set_source_enabled(name, enabled)
        # 注册表里存在但目录已消失的子插件：直接清掉，避免残留不可见工具。
        for stale in self.registry.sources():
            if stale not in result:
                self.registry.forget_source(stale)
                logger.info("[lazy-tools] 子插件 %s 已移除", stale)
        return result

    def set_enabled(self, name: str, enabled: bool) -> bool:
        """运行时启停子插件；返回是否发生了状态变化。"""
        if name not in self.registry.sources() and name not in self.loaded:
            return False
        self.registry.set_source_enabled(name, enabled)
        return True
