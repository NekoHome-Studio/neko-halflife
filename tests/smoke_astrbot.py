# -*- coding: utf-8 -*-
"""集成冒烟测试：在**真实 AstrBot 环境**里验证注册契约。

这个测试回答的是纯逻辑自检回答不了的问题：

1. ``@lazy_tool`` 包装后的函数，``filter.llm_tool`` 还能不能正确解析 docstring？
   （描述取首段、参数取 ``Args:`` 段，缺类型注解会直接 ``ValueError``）
2. 工具是不是真的进了 AstrBot 的全局 ``llm_tools``（路线 A 的前提）？
3. 子插件工具能不能被加载器导入并同样完成注册？

需要 AstrBot 源码可导入。用环境变量 ``ASTRBOT_SRC`` 指定源码根目录，
缺省按常见的下载路径推断。用法::

    python tests/smoke_astrbot.py

注意：这里刻意**不用** ``ASTRBOT_ROOT`` 这个名字——那是 AstrBot 自己的环境变量，
用来决定它的运行根目录（``<root>/data``）。本测试会把它临时指向插件目录内的一个
临时目录，跑完自动删除，避免把 ``data/`` 垃圾目录写进仓库。
"""

from __future__ import annotations

import atexit
import importlib
import importlib.util
import os
import shutil
import sys
import types
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

#: 本地测试用的合成包名。当插件位于仓库根目录时，目录名是 ``neko-halflife``
#: 这类含连字符的名字，**不能**直接当 Python 包名 import。AstrBot 安装时会按
#: ``metadata.name`` 把目录重命名成合法标识符，所以这里用一个别名把插件目录
#: 当成包加载即可，测试因此与所在目录名解耦。
PKG_ALIAS = "neko_halflife_under_test"

_DEFAULT_ROOTS = (
    r"C:\Users\haoxu\Downloads\AstrBot-master",
    str(Path.home() / "Downloads" / "AstrBot-master"),
)
ASTRBOT_SRC = os.environ.get("ASTRBOT_SRC", "")
if not ASTRBOT_SRC:
    ASTRBOT_SRC = next((p for p in _DEFAULT_ROOTS if Path(p).is_dir()), "")

if not ASTRBOT_SRC:
    print("跳过：未找到 AstrBot 源码根目录，请设置 ASTRBOT_SRC")
    raise SystemExit(0)

sys.path.insert(0, ASTRBOT_SRC)

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


def load_plugin_main() -> types.ModuleType:
    """把插件目录当作一个合成包导入，返回 ``main`` 模块。

    用 ``<alias>.main`` 而非文件路径导入，是为了让 ``main.py`` 里的相对导入
    （``from .core import injector``）正常工作；后续所有子模块都必须用同一个
    别名导入，否则会出现两份互不相干的 REGISTRY 单例。
    """
    package = types.ModuleType(PKG_ALIAS)
    package.__path__ = [str(PLUGIN_ROOT)]
    sys.modules[PKG_ALIAS] = package

    module_name = f"{PKG_ALIAS}.main"
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_ROOT / "main.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载插件入口：{PLUGIN_ROOT / 'main.py'}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    print(f"AstrBot 源码：{ASTRBOT_SRC}")
    print(f"插件目录：  {PLUGIN_ROOT}")
    print()

    # AstrBot 会按 get_astrbot_root() 找 <root>/data 并在导入期创建目录。
    # 把它临时重定向到插件目录内的临时目录（已在 .gitignore 里），跑完自动删除。
    scratch = PLUGIN_ROOT / ".smoke-astrbot-root"
    scratch.mkdir(parents=True, exist_ok=True)
    atexit.register(shutil.rmtree, scratch, True)
    os.environ["ASTRBOT_ROOT"] = str(scratch)

    from astrbot.core.provider.register import llm_tools

    # 导入即完成注册（模块级 @lazy_tool 装饰器在导入期执行）。
    plugin_main = load_plugin_main()
    REGISTRY = importlib.import_module(f"{PKG_ALIAS}.core.registry").REGISTRY
    ToolIndex = importlib.import_module(f"{PKG_ALIAS}.core.retriever").ToolIndex
    SubPluginLoader = importlib.import_module(f"{PKG_ALIAS}.core.subplugins").SubPluginLoader

    print("1. 主插件工具注册")
    names = {tool.name for tool in llm_tools.func_list}
    for tool_name in (
        "lazy_search_tools",
        "lazy_activate_tool",
        "lazy_deactivate_tool",
    ):
        check(tool_name in names, f"{tool_name} 已进入 AstrBot 全局 llm_tools")
        meta = REGISTRY.get(tool_name)
        check(meta is not None and bool(meta.description), f"{tool_name} 描述解析成功")
        check(
            meta is not None and meta.always_active,
            f"{tool_name} 标记为常驻（不参与裁剪与检索排名）",
        )

    search_tool = next(
        (t for t in llm_tools.func_list if t.name == "lazy_search_tools"), None
    )
    check(search_tool is not None and search_tool.handler is not None, "工具带 handler")
    props = (search_tool.parameters or {}).get("properties", {}) if search_tool else {}
    check("query" in props, "docstring Args 段被解析成 query 参数")
    # 注意：AstrBot 的 register_llm_tool 不产出 required 数组（它只从 docstring
    # 收集 type/name/description），因此这里不臆断 required，只记录实际形状。
    print(f"       parameters = {search_tool.parameters if search_tool else None}")
    check(
        props.get("query", {}).get("type") == "string",
        "query 的参数类型按 docstring 的 (string) 标注解析",
    )

    activate_tool = next(
        (t for t in llm_tools.func_list if t.name == "lazy_activate_tool"), None
    )
    aprops = (activate_tool.parameters or {}).get("properties", {}) if activate_tool else {}
    check("tool_name" in aprops, "lazy_activate_tool 参数解析正确")

    print()
    print("2. 子插件加载")
    loader = SubPluginLoader(PLUGIN_ROOT, REGISTRY)
    discovered = loader.discover()
    check("demo_tools" in discovered, f"发现子插件：{discovered}")
    ok = loader.load("demo_tools")
    check(ok, f"子插件导入成功（错误：{loader.errors or '无'}）")
    check(loader.set_enabled("demo_tools", True), "子插件启用状态可切换")

    for tool_name in ("demo_echo", "demo_word_count", "demo_dangerous_reset"):
        check(tool_name in {t.name for t in llm_tools.func_list}, f"{tool_name} 已注册")
        meta = REGISTRY.get(tool_name)
        check(
            meta is not None and meta.source == "demo_tools",
            f"{tool_name} 来源标记为 demo_tools",
        )

    risky = REGISTRY.get("demo_dangerous_reset")
    check(risky is not None and risky.risk == "high", "高风险标记被采集")

    print()
    print("3. 纯函数签名（子插件不得写成方法）")
    demo = next((t for t in llm_tools.func_list if t.name == "demo_echo"), None)
    check(demo is not None and demo.handler is not None, "demo_echo 有 handler")
    import inspect

    if demo is not None and demo.handler is not None:
        params = list(inspect.signature(demo.handler).parameters)
        check(params[:1] == ["event"], f"第一个参数是 event 而非 self：{params}")
        check("text" in params, f"业务参数在签名里：{params}")

    print()
    print("4. 索引与检索")
    index = ToolIndex()
    index.build(REGISTRY.candidates())
    check(index.size >= 3, f"索引收录 {index.size} 个候选工具")

    hits = index.search("帮我把这句话回显一下", top_k=3, min_score=0.2)
    check(bool(hits), f"示例查询有命中：{[(m.name, round(s, 3)) for m, s in hits]}")
    check(
        bool(hits) and hits[0][0].name == "demo_echo",
        "「回显」命中 demo_echo 且排第一",
    )
    risky_hits = [m.name for m, _ in index.search("重置全部数据", top_k=5, min_score=0.2)]
    check("demo_dangerous_reset" in risky_hits, "高风险工具仍可被检索到（只是不自动激活）")

    print()
    print("5. 元工具不被当成检索候选")
    check(
        all(not m.always_active for m in REGISTRY.candidates()),
        "candidates 里没有常驻元工具",
    )

    print()
    print("6. 真实跑一遍 on_llm_request 的决策逻辑（用真 FunctionTool / 真 ToolSet）")
    import asyncio
    import types

    from astrbot.core.agent.tool import FunctionTool, ToolSet

    plugin = plugin_main.LazyToolsPlugin.__new__(plugin_main.LazyToolsPlugin)
    plugin.config = {}
    plugin.index = ToolIndex()
    plugin.loader = SubPluginLoader(PLUGIN_ROOT, REGISTRY)
    plugin._turns = {}
    plugin._apply_config(
        {
            "enabled": True,
            "prefetch_enabled": True,
            "top_k": 3,
            "min_score": 0.35,
            "default_ttl_turns": 3,
            "default_ttl_seconds": 600,
            "max_active_per_session": 12,
            "meta_tools_enabled": True,
            "max_result_chars": 4000,
        }
    )
    plugin._rebuild_index()

    real = {tool.name: tool for tool in llm_tools.func_list}
    foreign = FunctionTool(
        name="astrbot_file_read_tool",
        description="内置的读文件工具",
        parameters={"type": "object", "properties": {}},
        handler=None,
    )

    def make_request(prompt: str, names: list[str]):
        toolset = ToolSet()
        for name in names:
            if name == foreign.name:
                toolset.add_tool(foreign)
            elif name in real:
                toolset.add_tool(real[name])
        event = types.SimpleNamespace(
            unified_msg_origin="aiocqhttp:GroupMessage:10086",
            message_str=prompt,
        )
        return types.SimpleNamespace(prompt=prompt, func_tool=toolset), event

    all_names = ["lazy_search_tools", "lazy_activate_tool", "lazy_deactivate_tool",
                 "demo_echo", "demo_word_count", "demo_dangerous_reset",
                 "astrbot_file_read_tool"]
    request, event = make_request("帮我把这句话回显一下", all_names)
    plugin._decide(event, request)
    after = set(request.func_tool.names())
    check("lazy_search_tools" in after, "元工具常驻保留")
    check("astrbot_file_read_tool" in after, "非本插件工具保留")
    check("demo_echo" in after, "预检索命中的 demo_echo 被自动激活")
    check("demo_word_count" not in after, "未命中的 demo_word_count 被裁掉")
    check("demo_dangerous_reset" not in after, "高风险工具未被自动激活")
    check(
        len(after) < len(all_names),
        f"确实减少了注入项：{len(all_names)} -> {len(after)}",
    )

    print()
    print("7. 元工具激活后当轮即可用")
    message = asyncio.run(plugin.lazy_activate_tool(event, "demo_word_count"))
    check("demo_word_count" in request.func_tool.names(), "激活后立刻出现在本轮请求里")
    check("已激活" in message, f"返回可读结果：{message}")

    message = asyncio.run(plugin.lazy_deactivate_tool(event, "demo_word_count"))
    check(
        "demo_word_count" not in request.func_tool.names(),
        "取消激活后立刻从本轮请求里移除",
    )

    search_result = asyncio.run(plugin.lazy_search_tools(event, "回显"))
    check("demo_echo" in search_result, "元工具搜索能召回工具")

    print()
    print("8. 安全：上游排除的工具无法被激活表复活")
    request2, event2 = make_request(
        "帮我把这句话回显一下",
        ["lazy_search_tools", "lazy_activate_tool", "lazy_deactivate_tool",
         "astrbot_file_read_tool"],  # demo_* 全部被上游排除
    )
    plugin._decide(event2, request2)
    after2 = set(request2.func_tool.names())
    check("demo_echo" not in after2, "预检索命中的工具若被上游排除，依然不注入")
    check("lazy_search_tools" in after2, "本插件仍可在被允许的范围内工作")
    denied = asyncio.run(plugin.lazy_activate_tool(event2, "demo_echo"))
    check("无法激活" in denied, f"元工具明确拒绝越权激活：{denied}")

    print()
    print("9. 总开关关闭时不干预")
    plugin._apply_config({"enabled": False})
    request3, event3 = make_request("回显", all_names)
    plugin._decide(event3, request3)
    check(
        set(request3.func_tool.names()) == set(all_names),
        "关闭后工具集原样保留（便于对比排查）",
    )

    total = _PASSED + len(_FAILED)
    print()
    print(f"通过 {_PASSED}/{total}")
    if _FAILED:
        print("失败项：")
        for label in _FAILED:
            print(f"  - {label}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
