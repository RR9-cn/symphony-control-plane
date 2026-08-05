const state = { token: sessionStorage.getItem("acp_api_token") || "", projects: [], projectId: localStorage.getItem("acp_project_id") || "", issues: [], runtimes: [], workers: [], runner: null, selected: null, decisionDrafts: new Map() };
const $ = (selector) => document.querySelector(selector);
const dom = {
  connection: $("#connection"), refresh: $("#refresh-button"), tokenButton: $("#token-button"), newIssue: $("#new-issue-button"), newProject: $("#new-project-button"), projectFilter: $("#project-filter"), projectList: $("#project-list"), projectCount: $("#project-count"),
  runnerState: $("#runner-state"), runnerDetail: $("#runner-detail"), runnerButton: $("#runner-button"),
  total: $("#metric-total"), running: $("#metric-running"), human: $("#metric-human"), done: $("#metric-done"),
  runtimeCount: $("#runtime-count"), runtimeList: $("#runtime-list"), issueList: $("#issue-list"), search: $("#issue-search"),
  issueModal: $("#issue-modal"), issueForm: $("#issue-form"), issueError: $("#issue-error"),
  projectModal: $("#project-modal"), projectForm: $("#project-form"), projectError: $("#project-error"),
  detailModal: $("#detail-modal"), detailId: $("#detail-id"), detailTitle: $("#detail-title"), detailContent: $("#detail-content"), detailActions: $("#detail-actions"), detailError: $("#detail-error"),
  tokenModal: $("#token-modal"), tokenForm: $("#token-form"), tokenInput: $("#token-input"), toast: $("#toast"),
};

const STATUS = {
  ready: "Ready", running: "Running", retry_queued: "Retry queued", needs_human: "Needs human", blocked: "Blocked",
  reviewing: "Final review", awaiting_publish: "Awaiting publish", pr_open: "PR / MR open", done: "Done", cancelled: "Cancelled",
};
const PHASE = {
  claimed: "已领取",
  preparing_workspace: "准备 Workspace",
  before_run_hook: "执行 Before Run Hook",
  building_prompt: "构建 Prompt",
  launching_agent: "启动 Codex",
  initializing_session: "初始化 Session",
  session_ready: "Session 已就绪",
  streaming_turn: "Turn 执行中",
  refreshing_issue: "刷新 Issue 状态",
  turn_failed: "Turn 失败",
  snapshot_unavailable: "实时快照不可用",
};
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
const short = (value, size = 12) => value ? `${String(value).slice(0, size)}${String(value).length > size ? "…" : ""}` : "—";
const formatDate = (value) => value ? new Date(value).toLocaleString() : "—";
function formatDuration(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value)) return "—";
  if (value < 1) return `${Math.round(value * 1000)}ms`;
  if (value < 60) return `${value.toFixed(1)}s`;
  const minutes = Math.floor(value / 60); const remainder = Math.round(value % 60);
  return `${minutes}m ${remainder}s`;
}

function renderAttempt(attempt, index, issue) {
  const phase = attempt.turn_count > 0 ? `Turn ${attempt.turn_count}` : attempt.thread_id ? "Session 已创建，尚未进入 Turn" : "尚未创建 Codex Session / Turn";
  const retry = index === 0 && attempt.status === "retry_queued" && issue.retry_at
    ? `<div class="attempt-retry"><span>下次重试</span><strong>${escapeHtml(formatDate(issue.retry_at))}</strong></div>`
    : "";
  const reason = attempt.status_reason
    ? `<div class="attempt-reason"><span>状态原因</span><code>${escapeHtml(attempt.status_reason)}</code></div>`
    : "";
  return `<article class="attempt"><header><strong>Attempt #${attempt.attempt_number}</strong>${badge(attempt.status)}</header>
    <div class="meta attempt-meta"><span>${escapeHtml(phase)}</span><span>Worker ${escapeHtml(attempt.worker_id)}</span><span>开始 ${escapeHtml(formatDate(attempt.started_at))}</span><span>耗时 ${escapeHtml(formatDuration(attempt.duration_seconds))}</span></div>
    <div class="attempt-session"><span>Session</span><code title="${escapeHtml(attempt.session_id || "")}">${escapeHtml(short(attempt.session_id, 28))}</code></div>
    ${reason}${retry}</article>`;
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body) headers.set("Content-Type", "application/json");
  if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try { message = (await response.json()).error?.message || message; } catch (_) { /* noop */ }
    const error = new Error(message); error.status = response.status; throw error;
  }
  return response.status === 204 ? null : response.json();
}

function toast(message, error = false) {
  dom.toast.textContent = message; dom.toast.style.background = error ? "#321519" : "#17241b"; dom.toast.hidden = false;
  window.setTimeout(() => { dom.toast.hidden = true; }, 3500);
}

function badge(status) { return `<span class="badge ${escapeHtml(status)}">${escapeHtml(STATUS[status] || status)}</span>`; }

async function refresh() {
  dom.refresh.disabled = true;
  try {
    const [health, projects, issues, runtimes, workers, runner] = await Promise.all([
      api("/health"), api("/api/projects"), api("/api/issues"), api("/api/agent-runtimes"), api("/api/workers"), api("/api/runner-control"),
    ]);
    state.projects = projects;
    if (!projects.some((project) => project.id === state.projectId)) state.projectId = projects[0]?.id || "";
    state.issues = issues; state.runtimes = runtimes; state.workers = workers; state.runner = runner;
    dom.connection.textContent = health.auth_enabled && !state.token ? "需要认证" : "已连接";
    render();
    // Replacing detailContent while a decision editor is focused interrupts IME
    // input and clears the browser selection. The surrounding dashboard can keep
    // polling; refresh this detail after the user leaves the editor.
    const editingDecision = document.activeElement?.matches?.("[data-decision-response]");
    if (state.selected && !editingDecision) await openDetail(state.selected, false);
  } catch (error) {
    dom.connection.textContent = error.status === 401 ? "需要认证" : "连接失败";
    if (error.status === 401) openToken(); else toast(error.message, true);
  } finally { dom.refresh.disabled = false; }
}

function render() {
  const options = state.projects.map((project) => `<option value="${escapeHtml(project.id)}" ${project.id === state.projectId ? "selected" : ""}>${escapeHtml(project.name)} · ${escapeHtml(project.status)}</option>`).join("");
  dom.projectFilter.innerHTML = options || '<option value="">请先新增项目</option>';
  dom.issueForm.elements.project_id.innerHTML = options || '<option value="">请先新增项目</option>';
  dom.total.textContent = state.issues.length;
  dom.running.textContent = state.issues.filter((issue) => issue.status === "running").length;
  dom.human.textContent = state.issues.filter((issue) => ["needs_human", "reviewing", "awaiting_publish", "pr_open"].includes(issue.status)).length;
  dom.done.textContent = state.issues.filter((issue) => issue.status === "done").length;
  renderProjects(); renderRunner(); renderRuntimes(); renderIssues();
}

function renderProjects() {
  dom.projectCount.textContent = state.projects.length;
  dom.projectList.innerHTML = state.projects.length ? state.projects.map((project) => { const snapshot = project.current_snapshot; const assets = snapshot?.parsed_config?.project_assets || {}; return `<article class="issue-card" data-project-id="${escapeHtml(project.id)}"><header><strong>${escapeHtml(project.name)} · ${escapeHtml(project.key)}</strong><span class="badge ${project.status === "available" ? "done" : "blocked"}">${escapeHtml(project.status)}</span></header><p>${escapeHtml(project.repository_path)}</p><div class="meta"><span>${escapeHtml(project.default_branch)}</span><span>HEAD ${escapeHtml(short(snapshot?.source_commit))}</span><span>Workflow ${escapeHtml(short(snapshot?.workflow_revision, 8))}</span><span>${assets.skills?.length || 0} Skills</span></div>${project.validation_error ? `<small>${escapeHtml(project.validation_error)}</small>` : ""}<div class="modal-actions"><button class="button ghost" data-project-validate="${escapeHtml(project.id)}">重新校验</button><button class="button ghost" data-project-delete="${escapeHtml(project.id)}" data-project-name="${escapeHtml(project.name)}">删除项目</button></div></article>`; }).join("") : '<div class="empty">尚未登记项目。新增本机 Git 仓库；缺少 WORKFLOW.md 时系统会生成默认模板。</div>';
}

function renderRunner() {
  const runner = state.runner?.runtimes?.find((runtime) => runtime.project_id === state.projectId) || { state: "stopped" };
  dom.runnerState.textContent = runner.state;
  const project = state.projects.find((item) => item.id === state.projectId);
  dom.runnerDetail.textContent = runner.process_id ? `${project?.name || "项目"} · PID ${runner.process_id}` : project ? `${project.name} · 未运行` : "请先新增项目";
  dom.runnerButton.textContent = ["running", "starting"].includes(runner.state) ? "停止" : "启动";
  dom.runnerButton.dataset.action = ["running", "starting"].includes(runner.state) ? "stop" : "start";
  dom.runnerButton.disabled = !state.projectId || project?.status !== "available";
}

function renderRuntimes() {
  const issueIds = new Set(state.issues.filter((issue) => !state.projectId || issue.project_id === state.projectId).map((issue) => issue.id));
  const live = state.runtimes.filter((runtime) => issueIds.has(runtime.issue_id) && !["done", "cancelled"].includes(runtime.state));
  dom.runtimeCount.textContent = live.length;
  dom.runtimeList.innerHTML = live.length ? live.map((runtime) => {
    const authoritative = runtime.runtime_source === "orchestrator";
    const phase = PHASE[runtime.phase] || runtime.phase || (authoritative ? "运行中" : "非运行状态");
    return `<article class="runtime-card" data-issue-id="${escapeHtml(runtime.issue_id)}">
      <header><strong>${escapeHtml(runtime.issue_id)}</strong>${badge(runtime.state === "waiting_human" ? "needs_human" : runtime.state)}</header>
      <p>${escapeHtml(runtime.title)}</p>
      <div class="runtime-source ${authoritative ? "live" : "stored"}"><i></i><span>${authoritative ? "ORCHESTRATOR LIVE" : "DATABASE STATE"}</span><strong>${escapeHtml(phase)}</strong></div>
      <div class="meta"><span>Attempt ${runtime.attempt_number || "—"}</span><span>Turn #${runtime.turn_count || 0}</span>${runtime.codex_app_server_pid ? `<span>Codex PID ${runtime.codex_app_server_pid}</span>` : ""}</div>
      <div class="attempt-session"><span>Codex Session</span><code title="${escapeHtml(runtime.session_id || "")}">${escapeHtml(short(runtime.session_id, 28))}</code></div>
      <div class="meta"><span>Thread ${escapeHtml(short(runtime.thread_id, 14))}</span><span>Current Turn ${escapeHtml(short(runtime.turn_id, 14))}</span></div>
      ${authoritative ? `<div class="runtime-activity"><span>${escapeHtml(runtime.last_message || runtime.last_event || "等待 Agent 事件")}</span><small>${escapeHtml(formatDuration(runtime.duration_seconds))} · 快照 ${escapeHtml(formatDate(runtime.snapshot_at))}</small></div>` : ""}
    </article>`;
  }).join("") : '<div class="empty">暂无活跃 Agent</div>';
}

function renderIssues() {
  const query = dom.search.value.trim().toLowerCase();
  const issues = state.issues.filter((issue) => (!state.projectId || issue.project_id === state.projectId) && (!query || issue.id.toLowerCase().includes(query) || issue.title.toLowerCase().includes(query)));
  dom.issueList.innerHTML = issues.length ? issues.map((issue) => `<article class="issue-card" data-issue-id="${escapeHtml(issue.id)}"><header><strong>${escapeHtml(issue.id)} · P${issue.priority}</strong>${badge(issue.status)}</header><p>${escapeHtml(issue.title)}</p><div class="meta"><span>${escapeHtml(short(issue.source_commit))}</span><span>Workflow ${escapeHtml(short(issue.workflow_revision, 8))}</span><span>v${issue.version}</span></div></article>`).join("") : '<div class="empty">还没有 Issue</div>';
}

function suggestedId() { return `ISSUE-${String(Date.now()).slice(-10)}`; }
function openNewIssue() { if (!state.projects.length) { dom.projectModal.showModal(); return; } dom.issueForm.reset(); dom.issueForm.elements.id.value = suggestedId(); dom.issueForm.elements.project_id.value = state.projectId; dom.issueError.hidden = true; dom.issueModal.showModal(); }
function openToken() { dom.tokenInput.value = state.token; if (!dom.tokenModal.open) dom.tokenModal.showModal(); }

function issuePayload() {
  const data = new FormData(dom.issueForm);
  const labels = String(data.get("labels") || "").split(",").map((value) => value.trim().toLowerCase()).filter(Boolean);
  const blockedBy = String(data.get("blocked_by") || "").split(/\r?\n/).map((line) => line.trim()).filter(Boolean).map((line) => { const [identifier, blockerState] = line.split("|", 2).map((value) => value.trim()); return { identifier, state: blockerState || null }; });
  return {
    id: String(data.get("id") || "").trim().toUpperCase(), title: String(data.get("title") || "").trim(), description: String(data.get("description") || "").trim(),
    priority: Number(data.get("priority") || 2),
    project_id: String(data.get("project_id") || ""), labels, blocked_by: blockedBy, dispatchable: data.get("dispatchable") === "true",
    acceptance_criteria: String(data.get("acceptance_criteria") || "").split(/\r?\n/).map((line) => line.trim()).filter(Boolean),
  };
}

async function openDetail(issueId, show = true) {
  state.selected = issueId;
  try {
    const [issue, attempts, events, decisions] = await Promise.all([
      api(`/api/issues/${encodeURIComponent(issueId)}`), api(`/api/issues/${encodeURIComponent(issueId)}/attempts`),
      api(`/api/issues/${encodeURIComponent(issueId)}/events`), api(`/api/issues/${encodeURIComponent(issueId)}/decisions`),
    ]);
    dom.detailId.textContent = issue.id; dom.detailTitle.textContent = issue.title;
    const latest = attempts[0];
    const runtime = state.runtimes.find((row) => row.issue_id === issue.id);
    dom.detailContent.innerHTML = `<p class="modal-copy">${escapeHtml(issue.description)}</p><div class="detail-grid">
      <div class="detail-field"><span>Status</span><strong>${escapeHtml(STATUS[issue.status] || issue.status)}</strong></div><div class="detail-field"><span>Priority</span><strong>P${issue.priority}</strong></div>
      <div class="detail-field"><span>Project</span><strong>${escapeHtml(state.projects.find((project) => project.id === issue.project_id)?.name || issue.project_id)}</strong></div><div class="detail-field"><span>Starting commit</span><strong>${escapeHtml(short(issue.source_commit, 16))}</strong></div>
      <div class="detail-field"><span>Workflow revision</span><strong>${escapeHtml(short(issue.workflow_revision, 16))}</strong></div><div class="detail-field"><span>Workspace</span><strong>${escapeHtml(issue.workspace_path)}</strong></div>
      <div class="detail-field"><span>Worker</span><strong>${escapeHtml(issue.claim.worker_id || "—")}</strong></div><div class="detail-field"><span>Codex Session</span><strong title="${escapeHtml(runtime?.session_id || latest?.session_id || "")}">${escapeHtml(short(runtime?.session_id || latest?.session_id, 28))}</strong></div>
      <div class="detail-field"><span>Thread</span><strong>${escapeHtml(short(runtime?.thread_id || latest?.thread_id, 20))}</strong></div><div class="detail-field"><span>Current Turn</span><strong>${escapeHtml(runtime?.turn_id ? `${short(runtime.turn_id, 20)} (#${runtime.turn_count})` : latest?.turn_id ? `${short(latest.turn_id, 20)} (#${latest.turn_count})` : "—")}</strong></div>
      <div class="detail-field"><span>Runtime Source</span><strong>${escapeHtml(runtime?.runtime_source === "orchestrator" ? "Orchestrator Live" : "Database")}</strong></div><div class="detail-field"><span>Agent Phase</span><strong>${escapeHtml(PHASE[runtime?.phase] || runtime?.phase || "—")}</strong></div>
      <div class="detail-field"><span>Last Agent Event</span><strong>${escapeHtml(runtime?.last_event || "—")}</strong></div><div class="detail-field"><span>Codex PID / Duration</span><strong>${escapeHtml(runtime?.codex_app_server_pid || "—")} / ${escapeHtml(formatDuration(runtime?.duration_seconds))}</strong></div>
      <div class="detail-field"><span>Dispatchable</span><strong>${issue.dispatchable ? "Yes" : "No"}</strong></div><div class="detail-field"><span>Labels</span><strong>${escapeHtml(issue.labels.join(", ") || "—")}</strong></div>
    </div>
    <section class="section"><h3>验收标准</h3><ul>${issue.acceptance_criteria.map((value) => `<li>${escapeHtml(value)}</li>`).join("")}</ul></section>
    ${issue.pull_request ? `<section class="section"><h3>PR / Merge Request</h3><p><a href="${escapeHtml(issue.pull_request)}" target="_blank" rel="noreferrer">${escapeHtml(issue.pull_request)}</a></p></section>` : ""}
    ${issue.blocker ? `<section class="section"><h3>Blocker</h3><pre>${escapeHtml(JSON.stringify(issue.blocker, null, 2))}</pre></section>` : ""}
    ${issue.blocked_by.length ? `<section class="section"><h3>Blocked By</h3><pre>${escapeHtml(JSON.stringify(issue.blocked_by, null, 2))}</pre></section>` : ""}
    ${renderDecisions(decisions)}
    <section class="section"><h3>Attempts</h3>${attempts.length ? attempts.map((attempt, index) => renderAttempt(attempt, index, issue)).join("") : '<p class="modal-copy">尚未执行</p>'}</section>
    <section class="section"><h3>事件</h3><div class="timeline">${events.map((event) => `<article><strong>${escapeHtml(event.event)}</strong><small> · ${escapeHtml(formatDate(event.created_at))}</small><div class="meta"><span>${escapeHtml(event.from_status || "—")} → ${escapeHtml(event.to_status || "—")}</span><span>${escapeHtml(event.actor_type)}</span></div>${event.payload?.reason ? `<p class="event-reason">${escapeHtml(event.payload.reason)}</p>` : ""}</article>`).join("")}</div></section>`;
    renderActions(issue, decisions);
    dom.detailError.hidden = true;
    if (show && !dom.detailModal.open) dom.detailModal.showModal();
  } catch (error) { if (show) toast(error.message, true); }
}

function renderDecisions(decisions) {
  const open = decisions.filter((decision) => decision.status === "open");
  if (!open.length) return "";
  return `<section class="section"><h3>等待人工输入</h3>${open.map((decision) => `<article class="decision"><strong>${escapeHtml(decision.question)}</strong>${decision.options.length ? `<p>${decision.options.map(escapeHtml).join(" / ")}</p>` : ""}<textarea data-decision-response="${escapeHtml(decision.id)}" placeholder="填写决定">${escapeHtml(state.decisionDrafts.get(decision.id) || "")}</textarea><button class="button secondary" data-resolve="${escapeHtml(decision.id)}">提交并恢复 Agent</button></article>`).join("")}</section>`;
}

function renderActions(issue) {
  const buttons = [];
  if (issue.status === "reviewing") buttons.push(["approve_result", "验收结果并生成 Commit"]);
  if (issue.status === "awaiting_publish") buttons.push(["authorize_publish", "授权 Push / 创建 PR 或 MR"]);
  if (issue.status === "pr_open") buttons.push(["confirm_merge", "核验 PR / MR 已合并"]);
  if (issue.status === "blocked") buttons.push(["retry_requested", "解除阻塞并重试"]);
  if (["ready", "running", "retry_queued", "needs_human", "blocked", "reviewing"].includes(issue.status)) buttons.push(["cancelled", issue.status === "running" ? "停止并取消 Issue" : "取消 Issue"]);
  dom.detailActions.innerHTML = buttons.map(([action, label]) => `<button class="button ${action === "cancelled" ? "ghost" : ""}" data-action="${action}" data-version="${issue.version}">${label}</button>`).join("");
}

async function runAction(action, version) {
  const issueId = state.selected; if (!issueId) return;
  try {
    if (["approve_result", "authorize_publish", "confirm_merge"].includes(action)) {
      if (!window.confirm("这是显式交付门禁，确认继续？")) return;
      await api(`/api/issues/${encodeURIComponent(issueId)}/delivery`, { method: "POST", body: JSON.stringify({ action, expected_version: Number(version), authorization: true }) });
    } else {
      const issue = state.issues.find((row) => row.id === issueId);
      const toStatus = action === "retry_requested" ? "ready" : "cancelled";
      await api(`/api/issues/${encodeURIComponent(issueId)}/status`, { method: "POST", body: JSON.stringify({ to_status: toStatus, event: action, actor_type: "human", actor_id: "control-plane-ui", payload: {} }) });
    }
    toast("操作完成"); await refresh();
  } catch (error) { dom.detailError.textContent = error.message; dom.detailError.hidden = false; }
}

dom.newIssue.addEventListener("click", openNewIssue); dom.newProject.addEventListener("click", () => { dom.projectForm.reset(); dom.projectError.hidden = true; dom.projectModal.showModal(); }); dom.tokenButton.addEventListener("click", openToken); dom.refresh.addEventListener("click", refresh);
dom.projectFilter.addEventListener("change", () => { state.projectId = dom.projectFilter.value; localStorage.setItem("acp_project_id", state.projectId); render(); });
dom.projectList.addEventListener("click", async (event) => { const remove = event.target.closest("[data-project-delete]"); if (remove) { if (!window.confirm(`确认删除项目“${remove.dataset.projectName}”？项目已有 Issue 时系统会拒绝删除。`)) return; try { await api(`/api/projects/${encodeURIComponent(remove.dataset.projectDelete)}`, { method: "DELETE" }); if (state.projectId === remove.dataset.projectDelete) { state.projectId = ""; localStorage.removeItem("acp_project_id"); } toast("项目已删除"); await refresh(); } catch (error) { toast(error.message, true); } return; } const validate = event.target.closest("[data-project-validate]"); if (validate) { try { const project = await api(`/api/projects/${encodeURIComponent(validate.dataset.projectValidate)}/validate`, { method: "POST", body: "{}" }); toast(project.status === "available" ? "Workflow 校验通过" : project.validation_error, project.status !== "available"); await refresh(); } catch (error) { toast(error.message, true); } return; } const card = event.target.closest("[data-project-id]"); if (!card) return; state.projectId = card.dataset.projectId; localStorage.setItem("acp_project_id", state.projectId); render(); });
dom.search.addEventListener("input", renderIssues);
dom.runnerButton.addEventListener("click", async () => { try { const action = dom.runnerButton.dataset.action; await api(`/api/projects/${encodeURIComponent(state.projectId)}/runtime/${action}`, { method: "POST", body: "{}" }); toast(action === "start" ? "项目 Runtime 已启动" : "项目 Runtime 已停止"); await refresh(); } catch (error) { toast(error.message, true); } });
dom.issueForm.addEventListener("submit", async (event) => { event.preventDefault(); if (event.submitter?.value === "cancel") { dom.issueModal.close(); return; } try { const issue = await api("/api/issues", { method: "POST", body: JSON.stringify(issuePayload()) }); dom.issueModal.close(); toast(`已创建 ${issue.id}`); await refresh(); } catch (error) { dom.issueError.textContent = error.message; dom.issueError.hidden = false; } });
dom.projectForm.addEventListener("submit", async (event) => { event.preventDefault(); if (event.submitter?.value === "cancel") { dom.projectModal.close(); return; } const data = new FormData(dom.projectForm); try { const project = await api("/api/projects", { method: "POST", body: JSON.stringify({ key: String(data.get("key") || "").trim(), name: String(data.get("name") || "").trim(), repository_path: String(data.get("repository_path") || "").trim(), default_branch: String(data.get("default_branch") || "").trim(), workflow_path: String(data.get("workflow_path") || "").trim(), enabled: true, bootstrap_workflow: true }) }); dom.projectModal.close(); state.projectId = project.id; localStorage.setItem("acp_project_id", project.id); toast(project.status === "available" ? "项目已登记，可启动 Runtime" : project.validation_error || "项目已登记，提交生成的 WORKFLOW.md 后重新校验", project.status !== "available"); await refresh(); } catch (error) { dom.projectError.textContent = error.message; dom.projectError.hidden = false; } });
dom.tokenForm.addEventListener("submit", (event) => { event.preventDefault(); if (event.submitter?.value === "cancel") { dom.tokenModal.close(); return; } state.token = dom.tokenInput.value.trim(); if (state.token) sessionStorage.setItem("acp_api_token", state.token); else sessionStorage.removeItem("acp_api_token"); dom.tokenModal.close(); refresh(); });
$("#issue-close-button").addEventListener("click", () => dom.issueModal.close());
$("#issue-cancel-button").addEventListener("click", () => dom.issueModal.close());
$("#token-cancel-button").addEventListener("click", () => dom.tokenModal.close());
dom.issueList.addEventListener("click", (event) => { const card = event.target.closest("[data-issue-id]"); if (card) openDetail(card.dataset.issueId); });
dom.runtimeList.addEventListener("click", (event) => { const card = event.target.closest("[data-issue-id]"); if (card) openDetail(card.dataset.issueId); });
dom.detailActions.addEventListener("click", (event) => { const button = event.target.closest("[data-action]"); if (button) runAction(button.dataset.action, button.dataset.version); });
dom.detailContent.addEventListener("input", (event) => { const editor = event.target.closest("[data-decision-response]"); if (editor) state.decisionDrafts.set(editor.dataset.decisionResponse, editor.value); });
dom.detailContent.addEventListener("click", async (event) => { const button = event.target.closest("[data-resolve]"); if (!button) return; const response = dom.detailContent.querySelector(`[data-decision-response="${CSS.escape(button.dataset.resolve)}"]`).value.trim(); if (!response) return; try { await api(`/api/issues/${encodeURIComponent(state.selected)}/decisions`, { method: "POST", body: JSON.stringify({ action: "resolve", decision_id: button.dataset.resolve, response, actor_id: "control-plane-ui" }) }); state.decisionDrafts.delete(button.dataset.resolve); toast("决定已提交，Issue 已恢复 Ready"); await refresh(); } catch (error) { toast(error.message, true); } });
$("#detail-close").addEventListener("click", () => { state.selected = null; dom.detailModal.close(); });
window.setInterval(() => { if (!document.hidden && !dom.issueModal.open && !dom.projectModal.open && !dom.tokenModal.open) refresh(); }, 5000);
refresh();
