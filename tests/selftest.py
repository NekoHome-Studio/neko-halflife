# -*- coding: utf-8 -*-
"""纯逻辑自检：不依赖 AstrBot，直接 ``python tests/selftest.py`` 运行。

覆盖检索打分、双过期、裁剪安全性，以及子插件上传的校验。
最重要的两项是**对抗性**的：``test_pruning_never_exceeds_pool`` 保证不会把
上游许可池之外的工具塞进请求；``test_install_roundtrip`` 保证 zip-slip
攻击写不出任何文件。
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from core import injector, uploads  # noqa: E402
from core.activation import ActivationStore  # noqa: E402
from core.models import RISK_HIGH, ToolMeta  # noqa: E402
from core.registry import ToolRegistry  # noqa: E402
from core.retriever import ToolIndex, tokenize  # noqa: E402
from core.uploads import UploadError  # noqa: E402

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


# ----------------------------------------------------------------------
# 6. 子插件上传校验（安全关键）
# ----------------------------------------------------------------------


def _expect_upload_error(fn, label: str) -> None:
    _expect_raises(fn, f"{label} → 已拒绝", UploadError)


def _expect_raises(fn, label: str, exc_type: type[BaseException]) -> None:
    try:
        fn()
    except exc_type as exc:
        check(True, f"{label}（{str(exc)[:36]}…）")
    except Exception as exc:  # noqa: BLE001
        check(False, f"{label} → 抛出了 {type(exc).__name__}，期望 {exc_type.__name__}")
    else:
        check(False, f"{label} → 竟然通过了")


def test_sanitize_name() -> None:
    print("uploads / 子插件名白名单")
    check(uploads.sanitize_subplugin_name("demo_tools.py") == "demo_tools", "接受 .py")
    check(uploads.sanitize_subplugin_name("weather.zip") == "weather", "接受 .zip")
    check(uploads.sanitize_subplugin_name("plain") == "plain", "接受无扩展名")
    check(
        uploads.sanitize_subplugin_name("dir/sub/name.py") == "name",
        "只取最后一段，挡住伪路径",
    )
    check(
        uploads.sanitize_subplugin_name("C:\\evil\\name.py") == "name",
        "反斜杠路径同样只取最后一段",
    )

    for bad, why in (
        ("_hidden.py", "下划线开头（加载器会跳过）"),
        (".secret.py", "点号开头（加载器会跳过）"),
        ("my-pack.py", "含连字符（不是合法模块名）"),
        ("class.py", "Python 关键字"),
        ("1abc.py", "数字开头"),
        ("", "空名字"),
        ("a" * 80 + ".py", "超长"),
    ):
        _expect_upload_error(
            lambda b=bad: uploads.sanitize_subplugin_name(b), why
        )

    # 关键不变量：合法名字必须是合法 Python 标识符，否则加载器构造模块名会出问题
    ok = all(
        uploads.sanitize_subplugin_name(f"{n}.py").isidentifier()
        for n in ("a", "abc_1", "X9")
    )
    check(ok, "通过校验的名字一定是合法 Python 标识符")


def test_zip_member_safety() -> None:
    print("uploads / zip 成员名安全")
    for good in ("a.py", "pkg/__init__.py", "pkg/sub/mod.py", "./a.py"):
        check(uploads.is_safe_zip_member(good), f"放行 {good}")
    for bad in ("../evil.py", "pkg/../../evil.py", "/abs.py", "C:/abs.py", "a\\..\\b.py"):
        check(not uploads.is_safe_zip_member(bad), f"拦截 {bad}")


def test_package_root_detection() -> None:
    print("uploads / 包根识别")
    check(uploads.detect_package_root(["__init__.py", "a.py"]) == "", "根布局")
    check(
        uploads.detect_package_root(["my_pack/__init__.py", "my_pack/a.py"]) == "my_pack",
        "单层包装（GitHub zip 常见）",
    )
    _expect_upload_error(
        lambda: uploads.detect_package_root(["a.py", "b.py"]), "没有 __init__.py"
    )
    _expect_upload_error(
        lambda: uploads.detect_package_root(
            ["x/__init__.py", "y/__init__.py"]
        ),
        "两个候选包根，无法判断",
    )


def test_zip_plan_rejects() -> None:
    print("uploads / 落盘前校验")

    def info(name: str, size: int = 10, mode: int = 0o100644) -> zipfile.ZipInfo:
        item = zipfile.ZipInfo(name)
        item.file_size = size
        item.external_attr = mode << 16
        return item

    check(
        uploads.plan_zip_upload(
            [info("__init__.py"), info("a.py")], max_total_bytes=1000
        )
        == "",
        "正常包通过校验并返回根前缀",
    )
    _expect_upload_error(
        lambda: uploads.plan_zip_upload(
            [info("../evil/__init__.py")], max_total_bytes=1000
        ),
        "含 ../ 的成员",
    )
    _expect_upload_error(
        lambda: uploads.plan_zip_upload(
            [info("__init__.py"), info("big.bin", size=99999)], max_total_bytes=1000
        ),
        "自报体积超限（压缩炸弹）",
    )
    _expect_upload_error(
        lambda: uploads.plan_zip_upload([], max_total_bytes=1000), "空包"
    )
    _expect_upload_error(
        lambda: uploads.plan_zip_upload(
            [info(f"f{i}.py") for i in range(uploads.MAX_ENTRIES + 1)],
            max_total_bytes=10**9,
        ),
        "条目数超限",
    )
    # 符号链接：Unix 权限位为 0o120777
    _expect_upload_error(
        lambda: uploads.plan_zip_upload(
            [info("__init__.py"), info("link", mode=0o120777)], max_total_bytes=1000
        ),
        "含符号链接成员",
    )


def test_install_roundtrip() -> None:
    print("uploads / 真实安装与删除（含 zip-slip 攻击）")
    import shutil

    # 刻意用插件目录内的 scratch，而不是系统临时目录：某些受限环境（含本项目的
    # 开发沙箱）不允许在系统 temp 下创建/删除目录，用 tempdir 会让测试本身失败。
    scratch = PLUGIN_ROOT / ".selftest-scratch"
    if scratch.exists():
        shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)

    try:
        _install_roundtrip_body(scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _install_roundtrip_body(raw: Path) -> None:
    root = raw / "sub_plugins"
    root.mkdir()

    # 1) 单文件安装
    src_py = raw / "demo.py"
    src_py.write_text("x = 1\n", encoding="utf-8")
    dest = uploads.install_py_file(src_py, root, "demo")
    check(dest.is_file() and dest.name == "demo.py", "单文件子插件安装成功")
    _expect_upload_error(
        lambda: uploads.install_py_file(src_py, root, "demo"), "重复安装被拒"
    )
    check(uploads.remove_subplugin(root, "demo"), "单文件可删除")
    check(not dest.exists(), "删除后文件消失")

    # 2) 正常 zip 包安装（带一层包装目录）
    good_zip = raw / "pack.zip"
    with zipfile.ZipFile(good_zip, "w") as archive:
        archive.writestr("my_pack/__init__.py", "y = 2\n")
        archive.writestr("my_pack/helper.py", "z = 3\n")
    dest = uploads.install_zip_package(
        good_zip, root, "my_pack", max_total_bytes=10**7
    )
    check(
        (dest / "__init__.py").is_file() and (dest / "helper.py").is_file(),
        "zip 包子插件安装成功且剥掉了包装目录",
    )
    check(uploads.remove_subplugin(root, "my_pack"), "zip 子插件可删除")

    # 3) zip-slip 攻击：成员试图写到 sub_plugins 之外
    evil_zip = raw / "evil.zip"
    with zipfile.ZipFile(evil_zip, "w") as archive:
        archive.writestr("__init__.py", "ok\n")
        archive.writestr("../pwned.txt", "owned\n")
    _expect_upload_error(
        lambda: uploads.install_zip_package(
            evil_zip, root, "evil", max_total_bytes=10**7
        ),
        "zip-slip 成员",
    )
    check(not (raw / "pwned.txt").exists(), "zip-slip 没有写出任何文件")
    check(not (root / "evil").exists(), "失败的安装没有留下半个子插件")

    # 4) 不是 zip 的文件
    _expect_upload_error(
        lambda: uploads.install_zip_package(
            src_py, root, "notzip", max_total_bytes=10**7
        ),
        "非 zip 文件",
    )


def test_plugin_import_analysis() -> None:
    print("plugin_import / 已装插件的可移植性分析")
    import shutil

    scratch = PLUGIN_ROOT / ".selftest-scratch"
    if scratch.exists():
        shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        _plugin_import_body(scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _plugin_import_body(raw: Path) -> None:
    from core import plugin_import

    def make(name: str, source: str, metadata: str = "name: x\n") -> Path:
        path = raw / "plugins" / name
        path.mkdir(parents=True)
        (path / "main.py").write_text(source, encoding="utf-8")
        (path / "metadata.yaml").write_text(metadata, encoding="utf-8")
        return path

    # 1) 可导入：模块级普通函数 + @lazy_tool
    good = make(
        "good_tools",
        "from astrbot.api.event import AstrMessageEvent\n\n"
        "@lazy_tool(name='good_tool', tags=('t',))\n"
        "async def good_tool(event: AstrMessageEvent, x: str) -> str:\n"
        "    return x\n",
    )
    analysis = plugin_import.analyze_plugin_dir(good)
    check(analysis.portable, f"普通函数工具集判定为可导入（reason={analysis.reasons}）")
    check(analysis.lazy_tool_names == ["good_tool"], f"识别工具名：{analysis.lazy_tool_names}")
    check(analysis.tool_modules == ["main"], f"识别工具模块：{analysis.tool_modules}")

    # 2) 拒绝：用了 AstrBot 装饰器，却没有 Star 子类可供实例化
    bad = make(
        "bad_tools",
        "from astrbot.api.event import filter\n\n"
        "@filter.llm_tool(name='bad')\n"
        "async def bad(self, event):\n"
        "    return 'x'\n",
    )
    analysis = plugin_import.analyze_plugin_dir(bad)
    check(not analysis.portable, "没有任何 Star 子类的插件被拒绝")
    check(
        any("Star 子类" in reason for reason in analysis.reasons),
        "拒绝原因点名缺少 Star 子类",
    )

    # 2b) 宿主模式：常规插件（Star 子类 + @filter.llm_tool）现在可导入
    hosted = make(
        "hosted_tools",
        "from astrbot.api.event import filter\n"
        "from astrbot.api.star import Star\n\n\n"
        "class HostedPlugin(Star):\n"
        "    @filter.llm_tool(name='hosted_tool')\n"
        "    async def hosted_tool(self, event, x: str):\n"
        "        return x\n",
    )
    analysis = plugin_import.analyze_plugin_dir(hosted)
    check(analysis.portable, f"常规插件判定为可导入（{analysis.reasons}）")
    check(analysis.mode == "hosted", f"走宿主模式：{analysis.mode}")
    check(analysis.has_star_class, "识别出 Star 子类")
    check("main" in analysis.import_modules, f"入口要导入的模块：{analysis.import_modules}")
    check(bool(analysis.warnings), "宿主模式给出提示")

    # 3) 拒绝：没有任何 @lazy_tool（导入后不会注册任何工具）
    plain = make("plain", "x = 1\n")
    analysis = plugin_import.analyze_plugin_dir(plain)
    check(
        not analysis.portable
        and any("@lazy_tool" in reason for reason in analysis.reasons),
        "没有 @lazy_tool 的插件被拒绝",
    )

    # 4) Star 子类只警告、不阻断
    mixed = make(
        "mixed",
        "from astrbot.api.star import Star\n\n\n"
        "class P(Star):\n    pass\n\n\n"
        "@lazy_tool()\n"
        "async def tool_a(event, x: str) -> str:\n    return x\n",
    )
    analysis = plugin_import.analyze_plugin_dir(mixed)
    check(
        analysis.portable and analysis.has_star_class and bool(analysis.warnings),
        "有 Star 子类时只警告不阻断",
    )
    check("tool_a" in analysis.lazy_tool_names, "没写 name= 时用函数名兜底")

    # 5) 生成的包入口
    entry = plugin_import.build_entry_source(["main", "pkg.tools"])
    check("from . import main" in entry, "根模块生成 from . import main")
    check("from .pkg import tools" in entry, "子包模块生成 from .pkg import tools")

    # 6) 复制：不搬 metadata.yaml，且默认拒绝覆盖
    dest = raw / "sub_plugins" / "good_tools"
    copied = plugin_import.copy_plugin_tree(good, dest)
    check(copied >= 1 and (dest / "main.py").is_file(), f"复制了 {copied} 个文件")
    check(
        not (dest / "metadata.yaml").exists(),
        "不复制 metadata.yaml（子插件不是 AstrBot 插件）",
    )
    _expect_raises(
        lambda: plugin_import.copy_plugin_tree(good, dest),
        "目标已存在且未允许覆盖",
        FileExistsError,
    )
    check(
        plugin_import.copy_plugin_tree(good, dest, overwrite=True) >= 1,
        "允许覆盖时复制成功",
    )
    entry_path = plugin_import.write_entry(dest, ["main"])
    check(
        entry_path.is_file()
        and "from . import main" in entry_path.read_text(encoding="utf-8"),
        "写入包入口 __init__.py",
    )


def test_overrides_store() -> None:
    print("overrides / 覆盖层：施加、重置、落盘")
    import shutil

    from core.overrides import OverrideStore, normalize_tags

    check(normalize_tags("天气, 气温 ,天气") == ["天气", "气温"], "字符串去重去空白")
    check(normalize_tags(["a", "#b", "  "]) == ["a", "b"], "列表去井号与空项")
    check(len(normalize_tags([f"t{i}" for i in range(50)])) <= 12, "标签数量有上限")
    check(normalize_tags(None) == [], "None 得到空列表")
    check(normalize_tags("中文，全角逗号") == ["中文", "全角逗号"], "全角逗号也切分")

    scratch = PLUGIN_ROOT / ".selftest-scratch" / "overrides"
    if scratch.exists():
        shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)

    store = OverrideStore(scratch / "ov.json")
    meta = ToolMeta(
        name="t1", description="基线描述", module="m", source="main", tags=("原始",)
    )
    store.apply(meta)
    check(meta.tags == ("原始",) and meta.description == "基线描述", "无覆盖时等于基线")

    store.set("t1", tags=["新标签", "另一个"], source="manual")
    store.apply(meta)
    check(meta.tags == ("新标签", "另一个"), "覆盖生效")
    check(meta.description == "基线描述", "只覆盖 tags 时描述保持基线")

    store.set("t1", tags=["手改"], source="manual")
    store.set("t1", tags=["llm改的"], source="llm")
    store.apply(meta)
    check(meta.tags == ("手改",), "LLM 覆盖不会盖掉用户手改")

    store.apply_all.__self__  # noqa: B018 - 仅确认方法存在
    check(store.save() is True, "落盘成功")
    reloaded = OverrideStore(scratch / "ov.json")
    check(reloaded.load() == 1, "重新加载读回 1 条覆盖")
    check(reloaded.get("t1").tags == ["手改"], "落盘内容正确")

    store.set("t1", tags=[], source="manual")
    store.apply(meta)
    check(meta.tags == (), "显式传空列表 = 清空标签（与 None 区分）")

    store.remove("t1")
    store.apply(meta)
    check(meta.tags == ("原始",), "重置退回基线标签")
    check(meta.description == "基线描述", "重置退回基线描述")
    check(store.forget_missing({"other"}) == 0, "forget_missing 对不存在的名字是空操作")

    shutil.rmtree(scratch, ignore_errors=True)


def test_tags_drive_retrieval() -> None:
    print("overrides / 标签改动真的影响召回（本功能的意义所在）")
    from core.overrides import OverrideStore

    store = OverrideStore()
    meta = ToolMeta(
        name="legacy_tool",
        description="do something vague",
        module="m",
        source="main",
        tags=(),
    )
    index = ToolIndex()
    index.build([meta])
    check(
        index.search("帮我查一下北京的天气", top_k=3, min_score=0.3) == [],
        "无标签时口语查询召回不到",
    )

    store.apply(meta)
    store.set("legacy_tool", tags=["天气", "气温", "weather"], source="manual")
    store.apply(meta)
    index.build([meta])
    hits = index.search("帮我查一下北京的天气", top_k=3, min_score=0.3)
    check(
        bool(hits) and hits[0][0].name == "legacy_tool",
        "补上标签后同一句话就能召回",
    )


def test_enrich_parsing() -> None:
    print("enrich / 模型输出的多级兜底解析")
    from core.enrich import collect_briefs, parse_enrichment

    plain = '{"t1": {"summary": "查天气", "tags": ["天气", "气温"]}}'
    check(parse_enrichment(plain)["t1"]["tags"] == ["天气", "气温"], "纯 JSON 直接解析")

    fenced = '```json\n{"t1": {"summary": "查天气", "tags": ["天气"]}}\n```'
    check("t1" in parse_enrichment(fenced), "剥掉 ``` 围栏")

    noisy = '好的，结果如下：\n{"t1": {"summary": "查天气", "tags": ["天气"]}}\n希望有帮助！'
    check("t1" in parse_enrichment(noisy), "从寒暄噪声里抠出配平的对象")

    nested = '{"tools": {"t1": {"summary": "x", "tags": ["a"]}}}'
    check("t1" in parse_enrichment(nested), "识别被 tools 包了一层的形状")

    as_list = '[{"name": "t1", "summary": "x", "tags": ["a"]}]'
    check("t1" in parse_enrichment(as_list), "识别数组形状")

    string_form = '{"t1": "查天气"}'
    check(
        parse_enrichment(string_form)["t1"]["summary"] == "查天气",
        "值为字符串时当作 summary",
    )

    check(parse_enrichment("完全不是 JSON") == {}, "纯文本返回空（不抛错）")
    check(parse_enrichment("") == {}, "空串返回空")
    check(parse_enrichment('{"t1": {"tags": ["a"]}}', {"other"}) == {}, "过滤掉未请求的工具名")
    check(
        parse_enrichment('{"t1": {"tags": ["  ", "#"]}}') == {},
        "只有空标签时视为没有产出",
    )

    registry = ToolRegistry()
    tagged = ToolMeta(name="has_tags", description="d", module="m", tags=("已有",))
    untagged = ToolMeta(name="no_tags", description="d", module="m", tags=())
    registry.register(tagged)
    registry.register(untagged)
    only_missing = collect_briefs(registry, None, only_missing=True)
    check(
        [brief.name for brief in only_missing] == ["no_tags"],
        "only_missing 只挑没有标签的",
    )
    all_briefs = collect_briefs(registry, None, only_missing=False)
    check(len(all_briefs) == 2, "only_missing=False 时全都送")


def test_identifier_splitting() -> None:
    print("retriever / 标识符拆分（修掉 weather 匹配不到 get_weather 的漏洞）")
    from core.retriever import split_identifier

    check(
        split_identifier("get_weather") == ["get", "weather"],
        f"snake_case 拆分：{split_identifier('get_weather')}",
    )
    check(
        set(split_identifier("GetWeather")) == {"get", "weather"},
        f"PascalCase 拆分：{split_identifier('GetWeather')}",
    )
    check(
        set(split_identifier("get-weather.v2")) == {"get", "weather", "v2"},
        f"连字符与点号也拆：{split_identifier('get-weather.v2')}",
    )

    tokens = tokenize("get_weather")
    check("get_weather" in tokens, "整词仍然保留（改动是超集，不丢原有能力）")
    check("weather" in tokens and "get" in tokens, "子词也进了查询 token")

    index = ToolIndex()
    index.build(
        [
            ToolMeta(
                name="get_weather",
                description="query the weather",
                module="m",
                tags=(),
            )
        ]
    )
    hits = index.search("weather", top_k=3, min_score=0.3)
    check(
        bool(hits) and hits[0][0].name == "get_weather",
        "用户说 weather 就能召回 get_weather（这正是本次修复）",
    )


def test_fuzzy_and_single_char() -> None:
    print("retriever / 错拼容忍与单字兜底")
    index = ToolIndex()
    index.build(
        [
            ToolMeta(
                name="weather_tool",
                description="check the weather forecast",
                module="m",
                tags=("weather",),
            ),
            ToolMeta(
                name="cold_tool",
                description="查询天气冷的时候要穿什么",
                module="m",
                tags=(),
            ),
        ]
    )
    hits = index.search("wether", top_k=3, min_score=0.2)
    check(
        bool(hits) and hits[0][0].name == "weather_tool",
        f"拼错一个字母也能召回：{[(m.name, round(s, 3)) for m, s in hits]}",
    )
    exact = index.search("weather", top_k=1, min_score=0.0)
    check(
        bool(exact) and hits[0][1] < exact[0][1],
        f"但拼错的分数低于拼对：{round(hits[0][1], 3)} < {round(exact[0][1], 3)}",
    )
    check(
        index.search("wether", top_k=3, min_score=0.9) == [],
        "拼错的分数被 0.6 折扣压到高阈值之下（阈值对它是有效的）",
    )
    check(
        index.informative_tokens("wether") != [],
        "诊断口径与检索一致（错拼也算信息词）",
    )

    hits = index.search("冷", top_k=3, min_score=0.2)
    check(
        bool(hits) and hits[0][0].name == "cold_tool",
        f"单字查询靠单字索引兜底命中：{[(m.name, round(s, 3)) for m, s in hits]}",
    )


def test_learning_store() -> None:
    print("learning / 学习样例的存取与自我修正")
    import shutil

    from core.learning import LearningStore, normalize_query

    check(normalize_query("  a  ") == "", "过短的句子不学")
    check(normalize_query("x" * 200) == "", "过长的句子不学")
    check(normalize_query(" 帮我  查天气 ") == "帮我 查天气", "空白被规整")

    scratch = PLUGIN_ROOT / ".selftest-scratch" / "learning"
    if scratch.exists():
        shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)

    store = LearningStore(scratch / "learned.json", max_examples=3)
    check(store.record("t1", "帮我查天气") is True, "记录一条样例")
    check(store.record("t1", "帮我查天气") is True, "重复记录命中原样例")
    check(store.examples("t1") == ["帮我查天气"], "重复不产生新样例")
    check(store.count() == 1, "计数正确")

    for i in range(5):
        store.record("t1", f"第{i}种说法")
    check(len(store.examples("t1")) <= 3, f"样例数受上限约束：{store.examples('t1')}")
    check("帮我查天气" in store.examples("t1"), "命中最多的样例优先保留")

    # 负反馈：这条样例命中过 2 次（上面记了两遍），要失败 2 次才作废
    check(store.record_failure("t1", "帮我查天气") is True, "记一次负反馈")
    check("帮我查天气" in store.examples("t1"), "失败次数还不够，样例先保留")
    check(store.record_failure("t1", "帮我查天气") is True, "再记一次负反馈")
    check(
        "帮我查天气" not in store.examples("t1"),
        "失败次数达到命中次数后样例被作废",
    )

    store.record("t2", "另一种说法")
    check(store.save() is True, "落盘成功")
    reloaded = LearningStore(scratch / "learned.json")
    check(reloaded.load() >= 1, "重新加载读回样例")
    check(reloaded.examples("t2") == ["另一种说法"], "落盘内容正确")

    check(store.clear("t2") == 1, "清除指定工具")
    check(store.clear() >= 0, "清除全部")
    check(store.count() == 0, "清空后计数为 0")
    shutil.rmtree(scratch, ignore_errors=True)


def test_induction_suggests_tags() -> None:
    print("learning / 归纳：反复出现的词升格为候选标签")
    from core.learning import LearningStore

    store = LearningStore(None, max_examples=50)
    for text in ("帮我报销一下差旅费", "这个月报销还没提交", "报销单怎么填"):
        store.record("reimburse_tool", text)
    store.record("reimburse_tool", "把发票给我")

    suggestions = store.suggest_tags("reimburse_tool", [], min_hits=3)
    check("报销" in suggestions, f"高频词被提议为标签：{suggestions}")
    check("把发票给我" not in suggestions, "整句不会被当成标签")
    check(
        "报销" not in store.suggest_tags("reimburse_tool", ["报销"], min_hits=3),
        "已经是标签的就不再重复提议",
    )
    check(
        all(len(tag) >= 2 for tag in suggestions),
        "不提议单字（噪声太大）",
    )
    check(
        store.suggest_tags("reimburse_tool", [], min_hits=99) == [],
        "门槛调高就没有提议",
    )

    store.record("repeat_tool", "报销 报销 报销")
    store.record("repeat_tool", "随便说点别的吧")
    store.record("repeat_tool", "再随便说点啥")
    check(
        store.suggest_tags("repeat_tool", [], min_hits=3) == [],
        "同一个词在一句话里重复三遍不算三次证据（按不同样例计数）",
    )


def test_learning_improves_recall() -> None:
    print("learning / 归纳学习真的提升召回（本功能的因果证明）")
    from core.learning import LearningStore

    meta = ToolMeta(
        name="ticket_tool",
        description="submit something",
        module="m",
        source="main",
        tags=(),
    )
    index = ToolIndex()
    index.build([meta])
    question = "帮我把这个工单提一下"
    check(
        index.search(question, top_k=3, min_score=0.3) == [],
        "学之前，这句口语召回不到（描述里没有配套的词）",
    )

    store = LearningStore(None, max_examples=20)
    store.record("ticket_tool", question)
    store.apply(build_registry(meta))
    index.build([meta])
    hits = index.search(question, top_k=3, min_score=0.3)
    check(
        bool(hits) and hits[0][0].name == "ticket_tool",
        "学之后，同一句话就能召回 —— 这就是归纳学习的价值",
    )
    check(
        bool(meta.learned),
        f"学习样例已写进 meta 供检索使用：{meta.learned}",
    )


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
        test_sanitize_name,
        test_zip_member_safety,
        test_package_root_detection,
        test_zip_plan_rejects,
        test_install_roundtrip,
        test_plugin_import_analysis,
        test_overrides_store,
        test_tags_drive_retrieval,
        test_enrich_parsing,
        test_identifier_splitting,
        test_fuzzy_and_single_char,
        test_learning_store,
        test_induction_suggests_tags,
        test_learning_improves_recall,
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
