# -*- coding: utf-8 -*-
"""把已安装的 AstrBot 插件源码导入为子插件——可移植性分析与复制。

**为什么必须做预检**：一个普通 AstrBot 插件搬进 ``sub_plugins/`` 之后，
大多数情况下不是"不工作"，而是**更糟**：

* 它的工具是 ``Star`` 子类上的方法，需要 ``self``。我们的加载器只导入模块、
  不实例化 ``Star``，所以这些工具即使注册上了，一调用就会缺参报错。
* 更危险的是 ``@filter.llm_tool`` 这类装饰器**在导入时就有全局副作用**——
  它会真的把工具写进 AstrBot 的全局 ``llm_tools``，且 ``handler_module_path``
  指向我们插件的子目录。而这些工具**不在**本插件的注册表里，于是裁剪逻辑会把
  「查不到」当成「不是本插件的工具」而原样保留 → **每轮都被注入**，且调用必失败。

所以这里的策略是：**先在磁盘上静态扫描，明确拒绝**；复制之后还要做一次运行期
校验（见 ``LazyToolsPlugin`` 的导入流程），发现脏注册就回滚。

纯静态扫描是启发式的（正则，不做 AST），所以它的结论只用于**拒绝**，
不用于担保"一定能跑"——能不能跑最终由导入期校验说话。
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

#: 复制时的排除项。``__pycache__`` 之类是构建产物，``.git`` 是版本库。
_EXCLUDED_DIRS = {"__pycache__", ".git", ".github", ".venv", "venv", ".idea", ".vscode"}
_EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".pyd"}

#: 规模上限，避免把一个巨大的插件误搬进来。
MAX_FILES = 800
MAX_BYTES = 16 * 1024 * 1024

#: 本插件自己的工具装饰器——子插件必须用它才能被注册表收录。
_LAZY_DECOR_RE = re.compile(r"@\s*lazy_tool\b")
_LAZY_NAME_RE = re.compile(
    r"@\s*lazy_tool\(\s*[^)]*?name\s*=\s*[\"']([^\"']+)[\"']", re.S
)
_DEF_AFTER_DECOR_RE = re.compile(
    r"@\s*lazy_tool\b[^\n]*\n(?:\s*(?:#[^\n]*)?\n)*\s*(?:async\s+)?def\s+([A-Za-z_]\w*)"
)

#: AstrBot 自己的工具注册装饰器：导入即有全局副作用，且期望 ``self``。
_ASTRBOT_TOOL_RE = re.compile(
    r"@\s*(?:filter\.|event_filter\.)?(?:register_llm_tool|llm_tool)\b"
)
_ASTRBOT_DIRECT_RE = re.compile(r"\bregister_llm_tool\s*\(")

#: ``class Xxx(Star)`` 或 ``class Xxx(filter.Star)`` 之类
_STAR_CLASS_RE = re.compile(r"class\s+\w+\s*\([^)]*\bStar\b[^)]*\)")

#: 我们自己插件的名字——不允许把本插件导入成自己的子插件。
SELF_NAMES = {"astrbot_plugin_neko_halflife"}


@dataclass
class ImportAnalysis:
    """一个候选插件的可移植性分析结果。"""

    dir_name: str
    portable: bool = False
    reasons: list[str] = field(default_factory=list)
    """阻断原因。非空即 ``portable=False``。"""

    warnings: list[str] = field(default_factory=list)
    lazy_tool_names: list[str] = field(default_factory=list)
    astrbot_tool_hits: list[str] = field(default_factory=list)
    """命中的 AstrBot 工具装饰器所在文件（有就是硬拒绝）。"""

    has_star_class: bool = False
    tool_modules: list[str] = field(default_factory=list)
    """含 ``@lazy_tool`` 的模块路径（相对包根，点分）。生成的 __init__ 会导入它们。"""

    py_files: list[str] = field(default_factory=list)
    total_files: int = 0
    total_bytes: int = 0

    def to_dict(self) -> dict:
        return {
            "dir_name": self.dir_name,
            "portable": self.portable,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "lazy_tool_names": list(self.lazy_tool_names),
            "astrbot_tool_hits": list(self.astrbot_tool_hits),
            "has_star_class": self.has_star_class,
            "tool_modules": list(self.tool_modules),
            "total_files": self.total_files,
            "total_bytes": self.total_bytes,
        }


def _iter_source_files(plugin_dir: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(plugin_dir.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _EXCLUDED_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in _EXCLUDED_SUFFIXES:
            continue
        files.append(path)
    return files


def _module_dotted(relative: PurePosixPath) -> str:
    """把相对路径变成点分模块名（去掉 ``.py``，去掉 ``__init__``）。"""
    parts = list(relative.parts)
    stem = parts[-1]
    if stem.endswith(".py"):
        stem = stem[:-3]
    parts = parts[:-1] + ([stem] if stem and stem != "__init__" else [])
    return ".".join(parts)


def analyze_plugin_dir(plugin_dir: Path) -> ImportAnalysis:
    """静态分析一个已安装插件目录，判断能否当子插件导入。"""
    plugin_dir = Path(plugin_dir)
    analysis = ImportAnalysis(dir_name=plugin_dir.name)

    if not plugin_dir.is_dir():
        analysis.reasons.append("插件目录不存在。")
        return analysis

    all_files = _iter_source_files(plugin_dir)
    if not all_files:
        analysis.reasons.append("插件目录里没有任何文件。")
        return analysis

    analysis.total_files = len(all_files)
    analysis.total_bytes = sum(path.stat().st_size for path in all_files)
    if analysis.total_files > MAX_FILES:
        analysis.reasons.append(
            f"文件数过多（{analysis.total_files} > {MAX_FILES}），拒绝导入。"
        )
    if analysis.total_bytes > MAX_BYTES:
        analysis.reasons.append(
            f"体积过大（{analysis.total_bytes} 字节 > {MAX_BYTES}），拒绝导入。"
        )

    py_files = [path for path in all_files if path.suffix == ".py"]
    analysis.py_files = [
        path.relative_to(plugin_dir).as_posix() for path in py_files
    ]

    lazy_hits = 0
    for path in py_files:
        relative = path.relative_to(plugin_dir)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            analysis.warnings.append(f"读取 {relative.as_posix()} 失败：{exc}")
            continue

        if _STAR_CLASS_RE.search(text):
            analysis.has_star_class = True

        if _ASTRBOT_TOOL_RE.search(text) or _ASTRBOT_DIRECT_RE.search(text):
            analysis.astrbot_tool_hits.append(relative.as_posix())

        for match in _LAZY_NAME_RE.finditer(text):
            name = match.group(1).strip()
            if name and name not in analysis.lazy_tool_names:
                analysis.lazy_tool_names.append(name)
        for match in _DEF_AFTER_DECOR_RE.finditer(text):
            name = match.group(1).strip()
            if name and name not in analysis.lazy_tool_names:
                analysis.lazy_tool_names.append(name)

        count = len(_LAZY_DECOR_RE.findall(text))
        if count:
            lazy_hits += count
            dotted = _module_dotted(relative)
            if dotted and dotted not in analysis.tool_modules:
                analysis.tool_modules.append(dotted)

    # ---- 判定 ----
    if plugin_dir.name in SELF_NAMES:
        analysis.reasons.append("这是本插件自己，不能导入为自身的子插件。")

    if analysis.astrbot_tool_hits:
        analysis.reasons.append(
            "含 AstrBot 自带的工具装饰器（如 @filter.llm_tool）："
            f"{', '.join(analysis.astrbot_tool_hits[:3])}。"
            "这类装饰器在导入时就会把工具注册进 AstrBot 全局表，"
            "而它们的 handler 需要 self（本插件的加载器不会实例化 Star 类），"
            "结果是「每轮都被注入但一调用就报错」。"
        )

    if not lazy_hits:
        analysis.reasons.append(
            "没有找到任何 @lazy_tool。子插件的工具必须用本插件的 @lazy_tool 装饰"
            "（写成模块级普通函数，第一个参数是 event），否则导入后不会注册任何工具。"
        )

    if analysis.has_star_class and lazy_hits:
        analysis.warnings.append(
            "插件里定义了 Star 子类。本插件的加载器不会实例化它，"
            "该类里的东西（initialize、命令、监听器等）不会被激活；"
            "只有模块级、用 @lazy_tool 装饰的普通函数会生效。"
        )

    analysis.portable = not analysis.reasons
    return analysis


def build_entry_source(tool_modules: list[str]) -> str:
    """生成包入口 ``__init__.py``：导入全部含 @lazy_tool 的模块以触发注册。

    只导入、不复制代码——``@lazy_tool`` 在导入期完成注册，所以"导入"就是全部工作。
    """
    lines = [
        "# -*- coding: utf-8 -*-",
        '"""由 astrbot_plugin_neko_halflife 生成：从已装插件导入的子插件包。',
        "",
        "本文件只负责导入含 @lazy_tool 的模块，让装饰器完成工具注册。",
        "重新导入请通过 WebUI 面板操作，不要手工改这里。",
        '"""',
        "",
        "from __future__ import annotations",
        "",
    ]
    if not tool_modules:
        lines.append("# 没有检测到含 @lazy_tool 的模块。")
    for dotted in tool_modules:
        parts = dotted.split(".")
        if len(parts) == 1:
            lines.append(f"from . import {parts[0]}  # noqa: F401")
        else:
            parent = ".".join(parts[:-1])
            lines.append(f"from .{parent} import {parts[-1]}  # noqa: F401")
    lines.append("")
    return "\n".join(lines)


def copy_plugin_tree(src: Path, dest: Path, *, overwrite: bool = False) -> int:
    """把插件目录的文件复制到 ``dest``，并写入生成的 ``__init__.py``。

    返回复制的文件数。``overwrite=False`` 时目标已存在直接抛错。
    """
    src = Path(src)
    dest = Path(dest)
    if dest.exists():
        if not overwrite:
            raise FileExistsError(f"{dest.name} 已存在。")
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    copied = 0
    for path in _iter_source_files(src):
        relative = path.relative_to(src)
        # 不复制它自己的 metadata.yaml：子插件不是 AstrBot 插件，
        # 留着只会让人误以为它需要被 AstrBot 扫描。
        if relative.as_posix() == "metadata.yaml":
            continue
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        copied += 1
    return copied


def write_entry(dest: Path, tool_modules: list[str]) -> Path:
    entry = Path(dest) / "__init__.py"
    entry.write_text(build_entry_source(tool_modules), encoding="utf-8")
    return entry
