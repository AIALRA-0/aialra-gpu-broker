"use strict";

const state = { token: sessionStorage.getItem("gpuBrokerAdminToken") || "", dashboard: null, history: [], historyAt: 0, reconcile: null, busy: false };
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const n = (value) => Number(value || 0).toLocaleString("zh-CN");
const mib = (value) => `${n(value)} MiB`;
const pct = (value) => `${Math.max(0, Math.min(100, Number(value || 0)))}%`;
const clock = (ts) => ts ? new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false }) : "—";
const shortClock = (ts) => ts ? new Date(ts * 1000).toLocaleTimeString("zh-CN", { hour12: false }) : "—";
const projectName = { minimax: "MiniMax H3", live_translate: "Live Translate", manga: "Manga / PanelTone" };
const ownerProjectName = { h3: "H3", live: "Live Translate", manga: "Manga / PanelTone" };
const statusName = { ACCEPTED: "已登记", WAITING_GPU: "等待 GPU", RUNNING: "运行中", COMMITTING: "保存中", COMPLETED: "已完成", FAILED: "失败", CANCEL_REQUESTED: "取消中", CANCELLED: "已取消", NEEDS_RECOVERY: "需恢复", WAITING: "等待准入", ACTIVE: "已获许可", FINISHED: "已结束", UNCERTAIN: "待核实", READY: "会话就绪", PREPARING: "会话准备中", REQUESTED: "请求中", CLOSING: "关闭中", CLOSED: "已关闭" };
const reasonName = { WAIT_PAUSED: "观察模式暂停准入", WAIT_RECONCILE: "等待故障核实", WAIT_TELEMETRY: "等待新鲜遥测", WAIT_ACTIVE: "受管 GPU 正被占用", WAIT_REALTIME: "实时会话优先", WAIT_PROJECT_OFFLINE: "项目进程离线", WAIT_VRAM: "显存安全余量不足", PROFILE_NOT_FIT: "画像超出显存容量", WAIT_BATCH: "等待现有任务结束", WAIT_BACKEND_STOP: "等待后端确认停止", HEARTBEAT_LOST: "心跳丢失", OWNER_REPLACED: "进程实例已更换", PREPARE_TIMEOUT: "模型准备超时，待核实", BROKER_RESTART: "服务重启后待核实" };
const eventName = { TELEMETRY_READY: "遥测恢复", TELEMETRY_LOST: "遥测中断", PROJECT_INSTANCE: "项目进程上线", JOB_REGISTERED: "任务已登记", JOB_STATUS: "任务状态更新", JOB_CANCEL_REQUESTED: "请求取消业务任务", PROFILE_CREATED: "新建画像", PROFILE_UPDATED: "画像变更", PERMIT_REQUESTED: "申请 GPU", PERMIT_GRANTED: "许可已发放", PERMIT_FINISHED: "许可已结束", PERMIT_CANCELLED: "等待许可取消", PERMIT_CANCEL_REQUESTED: "请求取消运行任务", PERMIT_UNCERTAIN: "许可状态待核实", PERMIT_RECONCILED: "许可已核实", SESSION_REQUESTED: "实时会话申请", SESSION_PREPARING: "实时会话准备", SESSION_READY: "实时会话就绪", SESSION_CLOSING: "实时会话关闭中", SESSION_CLOSED: "实时会话已关闭", SESSION_UNCERTAIN: "会话状态待核实", SESSION_RECONCILED: "会话已核实", ALLOCATION_ENABLED: "准入已启用", ALLOCATION_PAUSED: "准入已暂停", BACKUP_CREATED: "校验备份已生成" };

function toast(message, error = false) { const el = $("toast"); el.textContent = message; el.className = `toast show${error ? " error" : ""}`; clearTimeout(toast.timer); toast.timer = setTimeout(() => el.className = "toast", 5000); }
function modal(id, show) { $(id).classList.toggle("open", show); }
function badge(status) { const cls = ["ACTIVE", "READY", "COMPLETED", "FINISHED"].includes(status) ? "green-pill" : ["UNCERTAIN", "FAILED", "NEEDS_RECOVERY"].includes(status) ? "red-pill" : ["WAITING", "PREPARING", "REQUESTED", "CANCEL_REQUESTED"].includes(status) ? "amber-pill" : "muted-pill"; return `<span class="pill ${cls}">${esc(statusName[status] || status)}</span>`; }
async function api(path, options = {}) {
  const response = await fetch(path, { ...options, cache: "no-store", headers: { Authorization: `Bearer ${state.token}`, ...(options.body ? { "Content-Type": "application/json" } : {}), ...(options.headers || {}) } });
  let body; try { body = await response.json(); } catch { body = {}; }
  if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
  return body;
}
async function connect() {
  const token = $("adminToken").value.trim();
  if (!token) { $("tokenError").textContent = "请输入管理员令牌。"; return; }
  state.token = token;
  try { const me = await api("/v1/me"); if (me.role !== "admin") throw new Error("需要管理员令牌"); sessionStorage.setItem("gpuBrokerAdminToken", token); modal("tokenModal", false); $("tokenError").textContent = ""; await refresh(); toast("已连接本地控制台"); }
  catch (error) { state.token = ""; $("tokenError").textContent = error.message; }
}
function setConnection(ok, detail) { $("sidebarDot").className = `status-dot ${ok ? "ok" : "bad"}`; $("sidebarStatus").textContent = detail; }
async function refresh() {
  if (!state.token || state.busy || document.hidden) return;
  state.busy = true;
  try {
    const data = await api("/v1/dashboard");
    state.dashboard = data;
    render(data);
    setConnection(true, "本机服务已连接");
    if (Date.now() - state.historyAt > 15000) {
      const samples = await api(`/v1/history?gpu_uuid=${encodeURIComponent(data.managed_gpu_uuid)}&minutes=${$("historyWindow").value}`);
      state.history = samples.samples;
      state.historyAt = Date.now();
      renderChart();
    }
  } catch (error) {
    setConnection(false, "连接或认证失败");
    $("lastUpdate").textContent = `更新失败：${error.message}`;
    markOwnerSnapshotUnavailable();
    if (/401|令牌|Bearer|Invalid token/.test(error.message)) { sessionStorage.removeItem("gpuBrokerAdminToken"); state.token = ""; modal("tokenModal", true); }
  } finally { state.busy = false; }
}
function render(data) {
  const now = Date.now() / 1000, snap = data.snapshot || {}, cards = snap.gpus || [], managed = cards.find((g) => g.uuid === data.managed_gpu_uuid), display = cards.find((g) => g.uuid === data.display_gpu_uuid);
  const fresh = !!(snap.ok && managed && now - snap.timestamp <= 10);
  $("lastUpdate").textContent = `刷新 ${shortClock(data.timestamp)}`;
  $("activeCount").textContent = n(data.counts.active);
  $("waitingCount").textContent = n(data.counts.waiting);
  $("uncertainCount").textContent = n(data.counts.uncertain);
  $("cpuValue").textContent = snap.host ? pct(snap.host.cpu_pct) : "—";
  $("ramValue").textContent = snap.host ? `${(snap.host.ram_used_mib / 1024).toFixed(1)} / ${(snap.host.ram_total_mib / 1024).toFixed(1)} GiB` : "—";
  $("sampleAge").textContent = snap.timestamp ? `${Math.max(0, Math.round(now - snap.timestamp))} 秒` : "—";
  $("serverTime").textContent = shortClock(data.timestamp);
  const mode = data.counts.uncertain ? "待核实冻结" : !fresh ? "遥测中断" : data.allocation_enabled ? "准入运行中" : "观察模式";
  $("modeBadge").textContent = mode;
  $("modeBadge").className = `pill ${data.counts.uncertain || !fresh ? "red-pill" : data.allocation_enabled ? "green-pill" : "amber-pill"}`;
  $("scheduleMode").textContent = mode;
  $("scheduleMode").className = $("modeBadge").className;
  const brokerModeDescription = data.counts.uncertain ? "有状态待核实。新任务准入已冻结，先确认后端停止。" : !fresh ? "受管 GPU 遥测不可用或过期，新的 GPU 许可不会发放。" : data.allocation_enabled ? "仅对已接入的项目调用发放许可；未接入路径仍可绕过。" : "目前只监控，不发放新的 GPU 许可；项目调用尚未统一受控。";
  $("modeDescription").textContent = `Legacy Broker：${brokerModeDescription}`;
  const onlineTimeout = Math.round(data.heartbeat_timeout_seconds ?? 15);
  const activeGrace = Math.round(data.active_heartbeat_grace_seconds ?? 180);
  const activeTimeout = onlineTimeout + activeGrace;
  const prepareTimeout = Math.round(data.session_prepare_timeout_seconds ?? 180);
  $("heartbeatPolicy").textContent = `项目在线 ${onlineTimeout}s · 活跃宽限 +${activeGrace}s（合计 ${activeTimeout}s） · 会话准备 ${prepareTimeout}s`;
  const legacyEnableBlocked = data.owner_v1?.configured === true && !data.allocation_enabled;
  $("allocationButton").disabled = legacyEnableBlocked;
  $("allocationButton").textContent = legacyEnableBlocked
    ? "Legacy：旧准入保持关闭"
    : data.allocation_enabled ? "Legacy：暂停旧准入" : "Legacy：启用旧准入";
  $("allocationButton").className = legacyEnableBlocked
    ? "text-button" : data.allocation_enabled ? "danger-button" : "primary-button";
  $("allocationButton").title = legacyEnableBlocked
    ? "Owner v1 已建立；旧准入不能从此页面重新启用。回退必须先完成三项目和整卡空闲核查。"
    : "只更改 Legacy Broker 的旧准入设置，不更改 Owner v1 所有权状态";
  gpuCard("managed", managed, data, true);
  gpuCard("display", display, data, false);
  renderOwnerStatus(data.owner_v1);
  renderSchedule(data); renderAlerts(data); renderProjects(data, now); renderJobs(data); renderProfiles(data); renderEvents(data);
}
function renderOwnerStatus(owner = {}) {
  const validStates = ["FREE", "OWNED", "UNKNOWN"];
  const configured = owner.configured === true;
  const stale = configured && owner.stale === true;
  const status = configured && !stale && validStates.includes(owner.state) ? owner.state : "UNKNOWN";
  const stateLabel = { FREE: "FREE · 已确认空闲", OWNED: "OWNED · 项目持有", UNKNOWN: "UNKNOWN · 状态未知" }[status];
  $("ownerV1Badge").textContent = configured ? status : "未配置 · UNKNOWN";
  if (stale) $("ownerV1Badge").textContent = "证据过期 · UNKNOWN";
  $("ownerV1Badge").className = `pill ${status === "OWNED" ? "green-pill" : status === "UNKNOWN" || !configured ? "amber-pill" : "muted-pill"}`;
  $("ownerV1State").textContent = configured && owner.stale === true
    ? "UNKNOWN · 证据过期"
    : stateLabel;
  const ownerProject = owner.owner_project
    ? (ownerProjectName[owner.owner_project] || owner.owner_project)
    : null;
  $("ownerV1ProjectLabel").textContent = stale && ownerProject
    ? "上次登记项目"
    : status === "OWNED" && ownerProject
      ? "当前登记 Owner"
      : "登记项目";
  $("ownerV1Project").textContent = stale && ownerProject
    ? `${ownerProject}（上次登记，待核实）`
    : status === "OWNED" && ownerProject
      ? ownerProject
      : "—";
  $("ownerV1Observed").textContent = clock(owner.last_observed_at);
  $("ownerV1Acquired").textContent = status === "OWNED" ? clock(owner.acquired_at) : "—";
  const guidance = $("ownerGuidance");
  guidance.className = `owner-guidance ${!configured ? "unconfigured" : status.toLowerCase()}`;
  if (!configured) {
    guidance.textContent = "未配置 Owner v1 数据库，当前无法确认 4080 是空闲还是被持有。Legacy Broker 的任务计数和准入开关不代表 Owner 状态。";
  } else if (stale) {
    guidance.textContent = "Owner 观察已过期，当前按 UNKNOWN 处理。上次项目登记仅供追溯；等待直接观察恢复后再启动新的 4080 重任务。";
  } else if (status === "FREE") {
    guidance.textContent = `Owner v1 最近确认 4080 无项目持有（${clock(owner.last_observed_at)}）。下方 NVML 数值是独立的整卡物理观测。`;
  } else if (status === "OWNED") {
    guidance.textContent = `Owner v1 登记由 ${ownerProject || "未知项目"} 持有 4080（${clock(owner.last_observed_at)}）。其他项目应等待释放；Legacy 准入按钮不会改变此状态。`;
  } else {
    guidance.textContent = "当前无法确认 4080 所有者。不要根据显存用量猜测项目；Owner 状态恢复为 FREE 前，不应开始新的重型任务。";
  }
  renderOwnerEvidence(owner);
}
function markOwnerSnapshotUnavailable() {
  const owner = state.dashboard?.owner_v1 || {};
  const ownerProject = owner.owner_project
    ? (ownerProjectName[owner.owner_project] || owner.owner_project)
    : null;
  $("ownerV1Badge").textContent = "读取失败 · UNKNOWN";
  $("ownerV1Badge").className = "pill amber-pill";
  $("ownerV1State").textContent = "UNKNOWN · 无法刷新";
  $("ownerV1ProjectLabel").textContent = "最近成功快照项目";
  $("ownerV1Project").textContent = ownerProject ? `${ownerProject}（未确认仍持有）` : "—";
  $("ownerV1Acquired").textContent = "—";
  const guidance = $("ownerGuidance");
  guidance.className = "owner-guidance unknown";
  guidance.textContent = owner.last_observed_at
    ? `刚才未能读取本机服务。上次成功观察为 ${clock(owner.last_observed_at)}；当前 Owner 状态未知，等待连接恢复后再确认。`
    : "刚才未能读取本机服务，因此无法确认 Owner 状态。等待连接恢复后再启动新的重型任务。";
  renderOwnerEvidence(owner, true);
}
function renderOwnerEvidence(owner = {}, forceUnknown = false) {
  const labels = { h3: "H3 直接观察", live: "Live 直接观察", manga: "Manga 直接观察", gpu: "GPU · NVML 直接遥测" };
  const activeState = forceUnknown || owner.stale === true ? "UNKNOWN" : owner.state;
  const maxAge = Number(owner.evidence_max_age_seconds);
  const freshnessWindow = Number.isFinite(maxAge) && maxAge > 0 ? maxAge : 10;
  for (const [source, label] of Object.entries(labels)) {
    const row = document.querySelector(`[data-owner-evidence="${source}"]`);
    if (!row) continue;
    const evidence = owner.configured === true ? owner.evidence_sources?.[source] : null;
    const timestamp = evidence?.observed_at;
    const hasTimestamp = typeof timestamp === "number" && Number.isFinite(timestamp) && timestamp > 0;
    const ageSeconds = hasTimestamp ? Date.now() / 1000 - timestamp : null;
    const fresh = !forceUnknown && evidence?.fresh === true && evidence?.state === "FRESH"
      && ageSeconds !== null && ageSeconds >= 0 && ageSeconds <= freshnessWindow;
    const required = owner.configured !== true || activeState === "UNKNOWN"
      || source === "gpu" || (activeState === "OWNED" && source === owner.owner_project);
    const status = row.querySelector(".evidence-state");
    const time = row.querySelector(".evidence-time");
    row.querySelector("span").textContent = label;
    status.className = `evidence-state ${!required ? "optional" : fresh ? "fresh" : "unknown"}`;
    status.textContent = forceUnknown ? "UNKNOWN · 读取失败"
      : !required
      ? fresh ? "证据新鲜 · 按需观察" : "按需观察 · 非本次必需"
      : fresh ? "证据新鲜"
      : evidence?.reason === "EXPIRED" || (ageSeconds !== null && ageSeconds > freshnessWindow)
        ? "UNKNOWN · 已过期"
        : evidence?.reason === "INVALID_TIMESTAMP" || (ageSeconds !== null && ageSeconds < 0)
          ? "UNKNOWN · 时间异常"
          : "UNKNOWN · 缺少证据";
    time.textContent = hasTimestamp
      ? `${clock(timestamp)}${ageSeconds >= 0 ? ` · ${Math.floor(ageSeconds)} 秒前` : ""}`
      : "尚无有效时间戳";
  }
}
function brokerRegisteredOwners(data) {
  const owners = new Map();
  for (const permit of data.permits || []) {
    if (!["ACTIVE", "CANCEL_REQUESTED", "UNCERTAIN"].includes(permit.status)) continue;
    const label = projectName[permit.project_id] || permit.project_id || "未知项目";
    const detail = permit.job_label || permit.stage;
    owners.set(label, detail ? `${label} · ${detail}` : label);
  }
  for (const session of data.sessions || []) {
    if (!["PREPARING", "READY", "CLOSING", "UNCERTAIN"].includes(session.status)) continue;
    const label = projectName[session.project_id] || session.project_id || "未知项目";
    if (!owners.has(label)) owners.set(label, `${label} · 会话占用`);
  }
  return [...owners.values()];
}
function gpuCard(prefix, gpu, data, managed) {
  $(prefix + "Uuid").textContent = gpu?.uuid || (managed ? data.managed_gpu_uuid : data.display_gpu_uuid) || "未配置";
  $(prefix + "Name").textContent = gpu?.name || (managed ? "计算卡未观测到" : "显示卡未观测到");
  $(prefix + "Memory").textContent = gpu ? `${mib(gpu.used_mib)} / ${mib(gpu.total_mib)}` : "—";
  $(prefix + "Free").textContent = gpu ? `空闲 ${mib(gpu.free_mib)}` : "空闲 —";
  $(prefix + "Bar").style.width = gpu ? pct(gpu.used_mib / gpu.total_mib * 100) : "0%";
  $(prefix + "Util").style.setProperty("--value", gpu?.utilization_pct || 0);
  $(prefix + "Util").querySelector("strong").textContent = gpu ? pct(gpu.utilization_pct) : "—";
  $(prefix + "Temp").textContent = gpu?.temperature_c == null ? "—" : `${gpu.temperature_c} °C`;
  if (managed) {
    const reserve = gpu ? Math.max(data.safety_floor_mib, Math.ceil(gpu.total_mib * data.safety_ratio)) : data.safety_floor_mib;
    const owners = brokerRegisteredOwners(data);
    $("managedReserve").textContent = `安全余量 ${mib(reserve)}`;
    $("managedDriver").textContent = gpu?.driver || "—";
    $("managedAdmission").textContent = data.allocation_enabled ? "Legacy 旧准入开启" : "Legacy 旧准入暂停";
    $("managedOwner").textContent = owners.length ? owners.join("；") : "无旧许可登记 · Owner 未知";
    const fresh = !!(data.snapshot?.ok && gpu && Date.now() / 1000 - data.snapshot.timestamp <= 10);
    $("managedObserved").textContent = fresh ? `${mib(gpu.used_mib)} 显存 · ${pct(gpu.utilization_pct)} 利用率` : "遥测缺失或过期";
  }
}
function renderChart() {
  const s = state.history, empty = !s || s.length < 2;
  $("chartEmpty").hidden = !empty;
  if (empty) { $("memoryArea").setAttribute("d", ""); $("memoryLine").setAttribute("d", ""); $("utilLine").setAttribute("d", ""); return; }
  const first = s[0].ts, span = Math.max(1, s[s.length - 1].ts - first);
  const point = (row, value) => `${((row.ts - first) / span * 900).toFixed(1)},${(165 - Math.max(0, Math.min(1, value)) * 140).toFixed(1)}`;
  const memory = s.map((row) => point(row, row.used_mib / Math.max(1, row.total_mib)));
  const util = s.map((row) => point(row, row.utilization_pct / 100));
  $("memoryLine").setAttribute("d", `M${memory.join(" L")}`);
  $("memoryArea").setAttribute("d", `M${memory[0]} L${memory.slice(1).join(" L")} L900,165 L0,165 Z`);
  $("utilLine").setAttribute("d", `M${util.join(" L")}`);
  $("chartStart").textContent = shortClock(first); $("chartEnd").textContent = shortClock(s[s.length - 1].ts);
}
function renderSchedule(data) {
  const active = data.permits.filter((p) => ["ACTIVE", "CANCEL_REQUESTED"].includes(p.status));
  const waiting = data.permits.filter((p) => p.status === "WAITING");
  const sessions = data.sessions.filter((s) => ["REQUESTED", "PREPARING", "READY", "CLOSING", "UNCERTAIN"].includes(s.status));
  const rows = [ ...active.map((p) => `<div class="stack-item"><div><strong>${esc(p.job_label)} · ${esc(p.stage)}</strong><small>${esc(projectName[p.project_id] || p.project_id)} · 已运行 ${p.granted_at ? n(Math.floor(Date.now()/1000-p.granted_at)) : 0} 秒</small></div>${badge(p.status)}</div>`), ...sessions.map((s) => `<div class="stack-item"><div><strong>实时会话 · ${esc(projectName[s.project_id] || s.project_id)}</strong><small>${esc(reasonName[s.reason] || s.reason || "等待项目上报")}</small></div><div>${badge(s.status)} ${s.status === "UNCERTAIN" ? `<button class="action-button danger-action" data-action="reconcile-session" data-id="${esc(s.id)}">核实</button>` : ""}</div></div>`), ...waiting.slice(0, 5).map((p) => `<div class="stack-item"><div><strong>${esc(p.job_label)} · ${esc(p.stage)}</strong><small>${esc(reasonName[p.reason] || p.reason || "排队等待")}</small></div>${badge(p.status)}</div>`) ];
  $("scheduleBody").className = rows.length ? "stack-list" : "stack-list empty-state";
  $("scheduleBody").innerHTML = rows.length ? rows.join("") : "暂无运行任务或等待许可；项目接入后显示队列。";
}
function renderAlerts(data) {
  const snap = data.snapshot || {};
  const gpu = (snap.gpus || []).find((card) => card.uuid === data.managed_gpu_uuid);
  const fresh = !!(snap.ok && gpu && Date.now() / 1000 - snap.timestamp <= 10);
  // Idle GPUs still use VRAM for driver/display bookkeeping. Warn only when
  // the unexplained load is material; the owner label remains unknown either way.
  const observedOccupied = gpu && (
    Number(gpu.used_mib || 0) >= Math.max(1024, Number(gpu.total_mib || 0) * 0.1) ||
    Number(gpu.utilization_pct || 0) >= 10
  );
  const unregisteredLoad = fresh && observedOccupied && brokerRegisteredOwners(data).length === 0
    ? [{ code: "UNATTRIBUTED_GPU_USAGE", level: "warning", text: "受管 GPU 有明显整卡负载，但 Broker 没有活动 Owner 登记；可能是未接入路径或驻留模型，具体项目/进程无法由此确认。" }]
    : [];
  const alerts = [...(data.alerts || []), ...unregisteredLoad];
  $("alertCount").textContent = n(alerts.length);
  $("alertsBody").className = alerts.length ? "stack-list" : "stack-list empty-state";
  $("alertsBody").innerHTML = alerts.length ? alerts.map((a) => `<div class="stack-item"><div><strong>${esc(a.text)}</strong><small>${esc(a.code)}</small></div><span class="pill ${a.level === "critical" ? "red-pill" : a.level === "warning" ? "amber-pill" : "muted-pill"}">${a.level === "critical" ? "阻断" : a.level === "warning" ? "注意" : "提示"}</span></div>`).join("") : "当前没有需要处理的告警。";
}
function renderProjects(data, now) {
  $("projectGrid").innerHTML = data.projects.map((p) => {
    const online = !!(p.last_seen && now - p.last_seen < 15);
    const jobs = data.jobs.filter((j) => j.project_id === p.id && !["COMPLETED", "FAILED", "CANCELLED"].includes(j.status)).length;
    const allocated = data.permits.filter((permit) => permit.project_id === p.id && ["ACTIVE", "CANCEL_REQUESTED"].includes(permit.status)).reduce((sum, permit) => sum + permit.peak_growth_mib, 0);
    const queued = data.permits.filter((permit) => permit.project_id === p.id && permit.status === "WAITING").length;
    return `<article class="panel project-card"><div class="project-head"><span class="project-icon">${esc((p.label || p.id).charAt(0))}</span><span class="pill ${online ? "green-pill" : "muted-pill"}">${online ? "心跳正常" : "无近期心跳"}</span></div><h3>${esc(p.label)}</h3><p>${online ? `上次心跳 ${esc(shortClock(p.last_seen))} · ${esc(p.reported_status || "online")}` : "等待项目发送心跳和任务状态"}</p><div class="project-stats"><div><span>未完成任务</span><strong>${n(jobs)}</strong></div><div><span>排队许可</span><strong>${n(queued)}</strong></div><div><span>当前获准增长</span><strong>${mib(allocated)}</strong></div><div><span>项目报告常驻</span><strong>${online ? mib(p.reported_resident_mib) : "—"}</strong></div><div><span>累计许可时间</span><strong>${n(Math.round(p.usage_seconds / 60))} 分钟</strong></div><div><span>调度权重</span><strong>${n(p.weight)}</strong></div></div></article>`;
  }).join("");
}
function renderJobs(data) {
  const filter = $("jobFilter").value;
  const list = data.permits.filter((p) => filter === "all" || (filter === "active" && ["ACTIVE", "WAITING", "CANCEL_REQUESTED"].includes(p.status)) || (filter === "uncertain" && p.status === "UNCERTAIN") || (filter === "finished" && ["FINISHED", "CANCELLED"].includes(p.status)));
  const permitJobs = new Set(data.permits.map((p) => p.job_id));
  const unpermitted = filter === "all" ? data.jobs.filter((j) => !permitJobs.has(j.id)).map((j) => `<tr><td><span class="cell-title">${esc(j.label)}</span><span class="cell-sub">${esc(j.external_id)}</span></td><td>${esc(projectName[j.project_id] || j.project_id)}</td><td>${badge(j.status)}</td><td>尚无 GPU 许可</td><td>${esc(clock(j.created_at))}</td><td>${["COMPLETED", "FAILED", "CANCELLED", "CANCEL_REQUESTED"].includes(j.status) ? "—" : `<button class="action-button" data-action="cancel-job" data-id="${esc(j.id)}">取消任务</button>`}</td></tr>`) : [];
  $("jobsTable").innerHTML = list.length || unpermitted.length ? list.map((p) => {
    const job = data.jobs.find((j) => j.id === p.job_id);
    const action = p.status === "UNCERTAIN" ? `<button class="action-button danger-action" data-action="reconcile-permit" data-id="${esc(p.id)}">核实</button>` : ["ACTIVE", "WAITING"].includes(p.status) && job?.status !== "CANCEL_REQUESTED" ? `<button class="action-button" data-action="cancel-job" data-id="${esc(p.job_id)}">取消任务</button>` : "—";
    return `<tr><td><span class="cell-title">${esc(p.job_label)}</span><span class="cell-sub">${esc(p.stage)} · ${esc(p.id.slice(0, 8))}</span></td><td>${esc(projectName[p.project_id] || p.project_id)}</td><td>${badge(p.status)}</td><td>${esc(p.profile_label)}<span class="cell-sub">峰值 +${mib(p.peak_growth_mib)}</span></td><td>${esc(reasonName[p.reason] || p.reason || (p.granted_at ? clock(p.granted_at) : clock(p.created_at)))}</td><td>${action}</td></tr>`;
  }).join("") + unpermitted.join("") : `<tr><td colspan="6" class="empty-cell">${data.jobs.length ? "此筛选没有许可记录" : "项目接入后会在这里显示任务；当前没有项目上报"}</td></tr>`;
}
function renderProfiles(data) {
  $("profilesTable").innerHTML = data.profiles.length ? data.profiles.map((p) => `<tr><td><span class="cell-title">${esc(p.label)}</span><span class="cell-sub mono">${esc(p.id.slice(0, 8))}</span></td><td>${esc(projectName[p.project_id] || p.project_id)}</td><td>${p.kind === "realtime" ? "实时会话" : "批处理"}</td><td>+${mib(p.peak_growth_mib)}</td><td>${n(p.max_seconds)} 秒</td><td>${p.respect_vram ? "开启" : "默认关闭"}</td><td>${p.enabled ? badge("ACTIVE") : badge("CLOSED")}</td><td><button class="action-button" data-id="${esc(p.id)}" data-enabled="${p.enabled ? "1" : "0"}">${p.enabled ? "停用" : "启用"}</button></td></tr>`).join("") : "<tr><td colspan='8' class='empty-cell'>暂无画像；接入前请按实测峰值创建</td></tr>";
}
function renderEvents(data) {
  $("eventList").className = data.events.length ? "event-list" : "event-list empty-state";
  $("eventList").innerHTML = data.events.length ? data.events.map((e) => `<div class="event-row"><span class="event-time">${esc(shortClock(e.ts))}</span><span class="event-kind">${esc(eventName[e.kind] || e.kind)}</span><span class="event-detail">${esc(projectName[e.project_id] || e.project_id || "系统")} · ${esc(JSON.stringify(e.detail || {}))}</span></div>`).join("") : "暂无事件";
}
async function mutate(path, body) { const result = await api(path, { method: "POST", ...(body ? { body: JSON.stringify(body) } : {}) }); await refresh(); return result; }
$("connectButton").addEventListener("click", connect);
$("adminToken").addEventListener("keydown", (e) => { if (e.key === "Enter") connect(); });
$("cancelTokenButton").addEventListener("click", () => { if (state.token) modal("tokenModal", false); });
$("tokenButton").addEventListener("click", () => { $("adminToken").value = ""; modal("tokenModal", true); $("adminToken").focus(); });
$("refreshButton").addEventListener("click", refresh);
$("historyWindow").addEventListener("change", () => { state.historyAt = 0; refresh(); });
$("jobFilter").addEventListener("change", () => state.dashboard && renderJobs(state.dashboard));
$("allocationButton").addEventListener("click", async () => {
  if (!state.dashboard) return;
  const enabled = !state.dashboard.allocation_enabled;
  if (enabled && state.dashboard.owner_v1?.configured === true) {
    toast("Owner v1 已建立；旧准入不能从此页面重新启用", true);
    return;
  }
  if (enabled && !window.confirm("启用后，已接入项目可获得 GPU 许可。请先确认未纳管的 GPU 调用已停止，资源画像和模型释放已验证。继续启用？")) return;
  try {
    await mutate("/v1/admin/allocation", { enabled });
    toast(enabled ? "已启用 GPU 准入" : "已暂停新准入");
  } catch (e) { toast(e.message, true); }
});
$("backupButton").addEventListener("click", async () => { try { const result = await mutate("/v1/admin/backup"); toast(`备份已保存：${result.path}`); } catch (e) { toast(e.message, true); } });
$("doctorButton").addEventListener("click", async () => { try { const result = await api("/v1/admin/doctor"); $("infoBody").textContent = JSON.stringify(result, null, 2); modal("infoModal", true); } catch (e) { toast(e.message, true); } });
$("newProfileButton").addEventListener("click", () => modal("profileModal", true));
$("profileForm").addEventListener("submit", async (e) => { e.preventDefault(); try { await api("/v1/profiles", { method: "POST", body: JSON.stringify({ project_id: $("profileProject").value, label: $("profileLabel").value.trim(), kind: $("profileKind").value, peak_growth_mib: Number($("profileMemory").value), max_seconds: Number($("profileSeconds").value), respect_vram: $("profileRespectVram").checked }) }); modal("profileModal", false); e.target.reset(); await refresh(); toast("资源画像已创建"); } catch (err) { toast(err.message, true); } });
$("jobsTable").addEventListener("click", async (e) => { const button = e.target.closest("button[data-action]"); if (!button) return; const id = button.dataset.id; if (button.dataset.action === "reconcile-permit") { state.reconcile = { type: "permits", id }; modal("reconcileModal", true); } else if (button.dataset.action === "cancel-job") { try { await mutate(`/v1/admin/jobs/${encodeURIComponent(id)}/cancel`); toast("业务任务取消请求已登记；运行中后端仍须确认停止"); } catch (err) { toast(err.message, true); } } });
$("scheduleBody").addEventListener("click", (e) => { const button = e.target.closest('button[data-action="reconcile-session"]'); if (!button) return; state.reconcile = { type: "sessions", id: button.dataset.id }; modal("reconcileModal", true); });
$("profilesTable").addEventListener("click", async (e) => { const button = e.target.closest("button[data-id]"); if (!button) return; try { await api(`/v1/profiles/${encodeURIComponent(button.dataset.id)}`, { method: "PATCH", body: JSON.stringify({ enabled: button.dataset.enabled !== "1" }) }); await refresh(); toast("画像状态已更新"); } catch (err) { toast(err.message, true); } });
$("reconcileForm").addEventListener("submit", async (e) => { e.preventDefault(); if (!state.reconcile) return; try { await mutate(`/v1/admin/${state.reconcile.type}/${encodeURIComponent(state.reconcile.id)}/reconcile`, { evidence: $("reconcileEvidence").value.trim(), backend_confirmed_inactive: $("reconcileConfirm").checked }); modal("reconcileModal", false); e.target.reset(); state.reconcile = null; toast("核实记录已保存"); } catch (err) { toast(err.message, true); } });
document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => modal(button.dataset.close, false)));
document.querySelectorAll(".nav-link").forEach((link) => link.addEventListener("click", () => { document.querySelectorAll(".nav-link").forEach((item) => item.classList.remove("active")); link.classList.add("active"); }));
if (state.token) { modal("tokenModal", false); refresh(); } else { $("adminToken").focus(); }
setInterval(refresh, 5000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
