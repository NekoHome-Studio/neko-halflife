# -*- coding: utf-8 -*-
"""本地关键词/标签检索（MVP：不调用 LLM，也不依赖向量库）。

设计要点：

* **中文按二元切分**。中文没有空格，且不引入 jieba 这类依赖，所以对汉字串做
  bigram；对长度 1~4 的短串额外保留整串，避免「天气」被切成「天气」以外的碎片。
* **字段加权**。工具名 > 标签 > 分组 > 示例 > 描述。名字和标签是作者手写的，
  信噪比最高；描述通常最长但最泛。
* **分数可解释、可设阈值**。最终分 = 覆盖率与命中强度的加权，落在 ``[0, 1]``，
  因此 ``min_score`` 这个配置项有直觉含义，而不是某个魔法量纲的绝对值。
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from typing import Iterable, Sequence

try:
    from .models import ToolMeta
except ImportError:  # pragma: no cover
    from core.models import ToolMeta

logger = logging.getLogger("astrbot_plugin_neko_halflife")

#: 各字段的权重。改动这里会同时影响覆盖率与强度两项。
FIELD_WEIGHTS: dict[str, float] = {
    "name": 3.0,
    "tags": 2.5,
    "group": 2.0,
    "examples": 1.5,
    "description": 1.0,
}

_ASCII_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+#.-]*|\d+")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")

#: 出现频率过高的词（几乎每轮对话都会出现）不参与打分，避免把工具全激活。
#: 中文部分特意收录了一批口语填充 bigram：它们在中文里几乎不携带信息，
#: 但会出现在描述或示例里，若参与打分就会把真正命中的那个词稀释掉。
_STOPWORDS = frozenset(
    {
        # 单字虚词
        "的", "了", "是", "我", "你", "他", "她", "它", "们", "吗", "呢", "吧", "啊",
        "这", "那", "个", "和", "与", "在", "有", "没", "不", "就", "都", "也",
        # 口语填充 bigram
        "一下", "一个", "一些", "什么", "怎么", "怎样", "如何", "可以", "能否",
        "帮我", "给我", "我的", "你的", "这句", "句话", "这个", "那个", "这些",
        "时候", "现在", "就是", "还是", "这样", "那样", "或者", "因为", "所以",
        "但是", "如果", "然后", "而且", "以及", "需要", "想要", "麻烦", "请问",
        "一下", "一下", "的话", "来说", "方面", "问题", "东西", "事情", "地方",
        # 英文虚词
        "the", "a", "an", "is", "are", "to", "of", "and", "or", "for", "in", "on",
        "please", "can", "you", "me", "my", "it", "this", "that", "with", "want",
    }
)

#: 覆盖率与强度在最终分中的占比。
_COVERAGE_WEIGHT = 0.65
_STRENGTH_WEIGHT = 0.35


def tokenize(text: str) -> Counter[str]:
    """把文本切成检索 token 并计数。中英文混排安全。"""
    tokens: Counter[str] = Counter()
    if not text:
        return tokens
    low = text.lower()
    for match in _ASCII_RE.finditer(low):
        token = match.group(0).strip(".-")
        if token and token not in _STOPWORDS:
            tokens[token] += 1
    for match in _CJK_RE.finditer(low):
        run = match.group(0)
        if len(run) == 1:
            if run not in _STOPWORDS:
                tokens[run] += 1
            continue
        for i in range(len(run) - 1):
            gram = run[i : i + 2]
            if gram not in _STOPWORDS:
                tokens[gram] += 1
        if len(run) <= 4 and run not in _STOPWORDS:
            tokens[run] += 1
    return tokens


class ToolIndex:
    """倒排索引 + 打分。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolMeta] = {}
        self._postings: dict[str, dict[str, float]] = {}
        self._doc_freq: dict[str, int] = {}

    @property
    def size(self) -> int:
        return len(self._tools)

    def build(self, tools: Iterable[ToolMeta]) -> None:
        """重建索引。工具集变化（插件重载、子插件启停）后必须调用。"""
        self._tools = {}
        self._postings = {}
        self._doc_freq = {}
        for meta in tools:
            self._tools[meta.name] = meta
            fields = meta.to_index_text()
            weights: dict[str, float] = {}
            for field, text in fields.items():
                weight = FIELD_WEIGHTS.get(field, 1.0)
                for token, count in tokenize(text).items():
                    # 同一字段内重复出现只按一次计，避免长描述靠复读刷分。
                    weights[token] = weights.get(token, 0.0) + weight * min(count, 1)
            for token, weight in weights.items():
                self._postings.setdefault(token, {})[meta.name] = weight
                self._doc_freq[token] = self._doc_freq.get(token, 0) + 1

    def search(
        self,
        query: str,
        *,
        top_k: int = 3,
        min_score: float = 0.0,
        excluded: frozenset[str] = frozenset(),
    ) -> list[tuple[ToolMeta, float]]:
        """检索工具，按分数降序返回 ``(meta, score)``，``score`` 落在 ``[0, 1]``。

        Args:
            query: 用户输入。
            top_k: 最多返回条数。
            min_score: 低于该分数直接丢弃。
            excluded: 不参与本次检索的工具名（例如已被上游过滤掉的）。
        """
        q_tokens = tokenize(query)
        if not q_tokens or not self._tools:
            return []

        # 先收集「查询里真正有信息量的词」：只有出现在倒排表里的 token 才参与分母。
        # 中文长句会被切成大量无意义的 bigram，如果把它们算进分母，
        # 真正命中的那个词会被稀释到阈值以下——这正是早期版本误漏工具的原因。
        informative: dict[str, float] = {}
        buckets: dict[str, dict[str, float]] = {}
        for token, q_count in q_tokens.items():
            posting = self._postings.get(token)
            if not posting:
                continue
            # idf：只在少数工具里出现的词更有区分度。
            df = max(self._doc_freq.get(token, 1), 1)
            idf = math.log(1.0 + len(self._tools) / df) + 1.0
            informative[token] = idf
            for tool_name, weight in posting.items():
                if tool_name in excluded:
                    continue
                bucket = buckets.setdefault(tool_name, {})
                bucket[token] = weight * idf * (1.0 + 0.25 * (q_count - 1))

        if not buckets:
            return []
        idf_total = float(sum(informative.values())) or 1.0
        name_weight = FIELD_WEIGHTS["name"]

        scored: list[tuple[ToolMeta, float]] = []
        for tool_name, bucket in buckets.items():
            meta = self._tools.get(tool_name)
            if meta is None or not bucket:
                continue
            matched = len(bucket)
            # 覆盖率：命中的信息词占全部信息词的比重。
            coverage = min(1.0, sum(informative[t] for t in bucket) / idf_total)
            # 强度：命中字段的平均权重相对于「全部命中工具名」的比值，饱和到 1。
            strength = min(1.0, sum(bucket.values()) / (name_weight * matched))
            score = _COVERAGE_WEIGHT * coverage + _STRENGTH_WEIGHT * strength
            if score >= min_score:
                scored.append((meta, score))

        scored.sort(key=lambda item: (-item[1], item[0].name))
        return scored[: max(top_k, 0)]

    def informative_tokens(self, query: str) -> list[str]:
        """返回查询里真正出现在倒排表中的 token。

        这是排查「为什么没命中」的第一现场：列表为空就说明这句话里的词
        与任何工具的名字/标签/描述/示例都没有交集，此时不该怀疑阈值。
        """
        return sorted(token for token in tokenize(query) if token in self._postings)

    def describe_scores(self, query: str, tools: Sequence[str]) -> dict[str, float]:
        """调试用：给出指定工具名与查询的匹配分。"""
        result = {name: 0.0 for name in tools}
        for meta, score in self.search(query, top_k=len(self._tools) or 1, min_score=0.0):
            if meta.name in result:
                result[meta.name] = round(score, 4)
        return result
