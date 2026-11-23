const lock = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3m4 5v3"/></svg>';

import { AdminClaudePanel } from './admin-claude.js';
import { AdminQuotasPanel } from './admin-quotas.js';
import { AccountBrowser } from './account-browser.js';

const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (s) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[s]));

class AdminError extends Error {
  constructor(message, status = 0) { super(message); this.status = status; }
}

export function passwordValidation(password, confirmation) {
  const length = Array.from(password).length;
  if (length < 12 || length > 1024) return '新密码需要 12 至 1024 个字符。';
  if (password !== confirmation) return '两次输入的新密码不一致，请重新核对。';
  return '';
}

/** Administrator credentials never enter page URLs or browser storage. */
export class AdminPage {
  constructor(host, { fetch: fetcher = (...args) => window.fetch(...args), claudeFactory = (options) => new AdminClaudePanel(options), quotaFactory = (options) => new AdminQuotasPanel(options), usageFactory = (options) => new AdminClaudePanel({ ...options, namespace: 'usage-auth' }), accountBrowserFactory = (options) => new AccountBrowser(options) } = {}) {
    this.host = host;
    this.fetch = fetcher;
    this.claudeFactory = claudeFactory;
    this.usageFactory = usageFactory;
    this.quotaFactory = quotaFactory;
    this.accountBrowserFactory = accountBrowserFactory;
    this.accountBrowser = null;
    const browserWindow = host.ownerDocument?.defaultView;
    browserWindow?.addEventListener('pagehide', () => this.accountBrowser?.suspend());
    browserWindow?.addEventListener('pageshow', (event) => {
      if (event.persisted && this.phase === 'ready' && this.csrf) this.accountBrowser?.resume();
    });
    this.quotaBusy = false;
    this.phase = 'loading';
    this.csrf = '';
    this.busy = false;
    this.generation = 0;
    this.policy = null;
    this.policyBusy = false;
    this.policyPending = null;
    this.policyMessage = '';
    this.policyFailed = false;
    this.policyMessageError = false;
    this.users = [];
    this.selectedUserIds = new Set();
    this.workspaces = [];
    this.managementLoaded = false;
    this.managementFailed = false;
    this.confirmingDelete = false;
  }

  node(id) { return this.host.querySelector(`#${id}`); }

  async request(path, { method = 'GET', body, authenticated = false, csrfToken = this.csrf } = {}) {
    let response;
    try {
      response = await this.fetch(`/api/admin/${path}`, {
        method, credentials: 'same-origin', cache: 'no-store',
        headers: {
          ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
          ...(authenticated ? { 'X-CSRF-Token': csrfToken } : {}),
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
    } catch {
      throw new AdminError('连接失败，暂时无法确认操作结果。请检查网络后重试。');
    }
    if (!response.ok) {
      const messages = {
        400: path.endsWith('/password') || path === 'user-password' ? '请确认两次新密码一致，且与管理员密码不同。' : '请求未被接受，请检查用户名和输入后重试。',
        401: authenticated ? '管理员登录已过期，请重新登录。' : '管理员密码不正确，请重试。',
        403: '登录校验失败，请刷新此页后重试。',
        404: '账号或工作区已不存在，请刷新管理列表。',
        409: path === 'users' ? '用户名已存在，请换一个用户名。' : '操作发生冲突，请刷新状态后重试。',
        422: path === 'model-policy' ? '权限设置格式不正确，请刷新状态后重试。' : '输入格式不正确，请检查用户名、密码长度和两次输入是否一致。',
        429: '尝试次数过多，请稍后再试。',
        503: path.endsWith('/password') || path === 'user-password' ? '密码保存服务暂不可用，请检查服务器存储后重试。' : path === 'model-policy' ? '模型权限暂时无法确认，请刷新状态并检查服务器后重试。' : '管理员服务暂不可用，请确认已完成服务器配置后重试。',
      };
      // Do not display response bodies, which may contain proxy or server details.
      throw new AdminError(messages[response.status] || '操作未完成，请稍后重试。', response.status);
    }
    try { return await response.json(); }
    catch { throw new AdminError('服务器返回了无法识别的结果，请刷新后重新检查。'); }
  }

  showMessage(message, success = false) {
    const node = this.node('admin-message');
    if (!node) return;
    node.textContent = message;
    node.hidden = !message;
    node.dataset.kind = success ? 'success' : 'error';
  }

  sectionMessage(id, message, success = false) {
    const node = this.node(id);
    if (!node) return;
    node.textContent = message; node.hidden = !message;
    node.dataset.kind = success ? 'success' : 'error';
  }

  controls() {
    this.host.querySelectorAll('input, button, select').forEach((node) => { node.disabled = this.busy || this.quotaBusy || this.policyBusy || this.confirmingDelete; });
    this.node('admin-card')?.setAttribute('aria-busy', String(this.busy));
    const submit = this.node('admin-submit');
    if (submit) submit.textContent = this.busy ? (this.phase === 'login' ? '正在登录…' : '正在保存…') : (this.phase === 'login' ? '登录管理员页面' : '保存新密码');
    if (this.phase === 'ready') {
      const noEditableUsers = !this.users.some((user) => !user.deleting);
      if (submit) submit.disabled = this.busy || this.quotaBusy || this.policyBusy || this.confirmingDelete || !this.managementLoaded || noEditableUsers;
      const selection = this.node('password-user');
      if (selection) selection.disabled = this.busy || this.quotaBusy || this.policyBusy || this.confirmingDelete || !this.managementLoaded || noEditableUsers;
      this.host.querySelectorAll('[data-delete-user]').forEach((node) => {
        node.disabled = this.busy || this.quotaBusy || this.policyBusy || this.confirmingDelete || !this.managementLoaded || this.managementFailed;
      });
      const selectionLocked = this.busy || this.quotaBusy || this.policyBusy || this.confirmingDelete || !this.managementLoaded || this.managementFailed;
      this.host.querySelectorAll('[data-select-user]').forEach((node) => {
        node.checked = this.selectedUserIds.has(node.dataset.selectUser);
        node.disabled = selectionLocked || (!node.checked && this.selectedUserIds.size >= 100);
      });
      const selectAll = this.node('select-all-users');
      if (selectAll) {
        selectAll.disabled = selectionLocked || !this.users.length;
        selectAll.checked = this.users.length > 0 && this.users.slice(0, 100).every((user) => this.selectedUserIds.has(user.id));
        selectAll.indeterminate = this.selectedUserIds.size > 0 && !selectAll.checked;
      }
      const deleteSelected = this.node('delete-selected-users');
      if (deleteSelected) deleteSelected.disabled = selectionLocked || !this.selectedUserIds.size;
      const selectedCount = this.node('admin-selected-user-count');
      if (selectedCount) selectedCount.textContent = `已选择 ${this.selectedUserIds.size} 个用户`;
      this.node('admin-users-card')?.setAttribute('aria-busy', String(this.busy || this.confirmingDelete));
      const clear = this.node('delete-all-workspaces');
      if (clear) clear.disabled = this.busy || this.quotaBusy || this.policyBusy || this.confirmingDelete || !this.managementLoaded || this.managementFailed || !this.workspaces.length;
    }
    const toggle = this.node('fable-allowed');
    if (toggle) {
      toggle.checked = this.policy?.fableAllowed === true;
      toggle.disabled = this.busy || this.quotaBusy || this.policyBusy || this.confirmingDelete || !this.policy || this.policyFailed || this.policy.enforcementReady === false;
      const state = this.node('fable-policy-state');
      const applicationPending = this.policy?.enforcementReady === false;
      state.textContent = this.policyFailed ? '状态待确认' : !this.policy ? '正在读取' : applicationPending ? '权限尚未完整应用' : this.policy.fableAllowed ? '已允许' : '已禁止';
      state.dataset.allowed = String(!this.policyFailed && !applicationPending && this.policy?.fableAllowed === true);
      const message = this.node('fable-policy-message');
      message.textContent = this.policyBusy ? (this.policyPending === null ? '正在读取服务器权限…' : this.policyPending ? '正在允许使用 Fable…' : '正在禁止使用 Fable…') : this.policyMessage || (applicationPending ? '权限已保存，但尚未完整应用。请重试应用权限后再操作。' : '');
      message.hidden = !message.textContent;
      message.dataset.kind = this.policyFailed || this.policyMessageError || applicationPending ? 'error' : 'success';
      const retry = this.node('fable-policy-apply');
      retry.hidden = !applicationPending;
      retry.disabled = this.busy || this.quotaBusy || this.policyBusy || this.confirmingDelete || this.policyFailed;
      this.node('fable-policy-card').setAttribute('aria-busy', String(this.policyBusy));
    }
    if (this.claude) { this.claude.locked = this.busy || this.quotaBusy || this.policyBusy || this.confirmingDelete; this.claude.controls(); }
    if (this.usageAuth) { this.usageAuth.locked = this.busy || this.quotaBusy || this.policyBusy || this.confirmingDelete; this.usageAuth.controls(); }
    if (this.quotas) { this.quotas.locked = this.busy || this.policyBusy || this.confirmingDelete; this.quotas.controls(); }
  }

  render() {
    const previousQuotas = this.quotas; this.quotas = null; previousQuotas?.unmount();
    this.claude?.unmount(); this.claude = null;
    this.usageAuth?.unmount(); this.usageAuth = null;
    const authenticated = this.phase === 'ready';
    if (!authenticated) this.accountBrowser?.close({ quiet: true });
    let content;
    if (this.phase === 'login') {
      content = `<div class="admin-card-icon">${lock}</div><h2>管理员登录</h2><p class="card-description">使用你在服务器上单独设置的管理员密码。</p>
        <form id="admin-login-form"><label for="admin-password">管理员密码</label><input id="admin-password" name="password" type="password" autocomplete="current-password" required maxlength="1024" placeholder="输入管理员密码" aria-describedby="admin-message" />
        <p id="admin-message" class="admin-message" role="status" aria-live="polite" hidden></p><button id="admin-submit" class="primary-button" type="submit">登录管理员页面</button></form>
        <p class="card-note">用户网页密码与管理员密码分别设置。</p>`;
    } else if (authenticated) {
      content = `<div class="admin-card-icon">${lock}</div><h2>重设用户密码</h2><p class="card-description">选择一个账号设置新密码。只会退出该用户的网页登录，其他用户继续使用。</p>
        <form id="admin-password-form"><label for="password-user">用户账号</label><select id="password-user" aria-describedby="account-migration-hint" disabled><option value="">正在读取账号…</option></select><p class="field-hint" id="account-migration-hint">用户账号由管理员创建；升级前已有的账号和数据保留。</p>
        <label for="new-password">新用户密码</label><input id="new-password" name="newPassword" type="password" autocomplete="new-password" minlength="12" maxlength="1024" required placeholder="至少 12 个字符" aria-describedby="password-hint admin-message" />
        <p class="field-hint" id="password-hint">12 至 1024 个字符，不能与管理员密码相同。</p>
        <label for="confirm-password">再次输入新密码</label><input id="confirm-password" name="confirmPassword" type="password" autocomplete="new-password" minlength="12" maxlength="1024" required placeholder="再次输入同一个密码" aria-describedby="admin-message" />
        <p id="admin-message" class="admin-message" role="status" aria-live="polite" hidden></p>
        <div class="change-summary"><span aria-hidden="true">↗</span><p>工作区文件、工作区密码与 Claude 统一授权保留。用户的网页账号密码和工作区密码分别管理。</p></div>
        <button id="admin-submit" class="primary-button" type="submit">保存新密码</button></form><p class="card-note">管理员密码保持不变。</p>`;
    } else if (this.phase === 'disabled') {
      content = `<div class="admin-card-icon">${lock}</div><h2>管理员入口尚未启用</h2><p class="card-description">请先在服务器设置独立的管理员密码，再更新网页服务。</p><code class="setup-command">python3 scripts/configure_admin.py</code><p class="card-note">在服务器终端按提示输入密码，无需把密码发到聊天中。</p><p id="admin-message" class="admin-message" role="status" hidden></p><button id="admin-retry" class="secondary-button" type="button">重新检查</button>`;
    } else if (this.phase === 'unavailable') {
      content = `<div class="admin-card-icon">${lock}</div><h2>暂时无法连接</h2><p class="card-description">请检查网络或服务器状态，然后重试。</p><p id="admin-message" class="admin-message" role="status" hidden></p><button id="admin-retry" class="secondary-button" type="button">重新检查</button>`;
    } else {
      content = '<p class="admin-loading" role="status">正在检查管理员登录状态…</p>';
    }
    const createCard = authenticated ? `<section class="admin-card" id="create-user-card" aria-labelledby="create-user-heading"><p class="policy-eyebrow">独立用户账号</p><h2 id="create-user-heading">创建用户</h2><p class="card-description">每个人使用自己的账号，只能看到自己创建的工作区。</p><form id="admin-create-user-form"><label for="create-username">用户名</label><input id="create-username" name="username" autocomplete="off" autocapitalize="off" spellcheck="false" minlength="3" maxlength="32" required placeholder="例如 alice" aria-describedby="create-username-hint" /><p class="field-hint" id="create-username-hint">3 至 32 位小写字母、数字、下划线或短横线。</p><label for="create-password">初始密码</label><input id="create-password" name="password" type="password" autocomplete="new-password" minlength="12" maxlength="1024" required placeholder="至少 12 个字符" /><p class="field-hint">创建后由你把账号和初始密码交给该用户。</p><label for="create-confirm-password">确认初始密码</label><input id="create-confirm-password" name="confirmPassword" type="password" autocomplete="new-password" minlength="12" maxlength="1024" required placeholder="再次输入同一个密码" /><p id="create-user-message" class="admin-message" role="status" aria-live="polite" hidden></p><button id="create-user-submit" class="primary-button" type="submit">创建用户账号</button></form></section>` : '';
    const usersCard = authenticated ? `<section class="admin-card admin-users-card" id="admin-users-card" aria-labelledby="admin-users-heading"><div class="admin-management-heading"><div><p class="policy-eyebrow">独立用户账号</p><h2 id="admin-users-heading">用户管理 <span id="admin-user-count" class="management-count">—</span></h2></div></div><p class="card-description">删除用户会一并删除该用户的所有工作区与文件。其他用户和管理员统一 Claude 授权不受影响。</p><div class="admin-user-selection"><label><input id="select-all-users" type="checkbox" disabled />全选（单次最多 100 个）</label><span id="admin-selected-user-count">已选择 0 个用户</span><button id="delete-selected-users" class="danger-button" type="button" disabled>删除所选用户</button></div><div id="admin-user-list" class="admin-workspace-list" aria-live="polite"><p class="card-description">正在读取用户…</p></div><p id="admin-delete-user-message" class="admin-message" role="status" aria-live="polite" hidden></p></section>` : '';
    const workspacesCard = authenticated ? `<section class="admin-card admin-workspaces-card" id="admin-workspaces-card" aria-labelledby="admin-workspaces-heading"><div class="admin-management-heading"><div><p class="policy-eyebrow">全部用户的工作区</p><h2 id="admin-workspaces-heading">工作区管理 <span id="admin-workspace-count" class="management-count">—</span></h2></div><button id="admin-management-refresh" class="secondary-button" type="button">刷新账号与工作区</button></div><div id="admin-workspace-list" class="admin-workspace-list" aria-live="polite"><p class="card-description">正在读取工作区…</p></div><p id="admin-management-message" class="admin-message" role="status" aria-live="polite" hidden></p><div class="admin-delete-summary"><div><strong>删除全部工作区</strong><p>停止所有工作区任务，并删除所有用户的工作区、上传文件与聊天记录。账号、密码和管理员统一 Claude 授权保留。</p></div><button id="delete-all-workspaces" class="danger-button" type="button" disabled>删除全部工作区</button></div><p id="admin-delete-message" class="admin-message" role="status" aria-live="polite" hidden></p></section>` : '';
    const accountServicesCard = authenticated ? `<section class="admin-card admin-account-services" id="admin-account-services" aria-labelledby="admin-account-services-heading"><div><p class="policy-eyebrow">服务器浏览器</p><h2 id="admin-account-services-heading">Claude 账号管理</h2><p class="card-description">在服务器浏览器中注册 Claude、管理订阅或购买官方额外用量。此入口仅管理员可用。</p></div><div class="admin-account-services-actions"><button id="admin-account-register" class="primary-button" type="button">注册 Claude</button><button id="admin-account-billing" class="secondary-button" type="button">订阅 / 充值</button></div><p class="field-hint">注册或付款不会自动更改工作区授权。需要关联账号时，请在下方「Claude 授权」中单独完成。</p><p id="admin-account-services-message" class="admin-message" role="status" aria-live="polite" hidden></p></section>` : '';
    this.host.innerHTML = `<div class="admin-shell"><header class="admin-header"><a class="admin-brand" href="/" aria-label="Claude Cloud 用户网页"><span aria-hidden="true">✳</span>Claude Cloud<small>管理</small></a><div class="admin-nav">${authenticated ? '<a href="#admin-account-services">Claude 账号管理</a><a href="#admin-claude-card">Claude 授权</a><a href="#admin-usage-auth-card">额度查询授权</a>' : ''}<a href="/">返回用户网页 <span aria-hidden="true">↗</span></a>${authenticated ? '<button id="admin-logout" type="button">退出管理员</button>' : ''}</div></header>
      <main class="admin-main ${authenticated ? 'admin-ready' : ''}"><section class="admin-intro"><p class="eyebrow">ACCESS MANAGEMENT</p><h1>${authenticated ? '管理访问，<br>继续专注工作。' : '你的工作台，<br>由你管理。'}</h1><p>统一管理用户账号、工作区、Claude 授权<br>以及用户额度与模型使用权限。</p><div class="admin-scope">${lock}<span>${authenticated ? '管理员已登录' : '独立管理员入口'}<small>独立账号、工作区与模型权限</small></span></div></section>
      ${accountServicesCard}<div class="admin-cards">${authenticated ? `<section class="admin-card model-policy-card" id="fable-policy-card" aria-labelledby="fable-policy-heading"><div class="policy-heading"><div><p class="policy-eyebrow">模型使用权限</p><h2 id="fable-policy-heading">Fable</h2></div><span class="policy-state" id="fable-policy-state">正在读取</span></div><label class="policy-switch" for="fable-allowed"><span>允许用户使用 Fable</span><input type="checkbox" role="switch" id="fable-allowed" aria-describedby="fable-policy-hint fable-policy-message" disabled /></label><p class="policy-description" id="fable-policy-hint">关闭会立即停止 Fable 任务；其他已确认模型的中文聊天继续运行。默认会话尚未识别模型时也会停止。对话记录和项目文件保留。</p><p class="policy-description">Fable 是否可用取决于 Claude 账号与客户端版本；可能使用额外付费额度，以 Claude 账号为准。</p><p id="fable-policy-message" class="admin-message" role="status" aria-live="polite" hidden></p><div class="policy-actions"><button id="fable-policy-apply" class="secondary-button" type="button" hidden>重试应用权限</button><button id="fable-policy-refresh" class="policy-refresh" type="button">刷新权限状态</button></div></section>` : ''}${createCard}<section class="admin-card" id="admin-card" aria-label="管理员密码管理">${content}</section></div>${usersCard}${workspacesCard}${authenticated ? '<section id="admin-quotas-card" class="admin-card admin-quotas-card" aria-labelledby="admin-quota-heading"></section>' : ''}${authenticated ? '<section id="admin-claude-card" class="admin-card admin-claude-card" aria-labelledby="admin-claude-heading"></section><section id="admin-usage-auth-card" class="admin-card admin-claude-card" aria-labelledby="admin-usage-auth-heading"></section>' : ''}</main>
      <footer class="admin-footer"><span>CLAUDE CLOUD · 自己的云端工作区</span><span>管理员入口 /admin</span></footer></div>`;
    this.node('admin-login-form')?.addEventListener('submit', (event) => { event.preventDefault(); this.login(); });
    this.node('admin-create-user-form')?.addEventListener('submit', (event) => { event.preventDefault(); this.createUser(); });
    this.node('admin-management-refresh')?.addEventListener('click', () => this.loadManagement());
    this.node('delete-all-workspaces')?.addEventListener('click', () => this.deleteAllWorkspaces());
    this.node('admin-user-list')?.addEventListener('click', (event) => {
      const button = event.target.closest('[data-delete-user]');
      if (button && this.node('admin-user-list').contains(button)) this.deleteUser(button.dataset.deleteUser);
    });
    this.node('admin-user-list')?.addEventListener('change', (event) => {
      if (event.target.matches('[data-select-user]')) this.selectUser(event.target.dataset.selectUser, event.target.checked);
    });
    this.node('select-all-users')?.addEventListener('change', (event) => this.selectAllUsers(event.target.checked));
    this.node('delete-selected-users')?.addEventListener('click', () => this.deleteSelectedUsers());
    this.node('password-user')?.addEventListener('change', () => { this.node('new-password').value = ''; this.node('confirm-password').value = ''; this.showMessage(''); });
    this.node('admin-password-form')?.addEventListener('submit', (event) => { event.preventDefault(); this.changePassword(); });
    this.node('admin-logout')?.addEventListener('click', () => this.logout());
    this.node('admin-account-register')?.addEventListener('click', () => this.openAccountBrowser('register'));
    this.node('admin-account-billing')?.addEventListener('click', () => this.openAccountBrowser('billing'));
    this.node('admin-retry')?.addEventListener('click', () => this.start());
    this.node('fable-allowed')?.addEventListener('change', (event) => this.changePolicy(event.target.checked));
    this.node('fable-policy-refresh')?.addEventListener('click', () => this.loadPolicy());
    this.node('fable-policy-apply')?.addEventListener('click', () => this.changePolicy(this.policy?.fableAllowed));
    if (authenticated) {
      const panelGeneration = this.generation;
      const authorizationOptions = {
        request: async (path, options = {}) => {
          if (panelGeneration !== this.generation || this.phase !== 'ready' || !this.csrf) throw new AdminError('管理员登录已过期，请重新登录。', 401);
          const generation = this.generation;
          try { return await this.request(path, { ...options, authenticated: true }); }
          catch (error) {
            if (error.status === 401 && generation === this.generation) { this.expire(); this.showMessage(error.message); }
            throw error;
          }
        },
        session: () => ({ authenticated: panelGeneration === this.generation && this.phase === 'ready' && Boolean(this.csrf), csrf: panelGeneration === this.generation ? this.csrf : '' }),
        expire: () => { if (panelGeneration === this.generation) { this.expire(); this.showMessage('管理员登录已过期，请重新登录。'); } },
      };
      this.claude = this.claudeFactory(authorizationOptions);
      this.claude.mount(this.node('admin-claude-card'));
      this.usageAuth = this.usageFactory({ ...authorizationOptions, namespace: 'usage-auth' });
      this.usageAuth.mount(this.node('admin-usage-auth-card'));
      this.quotas = this.quotaFactory({
        request: async (path, options = {}) => {
          if (panelGeneration !== this.generation || this.phase !== 'ready' || !this.csrf) throw new AdminError('管理员登录已过期，请重新登录。', 401);
          try { return await this.request(path, { ...options, authenticated: true }); }
          catch (error) {
            if (error.status === 401 && panelGeneration === this.generation) { this.expire(); this.showMessage(error.message); }
            throw error;
          }
        },
        session: () => ({ authenticated: panelGeneration === this.generation && this.phase === 'ready' && Boolean(this.csrf) }),
        expire: () => { if (panelGeneration === this.generation) { this.expire(); this.showMessage('管理员登录已过期，请重新登录。'); } },
        onBusy: (busy) => { this.quotaBusy = busy; this.controls(); },
      });
      this.quotas.mount(this.node('admin-quotas-card'));
    }
    this.controls();
  }

  async openAccountBrowser(purpose) {
    if (this.phase !== 'ready' || !this.csrf || this.busy || this.policyBusy || this.quotaBusy || this.confirmingDelete) return;
    if (!this.accountBrowser) {
      this.accountBrowser = this.accountBrowserFactory({
        page: this.host.ownerDocument, location: this.host.ownerDocument?.defaultView?.location,
        getContext: () => ({ authenticated: this.phase === 'ready' && Boolean(this.csrf), key: `admin:${this.generation}`, csrf: this.csrf }),
        toast: (message, error = false) => {
          const node = this.node('admin-account-services-message');
          if (!node) return;
          node.textContent = message; node.hidden = !message; node.dataset.kind = error ? 'error' : 'success';
        },
        api: async (path, options = {}) => {
          if (!['/api/admin/claude-account-browser', '/api/admin/claude-account-browser/stop'].includes(path)) throw new AdminError('浏览器请求无效。', 400);
          const generation = this.generation, csrfToken = options.csrfToken;
          const current = () => generation === this.generation && this.phase === 'ready' && Boolean(csrfToken) && csrfToken === this.csrf;
          // A delayed cleanup uses its captured token and lease ID, never a new administrator session.
          if (path.endsWith('/stop') ? !csrfToken : !current()) throw new AdminError('管理员登录已过期，请重新登录。', 401);
          try { return await this.request(path.slice('/api/admin/'.length), { ...options, authenticated: true, csrfToken }); }
          catch (error) {
            if (error.status === 401 && current()) { this.expire(); this.showMessage(error.message); }
            throw error;
          }
        },
      });
    }
    await this.accountBrowser.show(purpose);
  }

  async start() {
    if (this.busy || this.quotaBusy || this.policyBusy || this.confirmingDelete) return;
    const generation = ++this.generation;
    this.csrf = '';
    this.policy = null; this.policyFailed = false; this.policyMessage = ''; this.policyMessageError = false;
    this.users = []; this.selectedUserIds.clear(); this.workspaces = []; this.managementLoaded = false; this.managementFailed = false;
    this.phase = 'loading';
    this.busy = true;
    this.render();
    try {
      const session = await this.request('session');
      if (generation !== this.generation) return;
      if (session.mode === 'solo') {
        this.host.ownerDocument?.defaultView?.location.replace('/#settings');
        return;
      }
      if (typeof session.enabled !== 'boolean' || typeof session.authenticated !== 'boolean') throw new AdminError('服务器返回了无法识别的登录状态，请刷新后重试。');
      if (!session.enabled) this.phase = 'disabled';
      else if (session.authenticated === true && typeof session.csrfToken === 'string' && session.csrfToken) { this.phase = 'ready'; this.csrf = session.csrfToken; }
      else this.phase = 'login';
      this.render();
    } catch (error) {
      if (generation !== this.generation) return;
      this.phase = 'unavailable'; this.render(); this.showMessage(error.message);
    } finally {
      if (generation === this.generation) { this.busy = false; this.controls(); }
    }
    if (generation === this.generation && this.phase === 'ready') { await this.loadPolicy(); await this.loadManagement(); }
  }

  async login() {
    if (this.busy || this.quotaBusy || this.policyBusy || this.phase !== 'login') return;
    const input = this.node('admin-password');
    const password = input.value;
    if (!password || password.length > 1024) { this.showMessage('请输入管理员密码。'); return; }
    this.busy = true; this.showMessage(''); this.controls();
    input.value = '';
    try {
      const session = await this.request('login', { method: 'POST', body: { password } });
      if (session.authenticated !== true || typeof session.csrfToken !== 'string' || !session.csrfToken) throw new AdminError('服务器未确认管理员登录，请重试。');
      this.csrf = session.csrfToken; this.phase = 'ready'; this.render();
    } catch (error) { this.showMessage(error.message); }
    finally { this.busy = false; this.controls(); }
    if (this.phase === 'ready') { await this.loadPolicy(); await this.loadManagement(); }
  }

  async changePassword() {
    if (this.busy || this.quotaBusy || this.policyBusy || this.confirmingDelete || this.phase !== 'ready' || !this.csrf || !this.managementLoaded) return;
    const userId = this.node('password-user')?.value;
    const user = this.users.find((item) => item.id === userId);
    if (!user || user.deleting) { this.showMessage('请先选择可以登录的用户。待完成删除的账号不能修改密码。'); return; }
    const password = this.node('new-password').value;
    const confirmation = this.node('confirm-password').value;
    const error = passwordValidation(password, confirmation);
    if (error) { this.showMessage(error); return; }
    this.busy = true; this.showMessage(''); this.controls();
    const generation = this.generation;
    try {
      const result = await this.request(`users/${encodeURIComponent(userId)}/password`, { method: 'PUT', body: { newPassword: password, confirmPassword: confirmation }, authenticated: true });
      if (generation !== this.generation || this.phase !== 'ready') return;
      if (result.ok !== true || result.sessionsRevoked !== true) throw new AdminError('服务器未确认密码已保存，请重新检查后再试。');
      this.showMessage(`${user.username} 的登录密码已更新。旧密码已失效，该用户需重新登录。`, true);
    } catch (error) {
      if (generation !== this.generation || this.phase !== 'ready') return;
      if (error.status === 401) this.expire();
      this.showMessage(error.message);
    } finally {
      if (generation === this.generation) {
        if (this.node('new-password')) this.node('new-password').value = '';
        if (this.node('confirm-password')) this.node('confirm-password').value = '';
        this.busy = false; this.controls();
      }
    }
  }

  renderManagement() {
    const selection = this.node('password-user');
    if (!selection) return;
    const previous = selection.value;
    this.selectedUserIds = new Set([...this.selectedUserIds].filter((id) => this.users.some((user) => user.id === id)));
    const editable = this.users.filter((user) => !user.deleting);
    selection.innerHTML = editable.length ? editable.map((user) => `<option value="${esc(user.id)}">${esc(user.username)}</option>`).join('') : '<option value="">暂无可修改密码的用户</option>';
    selection.value = editable.some((user) => user.id === previous) ? previous : editable[0]?.id || '';
    if (selection.value !== previous) {
      this.node('new-password').value = ''; this.node('confirm-password').value = ''; this.showMessage('');
    }
    this.node('admin-user-count').textContent = String(this.users.length);
    this.node('admin-user-list').innerHTML = this.users.length ? `<table class="admin-workspace-table admin-user-table"><thead><tr><th aria-label="选择用户"></th><th>用户账号</th><th>工作区</th><th>状态</th><th><span class="admin-table-action-label">操作</span></th></tr></thead><tbody>${this.users.map((user) => `<tr data-user-id="${esc(user.id)}"><td class="admin-user-select"><input type="checkbox" data-select-user="${esc(user.id)}" aria-label="选择用户 ${esc(user.username)}" /></td><td><strong>${esc(user.username)}</strong></td><td data-user-workspace-count>${this.workspaces.filter((space) => space.ownerId === user.id).length}</td><td><span class="admin-user-state" data-deleting="${user.deleting === true}">${user.deleting ? '待完成删除 · 登录已停用' : '可登录'}</span></td><td class="admin-user-actions"><button class="danger-button" type="button" data-delete-user="${esc(user.id)}" aria-label="${esc(user.deleting ? `重试删除用户 ${user.username} 及全部数据` : `删除用户 ${user.username} 及全部数据`)}">${user.deleting ? '重试删除' : '删除用户'}</button></td></tr>`).join('')}</tbody></table>` : '<p class="admin-empty-workspaces">目前没有用户账号。可以在上方创建新用户，管理员功能继续可用。</p>';
    this.node('admin-workspace-count').textContent = String(this.workspaces.length);
    this.node('admin-workspace-list').innerHTML = this.workspaces.length ? `<table class="admin-workspace-table"><thead><tr><th>工作区</th><th>所属用户</th><th>状态</th><th>工作区密码</th></tr></thead><tbody>${this.workspaces.map((item) => `<tr><td><strong>${esc(item.name)}</strong></td><td>${esc(item.username || this.users.find((user) => user.id === item.ownerId)?.username || '未知用户')}</td><td>${item.running ? '运行中' : '已停止'}</td><td>${item.passwordProtected ? '已设置' : '未设置'}</td></tr>`).join('')}</tbody></table>` : '<p class="admin-empty-workspaces">目前没有工作区。</p>';
    this.controls();
  }

  async loadManagement() {
    if (this.busy || this.quotaBusy || this.policyBusy || this.confirmingDelete || this.phase !== 'ready' || !this.csrf) return;
    const generation = this.generation;
    this.busy = true; this.controls(); this.sectionMessage('admin-management-message', '');
    try {
      const [accounts, spaces] = await Promise.all([this.request('users', { authenticated: true }), this.request('workspaces', { authenticated: true })]);
      if (generation !== this.generation || this.phase !== 'ready') return;
      if (!Array.isArray(accounts?.users) || !accounts.users.every((user) => typeof user.id === 'string' && user.id && typeof user.username === 'string' && user.username && (user.deleting === undefined || typeof user.deleting === 'boolean'))
          || !Array.isArray(spaces?.workspaces) || !spaces.workspaces.every((space) => typeof space.id === 'string' && typeof space.name === 'string' && typeof space.running === 'boolean' && typeof space.passwordProtected === 'boolean')) throw new AdminError('服务器未确认账号和工作区列表，请刷新后重试。');
      this.users = accounts.users.map(({ id, username, createdAt, deleting }) => ({ id, username, createdAt, deleting: deleting === true }));
      this.workspaces = spaces.workspaces;
      this.managementLoaded = true; this.managementFailed = false;
      this.renderManagement();
    } catch (error) {
      if (generation !== this.generation || this.phase !== 'ready') return;
      if (error.status === 401) { this.expire(); this.showMessage(error.message); }
      else { this.managementLoaded = false; this.managementFailed = true; this.sectionMessage('admin-management-message', error.message); }
    } finally { if (generation === this.generation) { this.busy = false; this.controls(); } }
    if (generation === this.generation && this.phase === 'ready') await this.quotas?.refresh();
  }

  async createUser() {
    if (this.busy || this.quotaBusy || this.policyBusy || this.confirmingDelete || this.phase !== 'ready' || !this.csrf) return;
    const username = this.node('create-username').value.trim().toLowerCase();
    const password = this.node('create-password').value;
    const confirmation = this.node('create-confirm-password').value;
    const error = !/^[a-z0-9_-]{3,32}$/.test(username) ? '用户名需为 3 至 32 位小写字母、数字、下划线或短横线。' : passwordValidation(password, confirmation);
    if (error) { this.sectionMessage('create-user-message', error); return; }
    const generation = this.generation;
    this.busy = true; this.controls(); this.sectionMessage('create-user-message', '');
    let created = false;
    try {
      const result = await this.request('users', { method: 'POST', body: { username, password, confirmPassword: confirmation }, authenticated: true });
      if (generation !== this.generation || this.phase !== 'ready') return;
      if (!result?.user || typeof result.user.id !== 'string' || !result.user.id || result.user.username !== username) throw new AdminError('服务器未确认账号已创建，请刷新账号列表检查后再试。');
      this.node('create-username').value = '';
      this.sectionMessage('create-user-message', `已创建 ${username}。请将账号和初始密码交给该用户。`, true);
      created = true;
    } catch (error) {
      if (generation !== this.generation || this.phase !== 'ready') return;
      if (error.status === 401) { this.expire(); this.showMessage(error.message); }
      else this.sectionMessage('create-user-message', error.message);
    } finally {
      if (generation === this.generation) {
        if (this.node('create-password')) this.node('create-password').value = '';
        if (this.node('create-confirm-password')) this.node('create-confirm-password').value = '';
        this.busy = false; this.controls();
      }
    }
    if (created && generation === this.generation && this.phase === 'ready') await this.loadManagement();
  }

  confirmWorkspaceDeletion() {
    return this.confirmDeletion('删除全部工作区？', `当前列表有 ${this.workspaces.length} 个工作区。确认后将停止所有用户的工作区任务，永久删除工作区、上传文件、图片和聊天记录。`, '用户账号、密码及管理员的 Claude 统一授权会保留。此操作无法在网页撤销。', '确认删除全部工作区');
  }

  confirmUserDeletion(user) {
    const count = this.workspaces.filter((space) => space.ownerId === user.id).length;
    return this.confirmDeletion(`删除用户 ${user.username}？`, `确认后将停用 ${user.username} 的登录、退出该用户的所有会话并停止任务，永久删除这个账号及其全部工作区、上传文件、图片和聊天记录。当前列表有 ${count} 个属于该用户的工作区。`, '其他用户及其数据、独立管理员登录和 Claude 统一授权不受影响。此操作不可撤销。', user.deleting ? '确认重试删除用户及数据' : '确认删除用户及全部数据');
  }

  confirmSelectedUserDeletion(users) {
    const names = users.map((user) => `${user.username}（${this.workspaces.filter((space) => space.ownerId === user.id).length} 个工作区）`).join('、');
    return this.confirmDeletion(`删除选中的 ${users.length} 个用户？`, `将删除：${names}。确认后将停用这些账号的登录、退出其所有会话并停止任务，永久删除所选账号及其全部工作区、上传文件、图片和聊天记录。`, '未选中的用户及其数据、独立管理员登录和 Claude 统一授权不受影响。此操作不可撤销。', '确认删除所选用户及全部数据');
  }

  selectUser(userId, selected) {
    if (this.busy || this.quotaBusy || this.policyBusy || this.confirmingDelete || this.phase !== 'ready' || !this.managementLoaded || this.managementFailed) { this.controls(); return; }
    if (!this.users.some((user) => user.id === userId)) return;
    if (selected && this.selectedUserIds.size < 100) this.selectedUserIds.add(userId);
    if (!selected) this.selectedUserIds.delete(userId);
    this.controls();
  }

  selectAllUsers(selected) {
    if (this.busy || this.quotaBusy || this.policyBusy || this.confirmingDelete || this.phase !== 'ready' || !this.managementLoaded || this.managementFailed) { this.controls(); return; }
    this.selectedUserIds = new Set(selected ? this.users.slice(0, 100).map((user) => user.id) : []);
    this.controls();
  }

  validUserDeletion(result, user, unknownRemaining = false) {
    return typeof result?.deleted === 'boolean' && result.userId === user.id && result.username === user.username
      && Number.isSafeInteger(result.deletedWorkspaces) && result.deletedWorkspaces >= 0
      && ((Number.isSafeInteger(result.remaining) && result.remaining >= 0) || (unknownRemaining && !result.deleted && result.remaining === null))
      && Array.isArray(result.failed) && (!result.deleted || (result.remaining === 0 && result.failed.length === 0));
  }

  acceptUserDeletion(result, user) {
    if (result.deleted) {
      this.users = this.users.filter((item) => item.id !== user.id);
      this.workspaces = this.workspaces.filter((space) => space.ownerId !== user.id);
      this.selectedUserIds.delete(user.id);
    } else user.deleting = true;
  }

  confirmDeletion(heading, scope, note, action) {
    return new Promise((resolve) => {
      const dialog = this.host.ownerDocument.createElement('dialog');
      dialog.className = 'admin-confirm-dialog';
      dialog.setAttribute('aria-labelledby', 'admin-delete-confirm-heading');
      dialog.innerHTML = `<h2 id="admin-delete-confirm-heading">${esc(heading)}</h2><p>${esc(scope)}</p><p>${esc(note)}</p><div class="admin-confirm-actions"><button type="button" class="secondary-button" data-cancel>取消</button><button type="button" class="danger-button" data-confirm>${esc(action)}</button></div>`;
      let confirmed = false;
      dialog.querySelector('[data-cancel]').addEventListener('click', () => dialog.close());
      dialog.querySelector('[data-confirm]').addEventListener('click', () => { confirmed = true; dialog.close(); });
      dialog.addEventListener('close', () => { if (this.deleteDialog === dialog) this.deleteDialog = null; dialog.remove(); resolve(confirmed); }, { once: true });
      this.deleteDialog = dialog;
      this.host.ownerDocument.body.append(dialog); dialog.showModal(); dialog.querySelector('[data-cancel]').focus();
    });
  }

  async deleteUser(userId) {
    if (this.busy || this.quotaBusy || this.policyBusy || this.confirmingDelete || this.phase !== 'ready' || !this.csrf || !this.managementLoaded || this.managementFailed) return;
    const user = this.users.find((item) => item.id === userId);
    if (!user) return;
    const generation = this.generation;
    this.confirmingDelete = true; this.controls();
    let confirmed;
    try { confirmed = await this.confirmUserDeletion(user); }
    finally { if (generation === this.generation) { this.confirmingDelete = false; this.controls(); } }
    if (!confirmed || generation !== this.generation || this.phase !== 'ready' || !this.csrf) return;
    this.busy = true; this.controls();
    this.sectionMessage('admin-delete-user-message', `正在停用 ${user.username} 的登录、停止任务并删除账号与数据。请等待服务器返回结果。`);
    try {
      const result = await this.request(`users/${encodeURIComponent(userId)}/delete`, { method: 'POST', body: { confirmation: 'DELETE_USER_AND_DATA' }, authenticated: true });
      if (generation !== this.generation || this.phase !== 'ready') return;
      if (!this.validUserDeletion(result, user, true)) throw new AdminError('服务器未确认这个用户的删除结果。');
      this.acceptUserDeletion(result, user);
      if (result.deleted) {
        this.sectionMessage('admin-delete-user-message', `已删除用户 ${user.username} 及其数据，共删除 ${result.deletedWorkspaces} 个工作区。其他用户和 Claude 统一授权保留。`, true);
      } else {
        this.sectionMessage('admin-delete-user-message', `${user.username} 的删除尚未完成，登录已停用。已删除 ${result.deletedWorkspaces} 个工作区，${result.remaining === null ? '剩余工作区数量待确认' : `还剩 ${result.remaining} 个工作区`}。请检查服务器后点击“重试删除”，以完成账号和数据清理。`);
      }
      this.renderManagement();
    } catch (error) {
      if (generation !== this.generation || this.phase !== 'ready') return;
      if (error.status === 401) { this.expire(); this.showMessage(error.message); }
      else this.sectionMessage('admin-delete-user-message', `${error.message} 删除结果待确认，请刷新账号与工作区列表检查。`);
    } finally { if (generation === this.generation) { this.busy = false; this.controls(); } }
    if (generation === this.generation && this.phase === 'ready') await this.loadManagement();
  }

  async deleteSelectedUsers() {
    if (this.busy || this.quotaBusy || this.policyBusy || this.confirmingDelete || this.phase !== 'ready' || !this.csrf || !this.managementLoaded || this.managementFailed) return;
    const users = this.users.filter((user) => this.selectedUserIds.has(user.id));
    if (!users.length || users.length > 100) return;
    const generation = this.generation;
    this.confirmingDelete = true; this.controls();
    let confirmed;
    try { confirmed = await this.confirmSelectedUserDeletion(users); }
    finally { if (generation === this.generation) { this.confirmingDelete = false; this.controls(); } }
    if (!confirmed || generation !== this.generation || this.phase !== 'ready' || !this.csrf) return;
    this.busy = true; this.controls();
    this.sectionMessage('admin-delete-user-message', `正在停用选中的 ${users.length} 个账号、停止任务并删除数据。请等待服务器返回结果。`);
    try {
      const result = await this.request('users/delete-batch', { method: 'POST', body: { userIds: users.map((user) => user.id), confirmation: 'DELETE_USERS_AND_DATA' }, authenticated: true });
      if (generation !== this.generation || this.phase !== 'ready') return;
      if (!Array.isArray(result?.results) || result.results.length !== users.length
          || new Set(result.results.map((item) => item?.userId)).size !== users.length
          || !result.results.every((item) => { const user = users.find((target) => target.id === item?.userId); return user && this.validUserDeletion(item, user, true); })
          || result.deletedUsers !== result.results.filter((item) => item.deleted).length
          || result.failedUsers !== result.results.filter((item) => !item.deleted).length) throw new AdminError('服务器未完整确认所选用户的删除结果。');
      for (const item of result.results) this.acceptUserDeletion(item, users.find((user) => user.id === item.userId));
      const failed = result.results.filter((item) => !item.deleted);
      const deletedWorkspaces = result.results.reduce((sum, item) => sum + item.deletedWorkspaces, 0);
      this.sectionMessage('admin-delete-user-message', failed.length
        ? `已删除 ${result.deletedUsers} 个用户、${deletedWorkspaces} 个工作区。${failed.length} 个账号尚未完成删除：${failed.map((item) => item.username).join('、')}。这些账号登录已停用，仍保持选中；请检查服务器后重试删除。`
        : `已删除所选的 ${result.deletedUsers} 个用户及其全部数据，共删除 ${deletedWorkspaces} 个工作区。未选中的用户和 Claude 统一授权保留。`, !failed.length);
      this.renderManagement();
    } catch (error) {
      if (generation !== this.generation || this.phase !== 'ready') return;
      if (error.status === 401) { this.expire(); this.showMessage(error.message); }
      else this.sectionMessage('admin-delete-user-message', `${error.message} 删除结果待确认，请刷新账号与工作区列表检查。`);
    } finally { if (generation === this.generation) { this.busy = false; this.controls(); } }
    if (generation === this.generation && this.phase === 'ready') await this.loadManagement();
  }

  async deleteAllWorkspaces() {
    if (this.busy || this.quotaBusy || this.policyBusy || this.confirmingDelete || this.phase !== 'ready' || !this.csrf || !this.managementLoaded || this.managementFailed || !this.workspaces.length) return;
    const generation = this.generation;
    this.confirmingDelete = true; this.controls();
    let confirmed;
    try { confirmed = await this.confirmWorkspaceDeletion(); }
    finally { if (generation === this.generation) { this.confirmingDelete = false; this.controls(); } }
    if (!confirmed || generation !== this.generation || this.phase !== 'ready' || !this.csrf) return;
    this.busy = true; this.controls(); this.sectionMessage('admin-delete-message', '正在停止任务并删除全部工作区，请等待服务器返回结果。');
    try {
      const result = await this.request('workspaces/delete-all', { method: 'POST', body: { confirmation: 'DELETE_ALL_WORKSPACES' }, authenticated: true });
      if (generation !== this.generation || this.phase !== 'ready') return;
      if (!Number.isSafeInteger(result?.deleted) || result.deleted < 0 || !Number.isSafeInteger(result.remaining) || result.remaining < 0 || !Array.isArray(result.failed)) throw new AdminError('服务器未确认删除结果。请刷新工作区列表检查，不要将此提示视为已清空。');
      const complete = result.remaining === 0 && result.failed.length === 0;
      this.sectionMessage('admin-delete-message', complete ? `已删除 ${result.deleted} 个工作区。当前没有工作区，用户账号和统一授权保留。` : `已删除 ${result.deleted} 个工作区，仍有 ${result.remaining} 个。${result.failed.length ? `其中 ${result.failed.length} 个未能删除。` : ''}请检查服务器后重试。`, complete);
    } catch (error) {
      if (generation !== this.generation || this.phase !== 'ready') return;
      if (error.status === 401) { this.expire(); this.showMessage(error.message); }
      else this.sectionMessage('admin-delete-message', `${error.message} 删除结果待确认，请刷新工作区列表检查。`);
    } finally { if (generation === this.generation) { this.busy = false; this.controls(); } }
    if (generation === this.generation && this.phase === 'ready') await this.loadManagement();
  }

  async logout() {
    if (this.busy || this.quotaBusy || this.policyBusy || this.confirmingDelete || this.phase !== 'ready' || !this.csrf) return;
    this.busy = true; this.showMessage(''); this.controls();
    const generation = this.generation, csrfToken = this.csrf;
    try {
      // Disconnect immediately; session revocation must not wait behind a stalled browser request.
      this.accountBrowser?.close({ quiet: true }).catch(() => {});
      if (generation !== this.generation || this.phase !== 'ready') return;
      const result = await this.request('logout', { method: 'POST', authenticated: true, csrfToken });
      if (generation !== this.generation) return;
      if (result.ok !== true) throw new AdminError('服务器未确认退出，请重试。');
      this.expire();
      this.showMessage('已退出管理员页面。', true);
    } catch (error) {
      if (error.status === 401) this.expire();
      this.showMessage(error.message);
    } finally { this.busy = false; this.controls(); }
  }

  expire() {
    this.generation++;
    this.deleteDialog?.close(); this.deleteDialog = null;
    this.confirmingDelete = false; this.busy = false;
    this.csrf = ''; this.phase = 'login'; this.policy = null;
    this.users = []; this.selectedUserIds.clear(); this.workspaces = []; this.managementLoaded = false; this.managementFailed = false;
    this.policyBusy = false; this.policyPending = null; this.policyMessage = ''; this.policyFailed = false;
    this.policyMessageError = false;
    this.render();
  }

  acceptPolicy(value, generation) {
    if (generation !== this.generation || this.phase !== 'ready') return false;
    if (typeof value?.fableAllowed !== 'boolean' || !Number.isSafeInteger(value.revision) || value.revision < 0 || (value.enforcementReady !== undefined && typeof value.enforcementReady !== 'boolean')) throw new AdminError('服务器未确认模型权限，请刷新状态后重试。');
    if (this.policy && value.revision < this.policy.revision) return false;
    this.policy = { fableAllowed: value.fableAllowed, revision: value.revision, enforcementReady: value.enforcementReady !== false };
    this.policyFailed = false;
    return true;
  }

  async loadPolicy() {
    if (this.busy || this.quotaBusy || this.policyBusy || this.confirmingDelete || this.phase !== 'ready' || !this.csrf) return;
    const generation = this.generation;
    this.policyBusy = true; this.policyPending = null; this.policyMessage = ''; this.policyMessageError = false; this.controls();
    try {
      const policy = await this.request('model-policy', { authenticated: true });
      this.acceptPolicy(policy, generation);
    } catch (error) {
      if (generation !== this.generation || this.phase !== 'ready') return;
      if (error.status === 401) { this.expire(); this.showMessage(error.message); }
      else { this.policyFailed = true; this.policyMessage = error.message; this.policyMessageError = true; }
    } finally {
      if (generation === this.generation) { this.policyBusy = false; this.controls(); }
    }
  }

  async changePolicy(allowed) {
    if (this.busy || this.quotaBusy || this.policyBusy || this.confirmingDelete || this.phase !== 'ready' || !this.csrf || !this.policy || this.policyFailed || typeof allowed !== 'boolean' || (allowed === this.policy.fableAllowed && this.policy.enforcementReady !== false)) { this.controls(); return; }
    const generation = this.generation;
    this.policyBusy = true; this.policyPending = allowed; this.policyMessage = ''; this.policyMessageError = false; this.controls();
    try {
      const policy = await this.request('model-policy', { method: 'PUT', body: { fableAllowed: allowed }, authenticated: true });
      if (this.acceptPolicy(policy, generation)) {
        if (policy.fableAllowed !== allowed) throw new AdminError('服务器返回的权限状态与本次设置不同，请刷新状态确认。');
        this.policyMessage = this.policy.enforcementReady === false ? '权限已保存，但尚未完整应用。请重试应用权限。' : allowed ? '已允许用户选择 Fable。' : '已禁止使用 Fable，用户需选择其他模型。';
      }
    } catch (error) {
      if (generation !== this.generation || this.phase !== 'ready') return;
      if (error.status === 401) { this.expire(); this.showMessage(error.message); }
      else {
        // A failed PUT can still have committed the restriction before container
        // cleanup failed. Read the durable state rather than roll the switch back.
        this.policyFailed = true; this.policyMessageError = true;
        this.policyMessage = `${error.message} 请刷新权限状态后再操作。`;
        try {
          const current = await this.request('model-policy', { authenticated: true });
          if (this.acceptPolicy(current, generation)) this.policyMessage = `${error.message} ${this.policy.enforcementReady === false ? '权限尚未完整应用，请点击「重试应用权限」。' : '已重新读取服务器权限，请以当前状态为准。'}`;
        } catch (refreshError) {
          if (generation === this.generation && this.phase === 'ready' && refreshError.status === 401) { this.expire(); this.showMessage(refreshError.message); }
        }
      }
    } finally {
      if (generation === this.generation) { this.policyBusy = false; this.policyPending = null; this.controls(); }
    }
  }
}

if (typeof document !== 'undefined') {
  const host = document.querySelector('#admin-app');
  if (host) new AdminPage(host).start();
}
