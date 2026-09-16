/**
 * 懒加载工具 —— AstrBot Plugin Page 前端。
 *
 * 通信约束（决定了本文件的写法）：
 * Page 跑在 `allow-scripts` 但没有 `allow-same-origin` 的 iframe 里，
 * 所以**不能** fetch、不能读 localStorage、不能碰父页面 DOM。
 * 一切后端调用都必须走 dashboard 注入的 `window.AstrBotPluginPage` bridge，
 * 由它 postMessage 到外壳代为请求。endpoint 是插件内相对路径，不带插件名前缀。
 *
 * 同理，脚本必须是外部 `type="module"`：bridge SDK 由 dashboard 注入到 HTML 里，
 * module 脚本天然延后执行，能保证 `window.AstrBotPluginPage` 已经就位。
 */

const bridge = window.AstrBotPluginPage;

/* ------------------------------------------------------------------ */
/* 小工具                                                              */
/* ------------------------------------------------------------------ */

function t(key, fallback) {
  try {
    const value = bridge?.t?.(key, fallback);
    return value === undefined || value === null || value === "" ? fallback : value;
  } catch {
    return fallback;
  }
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null && text !== "") {
    node.textContent = String(text);
  }
  return node;
}

function clear(node) {
  if (node) node.replaceChildren();
}

function unwrap(value) {
  if (value && value.status === "error") {
    throw new Error(value.message || "请求失败");
  }
  if (
    value &&
    value.status === "ok" &&
    Object.prototype.hasOwnProperty.call(value, "data")
  ) {
    return value.data;
  }
  return value ?? {};
}

const dom = {
  heading: document.getElementById("heading"),
  subheading: document.getElementById("subheading"),
  stateBadge: document.getElementById("state-badge"),
  refreshButton: document.getElementById("refresh-button"),
  pageError: document.getElementById("page-error"),
  overview: document.getElementById("overview"),
  playgroundTitle: document.getElementById("playground-title"),
  playgroundHint: document.getElementById("playground-hint"),
  queryInput: document.getElementById("query-input"),
  topkInput: document.getElementById("topk-input"),
  thresholdInput: document.getElementById("threshold-input"),
  labelTopk: document.getElementById("label-topk"),
  labelThreshold: document.getElementById("label-threshold"),
  searchButton: document.getElementById("search-button"),
  searchSummary: document.getElementById("search-summary"),
  searchResult: document.getElementById("search-result"),
  toolsTitle: document.getElementById("tools-title"),
  toolsSummary: document.getElementById("tools-summary"),
  toolsHint: document.getElementById("tools-hint"),
  enrichButton: document.getElementById("enrich-button"),
  toolsBody: document.getElementById("tools-body"),
  thName: document.getElementById("th-name"),
  thSource: document.getElementById("th-source"),
  thTags: document.getElementById("th-tags"),
  thTtl: document.getElementById("th-ttl"),
  thRisk: document.getElementById("th-risk"),
  thDesc: document.getElementById("th-desc"),
  sourcesTitle: document.getElementById("sources-title"),
  sourcesHint: document.getElementById("sources-hint"),
  sourcesList: document.getElementById("sources-list"),
  sourcesCount: document.getElementById("sources-count"),
  rescanButton: document.getElementById("rescan-button"),
  sessionsTitle: document.getElementById("sessions-title"),
  sessionsSummary: document.getElementById("sessions-summary"),
  sessionsList: document.getElementById("sessions-list"),
  uploadTitle: document.getElementById("upload-title"),
  uploadHint: document.getElementById("upload-hint"),
  uploadInput: document.getElementById("upload-input"),
  uploadButton: document.getElementById("upload-button"),
  uploadResult: document.getElementById("upload-result"),
  importTitle: document.getElementById("import-title"),
  importHint: document.getElementById("import-hint"),
  importBody: document.getElementById("import-body"),
  commandsTitle: document.getElementById("commands-title"),
  commandsSummary: document.getElementById("commands-summary"),
  commandsBody: document.getElementById("commands-body"),
  toast: document.getElementById("toast"),
};

let busy = false;
let inputsInitialized = false;
let toastTimer = null;

/* ------------------------------------------------------------------ */
/* 交互反馈                                                            */
/* ------------------------------------------------------------------ */

function showError(message) {
  dom.pageError.textContent = String(message || "未知错误");
  dom.pageError.hidden = false;
}

function clearError() {
  dom.pageError.hidden = true;
  dom.pageError.textContent = "";
}

function showToast(message) {
  if (toastTimer) window.clearTimeout(toastTimer);
  dom.toast.textContent = String(message || "完成");
  dom.toast.hidden = false;
  toastTimer = window.setTimeout(() => {
    dom.toast.hidden = true;
  }, 3200);
}

function setBadge(text, kind) {
  dom.stateBadge.textContent = text;
  dom.stateBadge.className = `badge badge-${kind}`;
}

function setBusy(value) {
  busy = value;
  dom.refreshButton.disabled = value;
  dom.searchButton.disabled = value;
}

/* ------------------------------------------------------------------ */
/* 后端调用                                                            */
/* ------------------------------------------------------------------ */

async function callGet(endpoint, params) {
  return unwrap(await bridge.apiGet(endpoint, params));
}

async function callPost(endpoint, body) {
  return unwrap(await bridge.apiPost(endpoint, body));
}

/* ------------------------------------------------------------------ */
/* 文案（走 i18n，取不到就用中文兜底）                                  */
/* ------------------------------------------------------------------ */

function applyLabels() {
  document.title = t("pages.lazy-tools.title", "薛定谔的工具箱");
  dom.heading.textContent = t("pages.lazy-tools.heading", "薛定谔的工具箱");
  dom.subheading.textContent = t(
    "pages.lazy-tools.subheading",
    "工具照常注册，说明书按需注入——这里可以看到注册表、逐会话激活状态，并在线试跑检索。",
  );
  dom.refreshButton.textContent = t("pages.lazy-tools.refresh", "刷新");

  dom.playgroundTitle.textContent = t("pages.lazy-tools.playground", "检索试跑");
  dom.playgroundHint.textContent = t(
    "pages.lazy-tools.playground_hint",
    "只读，不会改动激活表",
  );
  dom.queryInput.placeholder = t(
    "pages.lazy-tools.query_placeholder",
    "输入一句用户可能会说的话，例如：帮我把这句话回显一下",
  );
  dom.labelTopk.textContent = t("pages.lazy-tools.top_k", "top_k");
  dom.labelThreshold.textContent = t("pages.lazy-tools.threshold", "阈值");
  dom.searchButton.textContent = t("pages.lazy-tools.run", "试跑");

  dom.toolsTitle.textContent = t("pages.lazy-tools.tools", "工具清单");
  dom.toolsHint.textContent = t(
    "pages.lazy-tools.tools_hint",
    "标签是懒加载的召回依据，可直接编辑后保存；改动只写入覆盖层，不动源码",
  );
  dom.enrichButton.textContent = t("pages.lazy-tools.enrich", "用 LLM 补全标签");
  dom.thName.textContent = t("pages.lazy-tools.col_name", "工具名");
  dom.thSource.textContent = t("pages.lazy-tools.col_source", "来源");
  dom.thTags.textContent = t("pages.lazy-tools.col_tags", "标签");
  dom.thTtl.textContent = t("pages.lazy-tools.col_ttl", "存活");
  dom.thRisk.textContent = t("pages.lazy-tools.col_risk", "风险");
  dom.thDesc.textContent = t("pages.lazy-tools.col_desc", "描述");

  dom.sourcesTitle.textContent = t("pages.lazy-tools.sources", "子插件");
  dom.sourcesHint.textContent = t(
    "pages.lazy-tools.sources_hint",
    "停用后其工具不再参与检索，也不会注入；状态会写回插件配置",
  );
  dom.sessionsTitle.textContent = t("pages.lazy-tools.sessions", "会话激活表");

  dom.uploadTitle.textContent = t("pages.lazy-tools.upload", "上传子插件");
  dom.uploadHint.textContent = t(
    "pages.lazy-tools.upload_hint",
    "支持单个 .py 或含 __init__.py 的 .zip 包；同名已存在时请先删除",
  );
  dom.uploadButton.textContent = t("pages.lazy-tools.install", "安装");

  dom.importTitle.textContent = t("pages.lazy-tools.import", "从已装插件导入");
  dom.importHint.textContent = t(
    "pages.lazy-tools.import_hint",
    "把 data/plugins 下插件的源码复制进 sub_plugins/；只接受用 @lazy_tool 写的普通函数工具集",
  );

  dom.commandsTitle.textContent = t("pages.lazy-tools.commands", "指令面板");
}

/* ------------------------------------------------------------------ */
/* 渲染                                                                */
/* ------------------------------------------------------------------ */

function renderOverview(state) {
  const stats = state.stats || {};
  const config = state.config || {};
  const cards = [
    [t("pages.lazy-tools.card_tools", "注册工具"), stats.tools, `${stats.all_tools} 个含元工具`],
    [t("pages.lazy-tools.card_meta", "常驻元工具"), stats.meta_tools, "搜索 / 激活 / 取消激活"],
    [t("pages.lazy-tools.card_index", "索引条目"), stats.indexed, `预检索 ${config.prefetch_enabled ? "开启" : "关闭"}`],
    [t("pages.lazy-tools.card_sessions", "活跃会话"), stats.sessions, `${stats.turn_snapshots} 个本轮快照`],
    [t("pages.lazy-tools.card_active", "当前激活"), stats.activations, `上限 ${config.max_active_per_session} / 会话`],
    [
      t("pages.lazy-tools.card_policy", "过期策略"),
      `${config.default_ttl_turns} 轮`,
      `${config.default_ttl_seconds} 秒，先到者生效`,
    ],
  ];

  clear(dom.overview);
  for (const [label, value, foot] of cards) {
    const card = el("div", "card");
    card.append(
      el("div", "card-label", label),
      el("div", "card-value", value),
      el("div", "card-foot", foot),
    );
    dom.overview.append(card);
  }

  if (!state.enabled) {
    setBadge(t("pages.lazy-tools.disabled", "已关闭（工具原样注入）"), "warn");
  } else if (!config.prefetch_enabled) {
    setBadge(t("pages.lazy-tools.manual", "启用中 · 仅元工具"), "neutral");
  } else {
    setBadge(t("pages.lazy-tools.running", "启用中"), "ok");
  }
}

function renderTools(tools) {
  clear(dom.toolsBody);
  dom.toolsSummary.textContent = `${tools.length}`;

  if (!tools.length) {
    const row = el("tr");
    const cell = el("td", "empty", "还没有注册任何工具。");
    cell.colSpan = 7;
    row.append(cell);
    dom.toolsBody.append(row);
    return;
  }

  for (const tool of tools) {
    const row = el("tr");

    const nameCell = el("td");
    nameCell.append(el("span", "tool-name", tool.name));
    if (!tool.enabled) {
      nameCell.append(el("span", "badge badge-warn", "已停用"));
    }
    if (tool.always_active) {
      nameCell.append(el("span", "badge badge-neutral", "常驻"));
    }
    if (tool.overridden) {
      nameCell.append(
        el(
          "span",
          "badge badge-ok",
          tool.override_source === "llm" ? "LLM 已补" : "已手改",
        ),
      );
    }
    row.append(nameCell);

    row.append(el("td", "muted", tool.source));

    const tagsCell = el("td");
    const tagsInput = document.createElement("input");
    tagsInput.type = "text";
    tagsInput.className = "cmd-input";
    tagsInput.value = (tool.tags || []).join(", ");
    tagsInput.placeholder = "还没有标签，可点右侧「用 LLM 补全标签」";
    tagsCell.append(tagsInput);
    row.append(tagsCell);

    const ttl = tool.ttl_seconds
      ? `${tool.ttl_turns} 轮 / ${tool.ttl_seconds}s`
      : `${tool.ttl_turns} 轮`;
    row.append(el("td", "muted", ttl));

    const riskCell = el("td");
    riskCell.append(
      tool.risk === "high"
        ? el("span", "badge badge-danger", "high")
        : el("span", "muted", "normal"),
    );
    row.append(riskCell);

    const descCell = el("td");
    const descInput = document.createElement("input");
    descInput.type = "text";
    descInput.className = "cmd-input";
    descInput.value = tool.description || "";
    descInput.title = tool.description || "";
    descCell.append(descInput);
    row.append(descCell);

    const actionCell = el("td");
    const actions = el("div", "row-actions");
    const save = el("button", "btn btn-small", "保存");
    save.type = "button";
    save.addEventListener("click", () =>
      saveTool(tool.name, tagsInput.value, descInput.value),
    );
    const reset = el("button", "btn btn-small", "重置");
    reset.type = "button";
    reset.disabled = !tool.overridden;
    reset.title = reset.disabled
      ? "没有覆盖项可重置"
      : "退回代码里写的基线标签与描述";
    reset.addEventListener("click", () => resetTool(tool.name));
    actions.append(save, reset);
    actionCell.append(actions);
    row.append(actionCell);

    dom.toolsBody.append(row);
  }
}

function renderSources(sources, state) {
  clear(dom.sourcesList);
  dom.sourcesHint.textContent = t(
    "pages.lazy-tools.sources_hint",
    "停用后其工具不再参与检索，也不会注入；状态会写回插件配置",
  );

  const loaded = sources.filter((item) => item.loaded).length;
  const tools = sources.reduce((sum, item) => sum + (item.tools || 0), 0);
  dom.sourcesCount.textContent = `磁盘 ${sources.length} 个 · 已加载 ${loaded} 个 · 共 ${tools} 个工具`;

  if (!sources.length) {
    dom.sourcesList.append(
      el(
        "div",
        "empty",
        "sub_plugins/ 下还没有子插件。若你是直接往目录里放的文件，点上面的「重新扫描目录」即可加载（本插件只在自己被加载/重载时才自动扫一次）。",
      ),
    );
    return;
  }

  for (const source of sources) {
    const item = el("div", "source-item");

    const meta = el("div", "source-meta");
    meta.append(el("div", "source-name", source.name));
    const detail = [`${source.tools} 个工具`];
    if (source.error) detail.push(`加载失败：${source.error}`);
    else if (source.retired) detail.push("已删除，工具已冻结不再注入");
    else if (!source.loaded) detail.push("未加载");
    else if (!source.enabled) detail.push("已停用");
    meta.append(el("div", "muted", detail.join(" · ")));
    item.append(meta);

    const label = el("label", "switch");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(source.enabled);
    // 已退休的来源不允许在界面上重新启用：它的文件已经删了，
    // 重新启用只会让工具从「冻结」变成「参与检索但检索不到」，徒增困惑。
    input.disabled = Boolean(state && state.enabled === false) || Boolean(source.retired);
    input.addEventListener("change", () =>
      toggleSource(source.name, input.checked, input),
    );
    label.append(input, el("span", "slider"));

    const actions = el("div", "source-actions");
    actions.append(label);
    if (!source.retired) {
      const del = el("button", "btn btn-small btn-danger", "删除");
      del.type = "button";
      del.addEventListener("click", () => deleteSubplugin(source.name));
      actions.append(del);
    }
    item.append(actions);

    dom.sourcesList.append(item);
  }
}

function renderSessions(sessions) {
  clear(dom.sessionsList);
  const total = sessions.reduce((sum, item) => sum + item.count, 0);
  dom.sessionsSummary.textContent = sessions.length
    ? `${sessions.length} 个会话 / ${total} 项激活`
    : "暂无激活";

  if (!sessions.length) {
    dom.sessionsList.append(
      el(
        "div",
        "empty",
        "当前没有任何会话激活了工具。发一条消息触发预检索后回到这里刷新即可看到。",
      ),
    );
    return;
  }

  for (const session of sessions) {
    const item = el("div", "session-item");

    const head = el("div", "session-head");
    head.append(el("div", "session-umo", session.umo));

    const button = el("button", "btn btn-small", "清空");
    button.type = "button";
    button.addEventListener("click", () => clearSession(session.umo));
    head.append(button);
    item.append(head);

    const tools = el("div", "session-tools");
    for (const tool of session.tools) {
      const chip = el("div", "session-tool");
      chip.append(document.createTextNode(tool.name));
      const bits = [];
      if (tool.remaining_turns !== null && tool.remaining_turns !== undefined) {
        bits.push(`${tool.remaining_turns} 轮`);
      }
      if (tool.expires_in !== null && tool.expires_in !== undefined) {
        bits.push(`${tool.expires_in}s`);
      }
      if (tool.score) bits.push(`分 ${tool.score}`);
      if (bits.length) {
        chip.append(el("span", "meta", ` ${bits.join(" · ")}`));
      }
      tools.append(chip);
    }
    item.append(tools);
    dom.sessionsList.append(item);
  }
}

function renderSearchResult(result) {
  const hits = result.hits || [];
  clear(dom.searchResult);

  const tokens = result.info_tokens || [];
  const summaryBits = [`阈值 ${result.min_score}`, `top_k ${result.top_k}`];
  if (tokens.length) {
    summaryBits.push(`信息词：${tokens.join(" / ")}`);
  } else {
    summaryBits.push("信息词：无");
  }
  dom.searchSummary.textContent = summaryBits.join(" · ");

  if (!tokens.length) {
    dom.searchResult.append(
      el(
        "div",
        "empty",
        "这句话里的词和任何工具的名字/标签/描述/示例都没有交集——调阈值不会有帮助，" +
          "应该给工具补 tags 或 examples。",
      ),
    );
    return;
  }

  if (!hits.length) {
    dom.searchResult.append(el("div", "empty", "没有任何工具被召回。"));
    return;
  }

  const wrap = el("div", "table-wrap");
  const table = el("table", "table");
  const thead = el("thead");
  const headRow = el("tr");
  for (const label of ["#", "工具", "分数", "本轮会激活", "来源", "状态"]) {
    headRow.append(el("th", "", label));
  }
  thead.append(headRow);
  table.append(thead);

  const tbody = el("tbody");
  for (const hit of hits) {
    const row = el("tr", "hit-row");
    row.append(el("td", "", `#${hit.rank}`));
    row.append(el("td", "tool-name", hit.name));

    const scoreCell = el("td");
    scoreCell.append(
      el(
        "span",
        `hit-score ${hit.passed ? "hit-pass" : "hit-fail"}`,
        hit.score.toFixed(4),
      ),
    );
    row.append(scoreCell);

    const verdict = el("td");
    if (hit.would_activate) {
      verdict.append(el("span", "badge badge-ok", "是"));
    } else if (!hit.passed) {
      verdict.append(el("span", "muted", "低于阈值"));
    } else if (!hit.allowed) {
      verdict.append(el("span", "badge badge-warn", "被上游排除"));
    } else if (!hit.in_top_k) {
      verdict.append(el("span", "muted", "超出 top_k"));
    } else {
      verdict.append(el("span", "badge badge-warn", "高风险，需手动激活"));
    }
    row.append(verdict);

    row.append(el("td", "muted", hit.source));

    const status = el("td");
    status.append(
      hit.active
        ? el("span", "badge badge-ok", "已激活")
        : el("span", "muted", "未激活"),
    );
    row.append(status);

    tbody.append(row);
  }
  table.append(tbody);
  wrap.append(table);
  dom.searchResult.append(wrap);
}

/* ------------------------------------------------------------------ */
/* 动作                                                                */
/* ------------------------------------------------------------------ */

async function loadState(force = false) {
  if (busy) return;
  setBusy(true);
  clearError();
  if (!force) setBadge(t("pages.lazy-tools.loading", "同步中…"), "neutral");
  try {
    const state = await callGet("state");
    renderOverview(state);
    renderTools(state.tools || []);
    renderSources(state.sources || [], state);
    renderSessions(state.sessions || []);

    if (!inputsInitialized) {
      const config = state.config || {};
      dom.topkInput.value = String(config.top_k ?? 3);
      dom.thresholdInput.value = String(config.min_score ?? 0.35);
      inputsInitialized = true;
    }
    if (!force) setBadge(t("pages.lazy-tools.synced", "已同步"), "ok");
    await loadCommands();
    await loadImportCandidates();
  } catch (error) {
    showError(error?.message || String(error));
    setBadge(t("pages.lazy-tools.failed", "读取失败"), "danger");
  } finally {
    setBusy(false);
  }
}

async function runSearch() {
  if (busy) return;
  const query = dom.queryInput.value.trim();
  if (!query) {
    showToast("请先输入一句测试文本");
    return;
  }
  setBusy(true);
  clearError();
  dom.searchSummary.textContent = "检索中…";
  clear(dom.searchResult);
  try {
    const result = await callPost("search", {
      query,
      top_k: Number(dom.topkInput.value) || undefined,
      min_score: dom.thresholdInput.value === "" ? undefined : Number(dom.thresholdInput.value),
    });
    renderSearchResult(result);
  } catch (error) {
    showError(error?.message || String(error));
    dom.searchSummary.textContent = "";
  } finally {
    setBusy(false);
  }
}

async function clearSession(umo) {
  if (busy) return;
  setBusy(true);
  clearError();
  try {
    const result = await callPost("sessions/clear", { umo });
    showToast(`已清空 ${result.umo}（${result.cleared} 项）`);
    await loadState(true);
  } catch (error) {
    showError(error?.message || String(error));
  } finally {
    setBusy(false);
  }
}

async function toggleSource(name, enabled, input) {
  if (busy) return;
  setBusy(true);
  clearError();
  try {
    const result = await callPost("sources/toggle", { name, enabled });
    showToast(`子插件 ${result.name} 已${result.enabled ? "启用" : "停用"}`);
    await loadState(true);
  } catch (error) {
    showError(error?.message || String(error));
    if (input) input.checked = !enabled;
  } finally {
    setBusy(false);
  }
}

/* ------------------------------------------------------------------ */
/* 子插件上传                                                          */
/* ------------------------------------------------------------------ */

async function uploadSubplugin() {
  if (busy) return;
  const file = dom.uploadInput.files?.[0];
  if (!file) {
    showToast("请先选择一个 .py 或 .zip 文件");
    return;
  }
  setBusy(true);
  clearError();
  dom.uploadResult.textContent = `上传中：${file.name}（${file.size} 字节）…`;
  try {
    // bridge.upload 的字段名由 dashboard 固定为 file，后端也是按 file 取
    const result = await unwrap(await bridge.upload("subplugins/upload", file));
    dom.uploadResult.textContent = `已安装 ${result.name}（${result.tools} 个工具，索引 ${result.indexed} 条）`;
    dom.uploadInput.value = "";
    showToast(`子插件 ${result.name} 已安装`);
    await loadState(true);
  } catch (error) {
    dom.uploadResult.textContent = "";
    showError(error?.message || String(error));
  } finally {
    setBusy(false);
  }
}

async function rescanSubplugins() {
  if (busy) return;
  setBusy(true);
  clearError();
  try {
    const result = await callPost("subplugins/rescan", {});
    const failures = Object.entries(result.errors || {});
    if (failures.length) {
      showError(
        `扫描完成，但有 ${failures.length} 个子插件加载失败：\n- ` +
          failures.map(([name, err]) => `${name} → ${err}`).join("\n- "),
      );
    } else {
      showToast(
        `扫描完成：磁盘 ${result.discovered.length} 个，已加载 ${result.loaded.length} 个` +
          (result.added.length ? `，新增 ${result.added.join(", ")}` : ""),
      );
    }
    await loadState(true);
  } catch (error) {
    showError(error?.message || String(error));
  } finally {
    setBusy(false);
  }
}

async function deleteSubplugin(name) {
  if (busy) return;
  if (!window.confirm(`删除子插件 ${name}？它的工具会一并从注册表移除。`)) return;
  setBusy(true);
  clearError();
  try {
    const result = await callPost("subplugins/delete", { name });
    showToast(
      `已删除 ${result.name}（文件${result.removed_files ? "已删除" : "不存在"}，工具 ${result.removed_tools} 个）`,
    );
    await loadState(true);
  } catch (error) {
    showError(error?.message || String(error));
  } finally {
    setBusy(false);
  }
}

/* ------------------------------------------------------------------ */
/* 工具标签：手动改 + LLM 补全                                          */
/* ------------------------------------------------------------------ */

async function saveTool(name, tagsText, description) {
  if (busy) return;
  setBusy(true);
  clearError();
  try {
    const tags = String(tagsText || "")
      .split(/[,，、]/)
      .map((item) => item.trim())
      .filter(Boolean);
    const result = await callPost("tools/update", {
      name,
      tags,
      description: String(description || ""),
    });
    showToast(
      `${name} 已保存（${(result.tags || []).length} 个标签）` +
        (result.persisted ? "" : "；但覆盖文件写入失败，重启后会丢失"),
    );
    await loadState(true);
  } catch (error) {
    showError(error?.message || String(error));
  } finally {
    setBusy(false);
  }
}

async function resetTool(name) {
  if (busy) return;
  setBusy(true);
  clearError();
  try {
    const result = await callPost("tools/reset", { name });
    showToast(
      result.removed
        ? `${name} 已重置回代码里的基线`
        : `${name} 本来就没有覆盖项`,
    );
    await loadState(true);
  } catch (error) {
    showError(error?.message || String(error));
  } finally {
    setBusy(false);
  }
}

async function enrichTools() {
  if (busy) return;
  setBusy(true);
  clearError();
  dom.toolsHint.textContent = "正在调用模型总结…";
  try {
    const result = await callPost("tools/enrich", { only_missing: true });
    const count = Object.keys(result.updated || {}).length;
    const errors = result.errors || [];
    if (count) {
      showToast(
        `已为 ${count} 个工具补充标签` +
          `（缺标签候选 ${result.considered} 个，本次送 ${result.sent} 个）`,
      );
    }
    if (errors.length) {
      showError(`LLM 总结未完成：${errors.join("；")}`);
    } else if (!count) {
      showToast("没有需要补标签的工具（都已有标签）");
    }
    await loadState(true);
  } catch (error) {
    showError(error?.message || String(error));
  } finally {
    setBusy(false);
  }
}

/* ------------------------------------------------------------------ */
/* 从已装插件导入为子插件                                              */
/* ------------------------------------------------------------------ */

async function loadImportCandidates() {
  clear(dom.importBody);
  let payload;
  try {
    payload = await callGet("plugin-import/candidates");
  } catch (error) {
    dom.importBody.append(
      el("div", "empty", `读取插件列表失败：${error?.message || error}`),
    );
    return;
  }
  if (!payload.supported) {
    dom.importBody.append(
      el("div", "empty", payload.reason || "此环境无法列出自装插件。"),
    );
    return;
  }
  const candidates = payload.candidates || [];
  if (!candidates.length) {
    dom.importBody.append(el("div", "empty", "data/plugins 下没有其它插件。"));
    return;
  }

  const list = el("div", "import-list");
  for (const item of candidates) {
    list.append(importRow(item));
  }
  dom.importBody.append(list);
}

function importRow(item) {
  const row = el("div", "import-item");

  const head = el("div", "import-head");
  const title = el("div", "import-title-cell");
  title.append(
    el("span", "source-name", item.display_name || item.dir_name),
    el("span", "muted", ` ${item.dir_name}${item.version ? ` · v${item.version}` : ""}`),
  );
  head.append(title);

  const badges = el("div", "import-badges");
  badges.append(
    item.portable
      ? el("span", "badge badge-ok", "可导入")
      : el("span", "badge badge-danger", "不可导入"),
  );
  if (item.already_imported) badges.append(el("span", "badge badge-warn", "已导入"));
  head.append(badges);
  row.append(head);

  if (item.desc) row.append(el("div", "muted", item.desc));

  if (item.portable && item.lazy_tool_names?.length) {
    row.append(
      el("div", "muted", `检测到工具：${item.lazy_tool_names.join(", ")}`),
    );
  }

  for (const reason of item.reasons || []) {
    row.append(el("div", "import-reason", reason));
  }
  for (const warning of item.warnings || []) {
    row.append(el("div", "import-warning", warning));
  }

  const actions = el("div", "import-actions");
  const button = el(
    "button",
    "btn btn-small btn-primary",
    item.already_imported ? "覆盖导入" : "导入",
  );
  button.type = "button";
  // 刻意**不禁用**不可导入的按钮：禁用的按钮点下去毫无反馈，会让人以为"什么都没发生"。
  // 保持可点，点了就把不能导入的原因明确摆出来。
  button.addEventListener("click", () => {
    if (!item.target_name) {
      showError(`${item.dir_name} 的目录名不能作为子插件名，无法导入。`);
      return;
    }
    if (!item.portable) {
      showError(
        `不能导入 ${item.dir_name}：\n- ` +
          ((item.reasons || []).join("\n- ") || "原因未知（插件未给出说明）"),
      );
      return;
    }
    importPlugin(item.dir_name, item.already_imported);
  });
  actions.append(button);
  row.append(actions);

  return row;
}

async function importPlugin(dirName, overwrite) {
  if (busy) return;
  if (
    overwrite &&
    !window.confirm(`覆盖导入会替换 sub_plugins 下已有的同名子插件，继续？`)
  ) {
    return;
  }
  setBusy(true);
  clearError();
  try {
    const result = await callPost("plugin-import/apply", {
      dir_name: dirName,
      overwrite: Boolean(overwrite),
    });
    showToast(
      `已导入 ${result.name}：${result.files} 个文件，${
        (result.tools || []).length
      } 个工具`,
    );
    if (result.warnings?.length) {
      dom.uploadResult.textContent = `导入 ${result.name} 的提醒：${result.warnings.join("；")}`;
    }
    await loadState(true);
  } catch (error) {
    showError(error?.message || String(error));
  } finally {
    setBusy(false);
  }
}

/* ------------------------------------------------------------------ */
/* 指令面板（AstrBot 官方 command_management 的薄前端）                 */
/* ------------------------------------------------------------------ */

async function loadCommands() {
  clear(dom.commandsBody);
  dom.commandsSummary.textContent = "";
  let payload;
  try {
    payload = await callGet("commands");
  } catch (error) {
    dom.commandsBody.append(
      el("div", "empty", `读取指令失败：${error?.message || error}`),
    );
    return;
  }
  if (!payload.supported) {
    dom.commandsBody.append(el("div", "empty", payload.reason || "指令面板不可用。"));
    return;
  }

  const commands = payload.commands || [];
  const conflicts = payload.conflicts || [];
  dom.commandsSummary.textContent = `${commands.length} 条指令${
    conflicts.length ? ` · ${conflicts.length} 组冲突` : ""
  }`;

  if (conflicts.length) {
    const box = el("div", "conflict-box");
    box.append(el("div", "conflict-title", "指令名冲突"));
    for (const group of conflicts) {
      const names = (group.handlers || [])
        .map((h) => `${h.plugin}:${h.current_name}`)
        .join(" / ");
      box.append(el("div", "muted", `${group.conflict_key} → ${names}`));
    }
    dom.commandsBody.append(box);
  }

  const wrap = el("div", "table-wrap");
  const table = el("table", "table");
  const thead = el("thead");
  const head = el("tr");
  for (const label of ["插件", "指令", "别名（逗号分隔）", "权限", "启用", ""]) {
    head.append(el("th", "", label));
  }
  thead.append(head);
  table.append(thead);

  const tbody = el("tbody");
  for (const cmd of commands) {
    tbody.append(commandRow(cmd));
  }
  table.append(tbody);
  wrap.append(table);
  dom.commandsBody.append(wrap);
}

function commandRow(cmd) {
  const row = el("tr");

  const pluginCell = el("td", "muted");
  pluginCell.append(el("div", "", cmd.plugin_display_name || cmd.plugin || ""));
  if (cmd.has_conflict) pluginCell.append(el("span", "badge badge-danger", "冲突"));
  if (cmd.reserved) pluginCell.append(el("span", "badge badge-neutral", "内置"));
  row.append(pluginCell);

  const nameCell = el("td");
  const nameInput = document.createElement("input");
  nameInput.type = "text";
  nameInput.value = cmd.current_fragment || "";
  nameInput.className = "cmd-input";
  nameInput.title = `原始指令：${cmd.original_command || ""}`;
  nameInput.disabled = Boolean(cmd.reserved);
  nameCell.append(nameInput);
  row.append(nameCell);

  const aliasCell = el("td");
  const aliasInput = document.createElement("input");
  aliasInput.type = "text";
  aliasInput.value = (cmd.aliases || []).join(", ");
  aliasInput.className = "cmd-input";
  aliasInput.disabled = Boolean(cmd.reserved);
  aliasCell.append(aliasInput);
  row.append(aliasCell);

  const permCell = el("td");
  const permSelect = document.createElement("select");
  permSelect.className = "cmd-select";
  const options = [
    ["", cmd.permission === "everyone" ? "默认（everyone）" : "保持不变"],
    ["admin", "admin"],
    ["member", "member"],
  ];
  for (const [value, label] of options) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    permSelect.append(option);
  }
  permSelect.value = "";
  permSelect.addEventListener("change", async () => {
    if (!permSelect.value) return;
    const target = permSelect.value;
    permSelect.disabled = true;
    try {
      await callPost("commands/permission", {
        handler_full_name: cmd.handler_full_name,
        permission: target,
      });
      showToast(`${cmd.effective_command} 权限已设为 ${target}`);
      await loadCommands();
    } catch (error) {
      showError(error?.message || String(error));
      permSelect.value = "";
    } finally {
      permSelect.disabled = false;
    }
  });
  permCell.append(permSelect);
  row.append(permCell);

  const enabledCell = el("td");
  const enabledInput = document.createElement("input");
  enabledInput.type = "checkbox";
  enabledInput.checked = Boolean(cmd.enabled);
  enabledInput.disabled = Boolean(cmd.reserved);
  enabledInput.addEventListener("change", async () => {
    enabledInput.disabled = true;
    try {
      await callPost("commands/toggle", {
        handler_full_name: cmd.handler_full_name,
        enabled: enabledInput.checked,
      });
      showToast(`${cmd.effective_command} 已${enabledInput.checked ? "启用" : "停用"}`);
    } catch (error) {
      showError(error?.message || String(error));
      enabledInput.checked = !enabledInput.checked;
    } finally {
      enabledInput.disabled = false;
    }
  });
  enabledCell.append(enabledInput);
  row.append(enabledCell);

  const actionCell = el("td");
  const save = el("button", "btn btn-small", "保存");
  save.type = "button";
  save.disabled = Boolean(cmd.reserved);
  save.addEventListener("click", async () => {
    const fragment = nameInput.value.trim();
    if (!fragment) {
      showToast("指令名不能为空");
      return;
    }
    const aliases = aliasInput.value
      .split(/[,，]/)
      .map((item) => item.trim())
      .filter(Boolean);
    save.disabled = true;
    try {
      await callPost("commands/rename", {
        handler_full_name: cmd.handler_full_name,
        fragment,
        aliases,
      });
      showToast(`${cmd.original_command} 已更新`);
      await loadCommands();
    } catch (error) {
      showError(error?.message || String(error));
    } finally {
      save.disabled = false;
    }
  });
  actionCell.append(save);
  row.append(actionCell);

  return row;
}

/* ------------------------------------------------------------------ */
/* 启动                                                                */
/* ------------------------------------------------------------------ */

function bindEvents() {
  dom.refreshButton.addEventListener("click", () => loadState(false));
  dom.searchButton.addEventListener("click", runSearch);
  dom.uploadButton.addEventListener("click", uploadSubplugin);
  dom.enrichButton.addEventListener("click", enrichTools);
  dom.rescanButton.addEventListener("click", rescanSubplugins);
  dom.queryInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      runSearch();
    }
  });
}

async function start() {
  applyLabels();
  bindEvents();

  if (!bridge) {
    showError(
      "未能连接 AstrBot Plugin Page bridge。请确认页面是通过 AstrBot WebUI 打开的，" +
        "并且没有把 index.html 直接当静态文件访问。",
    );
    setBadge("bridge 缺失", "danger");
    return;
  }

  // 主题跟随 WebUI，切换时 bridge 会回调
  bridge.onContext?.((context) => {
    document.documentElement.dataset.theme = context?.isDark ? "dark" : "light";
  });

  try {
    await bridge.ready();
    await loadState(true);
  } catch (error) {
    showError(error?.message || String(error));
    setBadge("初始化失败", "danger");
  }
}

start();
