# -*- coding: utf-8 -*-
"""导入工具时用 LLM 生成"检索友好"的描述与标签。

**为什么值得做**：懒加载的召回完全依赖工具的检索元数据。而导入进来的外来工具
只有它原本的名字与 docstring 首段——那些文字是写给**读代码的人**看的，
不一定包含**用户会说的词**。用 LLM 把它翻译成"用户会怎么问"的几个标签，
是提升召回最省力的一步。

设计上的取舍：

* **只做一次、结果落盘**（写入 :mod:`core.overrides`），不在每轮请求里调用模型；
* **可以失败**：拿不到 provider、模型返回垃圾、超时——一律跳过并保留原文，
  绝不因为"总结失败"让导入整体失败；
* **解析要抗噪**：模型经常裹 ```json 代码块、或前后加几句寒暄，
  所以 :func:`parse_enrichment` 做了多级兜底，纯逻辑、可单测；
* **不覆盖用户手改的**：写入时标记 ``source="llm"``，会被 manual 覆盖挡住。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

try:
    from .overrides import MAX_DESC_LEN, MAX_TAGS, normalize_tags
except ImportError:  # pragma: no cover
    from core.overrides import MAX_DESC_LEN, MAX_TAGS, normalize_tags

logger = logging.getLogger("astrbot_plugin_neko_halflife")

#: 单次请求最多塞进去多少个工具，避免 prompt 过大。
DEFAULT_BATCH = 20

#: 调用模型的超时（秒）。导入是同步等待的，不能无限挂。
DEFAULT_TIMEOUT = 60.0

SYSTEM_PROMPT = """你是 AstrBot 的工具检索元数据整理助手。

用户会在聊天里说一句自然语言，系统靠关键词把这句话匹配到某个工具。
你要为每个工具产出两样东西：

1. summary：一句话说明这个工具**能帮用户做什么**，用用户的口吻，不要复述函数名。
2. tags：3~8 个检索标签，**必须包含用户可能说出的口语词**（中文优先，
   必要时补英文同义词），而不是代码里的参数名或类型名。

要求：
- 只输出 JSON，不要任何解释、不要 Markdown 代码块。
- 顶层是对象，键是工具名，值形如 {"summary": "...", "tags": ["...", "..."]}。
- 工具名必须与输入完全一致，不要新增或改名。
- tags 里不要有空格、标点或井号；每个标签尽量 2~8 个字。"""


@dataclass
class ToolBrief:
    """送给模型的一个工具摘要。"""

    name: str
    description: str = ""
    parameters: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": list(self.parameters),
            "existing_tags": list(self.tags),
        }


@dataclass
class EnrichResult:
    updated: dict[str, dict] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    #: 非致命说明（例如"模型覆盖被忽略"）。与 errors 分开：这些不算失败。
    notes: list[str] = field(default_factory=list)
    raw: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.updated)


def build_user_prompt(briefs: list[ToolBrief]) -> str:
    payload = [brief.to_dict() for brief in briefs]
    return (
        "请为下面这些工具生成 summary 与 tags，按规定的 JSON 格式输出：\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    # ```json\n{...}\n``` → {...}
    stripped = re.sub(r"^```[a-zA-Z]*\s*", "", stripped)
    stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


def _extract_json_object(text: str) -> str | None:
    """从噪声里抠出第一个**配平**的 JSON 对象/数组。"""
    start = None
    opening = ""
    for index, char in enumerate(text):
        if char in "{[":
            start = index
            opening = char
            break
    if start is None:
        return None
    closing = "}" if opening == "{" else "]"
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _coerce_items(data: Any) -> dict[str, dict]:
    """把模型可能的几种输出形状统一成 ``{name: {...}}``。"""
    items: dict[str, dict] = {}
    if isinstance(data, dict):
        # 可能是 {"tools": {...}} 这种包了一层
        for key in ("tools", "result", "results", "data"):
            inner = data.get(key)
            if isinstance(inner, (dict, list)) and len(data) <= 2:
                nested = _coerce_items(inner)
                if nested:
                    return nested
        for name, value in data.items():
            if isinstance(value, dict):
                items[str(name)] = value
            elif isinstance(value, str):
                items[str(name)] = {"summary": value}
            elif isinstance(value, list):
                items[str(name)] = {"tags": value}
        return items
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict):
                name = entry.get("name") or entry.get("tool") or entry.get("tool_name")
                if name:
                    items[str(name)] = entry
            elif isinstance(entry, str):
                items.setdefault(entry, {})
    return items


def parse_enrichment(text: str, allowed: set[str] | None = None) -> dict[str, dict]:
    """把模型的回复解析成 ``{name: {"summary": str, "tags": [str]}}``。

    逐级兜底：直接 json → 剥掉 ``` 围栏 → 从噪声里抠配平片段。
    解析不出来时返回空 dict（调用方据此跳过，而不是报错）。
    """
    if not text:
        return {}
    candidates = [_strip_code_fence(text)]
    extracted = _extract_json_object(candidates[0])
    if extracted:
        candidates.append(extracted)

    data: Any = None
    for candidate in candidates:
        try:
            data = json.loads(candidate)
            break
        except Exception:  # noqa: BLE001
            continue
    if data is None:
        return {}

    items = _coerce_items(data)
    out: dict[str, dict] = {}
    for name, value in items.items():
        if allowed is not None and name not in allowed:
            continue
        summary = value.get("summary") or value.get("description") or value.get("desc")
        tags = value.get("tags") or value.get("keywords") or value.get("labels")
        entry: dict[str, Any] = {}
        if isinstance(summary, str) and summary.strip():
            entry["summary"] = summary.strip()[:MAX_DESC_LEN]
        normalized = normalize_tags(tags)
        if normalized:
            entry["tags"] = normalized[:MAX_TAGS]
        if entry:
            out[name] = entry
    return out


def _brief_summary(brief: ToolBrief) -> str:
    text = (brief.description or "").splitlines()[0]
    return text[:60]


def describe_provider(provider: Any) -> dict[str, str]:
    """把 provider 摘成 ``{id, model, type}``，用于面板展示与报错。

    一律走 ``getattr`` 兜底：第三方 provider 不一定实现了 ``meta()``，
    而这个信息只用于展示，**不该因为拿不到就让总结整个失败**。
    """
    info = {"id": "", "model": "", "type": ""}
    try:
        meta = provider.meta()
        info["id"] = str(getattr(meta, "id", "") or "")
        info["model"] = str(getattr(meta, "model", "") or "")
        info["type"] = str(getattr(meta, "type", "") or "")
    except Exception:  # noqa: BLE001
        pass
    if not info["model"]:
        getter = getattr(provider, "get_model", None)
        if callable(getter):
            try:
                info["model"] = str(getter() or "")
            except Exception:  # noqa: BLE001
                pass
    if not info["type"]:
        config = getattr(provider, "provider_config", None)
        if isinstance(config, dict):
            info["type"] = str(config.get("type") or "")
            if not info["id"]:
                info["id"] = str(config.get("id") or "")
    if not info["id"]:
        info["id"] = "default"
    return info


def provider_supports_model(provider: Any) -> bool:
    """该 provider 的 ``text_chat`` 能不能接受 ``model`` 参数。

    基类 ``Provider.text_chat`` 的签名里有 ``model``（OpenAI 兼容实现会用它
    ``model = model or self.get_model()``），但不是每个第三方 provider 都跟上了。
    先探测再传，避免为了一个"锦上添花"的模型覆盖把整次总结搞崩。
    """
    handler = getattr(provider, "text_chat", None)
    if not callable(handler):
        return False
    try:
        parameters = inspect.signature(handler).parameters
    except (TypeError, ValueError):
        return False
    if "model" in parameters:
        return True
    return any(
        param.kind is inspect.Parameter.VAR_KEYWORD for param in parameters.values()
    )


async def enrich(
    provider: Any,
    briefs: list[ToolBrief],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    model: str | None = None,
) -> EnrichResult:
    """调用模型为一批工具生成 summary/tags。

    provider 为 ``None`` 或调用失败时返回带 errors 的结果，**不抛异常**。

    ``model`` 是**按次**覆盖（``text_chat(model=...)``），不会去改 provider
    在全局配置里的模型——否则一次后台总结就会把主人正在聊天的模型换掉。
    """
    result = EnrichResult()
    if not briefs:
        return result
    if provider is None:
        result.errors.append("没有可用的对话模型（请先在 AstrBot 里配置一个 LLM 提供商）。")
        return result

    kwargs: dict[str, Any] = {
        "prompt": build_user_prompt(briefs),
        "system_prompt": SYSTEM_PROMPT,
    }
    if model:
        if provider_supports_model(provider):
            kwargs["model"] = model
            result.notes.append(f"已按配置覆盖模型：{model}")
        else:
            # 说清楚"你配了但没生效"，而不是静默忽略
            result.notes.append(
                f"该提供商不支持按次指定模型，已忽略 llm_enrich_model={model}。"
            )
            logger.warning(
                "[neko-halflife] provider 不支持 model 参数，忽略模型覆盖：%s", model
            )

    allowed = {brief.name for brief in briefs}
    try:
        response = await asyncio.wait_for(
            provider.text_chat(**kwargs),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        result.errors.append(f"调用模型超时（>{timeout:.0f}s）。")
        return result
    except Exception as exc:  # noqa: BLE001 - 模型层什么都可能抛
        result.errors.append(f"调用模型失败：{type(exc).__name__}: {exc}")
        return result

    text = str(getattr(response, "completion_text", "") or "")
    result.raw = text
    parsed = parse_enrichment(text, allowed)
    if not parsed:
        result.errors.append("模型返回的内容解析不出 JSON（已跳过，保留原描述）。")
        return result

    result.updated = parsed
    result.skipped = sorted(allowed - set(parsed))
    logger.info(
        "[neko-halflife] LLM 总结：%d 个工具成功，%d 个未返回",
        len(parsed),
        len(result.skipped),
    )
    return result


def collect_briefs(registry: Any, names: list[str] | None = None, *, only_missing: bool):
    """从注册表里挑出要送给模型的工具。

    ``only_missing=True`` 时只挑"标签为空"的——避免把用户已经手写好标签的工具
    再送去总结一遍（既费 token 又可能把好标签冲掉）。
    """
    briefs: list[ToolBrief] = []
    wanted = set(names) if names else None
    for meta in registry.candidates():
        if wanted is not None and meta.name not in wanted:
            continue
        if only_missing and meta.tags:
            continue
        briefs.append(
            ToolBrief(
                name=meta.name,
                description=meta.description,
                tags=list(meta.tags),
            )
        )
    return briefs
