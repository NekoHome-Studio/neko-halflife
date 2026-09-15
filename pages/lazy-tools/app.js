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
  sessionsTitle: document.getElementById("sessions-title"),
  sessionsSummary: document.getElementById("sessions-summary"),
  sessionsList: document.getElementById("sessions-list"),
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
  document.title = t("pages.lazy-tools.title", "懒加载工具");
  dom.heading.textContent = t("pages.lazy-tools.heading", "懒加载工具");
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
    cell.colSpan = 6;
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
    row.append(nameCell);

    row.append(el("td", "muted", tool.source));

    const tagCell = el("td");
    const tags = el("div", "tags");
    const tagList = (tool.tags || []).slice(0, 6);
    if (tagList.length) {
      for (const tag of tagList) tags.append(el("span", "tag", tag));
    } else {
      tags.append(el("span", "muted", "—"));
    }
    tagCell.append(tags);
    row.append(tagCell);

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

    const desc = el("td", "desc-cell", tool.description);
    desc.title = tool.description || "";
    row.append(desc);

    dom.toolsBody.append(row);
  }
}

function renderSources(sources, state) {
  clear(dom.sourcesList);
  dom.sourcesHint.textContent = t(
    "pages.lazy-tools.sources_hint",
    "停用后其工具不再参与检索，也不会注入；状态会写回插件配置",
  );

  if (!sources.length) {
    dom.sourcesList.append(el("div", "empty", "sub_plugins/ 下还没有子插件。"));
    return;
  }

  for (const source of sources) {
    const item = el("div", "source-item");

    const meta = el("div", "source-meta");
    meta.append(el("div", "source-name", source.name));
    const detail = [`${source.tools} 个工具`];
    if (source.error) detail.push(`加载失败：${source.error}`);
    else if (!source.loaded) detail.push("未加载");
    else if (!source.enabled) detail.push("已停用");
    meta.append(el("div", "muted", detail.join(" · ")));
    item.append(meta);

    const label = el("label", "switch");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(source.enabled);
    input.disabled = Boolean(state && state.enabled === false);
    input.addEventListener("change", () =>
      toggleSource(source.name, input.checked, input),
    );
    label.append(input, el("span", "slider"));
    item.append(label);

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
/* 启动                                                                */
/* ------------------------------------------------------------------ */

function bindEvents() {
  dom.refreshButton.addEventListener("click", () => loadState(false));
  dom.searchButton.addEventListener("click", runSearch);
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
