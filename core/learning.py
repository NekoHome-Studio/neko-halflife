# -*- coding: utf-8 -*-
"""归纳学习：从"实际被用到的工具"反推"用户会怎么说"。

**信号来源**（这是这套机制能成立的前提）：AstrBot 提供两个工具级钩子——

* ``on_using_llm_tool(event, tool, tool_args)``：模型**真的选了这个工具**；
* ``on_llm_tool_respond(event, tool, tool_args, tool_result)``：调用结果回来了，
  能看出 ``isError``。

所以学习闭环是：

```
本轮用户说了什么（_decide 时记下）
      ↓ 模型真的调用了工具 X
把 (那句话 → X) 记成 X 的学习样例，并进入检索索引
      ↓ 那句话里的词以后就能召回 X
调用报错 → 这次学习作废（负反馈）
```

比起"作者凭空写 examples"，这是**实证**：一句话真的导致某个工具被使用，
说明这句话与这个工具确实相关。而 :meth:`LearningStore.suggest_tags` 再做一层
**归纳**：在多条学习样例里反复出现的词，就是这个工具"真正的别名"，
可以提议升格为标签。

**边界**：只学本插件注册表认得的工具（别去学别人的工具）；样例有数量上限与长度
上限；短句不学（信息量不足）；调用出错会抵消学习结果。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from .retriever import tokenize
except ImportError:  # pragma: no cover
    from core.retriever import tokenize

logger = logging.getLogger("astrbot_plugin_neko_halflife")

#: 一条学习样例的长度区间：太短没信息，太长不是"用户会说的话"。
MIN_EXAMPLE_LEN = 2
MAX_EXAMPLE_LEN = 80

#: 单个工具默认最多留多少条学习样例。
DEFAULT_MAX_EXAMPLES = 20

#: 提议为标签时，这个词至少要在多少条**不同**样例里出现。
DEFAULT_INDUCT_MIN_HITS = 3

#: 一次最多提议几个标签。
MAX_SUGGESTIONS = 8

_WS_RE = re.compile(r"\s+")


def normalize_query(text: str) -> str:
    """规整一句话作为学习样例；不合格返回空串。"""
    cleaned = _WS_RE.sub(" ", str(text or "")).strip()
    if len(cleaned) < MIN_EXAMPLE_LEN or len(cleaned) > MAX_EXAMPLE_LEN:
        return ""
    return cleaned


@dataclass
class LearnedExample:
    text: str
    hits: int = 1
    failures: int = 0
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "hits": self.hits,
            "failures": self.failures,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LearnedExample":
        return cls(
            text=str(data.get("text") or ""),
            hits=int(data.get("hits") or 1),
            failures=int(data.get("failures") or 0),
            updated_at=float(data.get("updated_at") or 0.0),
        )


class LearningStore:
    """学习样例的存储与归纳。"""

    def __init__(self, path: Path | None = None, *, max_examples: int = DEFAULT_MAX_EXAMPLES) -> None:
        self.path = Path(path) if path else None
        self.max_examples = max(int(max_examples), 1)
        self._tools: dict[str, list[LearnedExample]] = {}
        self._dirty = False

    # ---- 持久化 --------------------------------------------------------

    def load(self) -> int:
        self._tools.clear()
        if self.path is None or not self.path.is_file():
            return 0
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[neko-halflife] 读取学习样例失败（将忽略）：%s", exc)
            return 0
        items = data.get("tools") if isinstance(data, dict) else None
        if not isinstance(items, dict):
            return 0
        total = 0
        for tool, entries in items.items():
            if not isinstance(entries, list):
                continue
            kept: list[LearnedExample] = []
            for entry in entries:
                if isinstance(entry, dict):
                    example = LearnedExample.from_dict(entry)
                    if example.text:
                        kept.append(example)
            if kept:
                self._tools[str(tool)] = kept[: self.max_examples]
                total += len(self._tools[str(tool)])
        return total

    def save(self) -> bool:
        if self.path is None:
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "tools": {
                    tool: [example.to_dict() for example in entries]
                    for tool, entries in sorted(self._tools.items())
                },
            }
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self._dirty = False
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("[neko-halflife] 保存学习样例失败：%s", exc)
            return False

    # ---- 记录 ----------------------------------------------------------

    def _entries(self, tool: str) -> list[LearnedExample]:
        return self._tools.setdefault(tool, [])

    def record(self, tool: str, query: str) -> bool:
        """记一次正样本：这句话导致了该工具被调用。"""
        text = normalize_query(query)
        if not text:
            return False
        entries = self._entries(tool)
        for example in entries:
            if example.text == text:
                example.hits += 1
                example.updated_at = time.time()
                self._dirty = True
                self._trim(tool)
                return True
        entries.append(
            LearnedExample(text=text, hits=1, updated_at=time.time())
        )
        self._dirty = True
        self._trim(tool)
        return True

    def record_failure(self, tool: str, query: str) -> bool:
        """记一次负反馈：该工具这次调用出错了，抵消掉对应的学习。

        规则：失败次数达到命中次数，就把这条样例删掉——"学了但一用就错"的样例
        不该继续影响召回。
        """
        text = normalize_query(query)
        if not text or tool not in self._tools:
            return False
        for example in list(self._tools[tool]):
            if example.text != text:
                continue
            example.failures += 1
            self._dirty = True
            if example.failures >= example.hits:
                self._tools[tool].remove(example)
                logger.info(
                    "[neko-halflife] 学习样例作废（调用失败）：%s → %s", tool, text
                )
                if not self._tools[tool]:
                    self._tools.pop(tool, None)
            return True
        return False

    def forget(self, tool: str, text: str) -> bool:
        """删掉一条具体的样例（人工纠正：学错了就删掉它）。

        自动负反馈依赖 ``isError``，而 AstrBot 的本地工具路径从不设置它
        （工具抛异常时钩子压根不被调用），所以**人工删除是唯一随时可用的
        纠正手段**，必须存在。
        """
        entries = self._tools.get(tool)
        if not entries:
            return False
        keep = [example for example in entries if example.text != text]
        if len(keep) == len(entries):
            return False
        if keep:
            self._tools[tool] = keep
        else:
            self._tools.pop(tool, None)
        self._dirty = True
        return True

    def _trim(self, tool: str) -> None:
        entries = self._tools.get(tool)
        if not entries or len(entries) <= self.max_examples:
            return
        # 保留命中最多的（并列时留新的），让高频说法沉淀下来
        entries.sort(key=lambda item: (item.hits, item.updated_at), reverse=True)
        del entries[self.max_examples :]

    # ---- 读取与归纳 ----------------------------------------------------

    def examples(self, tool: str) -> list[str]:
        return [example.text for example in self._tools.get(tool, [])]

    def all(self) -> dict[str, list[str]]:
        return {tool: self.examples(tool) for tool in self._tools}

    def count(self) -> int:
        return sum(len(entries) for entries in self._tools.values())

    def clear(self, tool: str | None = None) -> int:
        if tool is None:
            removed = self.count()
            self._tools.clear()
        else:
            removed = len(self._tools.pop(tool, []))
        if removed:
            self._dirty = True
        return removed

    def forget_missing(self, names: set[str]) -> int:
        doomed = [tool for tool in self._tools if tool not in names]
        removed = 0
        for tool in doomed:
            removed += len(self._tools.pop(tool, []))
        if removed:
            self._dirty = True
        return removed

    def suggest_tags(
        self,
        tool: str,
        existing: Any = (),
        *,
        min_hits: int = DEFAULT_INDUCT_MIN_HITS,
    ) -> list[str]:
        """从学习样例里归纳出候选标签。

        统计"出现在多少条**不同**样例里"而不是出现次数——同一个词在一句话里
        重复三遍不该被当成三次证据。只提议长度 ≥2 的词（单字噪声太大），
        且排除已经是标签的词。
        """
        entries = self._tools.get(tool) or []
        if len(entries) < max(min_hits, 1):
            return []
        existing_set = {str(tag).strip() for tag in (existing or ())}
        counters: dict[str, int] = {}
        for example in entries:
            for token in set(tokenize(example.text)):
                if len(token) < 2:
                    continue
                counters[token] = counters.get(token, 0) + 1
        proposals = [
            (token, hits)
            for token, hits in counters.items()
            if hits >= min_hits and token not in existing_set
        ]
        proposals.sort(key=lambda item: (-item[1], item[0]))
        return [token for token, _ in proposals[:MAX_SUGGESTIONS]]

    # ---- 施加 ----------------------------------------------------------

    def apply(self, registry: Any) -> int:
        """把学习样例写进注册表里的 meta，供检索索引使用。"""
        applied = 0
        for meta in registry.all():
            learned = tuple(self.examples(meta.name))
            if learned != meta.learned:
                meta.learned = learned
            if learned:
                applied += 1
        return applied


#: 全局单例：与 REGISTRY / OVERRIDES 同源。
LEARNING = LearningStore()


def default_learning_path(plugin_name: str) -> Path | None:
    """学习数据的默认位置：AstrBot 的插件数据目录。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        base = Path(get_astrbot_plugin_data_path()) / plugin_name
    except Exception:  # noqa: BLE001
        base = Path.cwd() / f".{plugin_name}-data"
    return base / "learned_examples.json"
