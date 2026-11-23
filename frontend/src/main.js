import { BilingualChat } from './chat';
import './style.css';
import './chat.css';
import './markdown.css';
import './workspace-layout.css';
import './attachments.css';
import { workspacePasswordForm, lockedWorkspaceMarkup } from './workspace-access.js';
import './workspace-access.css';
import { ArchiveDownload, ARCHIVE_LIMIT_HINT } from './archive-download.js';
import './archive-download.css';
import { AttentionAudio } from './attention-audio.js';
import { WorkspaceActivity } from './workspace-activity.js';
import { AccountUsageMenu } from './account-usage.js';
import './quota.css';
import { SoloSettings } from './solo-settings.js';
import { createSoloWorkspaceForm } from './workspace-create.js';
import './solo-settings.css';
import './account-browser.css';

const paths = {
  plus: '<path d="M12 5v14M5 12h14"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/>',
  terminal: '<path d="m5 6 6 6-6 6m8 0h6"/>',
  arrow: '<path d="M5 12h14m-5-5 5 5-5 5"/>',
  shield: '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Z"/><path d="m8 12 3 3 5-6"/>',
  logout: '<path d="M9 4H5v16h4m6-13 5 5-5 5m-6-5h11"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  chevron: '<path d="m9 5 7 7-7 7"/>',
  down: '<path d="m5 9 7 7 7-7"/>',
  play: '<path d="m7 4 14 8-14 8Z"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  refresh: '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6 7a7 7 0 0 1 12-1l2 3M4 15l2 3a7 7 0 0 0 12-1"/>',
  file: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9Z"/><path d="M14 3v6h6"/>',
  upload: '<path d="M12 16V3m-5 5 5-5 5 5M4 16v5h16v-5"/>',
  download: '<path d="M12 3v13m-5-5 5 5 5-5M4 16v5h16v-5"/>',
  trash: '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
  save: '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h12l4 4v12a2 2 0 0 1-2 2Z"/><path d="M7 3v6h10V3M7 21v-8h10v8"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10v1"/>',
  lock: '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
  cloud: '<path d="M6 18a5 5 0 1 1 1-10 6 6 0 0 1 11 2 4 4 0 1 1 0 8Z"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  home: '<path d="m3 10 9-7 9 7v11H3Z"/><path d="M9 21v-8h6v8"/>',
  expand: '<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/>',
};
const icon = (name, className = '') => `<svg class="icon ${className}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.file}</svg>`;
const mark = '<span class="brand-mark" aria-hidden="true">✳</span>';
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (s) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[s]));
const app = document.querySelector('#app');
const maxBytes = 10 * 1024 * 1024;
const state = { mode: 'multiuser', admin: false, csrf: '', user: null, generation: 0, workspaces: [], selectedId: null, status: null, filePath: '', entries: [], selectedFile: null, dirty: false, authenticated: false, busy: false, fileLoading: false, view: 'terminal' };
let pollTimer;
let workspaceListGeneration = 0;
let selectedStorage;
let soloSettings;
const isSolo = () => state.mode === 'solo';
const selected = () => state.workspaces.find((item) => item.id === state.selectedId);
let fileRequestGeneration = 0;
let openFileRequestGeneration = 0;
const workspaceUrl = (suffix = '') => `/api/workspaces/${encodeURIComponent(state.selectedId)}${suffix}`;
const fileJoin = (name) => state.filePath ? `${state.filePath}/${name}` : name;
const attention = new AttentionAudio({ onChange: () => accountMenu.syncSound(), onHint: (message) => toast(message) });
const activity = new WorkspaceActivity({ send: api });
const accountMenu = new AccountUsageMenu({ api, getMode: () => state.mode, getUser: () => state.authenticated ? state.user : null, audio: attention, page: document, setTimer: (...args) => setInterval(...args), clearTimer: (...args) => clearInterval(...args) });
const chat = new BilingualChat({
  api, toast, modal, selected, attention, getMode: () => state.mode,
  isAuthenticated: () => state.authenticated,
  isBusy: () => state.busy,
  onState: (model, { completed }) => {
    if (selected()) selected().chatActive = model.active;
    updateConversationControls();
    if (completed && (model.quotaPaused || model.phase === 'paused_quota')) refreshWorkspaces(false).catch(() => {});
    else if (completed && selected()?.running) loadFiles().catch(() => {});
  },
  onStopped: async () => { await refreshWorkspaces(false); if (selected()?.running) await loadFiles(); },
});
const archiveDownload = new ArchiveDownload({
  getContext: () => {
    const workspace = selected();
    return state.authenticated && workspace?.running && !workspace.locked
      ? { id: workspace.id, name: workspace.name, key: `${state.generation}:${state.user?.id}:${workspace.id}` } : null;
  },
  fetch: (...args) => fetch(...args), document, urlApi: URL,
  onState: () => updateArchiveControls(),
  onError: (message) => toast(message, true),
  onHttpError: (status, message) => {
    if (status === 401) { showLogin(); toast('登录已过期，请重新登录。', true); return true; }
    if (status === 423 && selected()) { markWorkspaceLocked(state.selectedId); toast(message, true); return true; }
    if (status === 404 && state.authenticated) refreshWorkspaces(false).catch(() => {});
    return false;
  },
});

function toast(message, error = false) {
  const element = document.createElement('div');
  element.className = `toast ${error ? 'error' : ''}`;
  element.innerHTML = `${icon(error ? 'info' : 'check')}<span>${esc(message)}</span>`;
  document.querySelector('#toasts').append(element);
  setTimeout(() => element.remove(), error ? 7000 : 4000);
}

async function api(path, options = {}) {
  const { method = 'GET', body, csrfToken = state.csrf } = options;
  const generation = state.generation;
  const workspaceId = path.match(/^\/api\/workspaces\/([^/?]+)/)?.[1];
  const response = await fetch(path, {
    method, credentials: 'same-origin', cache: 'no-store',
    headers: { ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}), ...(method !== 'GET' ? { 'X-CSRF-Token': csrfToken } : {}) },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
  let data;
  try { data = await response.json(); } catch { throw new Error(`服务暂时不可用（HTTP ${response.status}）`); }
  if (generation !== state.generation) throw new Error('登录状态已改变，请重试。');
  if (!response.ok) {
    if (response.status === 401 && state.authenticated && csrfToken === state.csrf) { showLogin(); toast('登录已过期，请重新登录。', true); }
    if (response.status === 423 && workspaceId) markWorkspaceLocked(workspaceId);
    if (response.status === 404 && workspaceId && state.authenticated) refreshWorkspaces(false).catch(() => {});
    const error = new Error(typeof data.detail === 'string' ? data.detail : `请求失败（HTTP ${response.status}）`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function broadcastSessionChange() {
  try { localStorage.setItem('claude-cloud-session-event', `${Date.now()}:${Math.random()}`); } catch { /* storage is optional */ }
}

function completeSession(session) {
  const fields = session?.mode === 'solo' ? [session.csrfToken, session.user?.id] : [session?.csrfToken, session?.user?.id, session?.user?.username];
  return Boolean(session && (session.mode !== 'solo' || session.admin === true) && fields.every((value) => typeof value === 'string' && value.trim()));
}

function adoptSession(session, { broadcast = true } = {}) {
  if (!completeSession(session)) {
    showLogin({ broadcast });
    throw new Error('登录信息不完整，请重新登录。');
  }
  archiveDownload.cancel();
  soloSettings?.close();
  accountMenu.unmount(); activity.reset(); attention.reset();
  state.generation++;
  workspaceListGeneration++;
  state.csrf = session.csrfToken;
  state.user = session.user;
  state.mode = session.mode === 'solo' ? 'solo' : 'multiuser';
  state.admin = session.admin === true;
  selectedStorage = null;
  try { selectedStorage = localStorage.getItem(`claude-cloud-workspace:${state.user.id}`); } catch { /* storage is optional */ }
  if (broadcast) broadcastSessionChange();
}

function clearWorkspacePrivate(id) {
  archiveDownload.cancel(id);
  if (activity.context?.id === id) activity.unmount();
  if (chat.workspaceId === id) chat.unmount();
  chat.drafts.delete(id);
  const attachments = chat.attachments;
  for (const item of attachments.list(id)) attachments.release(item);
  attachments.drafts.delete(id);
  attachments.clearPreviews();
  if (state.selectedId === id) {
    fileRequestGeneration++;
    openFileRequestGeneration++;
    state.filePath = '';
    state.entries = [];
    state.selectedFile = null;
    state.dirty = false;
    state.fileLoading = false;
  }
}

function markWorkspaceLocked(id) {
  const workspace = state.workspaces.find((item) => item.id === id);
  if (!workspace) return;
  workspaceListGeneration++;
  workspace.locked = true;
  clearWorkspacePrivate(id);
  renderWorkspaceList();
  if (state.selectedId === id) renderLockedWorkspace();
}

function renderLockedWorkspace() {
  const workspace = selected();
  if (!workspace) return;
  clearWorkspacePrivate(workspace.id);
  document.querySelector('#header-workspace').textContent = workspace.name;
  const content = document.querySelector('#dashboard-content');
  if (content.querySelector('#workspace-unlock-form')?.dataset.workspace === workspace.id) return;
  content.innerHTML = lockedWorkspaceMarkup(workspace);
  const form = content.querySelector('#workspace-unlock-form');
  form.dataset.workspace = workspace.id;
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const id = workspace.id, generation = state.generation;
    const input = form.querySelector('input');
    const button = form.querySelector('button');
    const password = input.value;
    input.value = '';
    button.disabled = true;
    form.querySelector('.form-error').textContent = '';
    try {
      const result = await api(`/api/workspaces/${encodeURIComponent(id)}/unlock`, { method: 'POST', body: { password } });
      if (generation !== state.generation || state.selectedId !== id || !state.authenticated) return;
      workspaceListGeneration++;
      Object.assign(selected(), result);
      renderWorkspaceList();
      renderWorkspace();
      if (!selected()?.locked && selected()?.running) await loadFiles();
      toast('工作区已解锁');
    } catch (error) { if (form.isConnected) form.querySelector('.form-error').textContent = error.message; }
    finally { button.disabled = false; }
  });
}

async function lockWorkspace() {
  if (isSolo()) return;
  const workspace = selected();
  if (!workspace?.passwordProtected || workspace.locked || state.busy || !(await mayDiscard())) return;
  const id = workspace.id;
  state.busy = true;
  // Hide local content before waiting on the server, including pending responses.
  markWorkspaceLocked(id);
  try {
    const result = await api(`/api/workspaces/${encodeURIComponent(id)}/lock`, { method: 'POST' });
    const current = state.workspaces.find((item) => item.id === id);
    if (current) Object.assign(current, result);
    toast('工作区已锁定，正在运行的任务会继续');
  } catch (error) { toast(error.message, true); }
  finally { state.busy = false; }
}

async function changeWorkspacePassword() {
  if (isSolo()) return;
  const workspace = selected();
  if (!workspace || workspace.locked || state.busy || !(await mayDiscard())) return;
  const id = workspace.id, generation = state.generation;
  const body = await workspacePasswordForm({ protectedWorkspace: workspace.passwordProtected });
  if (!body || generation !== state.generation || state.selectedId !== id) return;
  state.busy = true;
  updateWorkspaceControls();
  try {
    const result = await api(`/api/workspaces/${encodeURIComponent(id)}/password`, { method: 'PUT', body });
    if (state.selectedId !== id || generation !== state.generation) return;
    workspaceListGeneration++;
    const current = selected();
    Object.assign(current, result);
    renderWorkspaceList();
    renderWorkspace();
    if (current.running && !current.locked) await loadFiles();
    toast(current.passwordProtected ? '工作区密码已更新' : '已取消工作区额外密码');
  } catch (error) { toast(error.message, true); }
  finally { if (generation === state.generation) { state.busy = false; updateWorkspaceControls(); } }
}

function modal({ title, text = '', label, initial = '', placeholder = '', submit = '确认', danger = false, validate }) {
  return new Promise((resolve) => {
    const dialog = document.createElement('dialog');
    dialog.className = 'modal';
    dialog.innerHTML = `<form method="dialog"><div class="modal-heading"><h2>${esc(title)}</h2><button type="button" class="icon-button" data-dismiss aria-label="关闭">${icon('close')}</button></div>${text ? `<p class="modal-description">${esc(text)}</p>` : ''}${label ? `<label class="field-label" for="modal-input">${esc(label)}</label><input id="modal-input" class="text-input" type="text" maxlength="200" value="${esc(initial)}" placeholder="${esc(placeholder)}" autocomplete="off" required>` : ''}<p class="form-error" role="alert"></p><div class="modal-footer"><button type="button" class="button secondary" data-dismiss>取消</button><button class="button ${danger ? 'danger' : 'primary'}" type="submit">${esc(submit)}</button></div></form>`;
    document.body.append(dialog);
    let result = null;
    dialog.querySelectorAll('[data-dismiss]').forEach((button) => button.addEventListener('click', () => dialog.close()));
    dialog.querySelector('form').addEventListener('submit', (event) => {
      event.preventDefault();
      const value = label ? dialog.querySelector('input').value.trim() : true;
      const error = validate?.(value);
      if (error) { dialog.querySelector('.form-error').textContent = error; return; }
      result = value;
      dialog.close();
    });
    dialog.addEventListener('close', () => { dialog.remove(); resolve(result); });
    dialog.showModal();
    (dialog.querySelector('input') || dialog.querySelector('.modal-footer button')).focus();
  });
}

async function mayDiscard() {
  return !state.dirty || Boolean(await modal({ title: '放弃未保存的修改？', text: '当前文件的修改还没有保存。离开后，这些修改将丢失。', submit: '放弃修改', danger: true }));
}

function showLogin({ broadcast = true } = {}) {
  archiveDownload.cancel();
  soloSettings?.close();
  accountMenu.unmount(); activity.reset(); attention.reset();
  const wasAuthenticated = state.authenticated;
  state.generation++;
  workspaceListGeneration++;
  chat.unmount({ forgetDrafts: true, forgetAttachments: true });
  clearInterval(pollTimer);
  state.authenticated = false;
  if (broadcast && wasAuthenticated) broadcastSessionChange();
  state.user = null;
  state.admin = false;
  state.csrf = '';
  state.workspaces = [];
  state.selectedId = null;
  selectedStorage = null;
  state.entries = [];
  state.filePath = '';
  state.status = null;
  state.busy = false;
  fileRequestGeneration++;
  openFileRequestGeneration++;
  state.dirty = false;
  state.selectedFile = null;
  document.querySelectorAll('dialog').forEach((dialog) => dialog.close());
  app.innerHTML = `<main class="login-page"><a class="brand login-brand" href="/">${mark}<span>Claude Cloud</span></a><section class="login-panel"><div class="login-card"><header class="login-welcome"><h1>回到你的工作台</h1><p>${isSolo() ? '输入网站密码，进入你的独享空间。' : '使用管理员为你创建的账号登录。'}</p></header><form id="login-form">${isSolo() ? '' : '<label class="field-label" for="username">用户名</label><input class="text-input" id="username" name="username" type="text" autocomplete="username" maxlength="32" required>'}<label class="field-label" for="password">${isSolo() ? '网站密码' : '账号密码'}</label><div class="password-wrap"><input class="text-input" id="password" name="password" type="password" autocomplete="current-password" required><button class="text-button password-toggle" type="button" data-action="toggle-password">显示</button></div><p id="login-error" class="form-error" role="alert"></p><button class="button primary login-submit" type="submit"><span>进入工作台</span>${icon('arrow')}</button></form><div class="login-note">${icon('info')}<p>${isSolo() ? '你的 Claude、文件和设置，都在这里。' : '账号由管理员创建。每个账号只显示自己的工作区。'}</p></div></div></section><footer><div class="login-panel-foot">你的服务器 · 你的工作区${isSolo() ? '' : ' · <a href="/admin/">管理员入口</a>'}</div><p class="login-footnote">自托管工作台 · 非 Anthropic 官方产品</p></footer></main>`;
  document.querySelector('#login-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = event.currentTarget.querySelector('button[type="submit"]');
    const password = document.querySelector('#password');
    button.disabled = true;
    document.querySelector('#login-error').textContent = '';
    try {
      const session = await api('/api/login', { method: 'POST', body: { ...(isSolo() ? {} : { username: document.querySelector('#username').value.trim().toLowerCase() }), password: password.value } });
      password.value = '';
      adoptSession(session);
      await showDashboard();
    } catch (error) { document.querySelector('#login-error')?.replaceChildren(document.createTextNode(error.message)); }
    finally { button.disabled = false; }
  });
}

async function showDashboard() {
  if (!completeSession({ user: state.user, csrfToken: state.csrf, mode: state.mode, admin: state.admin })) {
    showLogin();
    return;
  }
  state.authenticated = true;
  app.innerHTML = `<div class="workspace-app"><aside class="sidebar" id="sidebar"><div class="sidebar-brand"><a class="brand" href="/">${mark}<span>Claude Cloud</span></a><button class="icon-button mobile-only" data-action="close-sidebar" aria-label="关闭侧栏">${icon('close')}</button></div><div class="sidebar-label"><span>工作区</span><span class="workspace-count" id="workspace-count">0</span></div><button class="new-workspace" data-action="create-workspace">${icon('plus')} 新建工作区</button><nav class="workspace-list" id="workspace-list" aria-label="工作区列表"><div class="list-loading">正在读取工作区…</div></nav><div class="sidebar-bottom"><div class="isolation-note">${icon('shield')}<div><strong>独立空间，专注工作</strong><p>仅访问网页创建的工作区<br>与对应的 Claude 客户端</p></div></div><button class="account-button" id="account-button" aria-label="账号与使用额度"><span class="avatar">${isSolo() ? '✳' : 'U'}</span><span>${isSolo() ? '菜单与用量' : '个人工作台'}<small>${isSolo() ? '个人独享' : esc(state.user.username)}</small></span>${icon('down')}</button></div></aside><button class="sidebar-scrim" data-action="close-sidebar" aria-label="关闭侧栏"></button><main class="main"><header class="topbar"><button class="icon-button mobile-only" data-action="open-sidebar" aria-label="打开侧栏">${icon('menu')}</button><div class="breadcrumb">工作台 <span>/</span> <strong id="header-workspace">概览</strong></div>${isSolo() ? '<button type="button" class="button secondary small solo-menu-shortcut" data-action="solo-menu">菜单</button>' : ''}<div class="server-status" id="server-status" role="status"><span class="status-dot neutral"></span>正在连接</div></header><div id="dashboard-content"></div><footer class="app-footer"><span>CLAUDE CLOUD</span><span>自托管 · 独立工作区</span></footer></main></div>`;
  accountMenu.mount(document.querySelector('#account-button'));
  attention.attach(document);
  await Promise.all([refreshWorkspaces(true), refreshStatus()]);
  if (isSolo() && location.hash === '#settings') { history.replaceState(null, '', location.pathname); openSoloSettings('claude'); }
  clearInterval(pollTimer);
  let ticks = 0;
  pollTimer = setInterval(() => {
    if (!state.authenticated || document.visibilityState === 'hidden') return;
    refreshWorkspaces(false).catch(() => {});
    if (++ticks % 4 === 0) refreshStatus().catch(() => {});
  }, 15000);
}

async function refreshStatus() {
  try {
    state.status = await api('/api/status');
    const ready = state.status.docker && state.status.workerImage && !state.status.error;
    const node = document.querySelector('#server-status');
    if (node) {
      node.innerHTML = `<span class="status-dot ${ready ? '' : 'warning'}"></span>${ready ? '服务器已就绪' : '需要配置服务器'}`;
      node.title = state.status.error || (ready ? 'Docker 和 Claude 镜像已就绪' : '请检查 Docker 服务和 Claude 工作区镜像');
    }
    renderWarning();
  } catch (error) {
    const node = document.querySelector('#server-status');
    if (node) { node.innerHTML = '<span class="status-dot warning"></span>服务器连接失败'; node.title = error.message; }
  }
}

function renderWarning() {
  const warning = document.querySelector('#server-warning');
  if (!warning) return;
  const unavailable = state.status && (!state.status.docker || !state.status.workerImage || state.status.error || state.status.claudeConfigured === false);
  warning.hidden = !unavailable;
  const needsClaude = isSolo() && state.status?.claudeAuthenticated === false;
  warning.hidden = !unavailable && !needsClaude;
  warning.innerHTML = needsClaude ? '首次使用，请先连接你的 Claude。 <button class="button primary small" type="button" data-action="solo-settings" data-section="claude">连接 Claude</button>' : unavailable ? (isSolo() ? '服务器暂未就绪，请稍后重试或查看部署结果。' : '服务暂未就绪，请联系管理员完成配置后再试。') : '';
}

async function refreshWorkspaces(initial = false) {
  if (state.busy && !initial) return;
  const requestGeneration = ++workspaceListGeneration;
  try {
    const data = await api('/api/workspaces');
    if (!state.authenticated || requestGeneration !== workspaceListGeneration) return;
    const previouslyRunning = selected()?.running;
    const previouslyLocked = selected()?.locked;
    const previous = state.workspaces;
    state.workspaces = data.workspaces;
    for (const workspace of previous) {
      const next = state.workspaces.find((item) => item.id === workspace.id);
      if (!next || next.locked) clearWorkspacePrivate(workspace.id);
    }
    renderWorkspaceList();
    if (initial || (state.selectedId && !selected())) {
      const target = state.workspaces.find((item) => item.id === selectedStorage) || state.workspaces[0];
      state.selectedId = null;
      if (target) await selectWorkspace(target.id, true); else renderEmpty();
    } else {
      if (selected()?.locked) renderLockedWorkspace();
      else if (previouslyLocked && selected()) { renderWorkspace(); if (selected()?.running) await loadFiles(); }
      else updateWorkspaceControls();
      if (previouslyRunning !== selected()?.running) {
        if (selected()?.running && !selected()?.locked) await loadFiles(); else renderFileList();
      }
    }
  } catch (error) {
    if (initial && state.authenticated) {
      document.querySelector('#workspace-list').innerHTML = `<p class="sidebar-error">${esc(error.message)}</p><button class="button secondary" data-action="reload">重新连接</button>`;
      renderEmpty();
      toast(error.message, true);
    } else throw error;
  }
}

function renderWorkspaceList() {
  const list = document.querySelector('#workspace-list');
  if (!list) return;
  document.querySelector('#workspace-count').textContent = state.workspaces.length;
  list.innerHTML = state.workspaces.length ? state.workspaces.map((item) => `<button class="workspace-item ${item.id === state.selectedId ? 'active' : ''}" data-action="select-workspace" data-id="${esc(item.id)}" ${item.id === state.selectedId ? 'aria-current="page"' : ''}>${icon(item.locked ? 'lock' : 'folder')}<span class="workspace-item-copy"><strong>${esc(item.name)}</strong><small>${item.locked ? '已锁定' : item.running ? '工作区运行中' : '已停止'}${item.passwordProtected && !item.locked ? ' · 密码保护' : ''}</small></span><span class="status-dot ${item.running ? '' : 'neutral'}"></span></button>`).join('') : '<div class="sidebar-empty">给下一个想法<br>留一个独立空间。</div>';
}

function renderEmpty() {
  archiveDownload.cancel();
  activity.unmount();
  chat.unmount();
  state.selectedId = null;
  document.querySelector('#header-workspace').textContent = '概览';
  document.querySelector('#dashboard-content').innerHTML = `<div class="empty-dashboard"><div class="eyebrow">A LITTLE SPACE TO CREATE</div><h1>从一个工作区开始。</h1><p>给项目一个名字，剩下的交给你和 Claude。<br>文件和对话，都在同一个地方。</p><button class="button primary" data-action="create-workspace">${icon('plus')} 创建第一个工作区</button><div class="empty-grid"><div><span class="feature-icon">${icon('folder')}</span><h3>文件有自己的位置</h3><p>浏览、编辑与上传文件，<br>始终留在当前工作区。</p></div><div><span class="feature-icon">${icon('terminal')}</span><h3>熟悉的 Claude Code</h3><p>中文输入，中文回复，<br>在网页中完成项目。</p></div><div><span class="feature-icon">${icon('cloud')}</span><h3>换个地方，接着做</h3><p>关闭网页后进程继续，<br>回来时重新连接即可。</p></div></div><p id="server-warning" class="server-warning" hidden></p></div>`;
  renderWarning();
}

async function selectWorkspace(id, force = false) {
  if (state.busy && !force) { toast('正在处理工作区操作，请稍候。'); return; }
  if (id === state.selectedId) { closeSidebar(); return; }
  if (!force && !(await mayDiscard())) return;
  archiveDownload.cancel();
  activity.unmount();
  chat.unmount();
  state.selectedId = id;
  state.filePath = '';
  fileRequestGeneration++;
  openFileRequestGeneration++;
  state.fileLoading = false;
  state.entries = [];
  state.selectedFile = null;
  state.dirty = false;
  state.view = 'terminal';
  try { if (state.user?.id) localStorage.setItem(`claude-cloud-workspace:${state.user.id}`, id); } catch { /* storage is optional */ }
  selectedStorage = id;
  renderWorkspaceList();
  renderWorkspace();
  closeSidebar();
  if (selected()?.running && !selected()?.locked) await loadFiles();
}

function renderWorkspace() {
  const workspace = selected();
  if (!workspace) return;
  if (workspace.locked) { renderLockedWorkspace(); return; }
  document.querySelector('#header-workspace').textContent = workspace.name;
  document.querySelector('#dashboard-content').innerHTML = `<div class="workspace-content">
    <section class="workspace-heading"><h1 title="${esc(workspace.name)}">${esc(workspace.name)}</h1><div class="workspace-actions">
      <button type="button" class="button secondary small files-toggle" id="files-toggle" data-action="toggle-files" aria-expanded="false" aria-controls="workspace-files">${icon('folder')}<span>工作区文件</span></button>
      <button type="button" class="button secondary small archive-download-button" id="archive-download" data-action="download-archive" aria-describedby="archive-hint">${icon('download')}<span>下载工作区 ZIP</span></button>
      <button type="button" class="button secondary small focus-toggle" id="focus-toggle" data-action="toggle-focus" aria-pressed="false">${icon('expand')}<span>专注模式</span></button>
      ${isSolo() ? '' : `<button type="button" class="button secondary small workspace-password-button" data-action="workspace-password">${icon('lock')}<span>${workspace.passwordProtected ? '修改密码' : '设置密码'}</span></button>`}
      ${!isSolo() && workspace.passwordProtected ? '<button type="button" class="text-button" data-action="lock-workspace">锁定</button>' : ''}
      <button type="button" class="text-button conversation-stop" id="chat-stop" data-action="stop-chat" disabled>停止会话</button>
      <button id="workspace-power" class="button secondary small" data-action="workspace-power"></button>
      <button class="icon-button danger-quiet" data-action="delete-workspace" title="删除工作区" aria-label="删除工作区">${icon('trash')}</button>
    </div></section>
    <p id="archive-hint" class="archive-hint" role="status" aria-live="polite"></p>
    <p id="server-warning" class="server-warning" hidden></p>
    <div class="panels" data-view="terminal">
      <div class="conversation-column" data-mode="chat"><section class="chat-panel" id="chat-panel" aria-label="中文聊天"></section></div>
      <section class="files-panel" id="workspace-files" aria-label="工作区文件"><div class="panel-header"><div class="panel-title">${icon('folder')}<h2>工作区文件</h2></div><div class="file-panel-actions"><button class="icon-button" data-action="refresh-files" title="刷新文件" aria-label="刷新文件">${icon('refresh')}</button><button class="icon-button" data-action="view-terminal" title="收起文件" aria-label="收起文件">${icon('close')}</button></div></div>
        <div class="file-breadcrumb" id="file-breadcrumb"></div><div class="file-toolbar"><button class="text-button" data-action="new-file">${icon('plus')} 文件</button><button class="text-button" data-action="new-folder">${icon('folder')} 文件夹</button><button class="text-button" data-action="upload">${icon('upload')} 上传</button><input type="file" id="upload-input" multiple hidden></div><div class="file-list" id="file-list"></div><div class="file-editor" id="file-editor" hidden></div><div class="files-footer">${icon('shield')} 仅当前工作区 · 单文件最多 10 MiB</div>
      </section>
    </div>
  </div>`;
  updateFocusControl();
  document.querySelector('#upload-input').addEventListener('change', uploadFiles);
  chat.mount(document.querySelector('#chat-panel'), workspace.id);
  updateWorkspaceControls();
  renderFileList();
  renderWarning();
}

function updateWorkspaceControls() {
  const workspace = selected();
  const button = document.querySelector('#workspace-power');
  if (!workspace || !button) return;
  const activityHost = document.querySelector('#dashboard-content');
  const activityKey = `${state.generation}:${state.user?.id}:${workspace.id}`;
  if (workspace.running && !workspace.locked) {
    if (activity.context?.key !== activityKey || activity.host !== activityHost) activity.mount(activityHost, { id: workspace.id, key: activityKey });
  } else activity.unmount();
  button.innerHTML = `${icon(workspace.running ? 'stop' : 'play')}${workspace.running ? '停止工作区' : '启动工作区'}`;
  button.disabled = state.busy;
  document.querySelectorAll('[data-action="workspace-password"], [data-action="lock-workspace"]').forEach((item) => { item.disabled = state.busy; });
  document.querySelectorAll('.file-toolbar button').forEach((item) => { item.disabled = !workspace.running || state.busy; });
  if (!workspace.running || workspace.locked) archiveDownload.cancel(workspace.id);
  updateArchiveControls();
  updateConversationControls();
  chat.controls();
}

function updateArchiveControls() {
  const button = document.querySelector('#archive-download');
  const hint = document.querySelector('#archive-hint');
  if (!button || !hint) return;
  const workspace = selected();
  const stopped = !workspace?.running;
  button.disabled = stopped || Boolean(workspace?.locked) || state.busy || archiveDownload.busy;
  button.setAttribute('aria-busy', String(archiveDownload.busy));
  button.querySelector('span').textContent = archiveDownload.busy ? '正在打包并下载…' : '下载工作区 ZIP';
  button.title = stopped ? '请先启动工作区，再下载 ZIP；无需启动 Claude 会话。' : '下载当前工作区内所有已保存文件';
  hint.textContent = archiveDownload.busy ? '正在打包并下载… 文件较多时请稍候。' : stopped
    ? '请先启动工作区再下载 ZIP，无需启动 Claude 会话。'
    : `ZIP ${ARCHIVE_LIMIT_HINT}${state.dirty ? '；编辑器有未保存修改，请先保存。' : '。'}`;
}

function updateConversationControls() {
  const stop = document.querySelector('#chat-stop');
  if (stop) stop.disabled = state.busy || !selected()?.chatActive;
}

async function loadFiles(path = state.filePath) {
  const workspace = selected();
  if (!workspace?.running || workspace.locked) { renderFileList(); return; }
  const id = workspace.id;
  const generation = ++fileRequestGeneration;
  state.fileLoading = true;
  renderFileList();
  try {
    const data = await api(workspaceUrl(`/files?path=${encodeURIComponent(path)}`));
    if (state.selectedId !== id || generation !== fileRequestGeneration) return;
    state.filePath = data.path;
    state.entries = data.entries;
  } catch (error) { toast(error.message, true); }
  finally { if (state.selectedId === id && generation === fileRequestGeneration) { state.fileLoading = false; renderFileList(); } }
}

function renderFileList() {
  const list = document.querySelector('#file-list');
  if (!list) return;
  const pieces = state.filePath.split('/').filter(Boolean);
  document.querySelector('#file-breadcrumb').innerHTML = `<button class="text-button" data-action="navigate-files" data-path="" title="工作区根目录">${icon('home')}<span>workspace</span></button>${pieces.map((piece, index) => `${icon('chevron')}<button class="text-button" data-action="navigate-files" data-path="${esc(pieces.slice(0, index + 1).join('/'))}">${esc(piece)}</button>`).join('')}`;
  if (!selected()?.running) { list.innerHTML = `<div class="files-empty">${icon('folder')}<p>工作区已停止</p><small>启动工作区后即可浏览文件。</small><button class="text-button" data-action="workspace-power">启动工作区 ${icon('arrow')}</button></div>`; return; }
  if (state.fileLoading) { list.innerHTML = '<div class="files-empty"><span class="spinner"></span><p>正在读取文件…</p></div>'; return; }
  const sorted = [...state.entries].sort((a, b) => (a.type !== b.type ? (a.type === 'directory' ? -1 : b.type === 'directory' ? 1 : 0) : a.name.localeCompare(b.name, 'zh-CN')));
  list.innerHTML = (pieces.length ? `<button class="file-row parent" data-action="navigate-files" data-path="${esc(pieces.slice(0, -1).join('/'))}">${icon('folder')}<span>..</span><small>上级目录</small></button>` : '') + (sorted.length ? sorted.map((entry) => `<div class="file-row-wrap"><button class="file-row ${state.selectedFile?.path === fileJoin(entry.name) ? 'selected' : ''}" data-action="open-entry" data-name="${esc(entry.name)}" ${entry.type === 'blocked' ? 'disabled title="不允许访问符号链接或特殊文件"' : ''}>${icon(entry.type === 'directory' ? 'folder' : entry.type === 'blocked' ? 'lock' : 'file')}<span>${esc(entry.name)}</span><small>${entry.type === 'directory' ? icon('chevron') : entry.type === 'blocked' ? '受限' : formatSize(entry.size)}</small></button><button class="file-delete icon-button danger-quiet" data-action="delete-file" data-name="${esc(entry.name)}" title="删除 ${esc(entry.name)}" aria-label="删除 ${esc(entry.name)}" ${entry.type === 'blocked' ? 'disabled' : ''}>${icon('trash')}</button></div>`).join('') : `<div class="files-empty">${icon('folder')}<p>这里还是一张白纸</p><small>新建或上传文件，<br>也可以让 Claude 从零开始。</small></div>`);
}

function formatSize(size = 0) { return size < 1024 ? `${size} B` : size < 1024 * 1024 ? `${(size / 1024).toFixed(1)} KB` : `${(size / 1024 / 1024).toFixed(1)} MB`; }

async function openEntry(name) {
  const entry = state.entries.find((item) => item.name === name);
  if (!entry || entry.type === 'blocked') return;
  if (entry.type === 'directory') { await loadFiles(fileJoin(name)); return; }
  if (!(await mayDiscard())) return;
  const id = state.selectedId;
  const generation = ++openFileRequestGeneration;
  try {
    const file = await api(workspaceUrl(`/file?path=${encodeURIComponent(fileJoin(name))}`));
    if (state.selectedId !== id || generation !== openFileRequestGeneration) return;
    state.selectedFile = file;
    state.dirty = false;
    renderEditor();
    renderFileList();
  } catch (error) { toast(error.message, true); }
}

function renderEditor() {
  const editor = document.querySelector('#file-editor');
  if (!editor || selected()?.locked) return;
  updateArchiveControls();
  const file = state.selectedFile;
  editor.hidden = !file;
  if (!file) { editor.innerHTML = ''; return; }
  const binary = file.encoding === 'base64';
  editor.innerHTML = `<div class="editor-heading"><div>${icon('file')}<span title="${esc(file.path)}">${esc(file.path.split('/').pop())}</span><i id="dirty-indicator" hidden aria-label="未保存"></i></div><button class="icon-button" data-action="close-editor" title="关闭编辑器" aria-label="关闭编辑器">${icon('close')}</button></div>${binary ? `<div class="binary-preview">${icon('file')}<p>二进制文件</p><small>${formatSize(file.size)} · 下载后查看</small></div>` : `<label class="sr-only" for="file-content">文件内容</label><textarea id="file-content" spellcheck="false" autocapitalize="off" autocomplete="off" autocorrect="off">${esc(file.content)}</textarea>`}<div class="editor-footer"><span>${binary ? 'BINARY' : 'UTF-8'}</span><div><button class="text-button" data-action="download-file">${icon('download')} 下载当前文件</button>${!binary ? `<button id="save-file" class="button primary small" data-action="save-file" disabled>${icon('save')} 保存</button>` : ''}</div></div>`;
  editor.querySelector('textarea')?.addEventListener('input', () => {
    state.dirty = editor.querySelector('textarea').value !== state.selectedFile.content;
    document.querySelector('#dirty-indicator').hidden = !state.dirty;
    document.querySelector('#save-file').disabled = !state.dirty;
    updateArchiveControls();
  });
  editor.querySelector('textarea')?.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === 's') { event.preventDefault(); saveFile(); }
    if (event.key === 'Tab') { event.preventDefault(); const input = event.target; const start = input.selectionStart; input.setRangeText('  ', start, input.selectionEnd, 'end'); input.dispatchEvent(new Event('input')); }
  });
}

async function saveFile() {
  const id = state.selectedId;
  const file = state.selectedFile;
  const input = document.querySelector('#file-content');
  if (!file || !input || !state.dirty) return;
  const content = input.value;
  if (new TextEncoder().encode(content).length > maxBytes) { toast('文件超过 10 MiB，无法保存。', true); return; }
  const button = document.querySelector('#save-file');
  button.disabled = true;
  try {
    await api(workspaceUrl('/file'), { method: 'PUT', body: { path: file.path, content, encoding: 'utf-8' } });
    file.content = content;
    file.size = new TextEncoder().encode(content).length;
    if (state.selectedId !== id || state.selectedFile !== file) { toast('文件已保存'); return; }
    state.dirty = input.value !== content;
    document.querySelector('#dirty-indicator').hidden = !state.dirty;
    updateArchiveControls();
    toast('文件已保存');
    await loadFiles();
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = !state.dirty; }
}

function downloadFile() {
  const file = state.selectedFile;
  if (!file) return;
  let content = file.content;
  if (file.encoding === 'base64') content = Uint8Array.from(atob(content), (character) => character.charCodeAt(0));
  else if (state.dirty) content = document.querySelector('#file-content').value;
  const url = URL.createObjectURL(new Blob([content], { type: 'application/octet-stream' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = file.path.split('/').pop();
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function validateFilename(name) {
  if (!name || ['.', '..'].includes(name) || /[/\\\u0000-\u001f]/.test(name)) return '请输入有效的名称，不要包含路径分隔符。';
  if (name.length > 200) return '名称最多 200 个字符。';
  return '';
}

async function createFileOrFolder(folder = false) {
  if (!selected()?.running) return;
  const name = await modal({ title: folder ? '新建文件夹' : '新建文件', label: folder ? '文件夹名称' : '文件名称', placeholder: folder ? '例如：src' : '例如：README.md', submit: '创建', validate: (value) => validateFilename(value) || (state.entries.some((item) => item.name === value) ? '该名称已存在。' : '') });
  if (!name) return;
  const id = state.selectedId;
  try {
    await api(workspaceUrl(folder ? '/directory' : '/file'), { method: folder ? 'POST' : 'PUT', body: folder ? { path: fileJoin(name) } : { path: fileJoin(name), content: '', encoding: 'utf-8' } });
    toast(folder ? '文件夹已创建' : '文件已创建');
    if (state.selectedId !== id) return;
    await loadFiles();
    if (!folder) await openEntry(name);
  } catch (error) { toast(error.message, true); }
}

async function uploadFiles(event) {
  const files = [...event.target.files];
  event.target.value = '';
  if (!files.length || !selected()?.running || selected()?.locked) return;
  const id = state.selectedId;
  const generation = state.generation;
  const path = state.filePath;
  let uploaded = 0;
  for (const file of files) {
    if (state.selectedId !== id || generation !== state.generation || selected()?.locked || !state.authenticated) break;
    if (file.size > maxBytes) { toast(`${file.name} 超过 10 MiB，已跳过。`, true); continue; }
    if (validateFilename(file.name)) { toast(`${file.name} 的名称不受支持。`, true); continue; }
    if (state.entries.some((entry) => entry.name === file.name) && !(await modal({ title: '覆盖已有文件？', text: `“${file.name}” 已存在。上传将替换它的内容。`, submit: '覆盖文件', danger: true }))) continue;
    try {
      const bytes = new Uint8Array(await file.arrayBuffer());
      if (state.selectedId !== id || generation !== state.generation || selected()?.locked || !state.authenticated) break;
      let binary = '';
      for (let index = 0; index < bytes.length; index += 16384) binary += String.fromCharCode(...bytes.subarray(index, index + 16384));
      await api(`/api/workspaces/${encodeURIComponent(id)}/file`, { method: 'PUT', body: { path: path ? `${path}/${file.name}` : file.name, content: btoa(binary), encoding: 'base64' } });
      uploaded++;
    } catch (error) { toast(`${file.name}：${error.message}`, true); }
  }
  if (uploaded) toast(`已上传 ${uploaded} 个文件`);
  if (state.selectedId === id) await loadFiles();
}

async function deleteFile(name) {
  const id = state.selectedId;
  const generation = state.generation;
  const entry = state.entries.find((item) => item.name === name);
  if (!entry || entry.type === 'blocked') return;
  const path = fileJoin(name);
  const confirmed = await modal({ title: entry.type === 'directory' ? '删除这个文件夹？' : '删除这个文件？', text: `“${name}”${entry.type === 'directory' ? '及其中的所有文件' : ''}将被永久删除，无法撤销。`, submit: '永久删除', danger: true });
  if (!confirmed || state.selectedId !== id || generation !== state.generation || selected()?.locked) return;
  try {
    await api(`/api/workspaces/${encodeURIComponent(id)}/file?path=${encodeURIComponent(path)}`, { method: 'DELETE' });
    if (state.selectedId !== id) { toast('已删除'); return; }
    if (state.selectedFile && (state.selectedFile.path === path || state.selectedFile.path.startsWith(`${path}/`))) { state.selectedFile = null; state.dirty = false; renderEditor(); }
    await loadFiles();
    toast('已删除');
  } catch (error) { toast(error.message, true); }
}

async function createWorkspace() {
  if (state.busy) return;
  if (!(await mayDiscard())) return;
  const payload = isSolo() ? await createSoloWorkspaceForm() : await workspacePasswordForm({ create: true });
  if (!payload) return;
  state.busy = true;
  document.querySelectorAll('[data-action="create-workspace"]').forEach((button) => { button.disabled = true; });
  try {
    const workspace = await api('/api/workspaces', { method: 'POST', body: payload });
    state.workspaces.push(workspace);
    await selectWorkspace(workspace.id, true);
    toast('工作区已创建，可以开始中文对话。');
  } catch (error) { toast(error.message, true); await refreshWorkspaces(false).catch(() => {}); }
  finally { state.busy = false; document.querySelectorAll('[data-action="create-workspace"]').forEach((button) => { button.disabled = false; }); updateWorkspaceControls(); }
}

async function powerWorkspace() {
  const workspace = selected();
  if (!workspace || workspace.locked || state.busy) return;
  const id = workspace.id, generation = state.generation;
  if (workspace.running) {
    if (!(await mayDiscard())) return;
    if (!(await modal({ title: '停止工作区？', text: '这会终止当前 Claude 会话和正在进行的任务。已保存的文件会保留。', submit: '停止工作区', danger: true }))) return;
  }
  if (state.selectedId !== id || generation !== state.generation || selected()?.locked) return;
  if (workspace.running) archiveDownload.cancel(id);
  state.busy = true;
  updateWorkspaceControls();
  try {
    const wasRunning = workspace.running;
    const result = await api(`/api/workspaces/${encodeURIComponent(id)}${wasRunning ? '/stop' : '/start'}`, { method: 'POST' });
    if (state.selectedId !== id || generation !== state.generation) return;
    workspaceListGeneration++;
    Object.assign(selected(), result);
    if (wasRunning) { state.selectedFile = null; state.dirty = false; renderWorkspace(); }
    else await loadFiles();
    renderWorkspaceList();
    toast(wasRunning ? '工作区已停止，文件已保留' : '工作区已启动');
  } catch (error) { toast(error.message, true); }
  finally { state.busy = false; updateWorkspaceControls(); }
}

async function deleteWorkspace() {
  const workspace = selected();
  if (!workspace || workspace.locked || state.busy) return;
  const id = workspace.id, generation = state.generation;
  const name = await modal({ title: '永久删除工作区？', text: `工作区“${workspace.name}”的全部文件和会话将被删除。此操作无法撤销。`, label: `输入工作区名称“${workspace.name}”以确认`, submit: '永久删除', danger: true, validate: (value) => value !== workspace.name ? '名称不一致，请重新输入。' : '' });
  if (!name || state.selectedId !== id || generation !== state.generation || selected()?.locked) return;
  state.busy = true;
  try {
    await api(`/api/workspaces/${encodeURIComponent(id)}`, { method: 'DELETE' });
    clearWorkspacePrivate(workspace.id);
    state.workspaces = state.workspaces.filter((item) => item.id !== workspace.id);
    state.selectedId = null;
    state.selectedFile = null;
    state.dirty = false;
    renderWorkspaceList();
    if (state.workspaces.length) await selectWorkspace(state.workspaces[0].id, true); else renderEmpty();
    toast('工作区已永久删除');
  } catch (error) { toast(error.message, true); }
  finally { state.busy = false; updateWorkspaceControls(); }
}

function closeSidebar() { document.querySelector('.workspace-app')?.classList.remove('sidebar-open'); }
function switchView(view) {
  state.view = view;
  document.querySelector('.panels').dataset.view = view;
  const toggle = document.querySelector('#files-toggle');
  toggle.setAttribute('aria-expanded', String(view === 'files'));
  toggle.querySelector('span').textContent = view === 'files' ? '收起文件' : '工作区文件';
  if (view !== 'files' && document.activeElement?.closest('.files-panel')) toggle.focus();
}

function updateFocusControl() {
  const active = document.querySelector('.workspace-app')?.classList.contains('focus-mode');
  const button = document.querySelector('#focus-toggle');
  if (button) {
    button.setAttribute('aria-pressed', String(Boolean(active)));
    button.querySelector('span').textContent = active ? '退出专注' : '专注模式';
  }
}

function openSoloSettings(section = 'claude') {
  if (!isSolo() || !state.authenticated || !state.admin) return;
  closeSidebar(); accountMenu.close();
  if (!soloSettings) soloSettings = new SoloSettings({
    api, page: document,
    getContext: () => ({ authenticated: state.authenticated, mode: state.mode, admin: state.admin, csrf: state.csrf, key: `${state.generation}:${state.user?.id}` }),
    expire: () => showLogin(),
    passwordChanged: () => { showLogin(); toast('网站密码已更新，请使用新密码登录。'); },
    changed: (status) => { if (state.status) state.status.claudeAuthenticated = status.authenticated; renderWarning(); },
  });
  soloSettings.show(section);
}

async function terminalHelp() {
  await chat.help();
}

document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-action]');
  if (!button || button.disabled) return;
  const action = button.dataset.action;
  try {
    if (action === 'toggle-password') { const input = document.querySelector('#password'); input.type = input.type === 'password' ? 'text' : 'password'; button.textContent = input.type === 'password' ? '显示' : '隐藏'; }
    else if (action === 'solo-settings') openSoloSettings(button.dataset.section);
    else if (action === 'solo-menu' && isSolo()) { closeSidebar(); accountMenu.open(); }
    else if (action === 'create-workspace') await createWorkspace();
    else if (action === 'select-workspace') await selectWorkspace(button.dataset.id);
    else if (action === 'workspace-power') await powerWorkspace();
    else if (action === 'workspace-password') await changeWorkspacePassword();
    else if (action === 'lock-workspace') await lockWorkspace();
    else if (action === 'delete-workspace') await deleteWorkspace();
    else if (action === 'stop-chat') await chat.stop();
    else if (action === 'refresh-files') await loadFiles();
    else if (action === 'navigate-files') await loadFiles(button.dataset.path);
    else if (action === 'open-entry') await openEntry(button.dataset.name);
    else if (action === 'new-file') await createFileOrFolder(false);
    else if (action === 'new-folder') await createFileOrFolder(true);
    else if (action === 'upload') document.querySelector('#upload-input').click();
    else if (action === 'save-file') await saveFile();
    else if (action === 'download-file') downloadFile();
    else if (action === 'download-archive') await archiveDownload.start();
    else if (action === 'close-editor') { if (await mayDiscard()) { state.selectedFile = null; state.dirty = false; renderEditor(); renderFileList(); } }
    else if (action === 'delete-file') await deleteFile(button.dataset.name);
    else if (action === 'terminal-help') await terminalHelp();
    else if (action === 'open-sidebar') document.querySelector('.workspace-app').classList.add('sidebar-open');
    else if (action === 'close-sidebar') closeSidebar();
    else if (action === 'toggle-files') switchView(state.view === 'files' ? 'terminal' : 'files');
    else if (action === 'toggle-focus') { document.querySelector('.workspace-app').classList.toggle('focus-mode'); updateFocusControl(); }
    else if (action === 'view-terminal') switchView('terminal');
    else if (action === 'view-files') switchView('files');
    else if (action === 'reload') await refreshWorkspaces(true);
    else if (action === 'logout') { if (await mayDiscard()) { archiveDownload.cancel(); soloSettings?.close(); await api('/api/logout', { method: 'POST' }); showLogin(); } }
  } catch (error) { toast(error.message, true); }
});

window.addEventListener('beforeunload', (event) => { if (state.dirty) { event.preventDefault(); event.returnValue = ''; } });
window.addEventListener('pagehide', () => { soloSettings?.suspend(); archiveDownload.cancel(); accountMenu.unmount(); activity.reset(); attention.reset(); });
window.addEventListener('pageshow', (event) => {
  if (event.persisted && state.authenticated) { accountMenu.mount(document.querySelector('#account-button')); attention.attach(document); updateWorkspaceControls(); }
});
window.addEventListener('storage', (event) => {
  if (event.key === 'claude-cloud-session-event' && state.authenticated) {
    showLogin({ broadcast: false });
    toast('登录状态已在另一个页面改变，请重新进入工作台。');
  }
});

async function init() {
  try {
    const session = await api('/api/session');
    state.mode = session.mode === 'solo' ? 'solo' : 'multiuser';
    if (session.authenticated) { adoptSession(session, { broadcast: false }); await showDashboard(); }
    else showLogin();
  } catch (error) { showLogin(); document.querySelector('#login-error').textContent = `无法连接工作台：${error.message}`; }
}

init();
