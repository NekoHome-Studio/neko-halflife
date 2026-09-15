# -*- coding: utf-8 -*-
"""纯逻辑自检：不依赖 AstrBot，直接 ``python tests/selftest.py`` 运行。

覆盖检索打分、双过期、以及**安全关键的裁剪逻辑**。最后一项最重要：
任何情况下都不能把「上游许可池之外」的工具塞进请求。
"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from core.activation import ActivationStore  # noqa: E402
from core import injector  # noqa: E402
from core.models import RISK_HIGH, ToolMeta  # noqa: E402
from core.registry import ToolRegistry  # noqa: E402
from core.retriever import ToolIndex, tokenize  # noqa: E402

_PASSED = 0
_FAILED: list[str] = []


def check(condition: bool, label: str) -> None:
    global _PASSED
    if condition:
        _PASSED += 1
        print(f"  ok   {label}")
    else:
        _FAILED.append(label)
        print(f"  FAIL {label}")


# ----------------------------------------------------------------------
# 测试替身：模拟 req.func_tool / ToolSet，形状与 AstrBot 一致
# ----------------------------------------------------------------------


class FakeTool:
    def __init__(self, name: str, module: str | None = None) -> None:
        self.name = name
        self.handler_module_path = module


class FakePool:
    def __init__(self, tools: list[FakeTool]) -> None:
        self.tools = list(tools)

    def add_tool(self, tool: FakeTool) -> None:
        if not any(t.name == tool.name for t in self.tools):
            self.tools.append(tool)

    def remove_tool(self, name: str) -> None:
        self.tools = [t for t in self.tools if t.name != name]

    def names(self) -> list[str]:
        return [t.name for t in self.tools]


class FakeRequest:
    def __init__(self, tools: list[FakeTool] | None) -> None:
        self.func_tool = FakePool(tools) if tools is not None else None


def meta(name: str, **kwargs) -> ToolMeta:
    kwargs.setdefault("description", "")
    kwargs.setdefault("module", f"pkg.main")
    return ToolMeta(name=name, **kwargs)


def build_registry(*metas: ToolMeta) -> ToolRegistry:
    registry = ToolRegistry()
    for m in metas:
        registry.register(m)
    return registry


# ----------------------------------------------------------------------
# 1. 分词
# ----------------------------------------------------------------------


def test_tokenize() -> None:
    print("tokenize")
    tokens = tokenize("北京今天天气怎么样")
    check("天气" in tokens, "中文按 bigram 切出「天气」")
    check("今天" in tokens, "中文按 bigram 切出「今天」")
    check("的" not in tokenize("我的天气"), "停用词被过滤")
    english = tokenize("Get Weather for Beijing")
    check("weather" in english and "beijing" in english, "英文按词切分且小写化")
    check("the" not in tokenize("the weather"), "英文停用词被过滤")
    check(len(tokenize("")) == 0, "空串返回空表")


# ----------------------------------------------------------------------
# 2. 检索排序
# ----------------------------------------------------------------------


def test_index_ranking() -> None:
    print("index")
    tools = [
        meta("get_weather", description="查询指定城市的天气", tags=("天气", "weather")),
        meta("download_video", description="下载视频", tags=("视频", "下载")),
        meta("send_email", description="发送邮件", tags=("邮件", "email")),
    ]
    index = ToolIndex()
    index.build(tools)
    check(index.size == 3, "索引收录 3 个工具")

    hits = index.search("北京今天天气怎么样", top_k=3, min_score=0.35)
    check(bool(hits) and hits[0][0].name == "get_weather", "「天气」查询命中 get_weather 且排第一")
    check(all(0.0 <= score <= 1.0 for _m, score in hits), "分数落在 [0,1]")

    unrelated = index.search("量子纠缠退相干", top_k=3, min_score=0.35)
    check(not unrelated, "无关查询不误激活任何工具")

    excl = index.search("天气", top_k=3, min_score=0.0, excluded=frozenset({"get_weather"}))
    check(all(m.name != "get_weather" for m, _ in excl), "excluded 参数能把工具排除在外")

    scores = index.describe_scores("下载视频", ["get_weather", "download_video"])
    check(scores["download_video"] > scores["get_weather"], "标签命中权重高于无关工具")


# ----------------------------------------------------------------------
# 3. 激活表与双过期
# ----------------------------------------------------------------------


def test_activation_turns() -> None:
    print("activation / 轮次过期")
    store = ActivationStore(default_ttl_turns=2, default_ttl_seconds=0, max_per_session=5)
    store.activate("umo-a", "tool_x")
    check("tool_x" in store.names("umo-a"), "激活当轮可见")

    store.begin_turn("umo-a")
    check("tool_x" in store.names("umo-a"), "第 2 轮仍可见（ttl=2）")

    store.begin_turn("umo-a")
    check("tool_x" not in store.names("umo-a"), "第 3 轮已过期移除")

    store.activate("umo-a", "tool_y")
    store.begin_turn("umo-a")
    store.activate("umo-a", "tool_y")
    check("tool_y" in store.names("umo-a"), "重复命中会刷新 TTL（粘性）")

    store.deactivate("umo-a", "tool_y")
    check(store.names("umo-a") == (), "deactivate 立即生效")


def test_activation_seconds() -> None:
    print("activation / 时间过期")
    clock = {"now": 1000.0}
    store = ActivationStore(
        default_ttl_turns=0,
        default_ttl_seconds=60,
        max_per_session=5,
        clock=lambda: clock["now"],
    )
    store.activate("umo-b", "tool_z")
    check("tool_z" in store.names("umo-b"), "刚激活时可见")
    clock["now"] += 61
    check("tool_z" not in store.names("umo-b"), "超过存活秒数后不可见")
    check(store.names("umo-b") == (), "过期项被清理")


def test_activation_capacity() -> None:
    print("activation / 容量上限")
    store = ActivationStore(default_ttl_turns=9, default_ttl_seconds=0, max_per_session=3)
    for i in range(5):
        store.activate("umo-c", f"tool_{i}", score=i / 10.0)
    names = store.names("umo-c")
    check(len(names) == 3, "超过上限后激活表被压回上限")
    check("tool_4" in names and "tool_0" not in names, "淘汰低分项，保留高分项")


# ----------------------------------------------------------------------
# 4. 裁剪逻辑（安全关键）
# ----------------------------------------------------------------------


def test_pruning_keeps_foreign_tools() -> None:
    print("injector / 非本插件工具一律保留")
    registry = build_registry(meta("lazy_a"), meta("lazy_b"))
    req = FakeRequest([FakeTool("lazy_a"), FakeTool("lazy_b"), FakeTool("astrbot_file_read_tool")])
    kept, pruned = injector.plan_pruning(req, registry, frozenset({"lazy_a"}))
    kept_names = [t.name for t in kept]
    check("astrbot_file_read_tool" in kept_names, "内置/别人的工具不被裁")
    check("lazy_a" in kept_names, "激活的工具保留")
    check(set(pruned) == {"lazy_b"}, "未激活的本插件工具被裁")


def test_pruning_never_exceeds_pool() -> None:
    print("injector / 不得突破上游许可池")
    registry = build_registry(meta("lazy_a"), meta("lazy_b"))
    # 许可池里只有 lazy_a（lazy_b 被 persona 或会话插件过滤掉了）
    req = FakeRequest([FakeTool("lazy_a")])
    allowed = injector.snapshot_allowed_names(req)
    check(allowed == frozenset({"lazy_a"}), "许可池快照正确")

    lazy_allowed = frozenset(n for n in registry.names() if n in allowed)
    keep = injector.build_keep_set(
        registry,
        active_names=frozenset({"lazy_a", "lazy_b"}),  # 激活表里两个都"激活"了
        lazy_allowed=lazy_allowed,
        meta_tools_enabled=False,
    )
    check(keep == frozenset({"lazy_a"}), "activated ∩ allowed 取交集，越权项被丢弃")

    kept, pruned = injector.plan_pruning(req, registry, keep)
    injector.apply_pruning(req, kept, pruned)
    check(req.func_tool.names() == ["lazy_a"], "最终请求里没有越权工具")
    check("lazy_b" not in req.func_tool.names(), "被上游排除的工具无法通过激活表复活")


def test_restore_withdraw() -> None:
    print("injector / 同轮补注入与撤回")
    registry = build_registry(meta("lazy_a"), meta("lazy_b"))
    req = FakeRequest([FakeTool("lazy_a"), FakeTool("lazy_b")])
    kept, pruned = injector.plan_pruning(req, registry, frozenset())
    injector.apply_pruning(req, kept, pruned)
    check(req.func_tool.names() == [], "全部裁剪后为空")

    check(injector.restore(req, pruned["lazy_a"]), "restore 把工具补回本轮请求")
    check("lazy_a" in req.func_tool.names(), "补回后可见")
    check(not injector.restore(req, pruned["lazy_a"]), "重复补回是幂等的")
    check(injector.withdraw(req, "lazy_a"), "withdraw 能摘掉工具")
    check("lazy_a" not in req.func_tool.names(), "摘掉后不可见")
    check(not injector.withdraw(req, "lazy_a"), "重复 withdraw 返回 False")


def test_none_pool() -> None:
    print("injector / 无工具请求的边界")
    req = FakeRequest(None)
    check(injector.snapshot_allowed_names(req) == frozenset(), "无 func_tool 时快照为空")
    kept, pruned = injector.plan_pruning(req, build_registry(meta("lazy_a")), frozenset())
    check(kept == [] and pruned == {}, "无 func_tool 时不产生裁剪")
    check(injector.apply_pruning(req, kept, pruned) == 0, "无 func_tool 时应用裁剪是空操作")


# ----------------------------------------------------------------------
# 5. 注册表与子插件来源
# ----------------------------------------------------------------------


def test_registry_sources() -> None:
    print("registry / 子插件来源开关")
    registry = build_registry(
        meta("lazy_a"),
        meta("demo_echo", source="demo_tools"),
        meta("lazy_search_tools", always_active=True),
    )
    check(len(registry.candidates()) == 2, "candidates 排除常驻元工具")
    check(len(registry.always_active()) == 1, "常驻元工具单独可查")
    check(registry.sources() == ("demo_tools",), "来源列表正确")

    registry.set_source_enabled("demo_tools", False)
    names = {m.name for m in registry.candidates()}
    check("demo_echo" not in names, "停用子插件后其工具不再参与检索")
    check(len(registry.always_active()) == 1, "停用子插件不影响元工具")

    removed = registry.forget_source("demo_tools")
    check(removed == 1 and registry.get("demo_echo") is None, "forget_source 彻底移除")


def test_keep_set_meta_toggle() -> None:
    print("injector / 元工具开关")
    registry = build_registry(
        meta("lazy_a"),
        meta("lazy_search_tools", always_active=True),
    )
    allowed = frozenset({"lazy_a", "lazy_search_tools"})
    on = injector.build_keep_set(
        registry, active_names=frozenset(), lazy_allowed=allowed, meta_tools_enabled=True
    )
    off = injector.build_keep_set(
        registry, active_names=frozenset(), lazy_allowed=allowed, meta_tools_enabled=False
    )
    check("lazy_search_tools" in on, "开启时元工具常驻")
    check(off == frozenset(), "关闭时元工具也被裁掉")


def test_high_risk_flag() -> None:
    print("models / 风险标记")
    risky = meta("danger", risk=RISK_HIGH)
    check(risky.risk == "high", "高风险标记可携带")
    check(meta("safe").risk == "normal", "默认为普通风险")


def main() -> int:
    for test in (
        test_tokenize,
        test_index_ranking,
        test_activation_turns,
        test_activation_seconds,
        test_activation_capacity,
        test_pruning_keeps_foreign_tools,
        test_pruning_never_exceeds_pool,
        test_restore_withdraw,
        test_none_pool,
        test_registry_sources,
        test_keep_set_meta_toggle,
        test_high_risk_flag,
    ):
        test()
        print()

    total = _PASSED + len(_FAILED)
    print(f"通过 {_PASSED}/{total}")
    if _FAILED:
        print("失败项：")
        for label in _FAILED:
            print(f"  - {label}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
