# 薛定谔的工具箱 · Neko Half-Life

> 仓库：`NekoHome-Studio/neko-halflife` · AstrBot 插件（`metadata.name = astrbot_plugin_neko_halflife`）
>
> 工具照常注册，说明书按需注入；本地检索先激活，再把 Schema 给模型看。
> 省的是提示词预算与上下文窗口，**不省工具执行算力**。

**名字的由来**：一个工具既已经注册（存在），又没有进入本轮请求（不可见），
只有在被本地检索「观测」到并激活之后，才坍缩成模型能看见的一整套 Schema——
就是薛定谔的猫（`neko` 是猫，`half-life` 是它待的那口箱子）。

AstrBot 默认会把**所有**已注册工具的完整 JSON Schema 一次性放进每轮 LLM 请求。
工具越多、描述越长、会话越长，提示词开销越大。本插件把这件事拆成两半：

| | 做什么 | 为什么不能省 |
|---|---|---|
| **全量注册** | 所有工具照常走 `@filter.llm_tool` 注册进 AstrBot 全局 `llm_tools` | 只有真的注册了，persona 工具白名单、会话级插件过滤、工具启用开关这些**上游权限**才会对它生效 |
| **按需注入** | `on_llm_request` 钩子里取「许可池快照」→ 本地检索 → 裁掉未激活工具 | 模型只看到本轮激活的工具，其余对模型不可见 |

```
第 1 步  本地预检索    用用户输入查关键词/标签索引，纯本地计算，不调用 LLM
第 2 步  激活表管理    per-UMO 激活表，轮次衰减 + 时间过期双过期
第 3 步  Schema 注入   许可池快照 ∩ 激活表，重建本轮 ToolSet
第 4 步  LLM 调用      模型看到的与普通 function calling 完全一致
第 5 步  统一执行      校验/执行/错误回传交给 AstrBot runner；本插件只做
                       结果裁剪（max_result_chars），不做重复的权限判定
```

---

## 定位：和内置的 skills_like 有什么区别

AstrBot 4.26.7 自带 `provider_settings.tool_schema_mode = skills_like | full`
（实现在 `tool_loop_agent_runner.py`）：`skills_like` 首轮只下发**工具名 + 描述**，
模型选中工具后再用**仅参数**的 schema 二次询问。

* `skills_like` 省的是**参数 schema**；
* 本插件省的是**名称 + 描述**本身——整个工具从请求里消失。

两者可以叠加，互不冲突。**如果只开了 skills_like 就以为工具 token 已经省到位，
描述文本那部分开销仍然每轮都在付。**

---

## 安装

本仓库的**根目录就是插件本体**（`metadata.yaml` 在根上），这是为了让 AstrBot
能直接按仓库地址安装——`PluginUpdator.find_plugin_metadata_entry` 只会在压缩包
根目录找 `metadata.yaml`，插件藏在子目录里会被判为「不是合法的 AstrBot 插件」。

两种装法：

**A. 从仓库地址安装（推荐）**
在 AstrBot WebUI 的插件管理里用仓库地址安装：

```
https://github.com/NekoHome-Studio/neko-halflife
```

AstrBot 会按 `metadata.name` 把目录重命名为 `astrbot_plugin_neko_halflife`，
所以仓库叫什么名字不影响安装结果。

**B. 手动 clone**

```
cd <AstrBot>/data/plugins
git clone https://github.com/NekoHome-Studio/neko-halflife astrbot_plugin_neko_halflife
```

装好后在 WebUI 插件管理里重载即可。无第三方依赖，不需要 `requirements.txt`。

要求 **AstrBot >= 4.26.7**：插件依赖 `on_llm_request` 钩子在 `req.func_tool`
组装完成之后触发，且依赖 `_plugin_tool_fix`（会话级插件过滤）在该钩子**之前**执行，
这两点是在 4.26.7 源码上逐行核对过的。

---

## 怎么用

### 1. 把自己的工具标记成懒加载

把 `@filter.llm_tool` 换成 `@lazy_tool`，其余写法完全一致（描述取 docstring、
参数取 `Args:` 段）：

```python
from astrbot.api.event import AstrMessageEvent
from .main import lazy_tool          # 主插件内

@lazy_tool(
    name="get_weather",
    tags=("天气", "weather", "气温", "下雨"),   # 检索标签，权重最高，写用户会说的词
    examples=("北京今天天气怎么样", "明天要带伞吗"),
    ttl_turns=5,                                 # 覆盖默认存活轮次
)
async def get_weather(self, event: AstrMessageEvent, city: str) -> str:
    """查询指定城市的天气。

    Args:
        city(string): 城市名
    """
    ...
```

装饰器参数：

| 参数 | 作用 |
|---|---|
| `tags` | 检索标签，权重最高（2.5，仅次于工具名的 3.0） |
| `examples` | 示例说法，把用户口语映射到工具（权重 1.5） |
| `group` | 分组名（权重 2.0），也便于将来做「一次激活整组」 |
| `ttl_turns` / `ttl_seconds` | 覆盖默认存活策略 |
| `always_active` | 常驻不裁剪（元工具用） |
| `risk="high"` | 高风险：不会被预检索自动激活，只能由模型显式激活 |
| `max_result_chars` | 覆盖全局结果裁剪长度 |

### 2. 子插件：插件里装工具集

```
sub_plugins/
├── demo_tools.py          # 单文件
└── weather/               # 包
    └── __init__.py
```

子插件**不是** AstrBot 插件：不需要继承 `Star`、不需要 `metadata.yaml`、
不会出现在插件管理面板里。加载器会把 `lazy_tool` 注入模块命名空间，
所以直接用即可，不必 import。

> **重要**：子插件里的工具必须写成**普通函数**（第一个参数是 `event`），不能写成方法。
> AstrBot 只会给「模块路径等于插件主模块」的 handler 绑定插件实例
> （`star_manager` 里的 `functools.partial(raw_handler, metadata.star_cls)`），
> 子插件的模块路径不满足该条件，写成 `self` 会缺实参。工具名不能写成 `__init__`。

### 3. 元工具兜底

预检索不可能万能。插件固定向模型暴露三个常驻工具：

| 工具 | 作用 |
|---|---|
| `lazy_search_tools` | 模型不确定有哪些工具时主动搜索 |
| `lazy_activate_tool` | 激活工具，**当轮即可用**（写回本轮 `ProviderRequest.func_tool`） |
| `lazy_deactivate_tool` | 取消激活，立刻释放提示词预算 |

代价是多一轮模型调用，所以设计上优先让预检索自动激活，元工具只做低置信兜底。

### 4. 命令

```
/lazy                状态总览
/lazy list           列出本会话许可的工具，★ 表示已激活
/lazy clear          清空本会话激活表
/lazy_sub on|off <名>  管理员启停子插件
```

### 5. WebUI 面板（Pages）

插件在 AstrBot WebUI 的插件详情页里提供一个 **懒加载工具** 面板
（`pages/lazy-tools/`），能做四件事：

* **状态总览**：注册工具数、常驻元工具数、索引条目、活跃会话、当前激活总数、过期策略；
* **检索试跑**（最实用的一块）：输入一句用户可能会说的话，直接看到召回候选、
  每一项的分数、是否过阈值、是否会被本轮激活，以及**被排除的原因**
  （低于阈值 / 被上游 persona 或插件过滤排除 / 超出 `top_k` / 高风险需手动激活）。
  它会额外回显「信息词」——如果这一列是空的，说明这句话的词和任何工具的
  名字/标签/描述/示例都没有交集，**此时调阈值没有任何用**，该做的是给工具补
  `tags` 或 `examples`。这是校准 `min_score`/`top_k` 最省事的方式，
  比改配置→发消息→翻日志快得多；
* **工具清单**：名称、来源（`main` 还是某个子插件）、标签、存活策略、风险等级、描述；
* **会话激活表**：按 UMO 列出每个会话当前激活了哪些工具、还剩几轮/几秒，可单独清空；
* **子插件开关**：在线启停 `sub_plugins/` 下的工具集，状态会写回插件配置。

页面通过 `window.AstrBotPluginPage` bridge 与 WebUI 外壳通信，主题跟随 WebUI
的明暗切换，文案走 `.astrbot-plugin/i18n/`（zh-CN / en-US）。

> Page 跑在 `allow-scripts` 但没有 `allow-same-origin` 的 iframe 里，
> 因此不能 fetch、不能用 localStorage、拿不到 dashboard cookie——所有请求都必须
> 走 bridge。新增接口时记得后端路由要带插件名前缀
> （`/{PLUGIN_NAME}/xxx`），而页面里 `bridge.apiGet("xxx")` **不带**前缀。

---

## 配置项

见 `_conf_schema.json`。几个关键的：

| 配置 | 默认 | 说明 |
|---|---|---|
| `enabled` | true | 总开关；关闭后工具原样注入，便于对比排查 |
| `prefetch_enabled` | true | 关闭后只能靠元工具手动搜索激活 |
| `top_k` | 3 | 每轮最多自动激活几个 |
| `min_score` | 0.35 | 匹配分阈值，落在 `[0,1]`，可解释 |
| `default_ttl_turns` | 3 | 存活轮次（含激活当轮） |
| `default_ttl_seconds` | 600 | 存活秒数，与轮次是「或」关系 |
| `max_active_per_session` | 12 | 单会话上限，超出按分淘汰 |
| `max_result_chars` | 4000 | 工具结果统一裁剪长度 |
| `allow_high_risk_auto` | false | 是否允许预检索自动激活 `risk="high"` 工具 |
| `sub_plugins_disabled` | `[]` | **停用**的子插件，空 = 全部启用 |

> `sub_plugins_disabled` 存的是停用名单而不是启用名单：启用名单里的空列表无法
> 区分「一个都不启用」和「留空 = 全部启用」，会把「全停」静默变成「全开」。

**调参直觉**：漏工具就把 `min_score` 调低或 `top_k` 调大（元工具仍能兜底）；
误激活太多就反过来调，并缩短 `default_ttl_turns`。拿不准时先用 Pages 里的
「检索试跑」看一眼真实分数，别盲调。

---

## 三处设计修正（相对最初的设计文档）

设计文档里有一处虚构配置、一处命名错误、一个安全漏洞，实现时按源码事实做了修正：

### 修正 1：`disable_group_queue` 不存在，改为复用平台会话锁

设计文档 §8 提到用 `disable_group_queue` 配置让群成员请求不共用群聊会话锁。
**AstrBot 源码里没有这个配置**（全仓 0 命中）。真实的会话串行化只有两处：

* `session_lock_manager.acquire_lock(event.unified_msg_origin)`
  （`astrbot/core/utils/session_lock.py`，调用点 `internal.py`）
* 限流阶段的 `self.locks[event.session_id]`（`rate_limit_check/stage.py`）

本插件的选择是**不自己造锁**：

1. `on_llm_request` 本身就是在持有 UMO 锁的临界区里被调用的，
   `asyncio.Lock` 不可重入，**再去获取同一把锁会直接死锁**；
2. 激活表按 UMO 分区，跨会话不共享；同一会话由平台的锁保证串行；
3. 模块内所有方法都是同步的、内部无 `await`，asyncio 单线程下不存在中间态。

所以 `activation.py` 里一把锁都没有——这是经过论证的结论，不是遗漏。

### 修正 2：「成员级隔离」的真实开关是 `platform_settings.unique_session`

设计文档 §8 描述的「群内成员级隔离配置」确实存在，但名字不是它说的那样，
真实开关是 **`platform_settings.unique_session`**（`default.py` 默认 `false`，
WebUI 显示为「隔离会话」），实现在 `waking_check/stage.py`：开启后为群消息构造
独立的 `session_id`——**而 `session_id` 正是 UMO 的组成部分**。

推论对本插件有利：激活表以 UMO 为 key，用户一开 `unique_session`，
群成员级隔离**自动获得，插件不需要写任何代码**。设计文档里设想的
「会话 key 从 bot_id+UMOP 扩展为 +user_id」是多余的。

另外 `platform/manager.py` 有明确约束：`unique_session` 只认默认配置。
本插件不读取、不覆盖它。

### 修正 3（安全）：重新注入会绕过上游过滤，改为「许可池取交集」

设计文档 §5 第三步与 §6 写的是「把已激活工具的 Schema 加回去」。
**这样做是权限泄露。** 拿到 `req.func_tool` 时，它已经被上游筛过三层：

1. persona 显式声明的工具（`_ensure_persona_and_skills`：persona 有 `tools` 就只放这些）
2. 工具启用开关 `FunctionTool.active`
3. 会话级插件选择 `event.plugins_name`（`_plugin_tool_fix`，在钩子**之前**执行）

直接加回去，就等于把 persona 明确排除的工具、管理员在本会话禁用的插件工具，
重新塞给模型。

**实际实现**（`core/injector.py`，全程只做减法）：

```python
allowed      = 钩子入口 req.func_tool 的全量快照        # 唯一合法来源
lazy_allowed = allowed ∩ 本插件注册表
keep         = (激活表 ∪ 常驻元工具) ∩ lazy_allowed     # 与，不是或
req.func_tool = 许可池中 (非本插件工具 ∪ keep)
```

这一条同时白送了设计文档 §7 想要的 bot 隔离与 §10 担心的越权调用：
上游怎么筛，本插件就怎么继承，一行权限判定都不需要复刻。

---

## 适用边界与已知限制

* **仅 `agent_runner_type=local` 生效**。第三方 runner（dify / coze / dashscope /
  deerflow）的工具清单来自远端平台，`on_llm_request` 触发时 `req.func_tool`
  还是空的；cron 与部分 webchat 路径也不经过该钩子。
* **工具仍会出现在 AstrBot 的工具管理面板里**，这是路线 A 的代价，也是它的价值：
  工具是「被注册但未被注入」，不是「没注册」。
* **同名工具按名字裁剪**。AstrBot 的 `add_func` 本身就按名字去重，
  因此同名冲突的处理与平台保持一致；插件不会去裁名字相同但属于别人的工具。
* **不省执行算力**。本地检索、索引与状态管理会给机器人侧增加开销；
  工具数量只有十几个时，收益可能不足以覆盖复杂度。
* **不持久化**。激活表是内存态，重启即清空（设计上也就该如此，它描述的是
  「最近几轮在聊什么」）。子插件启停状态来自配置，重启后按配置恢复。

---

## 测试

两层测试，都不需要启动 AstrBot：

```bash
# 1) 纯逻辑自检：检索打分、双过期、裁剪安全性（无依赖，49 项）
python tests/selftest.py

# 2) 集成冒烟：在真实 AstrBot 源码环境里验证注册契约与 Web API（87 项）
#    需要能 import astrbot；用 ASTRBOT_SRC 指定源码根目录
#    （刻意不用 ASTRBOT_ROOT——那是 AstrBot 自己的运行根目录变量）
python tests/smoke_astrbot.py
```

`tests/selftest.py` 里最重要的是 `test_pruning_never_exceeds_pool`：
它构造「激活表里有两个工具、许可池里只有一个」的场景，
断言最终请求里绝不会出现许可池之外的工具。

`tests/smoke_astrbot.py` 会真的导入 AstrBot、真的构造 `ToolSet` 与 `FunctionTool`、
真的跑一遍 `_decide()` 和四个 Page 接口，并校验 Page 目录与 i18n 文件齐备。
它把 AstrBot 的 `data/` 重定向到插件目录内的临时目录（已 gitignore），跑完自动删除。

---

## 版本敏感

本插件的每个机制断言都取自 **AstrBot 4.26.7** 源码。升级 AstrBot 后请复核：

* `on_llm_request` 是否仍在 `req.func_tool` 组装完成之后触发；
* `_plugin_tool_fix` 是否仍在该钩子之前执行；
* `ToolSet` 的 `tools` / `add_tool` / `remove_tool` 接口；
* `filter.llm_tool` 是否仍从 docstring 的 `Args:` 段解析参数；
* Page 是否仍只扫 `pages/<name>/index.html`，以及前端拼路由是否仍是
  `/api/v1/plugins/extensions/<metadata.name>/<endpoint>`。

`tests/smoke_astrbot.py` 就是为这件事写的——升级后先跑它。

---

## 许可

本项目采用 **MIT License**，见 [LICENSE](LICENSE)（Copyright (c) 2026 NekoHome-Studio）。

`docs/` 下的《AstrBot插件懒加载-按需工具注入-总结.md》是**实现前的初版设计文档**，
保留它是为了记录当初的设计取舍。它与最终实现有三处不一致（虚构配置名、
成员级隔离开关的真实名称、重新注入会绕过上游过滤），已在本文档的
「三处设计修正」一节逐条说明——**以代码与本文档为准，设计文档仅作历史参考**。
