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

import json
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

#: AstrBot 自己的工具注册装饰器：导入时有全局副作用，需要宿主模式接管。
_ASTRBOT_TOOL_RE = re.compile(
    r"@\s*(?:filter\.|event_filter\.)?(?:register_llm_tool|llm_tool)\b"
)
_ASTRBOT_DIRECT_RE = re.compile(r"\bregister_llm_tool\s*\(")

#: 任意 AstrBot filter 装饰器（命令、钩子、监听器……）。它们同样在导入期注册，
#: 因此这些模块必须被导入，导入后也必须绑 self。
_ASTRBOT_ANY_DECOR_RE = re.compile(r"@\s*(?:filter|event_filter)\.\w+")

#: ``class Xxx(Star)`` 或 ``class Xxx(filter.Star)`` 之类
_STAR_CLASS_RE = re.compile(r"class\s+\w+\s*\([^)]*\bStar\b[^)]*\)")

#: 我们自己插件的名字——不允许把本插件导入成自己的子插件。
SELF_NAMES = {"astrbot_plugin_neko_halflife"}


@dataclass
class ImportAnalysis:
    """一个候选插件的可移植性分析结果。"""

    dir_name: str
    portable: bool = False
    mode: str = "rejected"
    """``native``：用本插件 @lazy_tool 写的工具集，直接加载。
    ``hosted``：用 AstrBot 装饰器的常规插件，由本插件实例化 Star 类并绑定。
    ``rejected``：无法导入。"""

    reasons: list[str] = field(default_factory=list)
    """阻断原因。非空即 ``portable=False``。"""

    warnings: list[str] = field(default_factory=list)
    lazy_tool_names: list[str] = field(default_factory=list)
    astrbot_tool_hits: list[str] = field(default_factory=list)
    """命中 AstrBot 工具装饰器（如 @filter.llm_tool）的文件。"""

    astrbot_decor_hits: list[str] = field(default_factory=list)
    """命中任意 AstrBot filter 装饰器（命令/钩子等）的文件。"""

    has_star_class: bool = False
    import_modules: list[str] = field(default_factory=list)
    """生成的包入口需要导入的模块（点分，相对包根）。"""

    py_files: list[str] = field(default_factory=list)
    total_files: int = 0
    total_bytes: int = 0

    def to_dict(self) -> dict:
        return {
            "dir_name": self.dir_name,
            "portable": self.portable,
            "mode": self.mode,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "lazy_tool_names": list(self.lazy_tool_names),
            "astrbot_tool_hits": list(self.astrbot_tool_hits),
            "astrbot_decor_hits": list(self.astrbot_decor_hits),
            "has_star_class": self.has_star_class,
            "import_modules": list(self.import_modules),
            "total_files": self.total_files,
            "total_bytes": self.total_bytes,
        }

    @property
    def tool_modules(self) -> list[str]:
        """兼容旧名字：生成的入口要导入的模块。"""
        return self.import_modules


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
    module_order: list[str] = []

    def remember(dotted: str) -> None:
        if dotted and dotted not in module_order:
            module_order.append(dotted)

    for path in py_files:
        relative = path.relative_to(plugin_dir)
        dotted = _module_dotted(relative)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            analysis.warnings.append(f"读取 {relative.as_posix()} 失败：{exc}")
            continue

        star_here = bool(_STAR_CLASS_RE.search(text))
        if star_here:
            analysis.has_star_class = True

        if _ASTRBOT_TOOL_RE.search(text) or _ASTRBOT_DIRECT_RE.search(text):
            analysis.astrbot_tool_hits.append(relative.as_posix())
        if _ASTRBOT_ANY_DECOR_RE.search(text):
            analysis.astrbot_decor_hits.append(relative.as_posix())

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
            remember(dotted)

        # 宿主模式要导入"定义了 Star 类"或"用了 AstrBot 装饰器"的模块，
        # 否则那些装饰器不会执行、工具与命令都不会注册。
        if star_here or _ASTRBOT_ANY_DECOR_RE.search(text):
            remember(dotted)

    analysis.import_modules = module_order

    # ---- 判定 ----
    if plugin_dir.name in SELF_NAMES:
        analysis.reasons.append("这是本插件自己，不能导入为自身的子插件。")

    if lazy_hits:
        analysis.mode = "native"
    elif analysis.has_star_class:
        analysis.mode = "hosted"
    else:
        analysis.mode = "rejected"
        analysis.reasons.append(
            "既没有用 @lazy_tool 写模块级工具，也没有 Star 子类可供实例化 —— "
            "导入后无法注册任何东西。"
        )

    if analysis.mode == "native":
        if analysis.astrbot_tool_hits:
            analysis.warnings.append(
                "同时检测到 AstrBot 自带的工具装饰器"
                f"（{', '.join(analysis.astrbot_tool_hits[:3])}）。本插件把它们当作"
                "普通工具代码导入，这些装饰器会在导入期把它们注册进 AstrBot 全局表，"
                "但不会有人给它们绑 self —— 调用会失败。建议改用 @lazy_tool 重写。"
            )
        if analysis.has_star_class:
            analysis.warnings.append(
                "插件里定义了 Star 子类。native 模式不会实例化它，"
                "该类里的东西（initialize、命令、监听器等）不会被激活；"
                "只有模块级、用 @lazy_tool 装饰的普通函数会生效。"
            )
    else:  # hosted
        analysis.warnings.append(
            "宿主模式：本插件会自己实例化该 Star 子类（执行它的 __init__），"
            "并把工具与事件处理器绑定到该实例上。它会以子插件身份运行，"
            "与 AstrBot 原生加载互不影响；若原插件仍在启用，两者会各自持有一个实例。"
        )
        if not analysis.astrbot_tool_hits:
            analysis.warnings.append(
                "没有检测到 LLM 工具装饰器：导入后可能只有命令/钩子生效，"
                "不会有可懒加载的工具。"
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


#: 宿主标记文件名。放在子插件目录里，记录"这个包需要宿主模式"，
#: 这样 AstrBot 重启后 `load_all()` 重新导入它时还能续接宿主，
#: 而不是变成"装饰器注册了工具、却没人实例化 Star 类"的坏状态。
HOST_MARKER = ".neko-host.json"


def write_host_marker(dest: Path, *, mode: str, source_dir: str) -> Path:
    path = Path(dest) / HOST_MARKER
    path.write_text(
        json.dumps(
            {"mode": mode, "source_dir": source_dir, "hosted_by": "astrbot_plugin_neko_halflife"},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def read_host_marker(dest: Path) -> dict | None:
    path = Path(dest) / HOST_MARKER
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None
