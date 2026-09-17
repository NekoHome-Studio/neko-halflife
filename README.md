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
                       （标识符拆词 + 中文 bigram + 错拼近似 + 单字兜底 + 学习样例）
第 2 步  激活表管理    per-UMO 激活表，轮次衰减 + 时间过期双过期
第 3 步  Schema 注入   许可池快照 ∩ 激活表，重建本轮 ToolSet
第 4 步  LLM 调用      模型看到的与普通 function calling 完全一致
第 5 步  统一执行      校验/执行/错误回传交给 AstrBot runner；本插件只做
                       结果裁剪（max_result_chars），不做重复的权限判定
第 6 步  归纳学习      模型真的用了哪个工具，就把那一轮的用户原话记成该工具的
                       学习样例，下轮起参与召回；反复出现的词会被提议成标签
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

### 更新已安装的插件

`metadata.yaml` 里声明了 `repo`，所以 WebUI 插件列表里的**「更新」可以直接用**——
AstrBot 的更新流程是「下载 → 校验 → 删除旧目录 → 解包」，不会留下残留文件。

**不要用「从文件安装」去更新**。AstrBot 的 `install_plugin_from_file` 在目标目录
已存在时会硬失败：

```
安装失败：目录 astrbot_plugin_neko_halflife 已存在。
```

它没有覆盖/强制选项（`ignore_version_check` 管的是版本约束，不是文件覆盖）。
要更新只能二选一：走「更新」，或者先删掉 `data/plugins/astrbot_plugin_neko_halflife/`
再上传安装包。

> 补充：AstrBot **4.28.1** 的更新日志里有
> `Fixed plugin updates through URL and file installation. (#10053)`，
> 说明 4.27.x 上「通过 URL 或文件安装来更新插件」确实有缺陷。如果你在 4.27.x
> 撞到本节描述的报错，升级 AstrBot 本身也可能直接解决。

手动替换也可以（解包后重载插件）：

```bash
cd <AstrBot>/data/plugins/astrbot_plugin_neko_halflife
unzip -o /path/to/astrbot_plugin_neko_halflife-<版本>.zip
```

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

#### 检索是怎么匹配的

召回不看模型，只看一张倒排表。索引里每个工具贡献五个字段，权重不同：

| 字段 | 权重 | 来源 |
|---|---|---|
| 工具名 | 3.0 | `@lazy_tool` / `@filter.llm_tool` 的名字 |
| `tags` | 2.5 | 作者写的，或 WebUI 手改/LLM 总结/归纳采纳的 |
| 学习样例 | 2.2 | **实测**：模型真的用过这个工具的那句用户原话 |
| `group` | 2.0 | 分组名 |
| `examples` | 1.5 | 作者猜用户会怎么说 |
| 描述 | 1.0 | docstring 摘要 |

查询侧做了这几层处理：

* **标识符拆词**：`get_weather` 同时进 `get_weather`、`get`、`weather` 三个词。
  没有这一层，用户说「weather」永远召不回 `get_weather`——这是最隐蔽的一类漏召回；
* **中文 bigram**：`帮我查天气` 切成 `帮我/我查/查天/天气`，不需要分词器；
* **单字兜底**（0.4 权重）：再收录每个汉字，让「冷」这种一字查询有机会命中；
* **错拼近似**：长度 ≥5 的纯字母词允许编辑距离 1（`wether` → `weather`），
  但整条分数乘以 0.6 置信折扣——**只靠拼写猜中的分数必须真的低于拼中**，
  否则 `min_score` 对它形同虚设；
* **信息词分母**：只有真的出现在倒排表里的词才进分母。中文长句会被切成大量
  无意义 bigram，把它们算进分母会把真正命中的词稀释到阈值以下。

分数 = `0.65 × 覆盖率 + 0.35 × 强度`，落在 `[0,1]`，可解释、可试跑。

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

插件在 AstrBot WebUI 的插件详情页里提供一个 **薛定谔的工具箱** 面板
（`pages/lazy-tools/`），能做这些事：

* **状态总览**：注册工具数、常驻元工具数、索引条目、活跃会话、当前激活总数、过期策略；
* **检索试跑**（最实用的一块）：输入一句用户可能会说的话，直接看到召回候选、
  每一项的分数、是否过阈值、是否会被本轮激活，以及**被排除的原因**
  （低于阈值 / 被上游 persona 或插件过滤排除 / 超出 `top_k` / 高风险需手动激活）。
  它会额外回显「信息词」——如果这一列是空的，说明这句话的词和任何工具的
  名字/标签/描述/示例都没有交集，**此时调阈值没有任何用**，该做的是给工具补
  `tags` 或 `examples`。这是校准 `min_score`/`top_k` 最省事的方式，
  比改配置→发消息→翻日志快得多；
* **工具清单**：名称、来源、标签、存活策略、风险等级、描述；**标签与描述可直接编辑**
  （见下节），并有「用 LLM 补全标签」按钮，以及一条**总结模型**选择栏
  （选提供商 / 覆盖模型名 / 测试 / 设为默认）；
* **会话激活表**：按 UMO 列出每个会话当前激活了哪些工具、还剩几轮/几秒，可单独清空；
* **子插件开关**：在线启停 `sub_plugins/` 下的工具集，状态会写回插件配置；
* **上传子插件**：上传一个 `.py` 或 `.zip` 直接装进 `sub_plugins/` 并立即加载；
* **从已装插件导入**：把 `data/plugins/` 下某个插件的源码复制进 `sub_plugins/`；
* **学习与归纳**（见下节）：看每个工具学到了哪些用户原话、候选标签是什么，
  可逐条删除样例、清空重学、一键采纳候选标签；
* **指令面板**（默认关闭）：列出全部指令，支持改指令名/别名、启停、权限、冲突查看。

页面通过 `window.AstrBotPluginPage` bridge 与 WebUI 外壳通信，主题跟随 WebUI
的明暗切换，文案走 `.astrbot-plugin/i18n/`（zh-CN / en-US）。

> Page 跑在 `allow-scripts` 但没有 `allow-same-origin` 的 iframe 里，
> 因此不能 fetch、不能用 localStorage、拿不到 dashboard cookie——所有请求都必须
> 走 bridge。新增接口时记得后端路由要带插件名前缀
> （`/{PLUGIN_NAME}/xxx`），而页面里 `bridge.apiGet("xxx")` **不带**前缀。

#### 上传子插件

支持两种包：

* **单文件** `my_tools.py` → 装成 `sub_plugins/my_tools.py`；
* **包** `my_tools.zip` → 装成 `sub_plugins/my_tools/`，要求根目录有 `__init__.py`
  （整体套一层目录也认，会自动剥掉）。

子插件名会做白名单校验：必须是**合法 Python 标识符**（字母开头，后接字母/数字/下划线），
因为加载器要拿它构造模块名。不能以 `_` 或 `.` 开头——加载器会跳过这类条目，
否则会出现「上传成功但永远不加载」的静默失败。

> **⚠️ 安全边界**：上传的子插件会在 AstrBot 进程内**执行其中的 Python 代码**，
> 权限与 AstrBot 本身相同。这是本插件唯一会写入并执行代码的入口。威胁模型上，
> 能打开这个面板的人已经是 dashboard 管理员（本来就能装插件），能力是等价的；
> 但仍建议只上传自己信任的包。可用 `allow_subplugin_upload` 关掉该接口。
>
> 实现的防护：名字白名单、**落盘前**先校验压缩包全部成员（路径穿越 / 符号链接 /
> 压缩炸弹 / 条目数上限）、写入时按实际字节数二次限流、解压目标用 `resolve()`
> 复核仍在目标目录内、失败时清理不留半个子插件。`tests/selftest.py` 里有针对
> zip-slip 的对抗性测试，断言攻击包**写不出任何文件**。

删除子插件时，工具需要从**两处**移除：本插件注册表，以及 AstrBot 的全局
`llm_tools`。只清前者会造成一个很隐蔽的反向错误——工具仍留在全局表里，而插件
已不认识它们，裁剪逻辑会把「不认识」当成「不是本插件的工具」而**原样保留**，
于是删掉的东西反而变成每轮都注入。若全局移除失败，插件会退而把该来源标记为
「已退休」：保留定义以便仍被识别为本插件的工具，但永不进入保留集，保证每轮被裁掉。

#### 加了子插件却没反应？先看这三处

1. **「子插件」区块该行的灰字**——状态直接写在这里：`N 个工具`（已注册）、
   `加载失败：<原因>`（导入期报错，最常见是 docstring 的 `Args:` 里参数缺类型
   标注，`filter.llm_tool` 会在导入时抛 `ValueError`）、`已停用`（被
   `sub_plugins_disabled` 挡住）、`已删除，工具已冻结不再注入`（退休态）。
   工具栏还会显示 `磁盘 N 个 · 已加载 M 个 · 共 K 个工具`，一眼能看出有没有漏加载。
2. **工具清单里搜工具名**：**有** → 工具已注册，模型用不到是懒加载的正常表现
   （去「检索试跑」看分值与信息词，别去改代码）；**没有** → 注册没成功。
3. **如果文件是你自己放进 `sub_plugins/` 的**（scp / 手写）：本插件**只在自己被
   加载或重载时**才扫一次目录，不重载就什么都不会发生、也不会报错。点
   **「重新扫描目录」**即可加载，不必去 AstrBot 插件管理里重载整个插件。

> 还有一类"没反应"来自面板自身：0.4.2 之前的版本会给「不可导入」的插件渲染一个
> **禁用**的导入按钮，点下去毫无反馈。现在按钮保持可点，点了会把不能导入的原因
> 明确摆出来。如果**整个页面**都点不动，先硬刷新（Ctrl+F5）——那通常是浏览器里
> 还留着旧版 `app.js`，新旧 DOM 对不上会让脚本在启动时抛错，表现为全页面静默。

#### 工具标签与 LLM 总结

**标签就是懒加载的召回依据**：工具能不能在该轮被激活，取决于用户那句话的关键词
能否命中工具的名字/标签/描述/示例。而**导入进来的外来工具只有它原本的名字与
docstring 首段**——那些文字是写给**读代码的人**看的，往往不含**用户会说的词**。
所以这一步对召回质量的影响，比调阈值大得多。

面板的「工具清单」里，**标签与描述都是可编辑输入框**，改完点「保存」即可；有覆盖的
工具会带 `已手改` / `LLM 已补` 徽标，点「重置」退回代码里的基线。

三层优先级，**后者不覆盖前者**：

```
代码里的基线  <  LLM 总结（source=llm）  <  手动编辑（source=manual）
```

**覆盖不改你的源码**：导入的插件原文件保持原样，改的是本插件注册表里的副本，
落在 AstrBot 的插件数据目录（`data/plugin_data/astrbot_plugin_neko_halflife/tool_overrides.json`）。
首次施加覆盖时会记下基线，所以「重置」永远能退回去。

**导入时自动用 LLM 总结**（`llm_enrich_on_import`，默认开）：导入/上传子插件后，
把新工具的名字与原描述送给当前对话模型，让它产出「用户会怎么问」的 summary 与
3~8 个标签。要点：

* 只在导入时跑一次并落盘，**不在每轮请求里调用模型**；
* 默认只补**还没有标签**的工具（`only_missing`），不会把你手写好的标签再总结一遍；
* 单次上限 `llm_enrich_max_tools`（默认 20），`0` 即完全关闭；
* **失败只跳过**：拿不到 provider、超时、模型返回垃圾——一律保留原文，绝不让
  「总结失败」导致导入失败；
* 模型输出**多级兜底解析**：裹 ```` ```json ````、前后加寒暄、包一层 `{"tools": ...}`、
  甚至返回数组形状，都能认出来（见 `core/enrich.py` 的 `parse_enrichment`）。

面板上的「用 LLM 补全标签」按钮走同一个逻辑，可随时手动触发。

##### 指定专用的总结模型

默认用的是**当前会话的对话模型**。这在小规模下没问题，但总结这件事的性质和聊天
不一样：一次导入可能连打好几次请求、只要认出"用户会怎么问"、对推理能力几乎没要求。
拿主力模型去干这个，既浪费额度，也让它的上下文/并发和你正在聊的天搅在一起。

所以可以单独指定：

| 配置 | 作用 |
|---|---|
| `llm_enrich_provider_id` | 用哪个提供商。留空 = 当前会话默认模型。**在 AstrBot 的插件配置页里是一个「选择对话模型」选取器**：列出你已经配好的模型（各自带着当前模型名，还能直接测连通性），不用手抄 ID |
| `llm_enrich_model` | 覆盖模型名。一般不用填——选中的提供商本身就带着自己的模型；只有「同一个提供商换个模型」时才填 |

这个选取器用的是 AstrBot 官方的 `_special: select_provider`（v4.0.0+ 可用，
`docs/zh/dev/star/guides/plugin-config.md` 里对插件公开），渲染链路是
`ExtensionPage → AstrBotConfigV4 → ConfigItemRenderer → ProviderSelector`，
返回值是提供商 **ID 字符串**——所以配置项类型必须是 `string`，写成 `list` 会存成
错误的形状。官方另外点名了一批「内部实现、随时可能变动、请勿在插件中使用」的
`_special` 值，测试里有一条断言防止误用。

面板的「工具清单 → 总结模型」下拉框是同一件事的图形化版本，而且多一点：
它读的是**运行时**的提供商列表，能对选中的组合跑一次**真实的总结调用**（同一个
system prompt、同一个 JSON 解析，只是拿合成工具 `neko_ping_tool` 去问，所以通过
就说明这条路真的通，而不是"能 ping 到"），确认可用后点「设为默认」写回上面这两个
配置项。两边改的是同一份配置，不会各说各话。

几个刻意的取舍：

* **按次覆盖，不动全局**。`llm_enrich_model` 是作为 `text_chat(model=...)` 的一次性
  参数传下去的，**不会**修改该提供商在 AstrBot 全局配置里的模型——否则一次后台
  总结就会把主人正在用的模型换掉。provider 不支持按次指定模型时，会跳过覆盖并在
  结果里说明「已忽略」，而不是静默当作没事发生；
* **配错要报错，不要静默降级**。`llm_enrich_provider_id` 填了一个不存在的 ID 时，
  总结会明确失败并带上那个 ID，**不会**悄悄退回默认模型——否则你会以为"确实在用
  那个便宜模型"，而账单上是主力模型；
* **下拉框里找不到配置的 ID 时也会显式标出来**（`（已失效）xxx`），因为 `<select>`
  在赋值失败时会静默回落到第一项，看起来像配置生效了；
* 「设为默认」会先试解析一遍再落盘：把一个坏 ID 存进配置，之后每次导入都会失败，
  而失败信息只在日志里，很难查。

#### 从已装插件导入

面板会列出 `data/plugins/` 下的所有插件，并**逐个给出可移植性结论与模式**。点
「导入」即把该插件的源码复制进 `sub_plugins/<目录名>/`，生成包入口
`__init__.py`，随后立即加载。

**两种模式**：

| 模式 | 适用 | 本插件做什么 |
|---|---|---|
| `native` | 用本插件 `@lazy_tool` 写的模块级普通函数工具集 | 直接导入即可，装饰器自己完成注册 |
| `hosted` | **常规 AstrBot 插件**（`Star` 子类 + `@filter.llm_tool` 等装饰器） | 自己扮演 `star_manager`：实例化它的 `Star` 类，把工具与事件处理器绑定到该实例，并把这些工具纳入本插件注册表 → 它们变成**按需注入** |

也就是说：**`@lazy_tool` 不再是硬性限制**，常规插件现在也能搬进来，而且搬进来之后
它的工具会从"每轮都下发"变成"被检索激活时才下发"。

**为什么 `hosted` 必须由我们自己实现**：AstrBot 绑定 `self` 的条件是模块路径**精确
等于**插件主模块——`ft.handler.__module__ == metadata.module_path`
（`star_manager`）、`handler_module_path == module_name`
（`star_handlers_registry.get_handlers_by_module_name`）。搬进 `sub_plugins/` 的东西
两条都不满足，AstrBot 永远不会给它绑 `self`。所以我们照着 `star_manager` 的语义
自己绑：`cls(context=..., config=...)`（`TypeError` 时退回只传 `context`，与它一致），
再把该模块下的工具与处理器 `functools.partial` 到实例上。

**注意事项**（`hosted` 的代价）：

* 会**执行该插件的 `__init__`**。外来构造函数什么都可能做（网络、写文件），
  失败会被捕获并回滚；
* 插件以子插件身份运行，与 AstrBot 原生加载**是两个实例**。若原插件仍然启用，
  工具名会撞车（后注册的胜出）——但本插件的裁剪是**按工具名**的，所以无论谁胜出，
  该工具都仍然是懒加载的；
* 只会实例化**最外层的那个** `Star` 子类；发现多个会给出提示；
* 删除/卸载时会先 `await` 它的 `terminate()` 再解绑，避免"实例没了处理器还在"；
* 宿主状态写在子插件目录里的 `.neko-host.json`，**重启后据此续接宿主**——
  否则重新导入只会注册工具而没人实例化，退化成"每轮注入且一调用就报错"。

**三道关卡**：

1. **静态预检**（`core/plugin_import.py`）：既没有 `@lazy_tool` 也没有 `Star` 子类的
   直接拒绝（导入后无法注册任何东西），并说明原因；
2. **导入后校验**：比对导入前后的全局工具表，出现「不在本插件注册表里」的新工具
   即判定为脏注册——这能拦住静态扫描漏掉的写法；
3. **失败回滚**：停宿主、解绑、清脏注册、删掉刚复制的目录；覆盖导入时还会还原旧版本。

上传子插件（`subplugins/upload`）走的是**同一套收尾**：若上传的包是个常规插件，
一样会被宿主并纳入懒加载；若它注册了本插件管不到的工具，同样清理并回滚文件。

另外**不会**搬运 `metadata.yaml`（子插件不是 AstrBot 插件，留着只会让人误以为
它需要被 AstrBot 扫描）。

一个由本功能暴露并已修掉的加载器缺陷：原先只有**入口模块**会被注入 `lazy_tool`，
所以包子插件里 `__init__.py` 用 `from . import tools` 导入的子模块写裸名
`@lazy_tool` 会 `NameError`。现在导入窗口期内会把 `lazy_tool` 临时放进 `builtins`，
覆盖全部子模块，窗口结束立即移除。

#### 指令面板

它是对 AstrBot 官方 `astrbot.core.star.command_management` 的**薄前端**，
不另造存储：改名/启停写 AstrBot 的指令配置表，权限写 `alter_cmd` 偏好，
并同步修改运行时过滤器。因此与 dashboard 自带的「指令管理」**数据同源、互不冲突**，
改了哪边都能看到。

因为它是 AstrBot 的通用管理功能、与本插件「懒加载工具」的主题无关，
为保持插件定位清晰**默认关闭**（`enable_command_panel`）。另外它属于 AstrBot 的
core 内部模块而非 `astrbot.api` 公开接口，所以采用**容错导入**：拿不到就把面板
降级成「此版本不支持」，不影响插件其它功能。

> 接口细节：`update_command_permission` 的第二个形参名是 `permission_type`
> 而不是 `permission`，且只接受 `admin` / `member`（`everyone` 会被拒），
> 所以插件按位置传参并预先校验取值。

### 6. 归纳学习：让工具自己学会「用户会怎么说」

作者写得出 `tags`，但写不出所有用户的口语。「导入进来的外来工具」更糟——
它只有原始名字和一串英文描述，中文用户怎么说它，谁也不知道。

本插件用 AstrBot 的两个工具级钩子把这件事变成实证：

```
用户说了什么（_decide 时记进 TurnContext）
      ↓ 模型真的调用了工具 X（@filter.on_using_llm_tool）
把「那句话 → X」记成 X 的学习样例，并进入检索索引
      ↓ 同类说法反复出现（induction_min_hits 条不同样例里都有同一个词）
      ↓ 归纳提议成标签 → 面板上一键采纳（或 auto_induct_tags 自动采纳）
那句话以后就能召回 X
```

* **为什么可信**：不是猜的。一句话**真的导致**某个工具被调用，说明这句话与该工具
  确实相关——权重（2.2）因此排在作者手写的 `examples`（1.5）之上；
* **为什么不会学坏**：只学**本插件注册表认得**的工具（不去学别人的工具）；
  样例有长度与数量上限（超限保留命中最多的）；归纳按「出现在多少条**不同**样例里」
  计数，同一句话里重复三遍不算三次证据；只提议长度 ≥2 的词。

**关于自我纠错，有一个必须说清楚的现实**：设计上 `@filter.on_llm_tool_respond`
会用 `isError` 给失败的调用记负反馈，但 AstrBot 4.26/4.27 里——

* 本地工具**抛异常时这个钩子根本不会被调用**（异常在 runner 的 `except` 里被接住，
  位置在 `on_tool_end` 调用点之外）；
* 返回值是 `None` 也**不是失败**：那是「工具直接发消息给用户」的**成功**路径；
* 本地工具路径**从不设置** `isError`（只有 MCP 服务端会设）。

所以自动负反馈目前基本是「为未来 / 为 MCP 预留」。真正能兜底的是人：
面板里每条样例右边都有一个 `×`，学错了直接删；或者整个工具清空重学。
**自学习的功能如果不可纠正，就不该上线。**

归纳采纳写的是 `source="manual"` 的覆盖，与手动改标签等价——
LLM 总结不会把它冲掉，重启后依然在。

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
| `allow_subplugin_upload` | true | 允许 WebUI 上传子插件。会在进程内执行上传包里的 Python，管理员能力等价，可关掉 |
| `max_subplugin_upload_kb` | 2048 | 上传体积上限（压缩包与解压后都限） |
| `enable_command_panel` | false | 指令面板。AstrBot 通用管理功能、与本插件主题无关，默认关闭 |
| `llm_enrich_on_import` | true | 导入/上传后用 LLM 总结标签与描述。会消耗 token，失败只跳过 |
| `llm_enrich_max_tools` | 20 | 单次最多总结几个工具；`0` 表示关闭 LLM 总结 |
| `llm_enrich_timeout` | 60 | LLM 总结超时（秒）；超时跳过，不会卡住导入 |
| `llm_enrich_provider_id` | `""` | 总结专用的提供商。在插件配置页里渲染为**模型选取器**（`_special: select_provider`）；留空 = 当前会话默认模型。选中的提供商若被删除，会明确报错而不是静默退回 |
| `llm_enrich_model` | `""` | 总结时按次覆盖的模型名。一般不用填（提供商自带模型）；**不会**改动全局配置里的模型 |
| `learning_enabled` | true | 归纳学习总开关；关闭后不再采样，也不参与检索 |
| `learning_max_examples` | 20 | 单个工具最多保留多少条学习样例（超出时保留命中最多的） |
| `induction_min_hits` | 3 | 一个词至少在多少条**不同**样例里出现，才会被提议成标签 |
| `auto_induct_tags` | false | 自动采纳候选标签。默认关闭：自动改标签属于「自己改自己的召回依据」，应由人拍板 |

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
  **例外**：工具标签覆盖（`tool_overrides.json`）与学习样例
  （`learned_examples.json`）落在 AstrBot 的插件数据目录里，重启不丢。
* **学习需要模型真的用过工具**。如果预检索从来没激活过某个工具、模型也从来没
  调用过它，就不会有任何样例——这是「实证学习」的代价，不是 bug。
* **`on_llm_tool_respond` 的负反馈目前基本不生效**（AstrBot 不设 `isError`、
  工具抛异常时钩子不被调用），纠错以人工删除样例为主，见上文「归纳学习」。
* **宿主模式会执行外来插件的 `__init__`**。被导入的插件与原生加载互不影响，
  但若原插件仍然启用，两者会各持有一个实例（工具名相同，仍按名字裁剪，
  所以懒加载语义不会被破坏）。

---

## 测试

两层测试，都不需要启动 AstrBot：

```bash
# 1) 纯逻辑自检：检索、双过期、裁剪、上传与导入分析、覆盖层、总结解析、归纳学习
#    （无依赖，190 项）
python tests/selftest.py

# 2) 集成冒烟：真机验证注册契约、Web API、宿主模式、工具标签、总结模型与归纳学习（259 项）
#    需要能 import astrbot；用 ASTRBOT_SRC 指定源码根目录
#    （刻意不用 ASTRBOT_ROOT——那是 AstrBot 自己的运行根目录变量）
python tests/smoke_astrbot.py
```

`tests/selftest.py` 里最重要的几项都是**对抗性**或**因果**的：

* `test_pruning_never_exceeds_pool` 构造「激活表里有两个工具、许可池里只有一个」
  的场景，断言最终请求里绝不会出现许可池之外的工具；
* `test_install_roundtrip` 构造一个含 `../pwned.txt` 成员的 zip-slip 攻击包，
  断言它被拒绝、**且没有写出任何文件**、也没留下半个子插件；
* `test_tags_drive_retrieval` 直接证明本功能的因果：同一个工具、同一句口语查询，
  补标签前**召回不到**、补标签后**能召回**——这就是"标签值得改"的证据。
* `test_learning_improves_recall` 证明归纳学习的因果：同一句话，学之前
  **召回不到**、学之后**能召回**；
* `test_identifier_splitting` / `test_fuzzy_and_single_char` 锁住几类具体漏召回：
  「weather」召不回 `get_weather`、拼错一个字母召不回、单字查询召不回；
  同时断言**拼错的分数确实低于拼对的**（折扣不能是摆设）。

`tests/smoke_astrbot.py` 会真的导入 AstrBot、真的构造 `ToolSet` 与 `FunctionTool`、
真的跑一遍 `_decide()` 和全部 20 个 Page 接口，并校验 Page 目录与 i18n 文件齐备
（含一条**结构性**检查：前端 `t("pages.lazy-tools.X")` 用到的每个键，
两个语言文件里都必须真的有——缺了只会静默退回中文兜底，肉眼看不出来；
装了 node 时还会对 `app.js` 跑一次 `node --check`，因为前端语法错误同样表现为
"面板整个点不动"）。
其中五处是端到端的：

* **宿主模式**：造一个真实的常规插件（`Star` 子类 + `@filter.llm_tool` + `@filter.command`）
  走一遍导入，断言实例被创建、工具与命令都被绑定、**并且真的调用成功**
  （工具返回 `hosted:hi:1`、命令返回 `pong`）；删除后再断言工具与命令处理器都
  已从全局表移除；
* **脏注册兜底**：造一个静态扫描看不出来、只在导入期才注册脏工具的插件，断言运行期
  能拦下它、清掉脏注册并回滚目录；
* **标签与总结**：用假 provider 验证手动改标签能生效并**立刻改变召回**、
  LLM 结果不覆盖手改、重置退回基线、取不到模型时只报错不崩；
* **总结模型**：断言请求里指定的提供商被调用而**会话默认那个没被牵连**、
  模型名是按次传入的、配置的 ID 失效时明确报错而不是静默换模型顶替、
  试跑走的是真实总结路径、`providers/save` 先验证再落盘；
  另外校验配置项声明了官方的 `select_provider` 选取器且类型为 `string`，
  以及没有误用官方的内部 `_special`；
* **归纳学习**：真调一遍两个工具钩子，断言 `tool_result=None` **不算失败**、
  `isError=True` 才作废样例，`learning/forget` 能删掉单条、`auto_induct_tags`
  默认关闭而开启后生效；
* **上传 → 删除**：走两条删除路径（能清全局表时遗忘；清不掉时退休兜底）。

这些都用 scratch 目录（不污染仓库）。它把 AstrBot 的 `data/` 重定向到插件目录内的
临时目录（已 gitignore），跑完自动删除。

### 打包

```bash
git archive --format=zip -o dist/astrbot_plugin_neko_halflife-<ver>.zip HEAD
```

文件清单不手写——`.gitattributes` 里用 `export-ignore` 排掉了 `.gitignore` /
`.gitattributes` / `dist/`，其余全部按 git 追踪的内容原样进包，
`metadata.yaml` 落在压缩包根目录（`PluginUpdator.find_plugin_metadata_entry` 的要求）。
手写清单迟早会漏（`core/learning.py` 这样的新增文件最容易忘），所以别手写。

打包后建议再验一次：用 AstrBot 自己的 `PluginUpdator.validate_plugin_archive()`
过一遍，然后把包解到临时目录、在里面**再跑一次两层测试**——
这一步能抓到「源码里跑得通、但没被打进包」的缺文件问题。

---

## 版本敏感

本插件依赖 AstrBot 的若干内部契约，升级后请复核：

* `on_llm_request` 是否仍在 `req.func_tool` 组装完成之后触发；
* `_plugin_tool_fix` 是否仍在该钩子之前执行；
* `ToolSet` 的 `tools` / `add_tool` / `remove_tool` 接口；
* `filter.llm_tool` 是否仍从 docstring 的 `Args:` 段解析参数；
* `on_using_llm_tool` / `on_llm_tool_respond` 是否仍在工具调用前后触发，
  **以及 `on_tool_end` 是否仍排在异常处理之外**（决定了学习负反馈是否可自动生效）；
* 本地工具路径是否仍不设置 `CallToolResult.isError`；
* Page 是否仍只扫 `pages/<name>/index.html`，以及前端拼路由是否仍是
  `/api/v1/plugins/extensions/<metadata.name>/<endpoint>`。

`tests/smoke_astrbot.py` 就是为这件事写的——升级后先跑它。

### 已核对过的版本

| AstrBot | 核对方式 | 结果 |
|---|---|---|
| **4.26.7** | 逐行读源码 ＋ `smoke_astrbot.py` **真机跑通**（真实导入 AstrBot、真实构造 `ToolSet`/`FunctionTool`、真实调用注入钩子与全部 20 个 Page 接口，含宿主模式、LLM 总结、总结模型选择与归纳学习端到端），449 项断言全绿 | 全部成立 |
| **4.27.4** | 按上面五条逐条比对 tag `v4.27.4` 源码 | 全部成立 |

4.27.4 的差异都落在本插件不依赖的地方：provider 选择改为 `get_using_provider_async`、
TTS 查询改为 `get_using_tts_provider_async`、`_select_provider` 变成 `async`、
新增群消息历史工具与 checkpoint 路径。`astrbot/core/star/register/star_handler.py`
的**代码行与 4.26.7 完全一致**（仅一处中文 docstring 措辞变化），
`astrbot/dashboard/services/plugin_page_service.py` 的**整文件哈希一致**。
`provider_settings.tool_schema_mode` 的 `skills_like|full` 两个选项也仍在。
`astrbot/core/agent/runners/tool_loop_agent_runner.py` 里 `on_tool_end` 的调用点
仍在内层 `try` 之外（即工具抛异常时不会触发该钩子），与上文「归纳学习」的描述一致。

> 未核对：**4.28.x**。4.28.0 把「Agent 执行器」配置从模型提供商页移进了配置文件页
> （`#9821`），并提到工具 Schema 顺序按名字排序（`#9798`）——前者与本插件
> 「仅 `agent_runner_type=local` 生效」的范围有关，升级到 4.28 前请先跑
> `tests/smoke_astrbot.py` 并确认插件的日志就绪行仍出现。

---

## 许可

本项目采用 **MIT License**，见 [LICENSE](LICENSE)（Copyright (c) 2026 NekoHome-Studio）。

`docs/` 下的《AstrBot插件懒加载-按需工具注入-总结.md》是**实现前的初版设计文档**，
保留它是为了记录当初的设计取舍。它与最终实现有三处不一致（虚构配置名、
成员级隔离开关的真实名称、重新注入会绕过上游过滤），已在本文档的
「三处设计修正」一节逐条说明——**以代码与本文档为准，设计文档仅作历史参考**。
