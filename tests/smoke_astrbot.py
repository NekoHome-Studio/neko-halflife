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
import functools
import importlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
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
    plugin_import = importlib.import_module(f"{PKG_ALIAS}.core.plugin_import")

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
    plugin._hosted = {}
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

    # 前端脚本的语法错误会让整个面板静默失效：iframe 里没有可见的报错，
    # 页面只是"点不动"。所以这里真的解析一遍。
    # 输出走继承的 stdio（不走管道）——沙箱环境下管道会踩到命名管道限制。
    node = shutil.which("node")
    if node:
        checked = subprocess.run(
            [node, "--check", str(page_dir / "app.js")],
            check=False,
        )
        check(checked.returncode == 0, "app.js 通过 node --check 语法检查")
    else:
        print("     跳过：未找到 node，无法对 app.js 做语法检查")

    for locale in ("zh-CN", "en-US"):
        i18n_path = PLUGIN_ROOT / ".astrbot-plugin" / "i18n" / f"{locale}.json"
        check(i18n_path.is_file(), f"i18n/{locale}.json 存在")
        data = json.loads(i18n_path.read_text(encoding="utf-8"))
        page_i18n = data.get("pages", {}).get("lazy-tools", {})
        check(bool(page_i18n.get("title")), f"{locale} 提供 pages.lazy-tools.title")
        check(bool(page_i18n.get("description")), f"{locale} 提供 pages.lazy-tools.description")

    # 前端每个 t("pages.lazy-tools.X", ...) 都必须在两个语言文件里真的有 X。
    # 少了只会静默退回中文兜底 —— 英文用户看到中文，而且没人会发现。
    # （本次就是靠这个检查发现 tools_hint / enrich 两个键漏了。）
    app_source = (page_dir / "app.js").read_text(encoding="utf-8")
    used_keys = set(re.findall(r't\(\s*"pages\.lazy-tools\.([A-Za-z0-9_]+)"', app_source))
    check(len(used_keys) >= 20, f"从 app.js 提取到 {len(used_keys)} 个 i18n 键")
    for locale in ("zh-CN", "en-US"):
        i18n_path = PLUGIN_ROOT / ".astrbot-plugin" / "i18n" / f"{locale}.json"
        page_i18n = json.loads(i18n_path.read_text(encoding="utf-8"))
        page_i18n = page_i18n.get("pages", {}).get("lazy-tools", {})
        missing = sorted(key for key in used_keys if not page_i18n.get(key))
        check(not missing, f"{locale} 覆盖了前端用到的全部键（缺：{missing}）")

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

    # 配置项与代码必须双向对应。这一条是被真实的坑逼出来的：auto_induct_tags
    # 曾经只被读出来、在面板上显示，却从来没有被用过——一个"看起来有、实际没有"
    # 的开关比没有这个开关更糟。反向也一样：代码里读了 schema 里没有的键，
    # AstrBot 永远不会传给它，默认值会静默接管。
    schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    schema_keys = {key for key in schema if not key.startswith("$")}
    main_source = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
    apply_body = main_source.split("def _apply_config", 1)[1].split("\n    def ", 1)[0]
    read_keys = set(re.findall(r'get\(\s*"([A-Za-z0-9_]+)"', apply_body))
    check(
        not (schema_keys - read_keys),
        f"schema 里每个配置项都真的被读取（未读：{sorted(schema_keys - read_keys)}）",
    )
    check(
        not (read_keys - schema_keys),
        f"代码读的每个配置项都在 schema 里（多余：{sorted(read_keys - schema_keys)}）",
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
            ("subplugins/rescan", "POST"),
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

        # ---- 上传：装了但零工具，必须当场报错而不是静默成功 ----
        plugin_main.request = FakeWebRequest(
            {},
            files={
                "file": FakeUpload(
                    "empty_tools.py",
                    "value = 1\n\n\nasync def not_a_tool(event):\n"
                    '    """没有任何装饰器。"""\n'
                    "    return 1\n".encode("utf-8"),
                )
            },
        )
        resp = asyncio.run(plugin.page_upload_subplugin())
        message = body_of(resp).get("message", "")
        check(resp.status_code == 400, "零工具的上传被拒（不再静默成功）")
        check("没有找到任何工具" in message, f"错误信息说明原因：{message[:44]}…")
        check(
            not (scratch / "empty_tools.py").exists(),
            "零工具时文件已回滚删除",
        )
        check(
            REGISTRY.get("not_a_tool") is None,
            "零工具时没有在注册表里留下痕迹",
        )

        # ---- 手动往目录里放文件后，靠「重新扫描」加载 ----
        # 这正是「什么都没发生、也没报错」的典型场景：本插件只在自己被加载/重载时
        # 才扫一次目录，手动放的文件在重载前不会被发现。
        (scratch / "manual_tools.py").write_text(
            "@lazy_tool(name='manual_tool', tags=('manual',))\n"
            "async def manual_tool(event, text: str) -> str:\n"
            '    """手动放入的工具。\n\n    Args:\n        text(string): 内容\n    """\n'
            "    return text\n",
            encoding="utf-8",
        )
        check(
            "manual_tool" not in REGISTRY.names(),
            "前置条件：直接放文件不会自动加载（需要重扫或重载插件）",
        )
        plugin_main.request = FakeWebRequest({})
        payload = body_of(asyncio.run(plugin.page_rescan_subplugins()))
        check(
            "manual_tools" in payload.get("loaded", []),
            f"重新扫描后已加载：{payload.get('loaded')}",
        )
        check("manual_tools" in payload.get("added", []), "返回里标出了新增项")
        check("manual_tool" in REGISTRY.names(), "重新扫描后工具已注册")
        check(
            payload.get("tools", {}).get("manual_tools") == 1,
            f"按来源统计工具数：{payload.get('tools')}",
        )

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

    print()
    print("15. 从已装插件导入为子插件")

    scratch = PLUGIN_ROOT / ".selftest-scratch"
    if scratch.exists():
        shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    plugins_root = scratch / "plugins"
    plugins_root.mkdir()
    sub_root = scratch / "sub_plugins"
    sub_root.mkdir()

    real_loader_root = plugin.loader.root
    real_plugins_root = plugin_main.LazyToolsPlugin._plugins_root
    real_analyze = plugin_import.analyze_plugin_dir

    def write_plugin(name: str, source: str) -> None:
        path = plugins_root / name
        path.mkdir(parents=True)
        (path / "main.py").write_text(source, encoding="utf-8")
        (path / "metadata.yaml").write_text(
            f"name: {name}\ndisplay_name: {name}\nversion: 1.0.0\n", encoding="utf-8"
        )

    write_plugin(
        "good_tools",
        "@lazy_tool(name='imported_tool', tags=('imported',))\n"
        "async def imported_tool(event, text: str) -> str:\n"
        '    """导入的工具。\n\n    Args:\n        text(string): 内容\n    """\n'
        "    return text\n",
    )
    write_plugin(
        "bad_tools",
        "from astrbot.api.event import filter\n\n"
        "@filter.llm_tool(name='bad_tool')\n"
        "async def bad_tool(self, event):\n"
        "    return 'x'\n",
    )
    # 常规插件：Star 子类 + AstrBot 装饰器 → 走宿主模式
    write_plugin(
        "hosted_tools",
        "from astrbot.api.event import filter\n"
        "from astrbot.api.star import Star\n\n\n"
        "class HostedPlugin(Star):\n"
        "    def __init__(self, context, config=None):\n"
        "        super().__init__(context)\n"
        "        self.calls = 0\n\n"
        "    @filter.command('hostedping')\n"
        "    async def hosted_ping(self, event):\n"
        "        return 'pong'\n\n"
        "    @filter.llm_tool(name='hosted_tool')\n"
        "    async def hosted_tool(self, event, text: str):\n"
        '        """宿主工具。\n\n        Args:\n            text(string): 内容\n        """\n'
        "        self.calls += 1\n"
        "        return f'hosted:{text}:{self.calls}'\n",
    )
    # 静态扫描看不出来、只在导入期才注册脏工具的插件（用于测运行期兜底）
    write_plugin(
        "sneaky",
        "from astrbot.core.provider.register import llm_tools\n\n\n"
        "async def stray_handler(event):\n"
        '    """stray"""\n'
        '    return "x"\n\n\n'
        'llm_tools.add_func("stray_tool_xyz", [], "stray", stray_handler)\n',
    )

    plugin.loader.root = sub_root
    plugin_main.LazyToolsPlugin._plugins_root = staticmethod(lambda: plugins_root)
    try:
        registered3: list[tuple[str, list[str]]] = []

        class FakeContext3:
            def register_web_api(self, route, handler, methods, description):
                registered3.append((route, list(methods)))

            def get_llm_tool_manager(self):
                return llm_tools

        plugin.context = FakeContext3()
        plugin._register_web_apis()
        routes3 = dict(registered3)
        for suffix, method in (
            ("plugin-import/candidates", "GET"),
            ("plugin-import/apply", "POST"),
        ):
            route = f"/{plugin_main.PLUGIN_NAME}/{suffix}"
            check(routes3.get(route) == [method], f"注册了路由 {route} [{method}]")

        # ---- 候选列表 ----
        plugin_main.request = FakeWebRequest({})
        payload = body_of(asyncio.run(plugin.page_import_candidates()))
        check(payload.get("supported") is True, "候选列表可用")
        by_dir = {item["dir_name"]: item for item in payload["candidates"]}
        check("good_tools" in by_dir and by_dir["good_tools"]["portable"], "普通函数工具集可导入")
        check(
            not by_dir["bad_tools"]["portable"]
            and any("Star 子类" in r for r in by_dir["bad_tools"]["reasons"]),
            "没有 Star 子类可实例化的插件标为不可导入",
        )
        check(
            by_dir["hosted_tools"]["portable"]
            and by_dir["hosted_tools"]["mode"] == "hosted",
            f"常规插件判定为可导入并走宿主模式：{by_dir['hosted_tools']['mode']}",
        )

        # ---- 导入成功 ----
        plugin_main.request = FakeWebRequest({"dir_name": "good_tools"})
        resp = asyncio.run(plugin.page_import_apply())
        payload = body_of(resp)
        check(
            resp.status_code == 200 and payload.get("name") == "good_tools",
            f"导入成功：{payload}",
        )
        check(
            "imported_tool" in {t.name for t in llm_tools.func_list},
            "导入的工具进了 AstrBot 全局 llm_tools",
        )
        check(REGISTRY.get("imported_tool") is not None, "导入的工具进了本插件注册表")
        check((sub_root / "good_tools" / "__init__.py").is_file(), "生成了包入口")
        check(
            not (sub_root / "good_tools" / "metadata.yaml").exists(),
            "没有搬运 metadata.yaml",
        )

        plugin_main.request = FakeWebRequest({})
        payload = body_of(asyncio.run(plugin.page_import_candidates()))
        by_dir = {item["dir_name"]: item for item in payload["candidates"]}
        check(by_dir["good_tools"]["already_imported"], "再次列出时标记为已导入")

        # ---- 导入被拒（静态预检） ----
        plugin_main.request = FakeWebRequest({"dir_name": "bad_tools"})
        resp = asyncio.run(plugin.page_import_apply())
        check(resp.status_code == 400, "静态预检不过的插件被拒绝")
        check(not (sub_root / "bad_tools").exists(), "被拒时没有留下任何目录")

        # ---- 目录穿越 ----
        plugin_main.request = FakeWebRequest({"dir_name": "../evil"})
        resp = asyncio.run(plugin.page_import_apply())
        check(resp.status_code == 400, "目录穿越被拒")

        # ---- 宿主模式：常规插件被实例化、绑定，并纳入懒加载 ----
        plugin_main.request = FakeWebRequest({"dir_name": "hosted_tools"})
        resp = asyncio.run(plugin.page_import_apply())
        payload = body_of(resp)
        check(
            resp.status_code == 200 and payload.get("mode") == "hosted",
            f"宿主模式导入成功：{payload}",
        )
        host = plugin._hosted.get("hosted_tools")
        check(
            host is not None and host.instance is not None,
            "Star 类已被实例化（AstrBot 不会做这件事，模块路径不匹配）",
        )
        check(
            "hosted_tool" in REGISTRY.names(),
            "宿主插件的工具进了本插件注册表 —— 因此会被按需注入而不是每轮下发",
        )
        hosted_ft = next(
            (t for t in llm_tools.func_list if t.name == "hosted_tool"), None
        )
        check(hosted_ft is not None, "宿主插件的工具在 AstrBot 全局表里")
        check(
            isinstance(getattr(hosted_ft, "handler", None), functools.partial),
            "工具 handler 已 partial 到宿主实例上",
        )
        # 真的调用一次，证明 self 绑定正确（这正是去掉 @lazy_tool 限制的意义）
        called = asyncio.run(hosted_ft.handler(None, text="hi"))
        check(
            called == "hosted:hi:1",
            f"绑定后的工具可正常调用：{called}",
        )

        from astrbot.core.star.star_handler import (
            star_handlers_registry as _registry,
        )

        cmd_handler = next(
            (h for h in _registry._handlers if h.handler_name == "hosted_ping"), None
        )
        check(
            cmd_handler is not None
            and isinstance(cmd_handler.handler, functools.partial),
            "命令处理器也已绑定实例（不绑的话一触发就报错）",
        )
        check(
            asyncio.run(cmd_handler.handler(None)) == "pong",
            "绑定后的命令可正常调用",
        )
        check(
            (sub_root / "hosted_tools" / plugin_import.HOST_MARKER).is_file(),
            "写入了宿主标记（重启后据此续接宿主）",
        )

        # ---- 删除宿主子插件：工具与处理器都必须清干净 ----
        plugin_main.request = FakeWebRequest({"name": "hosted_tools"})
        asyncio.run(plugin.page_delete_subplugin())
        check(
            "hosted_tool" not in {t.name for t in llm_tools.func_list},
            "删除后工具已从全局表移除",
        )
        check(
            next(
                (h for h in _registry._handlers if h.handler_name == "hosted_ping"),
                None,
            )
            is None,
            "删除后命令处理器已从全局表移除（否则实例没了仍会被触发）",
        )
        check("hosted_tools" not in plugin._hosted, "宿主实例已注销")

        # ---- 运行期兜底：静态扫描漏掉的脏注册必须被拦下并回滚 ----
        stray_before = "stray_tool_xyz" in {t.name for t in llm_tools.func_list}
        check(not stray_before, "前置条件：脏工具此刻不在全局表里")
        plugin_import.analyze_plugin_dir = lambda path: plugin_import.ImportAnalysis(
            dir_name=path.name, portable=True, mode="native", import_modules=["main"]
        )
        plugin_main.request = FakeWebRequest({"dir_name": "sneaky"})
        resp = asyncio.run(plugin.page_import_apply())
        check(resp.status_code == 400, "运行期检测到脏注册，导入被拒")
        check(
            "非本插件的工具被注册" in body_of(resp).get("message", ""),
            f"错误信息点明脏注册：{body_of(resp).get('message', '')[:40]}…",
        )
        check(
            "stray_tool_xyz" not in {t.name for t in llm_tools.func_list},
            "脏注册的工具已被从全局表清理（否则会被当成别人的工具照常注入）",
        )
        check(not (sub_root / "sneaky").exists(), "失败后目录已回滚删除")
    finally:
        plugin.loader.root = real_loader_root
        plugin_main.LazyToolsPlugin._plugins_root = real_plugins_root
        plugin_import.analyze_plugin_dir = real_analyze
        shutil.rmtree(scratch, ignore_errors=True)

    print()
    print("16. 工具标签：手动覆盖与 LLM 总结")

    overrides_mod = importlib.import_module(f"{PKG_ALIAS}.core.overrides")
    models_mod = importlib.import_module(f"{PKG_ALIAS}.core.models")
    OVERRIDES = overrides_mod.OVERRIDES
    ToolMeta = models_mod.ToolMeta

    scratch = PLUGIN_ROOT / ".selftest-scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    OVERRIDES.path = scratch / "tool_overrides.json"
    real_ctx2 = plugin.context
    try:
        registered5: list[tuple[str, list[str]]] = []

        class FakeProvider:
            def __init__(self, model: str = "big-model", provider_id: str = "primary"):
                self.calls = 0
                self.seen_models: list[str | None] = []
                self.provider_config = {
                    "id": provider_id,
                    "type": "openai_chat_completion",
                }
                self._model = model

            def get_model(self) -> str:
                return self._model

            def meta(self):
                return types.SimpleNamespace(
                    id=self.provider_config["id"],
                    model=self._model,
                    type=self.provider_config["type"],
                )

            async def text_chat(self, prompt=None, system_prompt=None, **kwargs):
                self.calls += 1
                self.seen_models.append(kwargs.get("model"))
                # 只为本节用到的工具名作答。真实模型会给每个工具都回答，但那样
                # 别的工具也会被贴上同样的标签，"补标签 → 能召回"就退化成一次
                # 不确定的名次比较，断言也就不再说明问题。
                known = {"meta_test_tool", "neko_ping_tool"}
                names = [
                    name
                    for name in re.findall(r'"name":\s*"([^"]+)"', prompt or "")
                    if name in known
                ]
                payload = {
                    name: {
                        "summary": "把用户的话原样回显",
                        "tags": ["回显", "复述", "echo"],
                    }
                    for name in names
                }
                return types.SimpleNamespace(
                    completion_text=json.dumps(payload, ensure_ascii=False)
                )

        provider = FakeProvider()
        cheap_provider = FakeProvider(model="tiny-model", provider_id="cheap")

        class FakeContext5:
            def register_web_api(self, route, handler, methods, description):
                registered5.append((route, list(methods)))

            def get_llm_tool_manager(self):
                return llm_tools

            def get_using_provider(self):
                return provider

            def get_all_providers(self):
                return [provider, cheap_provider]

            def get_provider_by_id(self, provider_id):
                return {
                    "primary": provider,
                    "cheap": cheap_provider,
                }.get(provider_id)

        plugin.context = FakeContext5()
        plugin._register_web_apis()
        routes5 = dict(registered5)
        for suffix in ("tools/update", "tools/reset", "tools/enrich"):
            route = f"/{plugin_main.PLUGIN_NAME}/{suffix}"
            check(routes5.get(route) == ["POST"], f"注册了路由 {route} [POST]")
        for suffix, method in (
            ("providers", "GET"),
            ("providers/test", "POST"),
            ("providers/save", "POST"),
        ):
            route = f"/{plugin_main.PLUGIN_NAME}/{suffix}"
            check(routes5.get(route) == [method], f"注册了路由 {route} [{method}]")

        # 造一个"原描述含糊、没有任何标签"的工具（正是导入进来的工具的样子）
        REGISTRY.register(
            ToolMeta(
                name="meta_test_tool",
                description="vague description",
                module="smoke.main",
                source="smoke",
            )
        )
        OVERRIDES.apply_all(REGISTRY)
        plugin._rebuild_index()

        # ---- 手动改标签 ----
        plugin_main.request = FakeWebRequest(
            {
                "name": "meta_test_tool",
                "tags": ["北京天气", "气温"],
                "description": "查天气",
            }
        )
        payload = body_of(asyncio.run(plugin.page_update_tool()))
        check(
            payload.get("tags") == ["北京天气", "气温"],
            f"手动改标签生效：{payload.get('tags')}",
        )
        meta = REGISTRY.get("meta_test_tool")
        check(meta.tags == ("北京天气", "气温"), "注册表里的 meta 已更新")
        check(OVERRIDES.get("meta_test_tool").source == "manual", "标记为手动来源")
        hits = plugin.index.search("北京天气怎么样", top_k=3, min_score=0.3)
        check(
            bool(hits) and hits[0][0].name == "meta_test_tool",
            "改完标签立刻能被召回（索引已重建）",
        )
        check((scratch / "tool_overrides.json").is_file(), "覆盖已落盘")

        # ---- LLM 总结不覆盖手改 ----
        plugin_main.request = FakeWebRequest({"only_missing": False})
        payload = body_of(asyncio.run(plugin.page_enrich_tools()))
        check(payload.get("sent", 0) >= 1, f"LLM 总结送出 {payload.get('sent')} 个工具")
        check(provider.calls == 1, "确实调用了模型")
        check(
            OVERRIDES.get("meta_test_tool").tags == ["北京天气", "气温"],
            "LLM 没有盖掉用户手改的标签",
        )

        # ---- 重置回基线 ----
        plugin_main.request = FakeWebRequest({"name": "meta_test_tool"})
        body_of(asyncio.run(plugin.page_reset_tool()))
        check(meta.tags == (), "重置后标签回到基线（原本没有标签）")
        check(meta.description == "vague description", "重置后描述回到基线")

        # ---- only_missing=True 时才轮到 LLM 补 ----
        plugin_main.request = FakeWebRequest({"only_missing": True})
        payload = body_of(asyncio.run(plugin.page_enrich_tools()))
        check(
            "meta_test_tool" in (payload.get("updated") or {}),
            f"LLM 结果已写入：{list((payload.get('updated') or {}).keys())}",
        )
        check(meta.tags == ("回显", "复述", "echo"), f"标签来自 LLM：{meta.tags}")
        check(OVERRIDES.get("meta_test_tool").source == "llm", "标记为 LLM 来源")
        check(meta.description == "把用户的话原样回显", "描述被总结替换")
        hits = plugin.index.search("帮我把这句话复述一遍", top_k=3, min_score=0.3)
        check(
            bool(hits) and hits[0][0].name == "meta_test_tool",
            "LLM 补的标签同样能召回到",
        )

        # ---- 自定义总结模型：选提供商 + 按次覆盖模型名 ----
        plugin_main.request = FakeWebRequest({})
        payload = body_of(asyncio.run(plugin.page_providers()))
        listed = [item["id"] for item in payload.get("items", [])]
        check(listed == ["cheap", "primary"], f"providers 列出全部对话模型：{listed}")
        check(
            payload["effective"]["model"] == "big-model",
            f"未配置时用会话默认模型：{payload['effective']}",
        )
        check(payload["configured"]["provider_id"] == "", "配置里默认不指定总结模型")

        OVERRIDES.remove("meta_test_tool")
        OVERRIDES.apply_all(REGISTRY)
        plugin._rebuild_index()
        before_primary, before_cheap = provider.calls, cheap_provider.calls
        plugin_main.request = FakeWebRequest(
            {"only_missing": False, "provider_id": "cheap", "model": "tiny-2"}
        )
        payload = body_of(asyncio.run(plugin.page_enrich_tools()))
        check(cheap_provider.calls == before_cheap + 1, "请求里指定的提供商真的被调用")
        check(provider.calls == before_primary, "会话默认提供商没有被牵连")
        check(
            cheap_provider.seen_models[-1] == "tiny-2",
            f"模型名按次传入，不改全局配置：{cheap_provider.seen_models[-1]}",
        )
        check(
            payload["provider"]["id"] == "cheap"
            and payload["provider"]["model"] == "tiny-2",
            f"返回值说明实际用了谁：{payload.get('provider')}",
        )

        # 配置里的 ID 失效 → 明确报错，绝不静默换一个模型顶替
        plugin._llm_enrich_provider_id = "ghost"
        plugin_main.request = FakeWebRequest({"only_missing": False})
        payload = body_of(asyncio.run(plugin.page_enrich_tools()))
        check(payload.get("updated") == {}, "配置的 ID 失效时不写入任何东西")
        check(
            any("ghost" in str(item) for item in payload.get("errors") or []),
            f"报错里带上失效的 ID：{payload.get('errors')}",
        )
        plugin._llm_enrich_provider_id = ""

        # ---- 试跑：走真实总结路径（同一个 system prompt、同一个 JSON 解析）----
        plugin_main.request = FakeWebRequest({"provider_id": "cheap"})
        payload = body_of(asyncio.run(plugin.page_test_provider()))
        check(payload.get("ok") is True, f"试跑成功：{payload.get('sample')}")
        check(isinstance(payload.get("latency_ms"), int), "给出耗时")
        check(
            OVERRIDES.get("neko_ping_tool") is None,
            "试跑用的是合成工具名，不会往覆盖层写东西",
        )

        plugin_main.request = FakeWebRequest({"provider_id": "ghost"})
        resp = asyncio.run(plugin.page_test_provider())
        check(resp.status_code == 400, "试跑一个不存在的提供商返回 400")

        # ---- 设为默认：写进插件配置并落盘 ----
        class FakeConfig(dict):
            def __init__(self):
                super().__init__()
                self.saved = 0

            def save_config(self, *args, **kwargs):
                self.saved += 1

        real_config = plugin.config
        cfg = FakeConfig()
        plugin.config = cfg
        try:
            plugin_main.request = FakeWebRequest(
                {"provider_id": "cheap", "model": "tiny-3"}
            )
            payload = body_of(asyncio.run(plugin.page_save_provider()))
            check(payload.get("persisted") is True, "providers/save 把配置落盘了")
            check(cfg.get("llm_enrich_provider_id") == "cheap", f"写进了配置：{dict(cfg)}")
            check(plugin._llm_enrich_provider_id == "cheap", "内存里的设置同步了")
            check(
                plugin._summarizer_label().startswith("tiny-3"),
                f"总览的说明随之更新：{plugin._summarizer_label()}",
            )

            plugin_main.request = FakeWebRequest({"provider_id": "ghost"})
            resp = asyncio.run(plugin.page_save_provider())
            check(resp.status_code == 400, "存一个不存在的提供商被拒")
            check(cfg.get("llm_enrich_provider_id") == "cheap", "被拒后配置没被改坏")
        finally:
            plugin.config = real_config
            plugin._llm_enrich_provider_id = ""
            plugin._llm_enrich_model = ""

        # ---- 模型不可用时只报错、不崩 ----
        class NoProviderContext:
            def register_web_api(self, *args):
                pass

            def get_llm_tool_manager(self):
                return llm_tools

        plugin.context = NoProviderContext()
        # 注意用 only_missing=False：此时该工具已有标签，only_missing=True 会没有候选，
        # 那条路径压根不需要 provider，测不到"取不到模型"的分支。
        plugin_main.request = FakeWebRequest({"only_missing": False})
        resp = asyncio.run(plugin.page_enrich_tools())
        body = body_of(resp)
        check(resp.status_code == 200, "取不到对话模型时不返回 500")
        check(
            any("对话模型" in str(item) for item in body.get("errors") or []),
            f"给出可读错误：{body.get('errors')}",
        )
        check(body.get("updated") == {}, "取不到模型时不写入任何覆盖")
    finally:
        plugin.context = real_ctx2
        OVERRIDES.remove("meta_test_tool")
        REGISTRY.forget_source("smoke")
        OVERRIDES.apply_all(REGISTRY)
        OVERRIDES.path = None
        plugin._rebuild_index()
        shutil.rmtree(scratch, ignore_errors=True)

    print("17. 归纳学习：钩子注册、学习闭环、人工纠正")

    from astrbot.core.star.star_handler import EventType, star_handlers_registry

    learning_mod = importlib.import_module(f"{PKG_ALIAS}.core.learning")
    LEARNING = learning_mod.LEARNING

    hook_events = {
        handler.event_type
        for handler in star_handlers_registry.get_handlers_by_module_name(
            f"{PKG_ALIAS}.main"
        )
    }
    check(
        EventType.OnUsingLLMToolEvent in hook_events,
        "@filter.on_using_llm_tool 已进入 AstrBot 事件表（不是只写了个装饰器）",
    )
    check(
        EventType.OnLLMToolRespondEvent in hook_events,
        "@filter.on_llm_tool_respond 已进入 AstrBot 事件表",
    )

    learn_dir = PLUGIN_ROOT / ".selftest-scratch" / "smoke-learning"
    shutil.rmtree(learn_dir, ignore_errors=True)
    learn_dir.mkdir(parents=True, exist_ok=True)
    real_learn_path = LEARNING.path
    real_learn_max = LEARNING.max_examples
    real_learn_override_path = OVERRIDES.path
    real_ctx3 = plugin.context
    LEARNING.path = learn_dir / "learned_examples.json"
    OVERRIDES.path = learn_dir / "tool_overrides.json"
    LEARNING.max_examples = 20
    LEARNING.clear()
    try:
        registered6: list[tuple[str, list[str]]] = []

        class FakeContext6:
            def register_web_api(self, route, handler, methods, description):
                registered6.append((route, list(methods)))

            def get_llm_tool_manager(self):
                return llm_tools

        plugin.context = FakeContext6()
        plugin._register_web_apis()
        routes6 = dict(registered6)
        for suffix, method in (
            ("learning/list", "GET"),
            ("learning/clear", "POST"),
            ("learning/forget", "POST"),
            ("learning/induct", "POST"),
        ):
            route = f"/{plugin_main.PLUGIN_NAME}/{suffix}"
            check(routes6.get(route) == [method], f"注册了路由 {route} [{method}]")

        # 专门造一个"活着"的工具当被测对象：前面小节把 demo_tools 的目录删掉了，
        # 那个来源已经被退休（定义还在，但永不参与检索），不适合用来证明"学到了"。
        probe = "learn_probe_tool"
        REGISTRY.register(
            ToolMeta(
                name=probe,
                description="probe tool",
                module="smoke.learn",
                source="smoke_learn",
            )
        )
        plugin._rebuild_index()

        # 选一句"现有工具全都匹配不上"的口语，用来证明学习确实带来了新召回
        phrase = "帮我订一张去上海的机票"

        # 造一个真实的 FunctionTool 放进本轮工具集：_decide 的许可池就是从这里来的，
        # 工具不在池子里的话 _decide 会提前返回，压根不会记下本轮原话。
        probe_ft = FunctionTool(
            name=probe,
            description="probe tool",
            parameters={"type": "object", "properties": {}},
            handler=None,
        )
        toolset6 = ToolSet()
        toolset6.add_tool(probe_ft)
        for meta_name in ("lazy_search_tools", "lazy_activate_tool", "lazy_deactivate_tool"):
            if meta_name in real:
                toolset6.add_tool(real[meta_name])
        event6 = types.SimpleNamespace(
            unified_msg_origin="aiocqhttp:GroupMessage:learn",
            message_str=phrase,
        )

        check(
            plugin.index.search(phrase, top_k=3, min_score=0.35) == [],
            f"学之前「{phrase}」召回不到任何工具",
        )

        request6 = types.SimpleNamespace(prompt=phrase, func_tool=toolset6)
        plugin._decide(event6, request6)
        turn = plugin._turns.get(event6.unified_msg_origin)
        check(
            turn is not None and turn.query == phrase,
            f"本轮用户原话被记进 TurnContext：{turn and turn.query}",
        )

        probe_tool = probe_ft

        # ---- 模型真的调用了这个工具：这是学习信号 ----
        asyncio.run(plugin.on_using_llm_tool(event6, probe_tool, {"text": "x"}))
        check(
            LEARNING.examples(probe) == [phrase],
            f"钩子把（原话 → 工具）记成样例：{LEARNING.examples(probe)}",
        )
        check(
            (learn_dir / "learned_examples.json").is_file(),
            "学习数据已落盘（重启不丢）",
        )
        hits = plugin.index.search(phrase, top_k=3, min_score=0.35)
        check(
            bool(hits) and hits[0][0].name == probe,
            f"学完之后同一句话能召回该工具：{[(m.name, round(s, 3)) for m, s in hits]}",
        )

        # ---- 别人的工具不该被学 ----
        alien = FunctionTool(
            name="astrbot_file_read_tool",
            description="别人的工具",
            parameters={"type": "object", "properties": {}},
            handler=None,
        )
        asyncio.run(plugin.on_using_llm_tool(event6, alien, None))
        check(
            LEARNING.examples("astrbot_file_read_tool") == [],
            "只学本插件注册表认得的工具，不去学别人的",
        )

        # ---- tool_result=None 是成功路径，绝不能当成失败 ----
        asyncio.run(plugin.on_llm_tool_respond(event6, probe_tool, None, None))
        check(
            LEARNING.examples(probe) == [phrase],
            "结果为空（工具直接发消息给用户）不算失败，样例保留",
        )

        # ---- isError=True 才算失败：命中 2 次才抵消掉 ----
        asyncio.run(plugin.on_using_llm_tool(event6, probe_tool, {"text": "x"}))
        failed_result = types.SimpleNamespace(isError=True)
        asyncio.run(plugin.on_llm_tool_respond(event6, probe_tool, None, failed_result))
        check(
            LEARNING.examples(probe) == [phrase],
            "命中 2 次、失败 1 次，样例还在",
        )
        asyncio.run(plugin.on_llm_tool_respond(event6, probe_tool, None, failed_result))
        check(
            LEARNING.examples(probe) == [],
            "失败次数追平命中次数后样例作废（学错了会自我修正）",
        )
        check(
            plugin.index.search(phrase, top_k=3, min_score=0.35) == [],
            "样例作废后索引同步失效",
        )

        # ---- 归纳：多条样例里反复出现的词 → 候选标签 ----
        for text in ("帮我把这句话念一遍", "再念一遍刚才那句", "念一遍我发的话"):
            LEARNING.record(probe, text)
        LEARNING.apply(REGISTRY)
        plugin._rebuild_index()

        plugin_main.request = FakeWebRequest({})
        payload = body_of(asyncio.run(plugin.page_learning_list()))
        check(payload.get("enabled") is True, "learning/list 报告学习已启用")
        entry = next(
            (item for item in payload.get("tools", []) if item["name"] == probe),
            None,
        )
        check(entry is not None and entry["count"] == 3, f"列出一条工具 3 条样例：{entry and entry['count']}")
        check(
            "一遍" in (entry or {}).get("suggested_tags", []),
            f"归纳出候选标签：{(entry or {}).get('suggested_tags')}",
        )

        # ---- 采纳归纳标签：写成 manual 覆盖，模型总结也冲不掉 ----
        # 注意顺序：归纳要求"至少 3 条样例"，所以必须在删样例之前做。
        plugin_main.request = FakeWebRequest({"names": [probe]})
        payload = body_of(asyncio.run(plugin.page_learning_induct()))
        check(probe in (payload.get("adopted") or {}), f"采纳结果：{payload}")
        meta = REGISTRY.get(probe)
        check("一遍" in meta.tags, f"标签已写进注册表：{meta.tags}")
        check(OVERRIDES.get(probe) is not None, "同时写了覆盖")
        check(OVERRIDES.get(probe).source == "manual", "来源是 manual（人工动作）")
        check(
            (learn_dir / "tool_overrides.json").is_file(),
            "归纳出的标签已落盘（重启后仍在）",
        )
        check(
            bool(plugin.index.search("念一遍这个", top_k=3, min_score=0.35)),
            "采纳的标签立刻参与检索",
        )

        # ---- 人工删除单条样例（自动负反馈几乎不可用时唯一的纠正手段）----
        plugin_main.request = FakeWebRequest({"name": probe, "text": "再念一遍刚才那句"})
        payload = body_of(asyncio.run(plugin.page_learning_forget()))
        check(payload.get("count") == 2, f"learning/forget 删掉一条：剩 {payload.get('count')}")
        check(
            "再念一遍刚才那句" not in LEARNING.examples(probe),
            "被删的样例确实不在存储里了",
        )

        plugin_main.request = FakeWebRequest({"name": probe, "text": "根本没这条"})
        resp = asyncio.run(plugin.page_learning_forget())
        check(resp.status_code == 400, "删不存在的样例返回 400 而不是静默成功")

        # ---- 自动采纳开关：默认关，开了才动 ----
        OVERRIDES.remove(probe)
        OVERRIDES.apply_all(REGISTRY)  # meta.tags 回到基线（空）
        LEARNING.clear(probe)
        for text in ("帮我把这句话念一遍", "再念一遍刚才那句", "念一遍我发的话"):
            LEARNING.record(probe, text)
        check(
            plugin._maybe_auto_induct(probe) == []
            and REGISTRY.get(probe).tags == (),
            "auto_induct_tags 默认关闭：即使算得出候选也不采纳",
        )

        plugin._apply_config(
            {
                "enabled": True,
                "prefetch_enabled": True,
                "min_score": 0.35,
                "auto_induct_tags": True,
            }
        )
        adopted_auto = plugin._maybe_auto_induct(probe)
        check(
            "一遍" in adopted_auto,
            f"开启后达到门槛即自动采纳：{adopted_auto}",
        )
        check(
            "一遍" in REGISTRY.get(probe).tags,
            f"自动采纳也写进注册表：{REGISTRY.get(probe).tags}",
        )
        plugin._apply_config(
            {"enabled": True, "prefetch_enabled": True, "min_score": 0.35}
        )
        OVERRIDES.remove(probe)
        OVERRIDES.apply_all(REGISTRY)
        plugin._rebuild_index()

        # ---- 清空 ----
        plugin_main.request = FakeWebRequest({})
        payload = body_of(asyncio.run(plugin.page_learning_clear()))
        check(payload.get("total") == 0, f"learning/clear 清空：{payload}")
        check(LEARNING.count() == 0, "内存里的样例也清空了")
    finally:
        plugin.context = real_ctx3
        LEARNING.clear()
        LEARNING.path = real_learn_path
        LEARNING.max_examples = real_learn_max
        OVERRIDES.path = real_learn_override_path
        if OVERRIDES.get("learn_probe_tool") is not None:
            OVERRIDES.remove("learn_probe_tool")
        OVERRIDES.apply_all(REGISTRY)
        REGISTRY.forget_source("smoke_learn")
        plugin._rebuild_index()
        shutil.rmtree(learn_dir, ignore_errors=True)

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
