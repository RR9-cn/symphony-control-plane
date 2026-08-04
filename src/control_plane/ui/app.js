const STATUS_META = {
  draft: { label: "Draft", color: "#7d8795" },
  ready: { label: "Ready", color: "#d6ff3f" },
  running: { label: "Running", color: "#54d7e8" },
  needs_human: { label: "Needs human", color: "#b895ff" },
  stage_review: { label: "Stage review", color: "#72a7ff" },
  rework: { label: "Rework", color: "#ffb65c" },
  retry_queued: { label: "Retry queued", color: "#f29ad4" },
  blocked: { label: "Blocked", color: "#ff7272" },
  done: { label: "Done", color: "#66d996" },
  cancelled: { label: "Cancelled", color: "#667180" },
};

const ROLE_META = {
  solution_architect: { label: "Solution Architect", color: "#b895ff" },
  backend_builder: { label: "Backend Builder", color: "#54d7e8" },
  code_reviewer: { label: "Code Reviewer", color: "#72a7ff" },
  test_designer: { label: "Test Designer", color: "#ffb65c" },
  test_executor: { label: "Test Executor", color: "#66d996" },
};

const BOARD_COLUMNS = [
  { key: "draft", label: "Draft", statuses: ["draft"] },
  { key: "ready", label: "Ready", statuses: ["ready"] },
  { key: "running", label: "Running", statuses: ["running"] },
  { key: "needs_human", label: "Needs human", statuses: ["needs_human"] },
  { key: "stage_review", label: "Stage review", statuses: ["stage_review"] },
  { key: "rework", label: "Rework", statuses: ["rework"] },
  { key: "retry_queued", label: "Retry", statuses: ["retry_queued"] },
  { key: "blocked", label: "Blocked", statuses: ["blocked"] },
  { key: "done", label: "Done", statuses: ["done", "cancelled"] },
];

const EVENT_LABELS = {
  created: "工作项已创建",
  updated: "工作项已更新",
  claimed: "Worker 已领取",
  heartbeat: "收到 Heartbeat",
  agent_started: "Agent 开始执行",
  thread_started: "Codex Thread 已启动",
  turn_started: "Codex Turn 已启动",
  artifact_created: "产物已登记",
  validation_passed: "校验通过",
  human_input_requested: "请求人工决策",
  human_review_requested: "请求人工复核",
  human_decision_resolved: "人工决策已回复",
  work_item_blocked: "工作项被阻塞",
  blocker_resolved: "阻塞已解除",
  retry_scheduled: "已安排重试",
  retry_due: "重试已就绪",
  agent_completed: "Agent 已交付",
  stage_approved: "阶段已通过",
  stage_rejected: "阶段被退回",
  rework_queued: "返工已排队",
  dependency_satisfied: "依赖已满足",
  work_item_readied: "工作项已就绪",
  work_item_cancelled: "工作项已取消",
};

const state = {
  token: sessionStorage.getItem("acp_api_token") || "",
  features: [],
  workItems: [],
  profiles: [],
  selectedFeatureId: sessionStorage.getItem("acp_feature_id") || "",
  selectedItemId: null,
  detail: null,
  activeTab: "overview",
  pendingAction: null,
  loading: false,
  authPrompted: false,
};

const dom = {
  connectionPill: document.querySelector("#connection-pill"),
  connectionLabel: document.querySelector("#connection-label"),
  refreshButton: document.querySelector("#refresh-button"),
  tokenButton: document.querySelector("#token-button"),
  featureCount: document.querySelector("#feature-count"),
  featureList: document.querySelector("#feature-list"),
  featureSearch: document.querySelector("#feature-search"),
  featureId: document.querySelector("#feature-id"),
  featureTitle: document.querySelector("#feature-title"),
  featureDescription: document.querySelector("#feature-description"),
  workItemSearch: document.querySelector("#work-item-search"),
  roleFilter: document.querySelector("#role-filter"),
  board: document.querySelector("#board"),
  lastUpdated: document.querySelector("#last-updated"),
  metricTotal: document.querySelector("#metric-total"),
  metricCompletion: document.querySelector("#metric-completion"),
  metricRunning: document.querySelector("#metric-running"),
  metricWorkers: document.querySelector("#metric-workers"),
  metricHuman: document.querySelector("#metric-human"),
  metricRisk: document.querySelector("#metric-risk"),
  metricRetry: document.querySelector("#metric-retry"),
  drawer: document.querySelector("#detail-drawer"),
  drawerBackdrop: document.querySelector("#drawer-backdrop"),
  drawerClose: document.querySelector("#drawer-close"),
  drawerId: document.querySelector("#drawer-id"),
  drawerStatus: document.querySelector("#drawer-status"),
  drawerTitle: document.querySelector("#drawer-title"),
  drawerContent: document.querySelector("#drawer-content"),
  drawerActions: document.querySelector("#drawer-actions"),
  eventCount: document.querySelector("#event-count"),
  attemptCount: document.querySelector("#attempt-count"),
  artifactCount: document.querySelector("#artifact-count"),
  tokenModal: document.querySelector("#token-modal"),
  tokenForm: document.querySelector("#token-form"),
  tokenInput: document.querySelector("#token-input"),
  actionModal: document.querySelector("#action-modal"),
  actionForm: document.querySelector("#action-form"),
  actionEyebrow: document.querySelector("#action-eyebrow"),
  actionTitle: document.querySelector("#action-title"),
  actionDescription: document.querySelector("#action-description"),
  actionFields: document.querySelector("#action-fields"),
  actionConfirm: document.querySelector("#action-confirm"),
  actionError: document.querySelector("#action-error"),
  toastRegion: document.querySelector("#toast-region"),
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function parseApiDate(value) {
  if (!value) return null;
  const normalized =
    typeof value === "string" &&
    /^\d{4}-\d{2}-\d{2}T/.test(value) &&
    !/(?:Z|[+-]\d{2}:\d{2})$/i.test(value)
      ? `${value}Z`
      : value;
  const date = new Date(normalized);
  return Number.isNaN(date.valueOf()) ? null : date;
}

function formatDate(value, withSeconds = false) {
  if (!value) return "—";
  const date = parseApiDate(value);
  if (!date) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    ...(withSeconds ? { second: "2-digit" } : {}),
  }).format(date);
}

function relativeTime(value) {
  if (!value) return "—";
  const date = parseApiDate(value);
  if (!date) return String(value);
  const seconds = Math.round((date.valueOf() - Date.now()) / 1000);
  const formatter = new Intl.RelativeTimeFormat("zh-CN", { numeric: "auto" });
  if (Math.abs(seconds) < 60) return formatter.format(seconds, "second");
  const minutes = Math.round(seconds / 60);
  if (Math.abs(minutes) < 60) return formatter.format(minutes, "minute");
  const hours = Math.round(minutes / 60);
  if (Math.abs(hours) < 24) return formatter.format(hours, "hour");
  return formatter.format(Math.round(hours / 24), "day");
}

function compactId(value) {
  if (!value) return "—";
  return value.length > 18 ? `${value.slice(0, 8)}…${value.slice(-6)}` : value;
}

function toast(message, kind = "success") {
  const node = document.createElement("div");
  node.className = `toast ${kind}`;
  node.textContent = message;
  dom.toastRegion.append(node);
  window.setTimeout(() => node.remove(), 4200);
}

function setConnection(status, label) {
  dom.connectionPill.classList.remove("online", "offline");
  if (status) dom.connectionPill.classList.add(status);
  dom.connectionLabel.textContent = label;
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
  if (options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, { ...options, headers });
  let payload = null;
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) payload = await response.json();
  if (!response.ok) {
    const error = new Error(payload?.error?.message || `请求失败 (${response.status})`);
    error.status = response.status;
    error.code = payload?.error?.code;
    if (response.status === 401) {
      setConnection("offline", "需要凭据");
      if (!state.authPrompted && !dom.tokenModal.open) {
        state.authPrompted = true;
        openTokenModal();
      }
    }
    throw error;
  }
  return payload;
}

function currentFeatureItems() {
  const search = dom.workItemSearch.value.trim().toLowerCase();
  const role = dom.roleFilter.value;
  return state.workItems.filter((item) => {
    if (state.selectedFeatureId && item.feature_id !== state.selectedFeatureId) return false;
    if (role && item.agent_role !== role) return false;
    if (search) {
      const haystack = `${item.id} ${item.title} ${item.description}`.toLowerCase();
      if (!haystack.includes(search)) return false;
    }
    return true;
  });
}

function renderFeatures() {
  const query = dom.featureSearch.value.trim().toLowerCase();
  const itemCountByFeature = state.workItems.reduce((counts, item) => {
    counts[item.feature_id] = (counts[item.feature_id] || 0) + 1;
    return counts;
  }, {});
  const filtered = state.features.filter((feature) =>
    `${feature.id} ${feature.title}`.toLowerCase().includes(query),
  );
  dom.featureCount.textContent = String(state.features.length);

  const allItem = `
    <button class="feature-item ${state.selectedFeatureId ? "" : "active"}" data-feature-id="" type="button">
      <span class="feature-monogram">ALL</span>
      <span class="feature-copy"><strong>全部 Feature</strong><span>跨项目任务总览</span></span>
      <small>${state.workItems.length}</small>
    </button>`;
  const items = filtered
    .map((feature) => {
      const suffix = feature.id.split("-").at(-1) || "F";
      return `
        <button class="feature-item ${state.selectedFeatureId === feature.id ? "active" : ""}" data-feature-id="${escapeHtml(feature.id)}" type="button">
          <span class="feature-monogram">${escapeHtml(suffix.slice(-3))}</span>
          <span class="feature-copy"><strong>${escapeHtml(feature.title)}</strong><span>${escapeHtml(feature.id)}</span></span>
          <small>${itemCountByFeature[feature.id] || 0}</small>
        </button>`;
    })
    .join("");
  dom.featureList.innerHTML = allItem + items;
}

function renderHeader() {
  const feature = state.features.find((item) => item.id === state.selectedFeatureId);
  dom.featureId.textContent = feature?.id || "全部";
  dom.featureTitle.textContent = feature?.title || "任务总览";
  dom.featureDescription.textContent =
    feature?.description || "查看 Agent 执行状态、交接产物和人工决策。";
}

function renderMetrics(items) {
  const count = (statuses) => items.filter((item) => statuses.includes(item.status)).length;
  const total = items.length;
  const done = count(["done"]);
  const workers = new Set(
    items.filter((item) => item.status === "running" && item.claim.worker_id).map((item) => item.claim.worker_id),
  );
  dom.metricTotal.textContent = String(total);
  dom.metricCompletion.textContent = total ? `${Math.round((done / total) * 100)}% 已完成` : "暂无任务";
  dom.metricRunning.textContent = String(count(["running"]));
  dom.metricWorkers.textContent = `${workers.size} 个 Worker`;
  dom.metricHuman.textContent = String(count(["needs_human", "stage_review"]));
  dom.metricRisk.textContent = String(count(["blocked", "rework"]));
  dom.metricRetry.textContent = `${count(["retry_queued"])} 个重试`;
}

function workCard(item) {
  const role = ROLE_META[item.agent_role] || { label: item.agent_role, color: "#929cab" };
  const worker = item.claim.worker_id || (item.blocked_by.length ? `等待 ${item.blocked_by.length} 项依赖` : relativeTime(item.updated_at));
  return `
    <button class="work-card ${escapeHtml(item.status)}" data-item-id="${escapeHtml(item.id)}" type="button">
      <span class="card-topline">
        <span class="card-id">${escapeHtml(item.id)}</span>
        <span class="priority p${item.priority}">P${item.priority}</span>
      </span>
      <h4>${escapeHtml(item.title)}</h4>
      <p>${escapeHtml(item.description)}</p>
      <span class="card-meta">
        <span class="role-chip" style="--role-color:${role.color}">${escapeHtml(role.label)}</span>
        <span class="priority">v${item.version}</span>
      </span>
      <span class="card-footer">
        <span class="worker-label">
          <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="3"/><path d="M6.5 20a5.5 5.5 0 0 1 11 0"/></svg>
          ${escapeHtml(worker)}
        </span>
        <span>${escapeHtml(item.stage.replaceAll("_", " "))}</span>
      </span>
    </button>`;
}

function renderBoard() {
  const items = currentFeatureItems();
  renderMetrics(items);
  if (!state.features.length && !state.workItems.length) {
    dom.board.innerHTML = `<div class="empty-state"><div><strong>还没有研发任务</strong>通过 API 创建 Feature 和 WorkItem 后会显示在这里。</div></div>`;
    return;
  }
  dom.board.innerHTML = BOARD_COLUMNS.map((column) => {
    const columnItems = items.filter((item) => column.statuses.includes(item.status));
    const meta = STATUS_META[column.key];
    return `
      <section class="board-column" style="--column-color:${meta.color}">
        <header class="column-header">
          <span class="column-title"><span class="column-dot"></span>${column.label}</span>
          <span class="column-count">${columnItems.length}</span>
        </header>
        <div class="column-cards">
          ${columnItems.length ? columnItems.map(workCard).join("") : '<div class="empty-column">暂无工作项</div>'}
        </div>
      </section>`;
  }).join("");
}

function renderAll() {
  renderFeatures();
  renderHeader();
  renderBoard();
}

async function refresh({ quiet = false } = {}) {
  if (state.loading) return;
  state.loading = true;
  dom.refreshButton.disabled = true;
  try {
    const healthResponse = await fetch("/health");
    if (!healthResponse.ok) throw new Error("控制面健康检查失败");
    const health = await healthResponse.json();
    if (health.auth_enabled && !state.token) {
      setConnection("offline", "需要凭据");
      if (!state.authPrompted) {
        state.authPrompted = true;
        openTokenModal();
      }
      return;
    }
    const [features, workItems, profiles] = await Promise.all([
      api("/api/features"),
      api("/api/work-items"),
      api("/api/agent-profiles"),
    ]);
    state.features = features;
    state.workItems = workItems;
    state.profiles = profiles;
    if (state.selectedFeatureId && !features.some((feature) => feature.id === state.selectedFeatureId)) {
      state.selectedFeatureId = "";
      sessionStorage.removeItem("acp_feature_id");
    }
    setConnection("online", "实时连接");
    dom.lastUpdated.textContent = `更新于 ${formatDate(new Date(), true)}`;
    renderAll();
    if (state.selectedItemId && dom.drawer.classList.contains("open")) {
      await loadDetail(state.selectedItemId, { preserveContent: true });
    }
  } catch (error) {
    setConnection("offline", "连接失败");
    if (!quiet && error.status !== 401) toast(error.message, "error");
  } finally {
    state.loading = false;
    dom.refreshButton.disabled = false;
  }
}

function openTokenModal() {
  dom.tokenInput.value = state.token;
  dom.tokenModal.showModal();
  window.setTimeout(() => dom.tokenInput.focus(), 0);
}

function openDrawer() {
  dom.drawer.classList.add("open");
  dom.drawer.setAttribute("aria-hidden", "false");
  dom.drawerBackdrop.hidden = false;
  document.body.style.overflow = "hidden";
}

function closeDrawer() {
  dom.drawer.classList.remove("open");
  dom.drawer.setAttribute("aria-hidden", "true");
  dom.drawerBackdrop.hidden = true;
  document.body.style.overflow = "";
  state.selectedItemId = null;
  state.detail = null;
}

async function selectWorkItem(itemId) {
  state.selectedItemId = itemId;
  state.activeTab = "overview";
  document.querySelectorAll(".drawer-tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.tab === "overview");
  });
  openDrawer();
  dom.drawerContent.innerHTML = '<div class="skeleton" style="height:160px"></div>';
  dom.drawerActions.innerHTML = "";
  await loadDetail(itemId);
}

async function loadDetail(itemId, { preserveContent = false } = {}) {
  try {
    const [item, events, attempts, decisions] = await Promise.all([
      api(`/api/work-items/${encodeURIComponent(itemId)}`),
      api(`/api/work-items/${encodeURIComponent(itemId)}/events`),
      api(`/api/work-items/${encodeURIComponent(itemId)}/attempts`),
      api(`/api/work-items/${encodeURIComponent(itemId)}/decisions`),
    ]);
    if (state.selectedItemId !== itemId) return;
    state.detail = { item, events, attempts, decisions };
    const workItemIndex = state.workItems.findIndex((candidate) => candidate.id === item.id);
    if (workItemIndex >= 0) state.workItems[workItemIndex] = item;
    renderDrawer();
    renderBoard();
  } catch (error) {
    if (!preserveContent) {
      dom.drawerContent.innerHTML = `<div class="empty-state"><div><strong>详情加载失败</strong>${escapeHtml(error.message)}</div></div>`;
    }
    toast(error.message, "error");
  }
}

function statusChip(status) {
  const meta = STATUS_META[status] || { label: status, color: "#929cab" };
  return `<span class="status-chip" style="--chip-color:${meta.color}">${escapeHtml(meta.label)}</span>`;
}

function renderOverview(detail) {
  const { item, decisions } = detail;
  const role = ROLE_META[item.agent_role] || { label: item.agent_role };
  const openDecisions = decisions.filter((decision) => decision.status === "open");
  const blocker = item.blocker
    ? `<section class="detail-section"><h3>当前阻塞</h3><div class="blocker-box">${escapeHtml(JSON.stringify(item.blocker, null, 2))}</div></section>`
    : "";
  const decisionCards = openDecisions.length
    ? `<section class="detail-section"><h3>等待人工决策</h3>${openDecisions.map((decision) => `
        <article class="decision-card open">
          <p>${escapeHtml(decision.question)}</p>
          <small>${decision.options.length ? `选项：${escapeHtml(decision.options.join(" / "))}` : "允许自由回复"}</small>
        </article>`).join("")}</section>`
    : "";
  return `
    <section class="detail-section">
      <h3>任务说明</h3>
      <p class="detail-description">${escapeHtml(item.description)}</p>
    </section>
    ${blocker}
    ${decisionCards}
    <section class="detail-section">
      <h3>执行配置</h3>
      <div class="detail-grid">
        <div class="detail-field"><span>Agent Role</span><strong>${escapeHtml(role.label)}</strong></div>
        <div class="detail-field"><span>Stage</span><strong>${escapeHtml(item.stage)}</strong></div>
        <div class="detail-field"><span>Priority</span><strong>P${item.priority}</strong></div>
        <div class="detail-field"><span>Version</span><strong>v${item.version}</strong></div>
        <div class="detail-field"><span>Worker</span><strong>${escapeHtml(item.claim.worker_id || "未领取")}</strong></div>
        <div class="detail-field"><span>Lease</span><strong>${escapeHtml(item.claim.expires_at ? relativeTime(item.claim.expires_at) : "—")}</strong></div>
        <div class="detail-field"><span>Base Branch</span><strong>${escapeHtml(item.repository.base_branch)}</strong></div>
        <div class="detail-field"><span>Head Branch</span><strong>${escapeHtml(item.repository.head_branch || "—")}</strong></div>
      </div>
    </section>
    <section class="detail-section">
      <h3>验收标准</h3>
      <ul class="criteria-list">${item.acceptance_criteria.map((criterion) => `<li>${escapeHtml(criterion)}</li>`).join("")}</ul>
    </section>
    <section class="detail-section">
      <h3>依赖关系</h3>
      ${item.dependencies.length ? `<ul class="dependency-list">${item.dependencies.map((dependency) => `<li class="${item.blocked_by.includes(dependency) ? "blocked" : ""}"><span>${escapeHtml(dependency)}</span><span>${item.blocked_by.includes(dependency) ? "等待" : "完成"}</span></li>`).join("")}</ul>` : '<div class="empty-column">无前置依赖</div>'}
    </section>`;
}

function eventSummary(event) {
  const transitions = event.from_status && event.to_status && event.from_status !== event.to_status
    ? `${STATUS_META[event.from_status]?.label || event.from_status} → ${STATUS_META[event.to_status]?.label || event.to_status}`
    : "";
  const actor = event.actor_id || event.actor_type;
  return [transitions, actor ? `by ${actor}` : ""].filter(Boolean).join(" · ");
}

function renderActivity(detail) {
  if (!detail.events.length) return '<div class="empty-state"><div><strong>暂无事件</strong>执行事件会显示在这里。</div></div>';
  return `<div class="timeline">${[...detail.events].reverse().map((event) => {
    const color = STATUS_META[event.to_status]?.color || "#929cab";
    return `<article class="timeline-event">
      <span class="timeline-dot" style="--event-color:${color}"></span>
      <div class="timeline-copy">
        <strong>${escapeHtml(EVENT_LABELS[event.event_type] || event.event_type)}</strong>
        <time title="${escapeHtml(formatDate(event.created_at, true))}">${escapeHtml(relativeTime(event.created_at))}</time>
        <p>${escapeHtml(eventSummary(event))}</p>
      </div>
    </article>`;
  }).join("")}</div>`;
}

function renderAttempts(detail) {
  if (!detail.attempts.length) return '<div class="empty-state"><div><strong>还没有执行记录</strong>Runner 领取任务后会创建 Attempt。</div></div>';
  return [...detail.attempts].reverse().map((attempt) => {
    const profile = attempt.profile_snapshot || {};
    return `<article class="attempt-card">
      <div class="attempt-head"><strong>Attempt #${attempt.attempt_number}</strong>${statusChip(attempt.status)}</div>
      <dl>
        <div><dt>Worker</dt><dd>${escapeHtml(attempt.worker_id)}</dd></div>
        <div><dt>Profile</dt><dd>${escapeHtml(profile.profile_name || "—")} ${profile.profile_version ? `v${profile.profile_version}` : ""}</dd></div>
        <div><dt>Thread</dt><dd title="${escapeHtml(attempt.thread_id || "")}">${escapeHtml(compactId(attempt.thread_id))}</dd></div>
        <div><dt>Started</dt><dd>${escapeHtml(formatDate(attempt.started_at, true))}</dd></div>
      </dl>
    </article>`;
  }).join("");
}

function renderArtifacts(detail) {
  const artifacts = [
    ...detail.item.input_artifacts.map((artifact) => ({ ...artifact, direction: "input" })),
    ...detail.item.output_artifacts.map((artifact) => ({ ...artifact, direction: "output" })),
  ];
  if (!artifacts.length) return '<div class="empty-state"><div><strong>还没有登记产物</strong>Agent 交付的文档、代码 revision 和报告会显示在这里。</div></div>';
  return artifacts.map((artifact) => `
    <article class="artifact-card">
      <div class="artifact-head"><strong>${artifact.direction === "output" ? "输出产物" : "输入产物"}</strong><span>${escapeHtml(artifact.media_type || "artifact")}</span></div>
      <code>${escapeHtml(artifact.path)}</code>
      <div class="card-footer"><span>revision</span><span title="${escapeHtml(artifact.revision)}">${escapeHtml(compactId(artifact.revision))}</span></div>
    </article>`).join("");
}

function renderDrawerContent() {
  if (!state.detail) return;
  const renderers = {
    overview: renderOverview,
    activity: renderActivity,
    attempts: renderAttempts,
    artifacts: renderArtifacts,
  };
  dom.drawerContent.innerHTML = renderers[state.activeTab](state.detail);
}

function renderDrawerActions(item, decisions) {
  const actions = [];
  const openDecision = decisions.find((decision) => decision.status === "open");
  if (item.status === "draft") actions.push(["ready", "标记 Ready", "button-primary"]);
  if (item.status === "stage_review") {
    actions.push(["rework", "退回返工", "button-ghost"]);
    actions.push(["approve", "批准阶段", "button-primary"]);
  }
  if (item.status === "needs_human" && openDecision) {
    actions.push(["respond", "回复决策", "button-primary"]);
  }
  if (item.status === "blocked") actions.push(["unblock", "解除阻塞", "button-primary"]);
  if (item.status === "rework") actions.push(["queue-rework", "开始返工", "button-primary"]);
  if (item.status === "retry_queued") actions.push(["maintenance", "检查重试", "button-primary"]);
  if (!["done", "cancelled"].includes(item.status)) actions.unshift(["cancel", "取消任务", "button-danger"]);
  dom.drawerActions.innerHTML = actions
    .map(([action, label, className]) => `<button class="button ${className}" data-action="${action}" type="button">${label}</button>`)
    .join("");
}

function renderDrawer() {
  if (!state.detail) return;
  const { item, events, attempts, decisions } = state.detail;
  const meta = STATUS_META[item.status] || { label: item.status, color: "#929cab" };
  dom.drawerId.textContent = item.id;
  dom.drawerTitle.textContent = item.title;
  dom.drawerStatus.textContent = meta.label;
  dom.drawerStatus.style.setProperty("--chip-color", meta.color);
  dom.eventCount.textContent = String(events.length);
  dom.attemptCount.textContent = String(attempts.length);
  dom.artifactCount.textContent = String(item.input_artifacts.length + item.output_artifacts.length);
  renderDrawerContent();
  renderDrawerActions(item, decisions);
}

const ACTION_CONFIG = {
  approve: {
    eyebrow: "Stage gate",
    title: "批准阶段交付",
    description: "工作项将进入 Done，并解锁依赖它的后续任务。",
    confirm: "批准并完成",
    fields: '<label class="field"><span>审批说明（可选）</span><textarea name="comment" placeholder="记录本次审批依据"></textarea></label>',
  },
  rework: {
    eyebrow: "Stage gate",
    title: "退回并安排返工",
    description: "将记录退回原因，并把工作项重新放回 Ready 队列。",
    confirm: "确认退回",
    fields: '<label class="field"><span>返工原因</span><textarea name="reason" required placeholder="说明必须修正的问题"></textarea></label>',
  },
  cancel: {
    eyebrow: "Admin action",
    title: "取消工作项",
    description: "取消后不会再被 Runner 调度，已有 Claim 会立即撤销。",
    confirm: "确认取消",
    fields: '<label class="field"><span>取消原因</span><textarea name="reason" required placeholder="说明取消原因，便于审计"></textarea></label>',
  },
  ready: {
    eyebrow: "Workflow",
    title: "将工作项标记为 Ready",
    description: "依赖满足后，Runner 可以领取并执行这个工作项。",
    confirm: "标记 Ready",
    fields: "",
  },
  unblock: {
    eyebrow: "Workflow",
    title: "解除工作项阻塞",
    description: "工作项会返回 Ready 队列，等待 Runner 重新领取。",
    confirm: "解除阻塞",
    fields: '<label class="field"><span>解决说明</span><textarea name="resolution" required placeholder="说明阻塞条件如何被解决"></textarea></label>',
  },
  "queue-rework": {
    eyebrow: "Workflow",
    title: "开始返工",
    description: "工作项会返回 Ready，并优先恢复此前的 Codex Thread。",
    confirm: "加入执行队列",
    fields: '<label class="field"><span>返工范围</span><textarea name="scope" required placeholder="说明本轮返工边界"></textarea></label>',
  },
  maintenance: {
    eyebrow: "Maintenance",
    title: "检查重试队列",
    description: "执行一次维护 Tick；已到期的重试会回到 Ready，未到期任务保持不变。",
    confirm: "立即检查",
    fields: "",
  },
};

function openActionModal(action) {
  if (!state.detail) return;
  const config = ACTION_CONFIG[action];
  const openDecision = state.detail.decisions.find((decision) => decision.status === "open");
  if (action === "respond" && !openDecision) {
    toast("没有待回复的人工决策", "error");
    return;
  }
  state.pendingAction = { action, decision: openDecision };
  dom.actionError.hidden = true;
  dom.actionError.textContent = "";
  if (action === "respond") {
    dom.actionEyebrow.textContent = "Human decision";
    dom.actionTitle.textContent = "回复人工决策";
    dom.actionDescription.textContent = openDecision.question;
    dom.actionConfirm.textContent = "提交回复";
    const options = openDecision.options.map((option, index) => `
      <label class="option-choice"><input type="radio" name="response-option" value="${escapeHtml(option)}" ${index === 0 ? "checked" : ""} />${escapeHtml(option)}</label>`).join("");
    dom.actionFields.innerHTML = `${options ? `<div class="option-grid">${options}</div>` : ""}<label class="field"><span>${options ? "补充或自定义回复" : "回复内容"}</span><textarea name="custom-response" ${options ? "" : "required"} placeholder="输入回复"></textarea></label>`;
  } else {
    dom.actionEyebrow.textContent = config.eyebrow;
    dom.actionTitle.textContent = config.title;
    dom.actionDescription.textContent = config.description;
    dom.actionConfirm.textContent = config.confirm;
    dom.actionFields.innerHTML = config.fields;
  }
  dom.actionModal.showModal();
  window.setTimeout(() => dom.actionFields.querySelector("textarea, input")?.focus(), 0);
}

async function transition(itemId, body) {
  return api(`/api/work-items/${encodeURIComponent(itemId)}/status`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

async function executeAction(formData) {
  const { action, decision } = state.pendingAction;
  const item = state.detail.item;
  if (action === "approve") {
    await transition(item.id, {
      to_status: "done",
      event: "stage_approved",
      actor_type: "human",
      actor_id: "control-plane-ui",
      payload: { comment: formData.get("comment") || "Approved in Control Plane UI" },
    });
  } else if (action === "rework") {
    const reason = String(formData.get("reason") || "").trim();
    if (!reason) throw new Error("请填写返工原因");
    await transition(item.id, {
      to_status: "rework", event: "stage_rejected", actor_type: "human", actor_id: "control-plane-ui",
      payload: { rework_reason: reason },
    });
    await transition(item.id, {
      to_status: "ready", event: "rework_queued", actor_type: "control_plane", actor_id: "control-plane-ui",
      payload: { rework_scope: reason },
    });
  } else if (action === "cancel") {
    const reason = String(formData.get("reason") || "").trim();
    if (!reason) throw new Error("请填写取消原因");
    await transition(item.id, {
      to_status: "cancelled", event: "work_item_cancelled", actor_type: "admin", actor_id: "control-plane-ui",
      payload: { reason },
    });
  } else if (action === "ready") {
    await transition(item.id, {
      to_status: "ready", event: "work_item_readied", actor_type: "control_plane", actor_id: "control-plane-ui",
    });
  } else if (action === "unblock") {
    const resolution = String(formData.get("resolution") || "").trim();
    if (!resolution) throw new Error("请填写解决说明");
    await transition(item.id, {
      to_status: "ready", event: "blocker_resolved", actor_type: "control_plane", actor_id: "control-plane-ui",
      payload: { resolution },
    });
  } else if (action === "queue-rework") {
    const scope = String(formData.get("scope") || "").trim();
    if (!scope) throw new Error("请填写返工范围");
    await transition(item.id, {
      to_status: "ready", event: "rework_queued", actor_type: "control_plane", actor_id: "control-plane-ui",
      payload: { rework_scope: scope },
    });
  } else if (action === "maintenance") {
    const result = await api("/api/maintenance/tick", { method: "POST" });
    toast(`维护完成：${result.readied} 个任务已就绪`);
  } else if (action === "respond") {
    const custom = String(formData.get("custom-response") || "").trim();
    const selected = String(formData.get("response-option") || "").trim();
    const response = custom || selected;
    if (!response) throw new Error("请填写或选择回复");
    await api(`/api/work-items/${encodeURIComponent(item.id)}/decisions`, {
      method: "POST",
      body: JSON.stringify({
        action: "resolve",
        decision_id: decision.id,
        response,
        actor_id: "control-plane-ui",
      }),
    });
  }
}

dom.tokenButton.addEventListener("click", openTokenModal);
dom.refreshButton.addEventListener("click", () => refresh());
dom.featureSearch.addEventListener("input", renderFeatures);
dom.workItemSearch.addEventListener("input", renderBoard);
dom.roleFilter.addEventListener("change", renderBoard);

dom.featureList.addEventListener("click", (event) => {
  const button = event.target.closest("[data-feature-id]");
  if (!button) return;
  state.selectedFeatureId = button.dataset.featureId;
  if (state.selectedFeatureId) sessionStorage.setItem("acp_feature_id", state.selectedFeatureId);
  else sessionStorage.removeItem("acp_feature_id");
  renderAll();
});

dom.board.addEventListener("click", (event) => {
  const card = event.target.closest("[data-item-id]");
  if (card) selectWorkItem(card.dataset.itemId);
});

dom.drawerClose.addEventListener("click", closeDrawer);
dom.drawerBackdrop.addEventListener("click", closeDrawer);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && dom.drawer.classList.contains("open") && !dom.actionModal.open) closeDrawer();
});

document.querySelectorAll(".drawer-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    state.activeTab = tab.dataset.tab;
    document.querySelectorAll(".drawer-tab").forEach((candidate) => candidate.classList.toggle("active", candidate === tab));
    renderDrawerContent();
  });
});

dom.drawerActions.addEventListener("click", (event) => {
  const button = event.target.closest("[data-action]");
  if (button) openActionModal(button.dataset.action);
});

dom.tokenForm.addEventListener("submit", (event) => {
  event.preventDefault();
  if (event.submitter?.value === "cancel") {
    dom.tokenModal.close();
    return;
  }
  state.token = dom.tokenInput.value.trim();
  if (state.token) sessionStorage.setItem("acp_api_token", state.token);
  else sessionStorage.removeItem("acp_api_token");
  state.authPrompted = false;
  dom.tokenModal.close();
  refresh();
});

dom.actionForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (event.submitter?.value === "cancel") {
    dom.actionModal.close();
    state.pendingAction = null;
    return;
  }
  dom.actionConfirm.disabled = true;
  dom.actionError.hidden = true;
  try {
    await executeAction(new FormData(dom.actionForm));
    const actionTitle = dom.actionTitle.textContent;
    dom.actionModal.close();
    state.pendingAction = null;
    toast(`${actionTitle}已完成`);
    await refresh();
  } catch (error) {
    dom.actionError.textContent = error.message;
    dom.actionError.hidden = false;
  } finally {
    dom.actionConfirm.disabled = false;
  }
});

window.setInterval(() => {
  if (!document.hidden && !dom.actionModal.open && !dom.tokenModal.open) refresh({ quiet: true });
}, 5000);

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh({ quiet: true });
});

refresh();
