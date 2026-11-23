import { escapeQuota as esc, quotaWindowMarkup, officialMarkup, quotaUnit, quotaBlockReason } from './quota-view.js';

export class AccountUsageMenu {
  constructor({ api, getUser, audio, getMode = () => 'multiuser', page = globalThis.document, setTimer = (...args) => globalThis.setInterval(...args), clearTimer = (...args) => globalThis.clearInterval(...args) }) {
    Object.assign(this, { api, getUser, audio, getMode, page, setTimer, clearTimer }); this.generation = 0;
  }
  mount(button) {
    this.unmount(); this.button = button; if (!button) return;
    this.userId = this.getUser()?.id; this.toggle = () => this.opened ? this.close() : this.open();
    button.addEventListener('click', this.toggle); button.setAttribute('aria-haspopup', 'dialog'); button.setAttribute('aria-expanded', 'false');
    this.outside = (event) => { if (this.opened && !this.panel?.contains(event.target) && !this.button.contains(event.target)) this.close(); };
    this.escape = (event) => { if (event.key === 'Escape' && this.opened) { this.close(); this.button?.focus(); } };
    this.page.addEventListener('pointerdown', this.outside); this.page.addEventListener('keydown', this.escape);
  }
  current(generation) { return generation === this.generation && this.opened && this.panel?.isConnected && this.getUser()?.id === this.userId; }
  open() {
    if (!this.button || !this.getUser() || this.getUser().id !== this.userId) return;
    this.close(); this.opened = true; this.button.setAttribute('aria-expanded', 'true');
    this.panel = this.page.createElement('section'); this.panel.className = 'account-usage-menu'; this.panel.id = 'account-usage-menu'; this.panel.setAttribute('role', 'dialog'); this.panel.setAttribute('aria-label', '账号与使用额度');
    const solo = this.getMode() === 'solo';
    this.panel.innerHTML = `<header><div><strong>${solo ? '我的独享工作台' : esc(this.getUser().username)}</strong><p>${solo ? 'Claude 用量与网站设置' : '个人账号与使用情况'}</p></div><button type="button" class="icon-button" id="account-menu-close" aria-label="关闭账号菜单">×</button></header>${solo ? '<details class="solo-management-menu"><summary>管理与设置</summary><button class="button secondary small" type="button" data-action="solo-settings" data-section="claude">连接 Claude</button><button class="button secondary small" type="button" data-action="solo-settings" data-section="account">注册 / 订阅 / 充值</button><button class="button secondary small" type="button" data-action="solo-settings" data-section="password">修改网站密码</button><button class="text-button" type="button" data-action="solo-settings" data-section="usage">显示官方额度</button><button class="text-button" type="button" data-action="solo-settings" data-section="advanced">更多设置</button></details>' : ''}<div class="account-usage-content" id="account-usage-content"><p>正在读取额度…</p></div><p class="account-usage-error" id="account-usage-error" role="status" hidden></p><div class="account-menu-actions"><button class="button secondary small" type="button" id="account-usage-refresh">刷新额度</button><label class="account-sound-toggle"><input type="checkbox" id="account-sound-enabled">审批提示音</label><p id="account-sound-state"></p><button class="text-button" type="button" data-action="logout">${solo ? '退出网站' : '退出账号'}</button></div>`;
    this.page.body.append(this.panel);
    this.panel.querySelector('#account-menu-close').addEventListener('click', () => { this.close(); this.button?.focus(); });
    this.panel.querySelector('#account-usage-refresh').addEventListener('click', () => this.refresh(true));
    this.panel.querySelector('#account-sound-enabled').addEventListener('change', (event) => { this.audio.setEnabled(event.target.checked); this.syncSound(); });
    this.syncSound(); this.refresh(); this.timer = this.setTimer(() => this.refresh(), 60_000);
  }
  syncSound() {
    const checkbox = this.panel?.querySelector('#account-sound-enabled'); if (!checkbox) return;
    checkbox.checked = this.audio.enabled; this.panel.querySelector('#account-sound-state').textContent = this.audio.status;
  }
  async refresh(official = false) {
    if (!this.opened || this.busy) return;
    const generation = this.generation; this.busy = true;
    const button = this.panel.querySelector('#account-usage-refresh'); button.disabled = true; button.textContent = '正在刷新…';
    const error = this.panel.querySelector('#account-usage-error'); error.hidden = true;
    try {
      const result = await this.api(`/api/account/usage${official ? '/refresh' : ''}`, official ? { method: 'POST' } : {});
      if (!this.current(generation)) return;
      const solo = this.getMode() === 'solo';
      if ((!solo && (!result?.quota?.fiveHour || !result.quota.weekly)) || !Array.isArray(result?.activeTasks)) throw new Error('额度数据暂不可用，请稍后刷新。');
      const tasks = result.activeTasks.map((task) => `<li>${esc(task.name || '工作区')} · ${esc(task.phase === 'awaiting_permission' ? '等待确认' : task.phase === 'paused_quota' ? '额度暂停' : '任务进行中')}</li>`).join('');
      const unit = quotaUnit(result.quota), reason = quotaBlockReason(result.quota);
      const explanation = unit === 'percent' ? '个人站内配额按共享账号的用量分摊，不是官方个人账单。' : '用量按 Token 统计，不代表费用。';
      const personal = solo ? '' : `<h3>我的站内配额</h3>${quotaWindowMarkup(result.quota.fiveHour, '5 小时窗口', unit)}${quotaWindowMarkup(result.quota.weekly, '周窗口', unit)}<p class="quota-footnote">${explanation}两个窗口均有余额且状态已同步时可继续；按官方窗口恢复。每个账号同时最多 1 个任务。</p>${reason ? `<p class="quota-status">${esc(reason)}</p>` : ''}`;
      let officialHtml = officialMarkup(result.official);
      if (solo) officialHtml = officialHtml
        .replace('这是共享 Claude 账号的总用量，与个人站内配额分开统计。', '这是你的 Claude 账号总用量，以官方限制为准。')
        .replace('请管理员在「官方额度查询授权」中完成登录，并确认与当前共享 Claude 账号相同。', '如需显示用量，请在「管理与设置 → 显示官方额度」中使用同一个 Claude 账号登录。')
        .replace('管理员正在完成额度查询授权', '正在完成额度查询授权')
        .replace('或联系管理员检查客户端', '，也可在「管理与设置」中检查 Claude 连接')
        .replace('请联系管理员检查服务器存储后重试', '请检查服务器存储后重试');
      this.panel.querySelector('#account-usage-content').innerHTML = `${personal}${officialHtml}<h3>当前任务</h3>${tasks ? `<ul class="account-active-tasks">${tasks}</ul>` : '<p>当前没有运行中的任务。</p>'}<p class="quota-footnote">工作区在没有任务、且 ${Number.isFinite(result.idleMinutes) ? result.idleMinutes : 30} 分钟没有操作时自动关闭，文件保留。</p>`;
    } catch (failure) { if (this.current(generation)) { error.textContent = failure.message || '暂时无法读取额度。'; error.hidden = false; } }
    finally { if (this.current(generation)) { this.busy = false; button.disabled = false; button.textContent = '刷新额度'; } }
  }
  close() {
    this.generation++; this.opened = false; this.busy = false;
    if (this.timer !== undefined) this.clearTimer(this.timer); this.timer = undefined;
    this.panel?.remove(); this.panel = null; this.button?.setAttribute('aria-expanded', 'false');
  }
  unmount() {
    this.close(); this.button?.removeEventListener('click', this.toggle); this.button = null; this.userId = null;
    if (this.outside) this.page.removeEventListener('pointerdown', this.outside);
    if (this.escape) this.page.removeEventListener('keydown', this.escape);
  }
}
