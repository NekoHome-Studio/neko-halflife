# -*- coding: utf-8 -*-
"""工具元数据的持久化覆盖层。

**为什么需要它**：工具的检索元数据有两个来源会打架——

* 代码里自带的（``@lazy_tool(tags=...)`` 或导入工具的原描述）是**基线**；
* 用户在 WebUI 上改的、以及 LLM 总结出来的，是**覆盖**。

覆盖必须落盘（否则重启就没了），而且必须在**每次注册之后**重新施加，因为插件重载
会重新执行 ``@lazy_tool``，把 meta 冲回基线。所以这里做成"注册 + 施加覆盖"两步，
并提供 :meth:`OverrideStore.apply_all` 在批量注册后统一施加。

覆盖**不改用户的源码**：导入进来的插件原文件保持原样，改的是我们注册表里的副本。
这样"手动重置"永远能退回基线——基线在第一次施加覆盖时被记下来。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from .models import ToolMeta
except ImportError:  # pragma: no cover
    from core.models import ToolMeta

logger = logging.getLogger("astrbot_plugin_neko_halflife")

#: 单个工具最多保留多少标签，防止 LLM 一口气吐出几十个把索引搞糊。
MAX_TAGS = 12

#: 标签与描述的长度上限。
MAX_TAG_LEN = 24
MAX_DESC_LEN = 600


def normalize_tags(raw: Any) -> list[str]:
    """把任意输入规整成标签列表：去空白、去重、限长、限量。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        items = raw.replace("，", ",").replace("、", ",").split(",")
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        return []
    out: list[str] = []
    for item in items:
        tag = str(item).strip().strip("#").strip()
        if not tag:
            continue
        if len(tag) > MAX_TAG_LEN:
            tag = tag[:MAX_TAG_LEN]
        if tag not in out:
            out.append(tag)
        if len(out) >= MAX_TAGS:
            break
    return out


@dataclass
class ToolOverride:
    """一个工具的覆盖项。``None`` 表示该项不覆盖、沿用基线。"""

    tags: list[str] | None = None
    description: str | None = None
    source: str = "manual"
    """``manual``（用户在面板上改的）或 ``llm``（模型总结的）。
    LLM 的覆盖**不会**盖掉 manual 的。"""

    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "tags": self.tags,
            "description": self.description,
            "source": self.source,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ToolOverride":
        tags = data.get("tags")
        description = data.get("description")
        return cls(
            tags=normalize_tags(tags) if tags is not None else None,
            description=(
                str(description)[:MAX_DESC_LEN] if description is not None else None
            ),
            source=str(data.get("source") or "manual"),
            updated_at=float(data.get("updated_at") or 0.0),
        )


class OverrideStore:
    """覆盖项的加载、保存与施加。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._overrides: dict[str, ToolOverride] = {}
        #: 每个工具的基线（首次施加覆盖前的 tags/description），用于"重置"。
        self._base: dict[str, tuple[tuple[str, ...], str]] = {}
        self._dirty = False

    # ---- 持久化 --------------------------------------------------------

    def load(self) -> int:
        self._overrides.clear()
        if self.path is None or not self.path.is_file():
            return 0
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[neko-halflife] 读取工具覆盖失败（将忽略）：%s", exc)
            return 0
        items = data.get("tools") if isinstance(data, dict) else None
        if not isinstance(items, dict):
            return 0
        for name, raw in items.items():
            if isinstance(raw, dict):
                self._overrides[str(name)] = ToolOverride.from_dict(raw)
        return len(self._overrides)

    def save(self) -> bool:
        if self.path is None:
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "tools": {
                    name: override.to_dict()
                    for name, override in sorted(self._overrides.items())
                },
            }
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self._dirty = False
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("[neko-halflife] 保存工具覆盖失败：%s", exc)
            return False

    # ---- 读写 ----------------------------------------------------------

    def get(self, name: str) -> ToolOverride | None:
        return self._overrides.get(name)

    def all(self) -> dict[str, ToolOverride]:
        return dict(self._overrides)

    def set(
        self,
        name: str,
        *,
        tags: Any = None,
        description: Any = None,
        source: str = "manual",
    ) -> ToolOverride:
        """写入覆盖项。

        LLM 来源的写入**不会**盖掉已有的 manual 覆盖（用户手改的优先）。
        """
        existing = self._overrides.get(name)
        if existing is not None and existing.source == "manual" and source == "llm":
            return existing

        override = existing or ToolOverride()
        if tags is not None:
            override.tags = normalize_tags(tags)
        if description is not None:
            text = str(description).strip()
            override.description = text[:MAX_DESC_LEN] if text else None
        override.source = source
        override.updated_at = time.time()
        self._overrides[name] = override
        self._dirty = True
        return override

    def remove(self, name: str) -> bool:
        if name not in self._overrides:
            return False
        del self._overrides[name]
        self._dirty = True
        return True

    def forget_missing(self, names: set[str]) -> int:
        """清掉注册表里已经不存在的工具的覆盖项。"""
        doomed = [name for name in self._overrides if name not in names]
        for name in doomed:
            del self._overrides[name]
        if doomed:
            self._dirty = True
        return len(doomed)

    # ---- 施加 ----------------------------------------------------------

    def apply(self, meta: ToolMeta) -> ToolMeta:
        """把基线 + 覆盖写回一个 meta。

        第一次见到某个工具时记下它的基线，之后无论怎么改都能重置回去。
        """
        base = self._base.get(meta.name)
        if base is None:
            base = (tuple(meta.tags), meta.description)
            self._base[meta.name] = base

        override = self._overrides.get(meta.name)
        if override is not None and override.tags is not None:
            # 注意用 is not None 而不是真值判断：[] 表示"用户显式清空了标签"，
            # 而 None 表示"没设置过这一项"。混为一谈会让"清空标签"变回基线。
            meta.tags = tuple(override.tags)
        else:
            meta.tags = base[0]
        meta.description = (
            override.description
            if override is not None and override.description is not None
            else base[1]
        )
        return meta

    def apply_all(self, registry: Any) -> int:
        """对注册表里的每个工具施加覆盖，返回被覆盖的工具数。"""
        applied = 0
        for meta in registry.all():
            self.apply(meta)
            if meta.name in self._overrides:
                applied += 1
        return applied

    @property
    def count(self) -> int:
        return len(self._overrides)


#: 全局单例：注册表是全局的，覆盖也得是。
OVERRIDES = OverrideStore()


def default_override_path(plugin_name: str) -> Path | None:
    """覆盖文件的默认位置：AstrBot 的插件数据目录（不是插件源码目录）。

    AstrBot 的插件开发规范要求持久化数据放 ``data`` 下，所以这里用
    ``data/plugin_data/<插件名>/``；拿不到该路径时回退到当前目录，
    保证功能可用而不是直接崩掉。
    """
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        base = Path(get_astrbot_plugin_data_path()) / plugin_name
    except Exception:  # noqa: BLE001
        base = Path.cwd() / f".{plugin_name}-data"
    return base / "tool_overrides.json"
