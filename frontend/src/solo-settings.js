import { AdminClaudePanel } from './admin-claude.js';
import { AccountBrowser } from './account-browser.js';

/** The sole owner uses the same authenticated session for chat and settings. */
export class SoloSettings {
  constructor({ api, getContext, expire, changed = () => {}, passwordChanged = expire, page = globalThis.document,
    panelFactory = (options) => new AdminClaudePanel(options), browserFactory = (options) => new AccountBrowser(options) }) {
    Object.assign(this, { api, getContext, expire, changed, passwordChanged, page, panelFactory, browserFactory });
    this.panels = new Map();
    this.generation = 0;
  }

  allowed() { const value = this.getContext(); return value?.authenticated && value.mode === 'solo' && value.admin === true; }
  current(context) { return this.allowed() && this.getContext().key === context.key && this.getContext().csrf === context.csrf; }

  async request(path, options = {}, context = this.context) {
    const staleCleanup = path === '/api/admin/claude-account-browser/stop';
    if ((!staleCleanup && !this.current(context)) || !context?.csrf) throw new Error('登录已过期，请重新登录。');
    return this.api(path, { ...options, csrfToken: context.csrf });
  }

  show(section = 'claude') {
    if (!this.allowed()) return;
    if (!this.dialog) this.mount();
    const key = ['claude', 'account', 'usage', 'password', 'advanced'].includes(section) ? section : 'claude';
    this.dialog.querySelectorAll('[data-settings-section]').forEach((node) => { node.open = node.dataset.settingsSection === key; });
    this.openSection(key);
    this.dialog.querySelector(`[data-settings-section="${key}"] summary`).focus();
  }

  mount() {
    this.context = { ...this.getContext() };
    const dialog = this.page.createElement('dialog');
    this.dialog = dialog;
    dialog.className = 'solo-settings-dialog';
    dialog.setAttribute('aria-labelledby', 'solo-settings-title');
    dialog.innerHTML = `<header class="solo-settings-header"><div><p>个人独享</p><h1 id="solo-settings-title">管理与设置</h1></div><button class="button secondary small" type="button" data-settings-close>返回聊天</button></header>
      <p class="solo-settings-intro">一个网站密码，管理你的 Claude 和全部工作区。</p>
      <details data-settings-section="claude"><summary>连接 Claude</summary><section data-settings-claude></section></details>
      <details data-settings-section="account"><summary>注册、订阅与充值</summary><p>在服务器浏览器中的官方页面管理账号。注册或付款后，如需用于聊天，请再完成「连接 Claude」。</p><div class="solo-settings-actions"><button class="button secondary" type="button" data-account-purpose="register">注册 Claude</button><button class="button secondary" type="button" data-account-purpose="billing">订阅 / 充值</button></div></details>
      <details data-settings-section="usage"><summary>显示官方额度（可选）</summary><p>这项授权只用于显示官方用量条，不影响直接聊天。</p><section data-settings-usage></section></details>
      <details data-settings-section="password"><summary>修改网站密码</summary><p>修改后所有设备需要重新登录，工作区文件和 Claude 授权保留。</p><form data-password-form><label for="solo-current-password">当前网站密码</label><input class="text-input" id="solo-current-password" type="password" autocomplete="current-password" required maxlength="1024"><label for="solo-new-password">新网站密码</label><input class="text-input" id="solo-new-password" type="password" autocomplete="new-password" required minlength="12" maxlength="1024"><label for="solo-confirm-password">确认新密码</label><input class="text-input" id="solo-confirm-password" type="password" autocomplete="new-password" required minlength="12" maxlength="1024"><button class="button primary" type="submit">保存新密码</button><p data-password-message role="status"></p></form></details>
      <details data-settings-section="advanced"><summary>更多设置</summary><label class="solo-model-option"><input type="checkbox" data-fable-toggle disabled>启用 Fable 模型</label><p>模型是否可用、是否产生额外费用，以 Claude 账号为准。关闭会停止正在使用 Fable 的任务，保留文件和对话。</p><button class="text-button" type="button" data-policy-refresh>刷新模型设置</button><button class="button secondary small" type="button" data-policy-apply hidden>重试应用</button><p data-policy-message role="status"></p></details>
      <p class="solo-settings-message" data-settings-message role="status" hidden></p>`;
    this.page.body.append(dialog);
    dialog.querySelector('[data-settings-close]').addEventListener('click', () => this.close());
    dialog.addEventListener('cancel', (event) => { event.preventDefault(); this.close(); });
    dialog.querySelectorAll('[data-settings-section]').forEach((node) => node.addEventListener('toggle', () => { if (node.open) this.openSection(node.dataset.settingsSection); }));
    dialog.querySelectorAll('[data-account-purpose]').forEach((node) => node.addEventListener('click', () => this.openBrowser(node.dataset.accountPurpose)));
    dialog.querySelector('[data-password-form]').addEventListener('submit', (event) => { event.preventDefault(); this.changePassword(); });
    dialog.querySelector('[data-fable-toggle]').addEventListener('change', (event) => this.changePolicy(event.target.checked));
    dialog.querySelector('[data-policy-refresh]').addEventListener('click', () => { if (!this.policyBusy) this.loadPolicy(); });
    dialog.querySelector('[data-policy-apply]').addEventListener('click', () => this.changePolicy(this.policy?.fableAllowed));
    dialog.showModal();
  }

  openSection(key) {
    if (!this.dialog || !this.current(this.context)) return;
    if (['claude', 'usage'].includes(key) && !this.panels.has(key)) {
      const context = this.context;
      const namespace = key === 'usage' ? 'usage-auth' : 'claude';
      const panel = this.panelFactory({ namespace, solo: true,
        request: async (path, options) => {
          const result = await this.request(`/api/admin/${path}`, options, context);
          if (path === 'claude/status' && this.current(context)) this.changed(result);
          return result;
        },
        session: () => ({ authenticated: Boolean(this.dialog && this.current(context)), csrf: context.csrf }),
        expire: () => { this.close(); this.expire(); },
      });
      this.panels.set(key, panel);
      Promise.resolve(panel.mount(this.dialog.querySelector(`[data-settings-${key}]`))).catch(() => {});
    }
    if (key === 'advanced' && !this.policyLoaded && !this.policyBusy) this.loadPolicy();
  }

  message(text) { const node = this.dialog?.querySelector('[data-settings-message]'); if (node) { node.textContent = text; node.hidden = !text; } }

  openBrowser(purpose) {
    if (!this.dialog || !this.current(this.context)) return;
    const context = this.context;
    if (!this.browser) this.browser = this.browserFactory({ page: this.page, location: this.page.defaultView?.location, solo: true,
      api: (path, options) => this.request(path, options, { ...context, csrf: options?.csrfToken || context.csrf }),
      getContext: () => { const value = this.getContext(); return { ...value, authenticated: Boolean(this.dialog && this.allowed()) }; },
      toast: (message) => this.message(message),
    });
    this.browser.show(purpose);
  }

  async changePassword() {
    if (!this.dialog || !this.current(this.context) || this.passwordBusy) return;
    const context = this.context, dialog = this.dialog;
    const currentPassword = dialog.querySelector('#solo-current-password').value;
    const newPassword = dialog.querySelector('#solo-new-password').value;
    const confirmPassword = dialog.querySelector('#solo-confirm-password').value;
    const message = dialog.querySelector('[data-password-message]');
    if (!currentPassword || Array.from(newPassword).length < 12 || newPassword.length > 1024 || newPassword !== confirmPassword) {
      message.textContent = !currentPassword ? '请输入当前密码。' : newPassword !== confirmPassword ? '两次新密码不一致。' : '新密码需要 12 至 1024 个字符。'; return;
    }
    this.passwordBusy = true;
    const button = dialog.querySelector('[data-password-form] button'); button.disabled = true;
    dialog.querySelectorAll('[data-password-form] input').forEach((input) => { input.value = ''; });
    try {
      const result = await this.request('/api/settings/password', { method: 'POST', body: { currentPassword, newPassword, confirmPassword } }, context);
      if (this.dialog !== dialog || !this.current(context)) return;
      if (result.ok !== true || result.sessionsRevoked !== true) throw new Error('服务器尚未确认密码修改，请重新检查。');
      this.close(); this.passwordChanged();
    } catch (error) { if (this.dialog === dialog && this.current(context)) message.textContent = error.message; }
    finally { if (this.dialog === dialog) this.passwordBusy = false; button.disabled = false; }
  }

  acceptPolicy(result, dialog) {
    if (typeof result?.fableAllowed !== 'boolean') throw new Error('模型设置暂不可用。');
    this.policy = result; this.policyLoaded = true;
    const toggle = dialog.querySelector('[data-fable-toggle]');
    toggle.checked = result.fableAllowed; toggle.disabled = result.enforcementReady === false;
    dialog.querySelector('[data-policy-apply]').hidden = result.enforcementReady !== false;
    dialog.querySelector('[data-policy-message]').textContent = result.enforcementReady === false ? '设置尚未完整应用，请点击「重试应用」。' : '';
  }

  async loadPolicy() {
    const context = this.context, dialog = this.dialog;
    this.policyBusy = true;
    try {
      const result = await this.request('/api/admin/model-policy', {}, context);
      if (this.dialog !== dialog || !this.current(context)) return;
      this.acceptPolicy(result, dialog);
    } catch (error) { if (this.dialog === dialog && this.current(context)) { dialog.querySelector('[data-policy-message]').textContent = error.message; dialog.querySelector('[data-fable-toggle]').disabled = true; this.policyLoaded = false; } }
    finally { if (this.dialog === dialog) this.policyBusy = false; }
  }

  async changePolicy(allowed) {
    if (!this.dialog || !this.current(this.context) || this.policyBusy || typeof allowed !== 'boolean') return;
    const context = this.context, dialog = this.dialog;
    const toggle = dialog.querySelector('[data-fable-toggle]'); toggle.disabled = true; this.policyBusy = true;
    try {
      const result = await this.request('/api/admin/model-policy', { method: 'PUT', body: { fableAllowed: allowed } }, context);
      if (this.dialog !== dialog || !this.current(context)) return;
      this.acceptPolicy(result, dialog);
      if (result.enforcementReady !== false) dialog.querySelector('[data-policy-message]').textContent = '已保存模型设置。';
    } catch (error) {
      if (this.dialog !== dialog || !this.current(context)) return;
      // A failed update may already have saved the restriction. Re-read it.
      try {
        const result = await this.request('/api/admin/model-policy', {}, context);
        if (this.dialog !== dialog || !this.current(context)) return;
        this.acceptPolicy(result, dialog);
      } catch { if (this.dialog === dialog && this.current(context)) { this.policyLoaded = false; toggle.disabled = true; } }
      if (this.dialog === dialog && this.current(context)) dialog.querySelector('[data-policy-message]').textContent = `${error.message} 请以刷新后的设置为准。`;
    } finally { if (this.dialog === dialog) this.policyBusy = false; }
  }

  suspend() { this.close(); }
  close() {
    this.generation++;
    this.browser?.close({ quiet: true });
    for (const panel of this.panels.values()) panel.unmount();
    this.panels.clear();
    this.dialog?.querySelectorAll('input').forEach((input) => { input.value = ''; });
    this.dialog?.close(); this.dialog?.remove(); this.dialog = null;
    this.browser = null; this.policy = null; this.policyLoaded = false; this.policyBusy = false; this.passwordBusy = false;
  }
}
