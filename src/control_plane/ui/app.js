const state = { token: sessionStorage.getItem("acp_api_token") || "", issues: [], runtimes: [], workers: [], runner: null, selected: null };
const $ = (selector) => document.querySelector(selector);
const dom = {
  connection: $("#connection"), refresh: $("#refresh-button"), tokenButton: $("#token-button"), newIssue: $("#new-issue-button"),
  runnerState: $("#runner-state"), runnerDetail: $("#runner-detail"), runnerButton: $("#runner-button"),
  total: $("#metric-total"), running: $("#metric-running"), human: $("#metric-human"), done: $("#metric-done"),
  runtimeCount: $("#runtime-count"), runtimeList: $("#runtime-list"), issueList: $("#issue-list"), search: $("#issue-search"),
  issueModal: $("#issue-modal"), issueForm: $("#issue-form"), issueError: $("#issue-error"), resolveHead: $("#resolve-head"),
  detailModal: $("#detail-modal"), detailId: $("#detail-id"), detailTitle: $("#detail-title"), detailContent: $("#detail-content"), detailActions: $("#detail-actions"), detailError: $("#detail-error"),
  tokenModal: $("#token-modal"), tokenForm: $("#token-form"), tokenInput: $("#token-input"), toast: $("#toast"),
};

const STATUS = {
  ready: "Ready", running: "Running", retry_queued: "Retry queued", needs_human: "Needs human", blocked: "Blocked",
  reviewing: "Final review", awaiting_publish: "Awaiting publish", pr_open: "PR / MR open", done: "Done", cancelled: "Cancelled",
};
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
const short = (value, size = 12) => value ? `${String(value).slice(0, size)}${String(value).length > size ? "…" : ""}` : "—";

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
    const [health, issues, runtimes, workers, runner] = await Promise.all([
      api("/health"), api("/api/issues"), api("/api/agent-runtimes"), api("/api/workers"), api("/api/runner-control"),
    ]);
    state.issues = issues; state.runtimes = runtimes; state.workers = workers; state.runner = runner;
    dom.connection.textContent = health.auth_enabled && !state.token ? "需要认证" : "已连接";
    render();
    if (state.selected) await openDetail(state.selected, false);
  } catch (error) {
    dom.connection.textContent = error.status === 401 ? "需要认证" : "连接失败";
    if (error.status === 401) openToken(); else toast(error.message, true);
  } finally { dom.refresh.disabled = false; }
}

function render() {
  dom.total.textContent = state.issues.length;
  dom.running.textContent = state.issues.filter((issue) => issue.status === "running").length;
  dom.human.textContent = state.issues.filter((issue) => ["needs_human", "reviewing", "awaiting_publish", "pr_open"].includes(issue.status)).length;
  dom.done.textContent = state.issues.filter((issue) => issue.status === "done").length;
  renderRunner(); renderRuntimes(); renderIssues();
}

function renderRunner() {
  const runner = state.runner || { state: "stopped" };
  dom.runnerState.textContent = runner.state;
  dom.runnerDetail.textContent = runner.process_id ? `PID ${runner.process_id} · ${state.workers.length} 个 Worker 记录` : "未运行";
  dom.runnerButton.textContent = ["running", "starting"].includes(runner.state) ? "停止" : "启动";
  dom.runnerButton.dataset.action = ["running", "starting"].includes(runner.state) ? "stop" : "start";
}

function renderRuntimes() {
  const live = state.runtimes.filter((runtime) => !["done", "cancelled"].includes(runtime.state));
  dom.runtimeCount.textContent = live.length;
  dom.runtimeList.innerHTML = live.length ? live.map((runtime) => `<article class="runtime-card" data-issue-id="${escapeHtml(runtime.issue_id)}"><header><strong>${escapeHtml(runtime.issue_id)}</strong>${badge(runtime.state === "waiting_human" ? "needs_human" : runtime.state)}</header><p>${escapeHtml(runtime.title)}</p><div class="meta"><span>Attempt ${runtime.attempt_number || "—"}</span><span>Turn ${runtime.turn_count || 0}</span><span>Thread ${escapeHtml(short(runtime.thread_id, 10))}</span></div></article>`).join("") : '<div class="empty">暂无活跃 Agent</div>';
}

function renderIssues() {
  const query = dom.search.value.trim().toLowerCase();
  const issues = state.issues.filter((issue) => !query || issue.id.toLowerCase().includes(query) || issue.title.toLowerCase().includes(query));
  dom.issueList.innerHTML = issues.length ? issues.map((issue) => `<article class="issue-card" data-issue-id="${escapeHtml(issue.id)}"><header><strong>${escapeHtml(issue.id)} · P${issue.priority}</strong>${badge(issue.status)}</header><p>${escapeHtml(issue.title)}</p><div class="meta"><span>${escapeHtml(issue.repository.base_branch)}</span><span>${escapeHtml(short(issue.repository.commit))}</span><span>v${issue.version}</span></div></article>`).join("") : '<div class="empty">还没有 Issue</div>';
}

function suggestedId() { return `ISSUE-${String(Date.now()).slice(-10)}`; }
function openNewIssue() { dom.issueForm.reset(); dom.issueForm.elements.id.value = suggestedId(); dom.issueError.hidden = true; dom.issueModal.showModal(); }
function openToken() { dom.tokenInput.value = state.token; if (!dom.tokenModal.open) dom.tokenModal.showModal(); }

async function resolveHead() {
  const path = dom.issueForm.elements.repository_url.value.trim();
  if (!/^[a-zA-Z]:[\\/]/.test(path) && !/^\\\\/.test(path)) { toast("只有本机绝对路径可以自动读取 HEAD", true); return; }
  try {
    const result = await api("/api/repositories/resolve-head", { method: "POST", body: JSON.stringify({ path }) });
    dom.issueForm.elements.repository_url.value = result.path; dom.issueForm.elements.commit.value = result.commit; toast(`已锁定 ${short(result.commit)}`);
  } catch (error) { toast(error.message, true); }
}

function issuePayload() {
  const data = new FormData(dom.issueForm);
  const labels = String(data.get("labels") || "").split(",").map((value) => value.trim().toLowerCase()).filter(Boolean);
  const blockedBy = String(data.get("blocked_by") || "").split(/\r?\n/).map((line) => line.trim()).filter(Boolean).map((line) => { const [identifier, blockerState] = line.split("|", 2).map((value) => value.trim()); return { identifier, state: blockerState || null }; });
  return {
    id: String(data.get("id") || "").trim().toUpperCase(), title: String(data.get("title") || "").trim(), description: String(data.get("description") || "").trim(),
    priority: Number(data.get("priority") || 2),
    labels, blocked_by: blockedBy, dispatchable: data.get("dispatchable") === "true",
    repository: { url: String(data.get("repository_url") || "").trim(), base_branch: String(data.get("base_branch") || "").trim(), commit: String(data.get("commit") || "").trim().toLowerCase() },
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
    dom.detailContent.innerHTML = `<p class="modal-copy">${escapeHtml(issue.description)}</p><div class="detail-grid">
      <div class="detail-field"><span>Status</span><strong>${escapeHtml(STATUS[issue.status] || issue.status)}</strong></div><div class="detail-field"><span>Priority</span><strong>P${issue.priority}</strong></div>
      <div class="detail-field"><span>Workspace base</span><strong>${escapeHtml(issue.repository.base_branch)}</strong></div><div class="detail-field"><span>Starting commit</span><strong>${escapeHtml(short(issue.repository.commit, 16))}</strong></div>
      <div class="detail-field"><span>Worker</span><strong>${escapeHtml(issue.claim.worker_id || "—")}</strong></div><div class="detail-field"><span>Thread / Turn</span><strong>${escapeHtml(latest ? `${short(latest.thread_id, 12)} / ${latest.turn_count}` : "—")}</strong></div>
      <div class="detail-field"><span>Dispatchable</span><strong>${issue.dispatchable ? "Yes" : "No"}</strong></div><div class="detail-field"><span>Labels</span><strong>${escapeHtml(issue.labels.join(", ") || "—")}</strong></div>
    </div>
    <section class="section"><h3>验收标准</h3><ul>${issue.acceptance_criteria.map((value) => `<li>${escapeHtml(value)}</li>`).join("")}</ul></section>
    ${issue.pull_request ? `<section class="section"><h3>PR / Merge Request</h3><p><a href="${escapeHtml(issue.pull_request)}" target="_blank" rel="noreferrer">${escapeHtml(issue.pull_request)}</a></p></section>` : ""}
    ${issue.blocker ? `<section class="section"><h3>Blocker</h3><pre>${escapeHtml(JSON.stringify(issue.blocker, null, 2))}</pre></section>` : ""}
    ${issue.blocked_by.length ? `<section class="section"><h3>Blocked By</h3><pre>${escapeHtml(JSON.stringify(issue.blocked_by, null, 2))}</pre></section>` : ""}
    ${renderDecisions(decisions)}
    <section class="section"><h3>Attempts</h3>${attempts.length ? attempts.map((attempt) => `<article class="attempt"><strong>Attempt #${attempt.attempt_number} · ${escapeHtml(attempt.status)}</strong><div class="meta"><span>Turn ${attempt.turn_count}</span><span>${escapeHtml(short(attempt.thread_id, 16))}</span><span>${escapeHtml(attempt.worker_id)}</span></div></article>`).join("") : '<p class="modal-copy">尚未执行</p>'}</section>
    <section class="section"><h3>事件</h3><div class="timeline">${events.map((event) => `<article><strong>${escapeHtml(event.event)}</strong><small> · ${new Date(event.created_at).toLocaleString()}</small><div class="meta"><span>${escapeHtml(event.from_status || "—")} → ${escapeHtml(event.to_status || "—")}</span><span>${escapeHtml(event.actor_type)}</span></div></article>`).join("")}</div></section>`;
    renderActions(issue, decisions);
    dom.detailError.hidden = true;
    if (show && !dom.detailModal.open) dom.detailModal.showModal();
  } catch (error) { if (show) toast(error.message, true); }
}

function renderDecisions(decisions) {
  const open = decisions.filter((decision) => decision.status === "open");
  if (!open.length) return "";
  return `<section class="section"><h3>等待人工输入</h3>${open.map((decision) => `<article class="decision"><strong>${escapeHtml(decision.question)}</strong>${decision.options.length ? `<p>${decision.options.map(escapeHtml).join(" / ")}</p>` : ""}<textarea data-decision-response="${escapeHtml(decision.id)}" placeholder="填写决定"></textarea><button class="button secondary" data-resolve="${escapeHtml(decision.id)}">提交并恢复 Agent</button></article>`).join("")}</section>`;
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

dom.newIssue.addEventListener("click", openNewIssue); dom.tokenButton.addEventListener("click", openToken); dom.refresh.addEventListener("click", refresh); dom.resolveHead.addEventListener("click", resolveHead);
dom.search.addEventListener("input", renderIssues);
dom.runnerButton.addEventListener("click", async () => { try { const action = dom.runnerButton.dataset.action; await api(`/api/runner-control/${action}`, { method: "POST", body: "{}" }); toast(action === "start" ? "Runner 已启动" : "Runner 已停止"); await refresh(); } catch (error) { toast(error.message, true); } });
dom.issueForm.addEventListener("submit", async (event) => { event.preventDefault(); if (event.submitter?.value === "cancel") { dom.issueModal.close(); return; } try { const issue = await api("/api/issues", { method: "POST", body: JSON.stringify(issuePayload()) }); dom.issueModal.close(); toast(`已创建 ${issue.id}`); await refresh(); } catch (error) { dom.issueError.textContent = error.message; dom.issueError.hidden = false; } });
dom.tokenForm.addEventListener("submit", (event) => { event.preventDefault(); if (event.submitter?.value === "cancel") { dom.tokenModal.close(); return; } state.token = dom.tokenInput.value.trim(); if (state.token) sessionStorage.setItem("acp_api_token", state.token); else sessionStorage.removeItem("acp_api_token"); dom.tokenModal.close(); refresh(); });
$("#issue-close-button").addEventListener("click", () => dom.issueModal.close());
$("#issue-cancel-button").addEventListener("click", () => dom.issueModal.close());
$("#token-cancel-button").addEventListener("click", () => dom.tokenModal.close());
dom.issueList.addEventListener("click", (event) => { const card = event.target.closest("[data-issue-id]"); if (card) openDetail(card.dataset.issueId); });
dom.runtimeList.addEventListener("click", (event) => { const card = event.target.closest("[data-issue-id]"); if (card) openDetail(card.dataset.issueId); });
dom.detailActions.addEventListener("click", (event) => { const button = event.target.closest("[data-action]"); if (button) runAction(button.dataset.action, button.dataset.version); });
dom.detailContent.addEventListener("click", async (event) => { const button = event.target.closest("[data-resolve]"); if (!button) return; const response = dom.detailContent.querySelector(`[data-decision-response="${CSS.escape(button.dataset.resolve)}"]`).value.trim(); if (!response) return; try { await api(`/api/issues/${encodeURIComponent(state.selected)}/decisions`, { method: "POST", body: JSON.stringify({ action: "resolve", decision_id: button.dataset.resolve, response, actor_id: "control-plane-ui" }) }); toast("决定已提交，Issue 已恢复 Ready"); await refresh(); } catch (error) { toast(error.message, true); } });
$("#detail-close").addEventListener("click", () => { state.selected = null; dom.detailModal.close(); });
window.setInterval(() => { if (!document.hidden && !dom.issueModal.open && !dom.tokenModal.open) refresh(); }, 5000);
refresh();
