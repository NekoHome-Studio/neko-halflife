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
import json
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

#: 找不到源码时的候选路径。只用「家目录下的 Downloads」这种通用位置，
#: 不硬编码任何具体机器的用户名——这是公开仓库，别人 clone 下来跑得通才算数。
_DEFAULT_ROOTS = (
    Path.home() / "Downloads" / "AstrBot-master",
    Path.home() / "AstrBot",
    Path.home() / "Downloads" / "AstrBot",
)
ASTRBOT_SRC = os.environ.get("ASTRBOT_SRC", "")
if not ASTRBOT_SRC:
    ASTRBOT_SRC = str(next((p for p in _DEFAULT_ROOTS if Path(p).is_dir()), ""))

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

    print()
    print("10. Web UI 后端接口")
    plugin._apply_config({"enabled": True, "prefetch_enabled": True, "min_score": 0.35})

    registered: list[tuple[str, list[str]]] = []

    class FakeContext:
        def register_web_api(self, route, handler, methods, description):
            registered.append((route, list(methods)))

    plugin.context = FakeContext()
    plugin._register_web_apis()

    routes = dict(registered)
    for suffix, method in (
        ("state", "GET"),
        ("search", "POST"),
        ("sessions/clear", "POST"),
        ("sources/toggle", "POST"),
    ):
        route = f"/{plugin_main.PLUGIN_NAME}/{suffix}"
        check(route in routes, f"注册了路由 {route}")
        check(routes.get(route) == [method], f"{suffix} 的方法为 {method}")
    check(
        all(r.startswith(f"/{plugin_main.PLUGIN_NAME}/") for r, _ in registered),
        "所有路由都带插件名前缀（前端 endpoint 不带，由 dashboard 拼接）",
    )

    class _Files(dict):
        """模拟 PluginMultiDict：只需要 .get()。"""

    class FakeWebRequest:
        def __init__(self, payload=None, files=None):
            self.payload = payload or {}
            self._files = _Files(files or {})

        async def json(self, default=None):
            return self.payload

        async def files(self):
            return self._files

    class FakeUpload:
        """模拟 astrbot.api.web.PluginUploadFile。"""

        def __init__(self, filename, data: bytes):
            self.filename = filename
            self.content_type = "application/octet-stream"
            self.content_length = len(data)
            self._data = data

        async def save(self, destination):
            Path(destination).write_bytes(self._data)

    def body_of(response) -> dict:
        return json.loads(bytes(response.body).decode("utf-8"))

    state_payload = body_of(asyncio.run(plugin.page_state()))
    check(state_payload.get("plugin_name") == plugin_main.PLUGIN_NAME, "state 返回插件名")
    check(
        state_payload["stats"]["tools"] >= 3,
        f"state 统计到 {state_payload['stats']['tools']} 个候选工具",
    )
    tool_names = {tool["name"] for tool in state_payload["tools"]}
    check("lazy_search_tools" in tool_names, "state 覆盖常驻元工具")
    check("demo_echo" in tool_names, "state 覆盖子插件工具")
    source_names = {item["name"] for item in state_payload["sources"]}
    check("demo_tools" in source_names, "state 列出子插件来源")

    # 先造一个会话激活，再验证 state / clear 能看到它
    plugin_main.request = FakeWebRequest({})
    request4, event4 = make_request("帮我把这句话回显一下", all_names)
    plugin._decide(event4, request4)
    state_payload = body_of(asyncio.run(plugin.page_state()))
    check(state_payload["stats"]["sessions"] >= 1, "state 能看到活跃会话")

    plugin_main.request = FakeWebRequest({"query": "帮我把这句话回显一下"})
    search_payload = body_of(asyncio.run(plugin.page_search()))
    hit_names = [hit["name"] for hit in search_payload["hits"]]
    check("demo_echo" in hit_names, f"search 召回 demo_echo：{hit_names[:3]}")
    check(bool(search_payload["info_tokens"]), "search 返回信息词，便于排查漏召回")
    echo_hit = next(h for h in search_payload["hits"] if h["name"] == "demo_echo")
    check(echo_hit["would_activate"], "试跑确认该工具本轮会被激活")

    plugin_main.request = FakeWebRequest({"query": "量子纠缠退相干"})
    empty_payload = body_of(asyncio.run(plugin.page_search()))
    check(empty_payload["info_tokens"] == [], "无交集查询的信息词为空（提示用户该补 tags）")
    check(empty_payload["hits"] == [], "无交集查询不召回任何工具")

    plugin_main.request = FakeWebRequest({"query": ""})
    bad = asyncio.run(plugin.page_search())
    check(bad.status_code == 400, "空 query 返回 400")

    plugin_main.request = FakeWebRequest({"umo": event4.unified_msg_origin})
    cleared = body_of(asyncio.run(plugin.page_clear_session()))
    check(cleared["cleared"] >= 1, f"清空会话生效：{cleared}")
    check(plugin.activation.names(event4.unified_msg_origin) == (), "清空后激活表为空")

    plugin_main.request = FakeWebRequest({"name": "demo_tools", "enabled": False})
    toggled = body_of(asyncio.run(plugin.page_toggle_source()))
    check(toggled["enabled"] is False, "子插件可停用")
    check(not REGISTRY.is_source_enabled("demo_tools"), "注册表来源已停用")
    check(
        all(meta.source != "demo_tools" for meta in REGISTRY.candidates()),
        "停用后其工具退出检索候选",
    )
    check(
        plugin._sub_disabled == ["demo_tools"],
        f"停用写入停用名单（而非空名单）：{plugin._sub_disabled}",
    )

    plugin_main.request = FakeWebRequest({"name": "demo_tools", "enabled": True})
    toggled = body_of(asyncio.run(plugin.page_toggle_source()))
    check(toggled["enabled"] is True, "子插件可重新启用")
    check(REGISTRY.is_source_enabled("demo_tools"), "重新启用后来源可用")

    plugin_main.request = FakeWebRequest({"name": "not_exist", "enabled": True})
    missing = asyncio.run(plugin.page_toggle_source())
    check(missing.status_code == 404, "未知子插件返回 404")

    print()
    print("11. Page 与 i18n 文件齐备")
    page_dir = PLUGIN_ROOT / "pages" / "lazy-tools"
    check((page_dir / "index.html").is_file(), "pages/lazy-tools/index.html 存在（强制入口文件名）")
    check((page_dir / "app.js").is_file(), "app.js 存在")
    check((page_dir / "style.css").is_file(), "style.css 存在")
    check(
        not (page_dir / "index.html").read_text(encoding="utf-8").count("http://"),
        "index.html 不引用绝对 http 资源（iframe 无 same-origin，必须相对路径）",
    )
    html = (page_dir / "index.html").read_text(encoding="utf-8")
    check('type="module"' in html, "脚本以 module 方式加载（保证 bridge 先就位）")
    check("./app.js" in html and "./style.css" in html, "资源使用相对路径引用")

    for locale in ("zh-CN", "en-US"):
        i18n_path = PLUGIN_ROOT / ".astrbot-plugin" / "i18n" / f"{locale}.json"
        check(i18n_path.is_file(), f"i18n/{locale}.json 存在")
        data = json.loads(i18n_path.read_text(encoding="utf-8"))
        page_i18n = data.get("pages", {}).get("lazy-tools", {})
        check(bool(page_i18n.get("title")), f"{locale} 提供 pages.lazy-tools.title")
        check(bool(page_i18n.get("description")), f"{locale} 提供 pages.lazy-tools.description")

    print()
    print("12. metadata 与代码常量一致（改名时最容易漏的地方）")
    import yaml

    meta = yaml.safe_load((PLUGIN_ROOT / "metadata.yaml").read_text(encoding="utf-8"))
    check(
        meta.get("name") == plugin_main.PLUGIN_NAME,
        f"metadata.name 与 PLUGIN_NAME 一致：{meta.get('name')}",
    )
    check(
        isinstance(meta.get("name"), str) and meta["name"].isidentifier(),
        "metadata.name 是合法 Python 标识符（AstrBot 硬性要求，连字符会拒绝加载）",
    )
    for field in ("name", "desc", "version", "author"):
        value = meta.get(field)
        check(
            isinstance(value, str) and bool(value.strip()),
            f"metadata.{field} 是非空字符串（AstrBot 必需字段）",
        )
    check(bool(meta.get("display_name")), "metadata.display_name 已设置")
    # 没有 repo 字段时，AstrBot 的「更新」会直接报「未指定仓库地址或下载地址」，
    # 而「从文件安装」在目录已存在时又硬失败（install_plugin_from_file 无覆盖选项），
    # 结果是插件永远只能手工替换文件。这个字段是就地更新的前提。
    check(
        bool(meta.get("repo")),
        f"metadata.repo 已声明（就地更新的前提）：{meta.get('repo')}",
    )
    check(
        str(meta.get("astrbot_version", "")).strip() != "",
        "metadata.astrbot_version 已声明",
    )

    print()
    print("13. 无改名残留（连字符与下划线两种写法都要查）")
    # 两处易漏点：日志前缀用的是连字符 [lazy-tools]，项目标识用的是下划线。
    # 只 grep 其中一种会得到「干净」的假结论。模式分段拼接，避免本文件自身命中。
    stale_patterns = ("[lazy" + "-tools]", "astrbot_plugin_" + "lazy_tools")
    _TEXT_SUFFIXES = {".py", ".json", ".yaml", ".yml", ".js", ".html", ".md"}
    _SELF = Path(__file__).resolve()
    scanned = [
        path
        for path in PLUGIN_ROOT.rglob("*")
        if path.is_file()
        and path.suffix in _TEXT_SUFFIXES
        and "__pycache__" not in path.parts
        and ".git" not in path.parts
        and "dist" not in path.parts
        and path.resolve() != _SELF
    ]
    check(len(scanned) >= 10, f"扫描了 {len(scanned)} 个文本文件")

    offenders = [
        f"{path.relative_to(PLUGIN_ROOT)}: {pattern}"
        for path in scanned
        for pattern in stale_patterns
        if pattern in path.read_text(encoding="utf-8", errors="ignore")
    ]
    check(not offenders, f"无旧标识残留（命中 {offenders or '无'}）")

    # 反向断言：新前缀确实在用。否则上一条会因为「压根不存在任何前缀」而假通过。
    prefix_hits = sum(
        path.read_text(encoding="utf-8", errors="ignore").count("[neko-halflife]")
        for path in scanned
        if path.suffix == ".py"
    )
    check(prefix_hits >= 10, f"新日志前缀 [neko-halflife] 实际出现 {prefix_hits} 处")

    print()
    print("14. 子插件上传与指令面板")

    # 用 scratch 目录当 sub_plugins，避免测试往仓库里写文件
    scratch = PLUGIN_ROOT / ".selftest-scratch"
    if scratch.exists():
        shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    real_loader_root = plugin.loader.root
    real_cmd_mgmt = plugin_main.command_management
    plugin.loader.root = scratch
    try:
        registered2: list[tuple[str, list[str]]] = []

        class FakeContext2:
            def register_web_api(self, route, handler, methods, description):
                registered2.append((route, list(methods)))

        plugin.context = FakeContext2()
        plugin._register_web_apis()
        routes2 = dict(registered2)
        for suffix, method in (
            ("subplugins/upload", "POST"),
            ("subplugins/delete", "POST"),
            ("commands", "GET"),
            ("commands/rename", "POST"),
            ("commands/toggle", "POST"),
            ("commands/permission", "POST"),
        ):
            route = f"/{plugin_main.PLUGIN_NAME}/{suffix}"
            check(routes2.get(route) == [method], f"注册了路由 {route} [{method}]")

        # ---- 上传：边界 ----
        plugin_main.request = FakeWebRequest({}, files={})
        resp = asyncio.run(plugin.page_upload_subplugin())
        check(resp.status_code == 400, "没带文件时返回 400")

        plugin._apply_config({"allow_subplugin_upload": False})
        plugin_main.request = FakeWebRequest(
            {}, files={"file": FakeUpload("x.py", b"pass\n")}
        )
        resp = asyncio.run(plugin.page_upload_subplugin())
        check(
            resp.status_code == 400 and "未启用" in body_of(resp).get("message", ""),
            "上传开关关闭时拒绝",
        )

        plugin._apply_config(
            {"allow_subplugin_upload": True, "max_subplugin_upload_kb": 2048}
        )
        plugin_main.request = FakeWebRequest(
            {}, files={"file": FakeUpload("my-bad.py", b"pass\n")}
        )
        resp = asyncio.run(plugin.page_upload_subplugin())
        check(resp.status_code == 400, "非法名字（连字符）被拒")

        # ---- 上传：正常单文件 ----
        code = "\n".join(
            [
                "from astrbot.api.event import AstrMessageEvent",
                "",
                "",
                "@lazy_tool(name='smoke_tmp_tool', tags=('smoke',))",
                "async def smoke_tmp_tool(event: AstrMessageEvent, text: str) -> str:",
                '    """临时工具。',
                "",
                "    Args:",
                "        text(string): 内容",
                '    """',
                "    return text",
                "",
            ]
        ).encode("utf-8")
        plugin_main.request = FakeWebRequest(
            {}, files={"file": FakeUpload("smoke_tmp.py", code)}
        )
        resp = asyncio.run(plugin.page_upload_subplugin())
        payload = body_of(resp)
        check(
            resp.status_code == 200 and payload.get("name") == "smoke_tmp",
            f"单文件子插件上传成功：{payload}",
        )
        check(
            "smoke_tmp_tool" in {t.name for t in llm_tools.func_list},
            "上传的工具进了 AstrBot 全局 llm_tools",
        )
        check(REGISTRY.get("smoke_tmp_tool") is not None, "上传的工具进了本插件注册表")
        check((scratch / "smoke_tmp.py").is_file(), "文件落在 sub_plugins/")

        plugin_main.request = FakeWebRequest(
            {}, files={"file": FakeUpload("smoke_tmp.py", code)}
        )
        resp = asyncio.run(plugin.page_upload_subplugin())
        check(resp.status_code == 400, "同名重复上传被拒（需先删除）")

        # ---- 删除（降级路径）：全局表清不掉时，也必须保证不再注入 ----
        # FakeContext2 没有 get_llm_tool_manager，正好模拟「取不到全局工具表」
        plugin_main.request = FakeWebRequest({"name": "smoke_tmp"})
        resp = asyncio.run(plugin.page_delete_subplugin())
        payload = body_of(resp)
        check(
            resp.status_code == 200 and payload.get("removed_tools", 0) >= 1,
            f"删除返回：{payload}",
        )
        check(not (scratch / "smoke_tmp.py").exists(), "删除后文件消失")
        check(
            REGISTRY.get("smoke_tmp_tool") is not None
            and REGISTRY.is_source_retired("smoke_tmp"),
            "走兜底路径：工具定义保留并标记为已退休",
        )
        check(
            REGISTRY.get("smoke_tmp_tool") not in REGISTRY.candidates(),
            "退休后不再参与检索",
        )

        # 真正要守的不变量：无论走哪条路径，被删工具都不能出现在注入结果里
        toolset5 = ToolSet()
        for tool in llm_tools.func_list:
            if tool.name in ("smoke_tmp_tool", "lazy_search_tools"):
                toolset5.add_tool(tool)
        turn5 = types.SimpleNamespace(
            prompt="任意内容", func_tool=toolset5
        )
        event5 = types.SimpleNamespace(
            unified_msg_origin="aiocqhttp:GroupMessage:deleted",
            message_str="任意内容",
        )
        plugin._decide(event5, turn5)
        check(
            "smoke_tmp_tool" not in turn5.func_tool.names(),
            "降级路径下被删工具仍被裁掉（不会因为查不到而被照常注入）",
        )
        check("lazy_search_tools" in turn5.func_tool.names(), "元工具不受影响")

        # ---- 删除（正常路径）：能清全局表时就彻底遗忘 ----
        code2 = "\n".join(
            [
                "from astrbot.api.event import AstrMessageEvent",
                "",
                "",
                "@lazy_tool(name='smoke_tmp2_tool', tags=('smoke',))",
                "async def smoke_tmp2_tool(event: AstrMessageEvent, text: str) -> str:",
                '    """临时工具二。',
                "",
                "    Args:",
                "        text(string): 内容",
                '    """',
                "    return text",
                "",
            ]
        ).encode("utf-8")
        plugin_main.request = FakeWebRequest(
            {}, files={"file": FakeUpload("smoke_tmp2.py", code2)}
        )
        asyncio.run(plugin.page_upload_subplugin())
        check(
            "smoke_tmp2_tool" in {t.name for t in llm_tools.func_list},
            "第二个子插件上传成功",
        )

        class FakeContextWithManager:
            """带 get_llm_tool_manager 的替身，返回 AstrBot 真正的 llm_tools。"""

            def register_web_api(self, route, handler, methods, description):
                pass

            def get_llm_tool_manager(self):
                return llm_tools

        plugin.context = FakeContextWithManager()
        plugin_main.request = FakeWebRequest({"name": "smoke_tmp2"})
        payload = body_of(asyncio.run(plugin.page_delete_subplugin()))
        check(payload.get("removed_tools", 0) >= 1, f"正常路径删除返回：{payload}")
        check(
            "smoke_tmp2_tool" not in {t.name for t in llm_tools.func_list},
            "正常路径：已从 AstrBot 全局工具表移除",
        )
        check(
            REGISTRY.get("smoke_tmp2_tool") is None,
            "正常路径：本插件注册表也已遗忘（界面上不再显示）",
        )

        # ---- 指令面板：默认关闭 ----
        plugin._apply_config({})
        plugin_main.request = FakeWebRequest({})
        payload = body_of(asyncio.run(plugin.page_commands()))
        check(payload.get("supported") is False, "指令面板默认关闭")
        check("未启用" in payload.get("reason", ""), "给出可读的关闭原因")
        resp = asyncio.run(plugin.page_rename_command())
        check(resp.status_code == 400, "面板关闭时改指令被拒")

        # ---- 指令面板：用假实现钉住调用契约 ----
        calls: list[tuple] = []

        class FakeCommandManagement:
            async def list_commands(self):
                return [
                    {
                        "handler_full_name": "m.h",
                        "effective_command": "demo",
                        "aliases": [],
                        "permission": "everyone",
                        "enabled": True,
                        "plugin": "p",
                        "sub_commands": [],
                    }
                ]

            async def list_command_conflicts(self):
                return []

            async def rename_command(self, handler_full_name, new_fragment, aliases=None):
                calls.append(("rename", handler_full_name, new_fragment, aliases))
                return types.SimpleNamespace(
                    handler_full_name=handler_full_name,
                    effective_command=new_fragment,
                    aliases=aliases or [],
                )

            async def toggle_command(self, handler_full_name, enabled):
                calls.append(("toggle", handler_full_name, enabled))
                return types.SimpleNamespace(
                    handler_full_name=handler_full_name, enabled=enabled
                )

            async def update_command_permission(self, handler_full_name, permission_type):
                calls.append(("permission", handler_full_name, permission_type))
                return types.SimpleNamespace(
                    handler_full_name=handler_full_name, permission=permission_type
                )

        plugin_main.command_management = FakeCommandManagement()
        plugin._apply_config({"enable_command_panel": True})

        payload = body_of(asyncio.run(plugin.page_commands()))
        check(
            payload.get("supported") is True and len(payload.get("commands", [])) == 1,
            "开启后能列出指令",
        )

        plugin_main.request = FakeWebRequest(
            {"handler_full_name": "m.h", "fragment": "newname", "aliases": ["a", "b"]}
        )
        body_of(asyncio.run(plugin.page_rename_command()))
        check(
            calls[-1] == ("rename", "m.h", "newname", ["a", "b"]),
            f"rename 参数正确：{calls[-1]}",
        )

        plugin_main.request = FakeWebRequest(
            {"handler_full_name": "m.h", "enabled": False}
        )
        body_of(asyncio.run(plugin.page_toggle_command()))
        check(calls[-1] == ("toggle", "m.h", False), f"toggle 参数正确：{calls[-1]}")

        plugin_main.request = FakeWebRequest(
            {"handler_full_name": "m.h", "permission": "admin"}
        )
        body_of(asyncio.run(plugin.page_set_command_permission()))
        check(
            calls[-1] == ("permission", "m.h", "admin"),
            f"permission 按位置传参（形参名是 permission_type）：{calls[-1]}",
        )

        plugin_main.request = FakeWebRequest(
            {"handler_full_name": "m.h", "permission": "everyone"}
        )
        resp = asyncio.run(plugin.page_set_command_permission())
        check(resp.status_code == 400, "everyone 被拒（AstrBot 不接受该值）")

        plugin_main.request = FakeWebRequest({"handler_full_name": "", "fragment": "x"})
        resp = asyncio.run(plugin.page_rename_command())
        check(resp.status_code == 400, "缺 handler_full_name 被拒")
    finally:
        plugin.loader.root = real_loader_root
        plugin_main.command_management = real_cmd_mgmt
        shutil.rmtree(scratch, ignore_errors=True)

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
