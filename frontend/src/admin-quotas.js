import { escapeQuota as esc, tokenText, timeText, officialMarkup, parseWanTokens, quotaDistribution, validTokenCount, quotaWindowMarkup, percentageText, isIdleWindow, IDLE_WINDOW_TEXT } from './quota-view.js';

const amount = (value) => value === null ? '' : String(value / 10000);
const limitText = (value) => value === null ? '不限额' : tokenText(value);
const remaining = (limit, used) => limit === null ? '不限额' : tokenText(Math.max(0, limit - used));
const percentageValid = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100;
const bucketValid = (value, unit) => value && (unit === 'percent'
  ? ['limit', 'used', 'remaining'].every((key) => value[key] === null || percentageValid(value[key]))
  : (value.limit === null || validTokenCount(value.limit)) && validTokenCount(value.used)
  && (value.limit === null ? value.remaining === null : validTokenCount(value.remaining))
  )
  && (value.resetAt === null || typeof value.resetAt === 'string' || typeof value.resetAt === 'number');
const automaticMode = (view) => view?.settings?.mode === 'official_share';
const sameIds = (left, right) => left.length === right.length && left.every((id) => right.includes(id));

export function validQuotaView(view) {
  return Boolean(view && Array.isArray(view.users) && view.users.every((user) => typeof user.id === 'string' && user.id
    && typeof user.username === 'string' && user.username && (user.deleting === undefined || typeof user.deleting === 'boolean')
    && (user.quota?.unit === undefined || ['tokens', 'percent'].includes(user.quota.unit))
    && bucketValid(user.quota?.fiveHour, user.quota?.unit) && bucketValid(user.quota?.weekly, user.quota?.unit))
    && new Set(view.users.map((user) => user.id)).size === view.users.length
    && (view.settings === undefined || (['tokens', 'official_share'].includes(view.settings?.mode)
      && Array.isArray(view.settings.userIds) && view.settings.userIds.every((id) => typeof id === 'string' && id)
      && new Set(view.settings.userIds).size === view.settings.userIds.length))
    && (!automaticMode(view) || view.users.every((user) => user.quota.unit === 'percent'))
    && view.official && typeof view.official === 'object' && !Array.isArray(view.official));
}

/** Only normalized quota numbers enter this panel; no authorization tokens. */
export class AdminQuotasPanel {
  constructor({ request, session, expire, onBusy = () => {} }) {
    Object.assign(this, { request, session, expire, onBusy });
    this.generation = 0; this.locked = false; this.busy = false; this.view = null; this.failed = false;
    this.selectedIds = new Set();
  }
  node(id) { return this.host?.querySelector(`#${id}`); }
  current(generation) { return Boolean(this.host && generation === this.generation && this.session().authenticated); }
  mount(host) { this.host = host; this.render(); }
  unmount() {
    this.generation++; this.dialog?.close(); this.dialog = null;
    this.host = null; this.view = null; this.selectedIds.clear(); this.busy = false; this.onBusy(false);
  }
  setBusy(value) { this.busy = value; this.onBusy(value); this.controls(); }
  message(text, error = false) {
    const node = this.node('admin-quota-message');
    if (!node) return;
    node.textContent = text; node.hidden = !text; node.dataset.kind = error ? 'error' : 'success';
  }
  controls() {
    if (!this.host) return;
    this.host.setAttribute('aria-busy', String(this.busy));
    this.host.querySelectorAll('input, button').forEach((node) => {
      node.disabled = this.busy || this.locked || (node.id !== 'admin-quota-refresh' && (!this.view || this.failed || node.closest('[data-deleting="true"]')));
    });
    const distribute = this.node('admin-quota-distribute');
    if (distribute && !this.view?.users.some((user) => !user.deleting)) distribute.disabled = true;
    const apply = this.node('admin-quota-automatic-apply');
    if (apply && !this.selectedIds.size) apply.disabled = true;
    const count = this.node('admin-quota-selection-summary');
    if (count) count.textContent = this.selectedIds.size ? `已选择 ${this.selectedIds.size} 个用户；下个完整窗口每人 ${percentageText(100 / this.selectedIds.size)}。` : '请选择参与分配的用户，owner 账号也由你决定是否加入。';
  }
  render() {
    if (!this.host) return;
    const users = this.view?.users || [];
    const automatic = automaticMode(this.view);
    this.host.innerHTML = `<div class="admin-management-heading"><div><p class="policy-eyebrow">站内配额</p><h2 id="admin-quota-heading">站内配额与官方账号总额度</h2></div><button id="admin-quota-refresh" class="secondary-button" type="button">刷新额度状态</button></div>
      <p class="card-description">分别管理用户的 5 小时与每周站内配额。两个窗口都有剩余时才能继续聊天，支持多个用户并行使用。</p>
      <div class="admin-official-quotas">${officialMarkup(this.view?.official || { stale: true })}</div>
      ${this.automaticMarkup(users, automatic)}
      ${automatic ? '<div class="admin-quota-summary"><strong>当前使用自动站内配额</strong><p>下方百分比表示各用户占官方完整窗口的份额。官方数据尚未同步时保持待分配，不会将未知额度记为零消耗或自动放行。</p></div>' : '<div class="admin-quota-summary"><strong>手动站内配额 · 万 Token</strong><p>留空表示不限额，填 0 暂停该窗口使用。按输入、输出及缓存 Token 合计；收到官方恢复时间后按窗口恢复，未知时不会自行重置。</p></div>'}
      <div class="admin-quota-users">${this.view ? users.length ? users.map((user, index) => this.userMarkup(user, index)).join('') : '<p class="card-description">暂无用户，请先创建账号。</p>' : '<p class="card-description">正在读取额度…</p>'}</div>
      ${automatic ? '' : `<form id="admin-quota-distribution-form" class="admin-quota-distribution"><h3>将总额度均分给所有用户</h3><p>排除正在删除的账号。按用户编号稳定分配余数，最小单位为 1 Token。已有使用量保留，确认前可查看每个人的额度。</p>
        <div class="admin-quota-fields"><div><label for="quota-total-five">5 小时总额度 · 万 Token</label><input id="quota-total-five" inputmode="decimal" autocomplete="off" placeholder="留空不限额" /></div><div><label for="quota-total-weekly">每周总额度 · 万 Token</label><input id="quota-total-weekly" inputmode="decimal" autocomplete="off" placeholder="留空不限额" /></div><button id="admin-quota-distribute" class="secondary-button" type="submit">预览均分结果</button></div></form>`}
      <p id="admin-quota-message" class="admin-message" role="status" aria-live="polite" hidden></p>`;
    this.node('admin-quota-refresh').addEventListener('click', () => this.refresh({ official: true }));
    this.node('admin-quota-distribution-form')?.addEventListener('submit', (event) => { event.preventDefault(); this.distribute(); });
    this.node('admin-quota-automatic-form').addEventListener('submit', (event) => { event.preventDefault(); this.changeAutomatic(true); });
    this.node('admin-quota-automatic-disable')?.addEventListener('click', () => this.changeAutomatic(false));
    this.host.querySelectorAll('[data-automatic-user]').forEach((input) => input.addEventListener('change', () => {
      if (this.busy || this.locked || this.failed) { input.checked = this.selectedIds.has(input.dataset.automaticUser); return; }
      if (input.checked) this.selectedIds.add(input.dataset.automaticUser); else this.selectedIds.delete(input.dataset.automaticUser);
      this.controls();
    }));
    this.host.querySelectorAll('form[data-quota-user]').forEach((form) => form.addEventListener('submit', (event) => { event.preventDefault(); this.saveUser(form.dataset.quotaUser); }));
    this.controls();
  }
  userMarkup(user, index) {
    if (automaticMode(this.view)) {
      const included = this.view.settings.userIds.includes(user.id);
      return `<section data-quota-user="${esc(user.id)}" class="admin-quota-user"><div class="admin-quota-user-heading"><h3>${esc(user.username)} · 站内配额</h3><span>${included ? '参与自动分配' : '未参与分配'}</span></div>${user.quota.blockedReason ? `<p class="admin-quota-blocked">${esc(user.quota.blockedReason)}</p>` : ''}<div class="admin-quota-percent-windows">${quotaWindowMarkup(user.quota.fiveHour, '5 小时', 'percent')}${quotaWindowMarkup(user.quota.weekly, '每周', 'percent')}</div></section>`;
    }
    const fields = [['fiveHour', '5 小时'], ['weekly', '每周']].map(([key, label]) => {
      const item = user.quota[key], id = `quota-${index}-${key}`;
      return `<div><label for="${id}">${label}额度 · 万 Token</label><input id="${id}" data-window="${key}" inputmode="decimal" autocomplete="off" value="${esc(amount(item.limit))}" placeholder="留空不限额" /><p>已用 ${tokenText(item.used)} · 剩余 ${item.limit === null ? '不限额' : tokenText(item.remaining)}</p><p>下次恢复：${esc(timeText(item.resetAt))}</p></div>`;
    }).join('');
    return `<form data-quota-user="${esc(user.id)}" data-deleting="${user.deleting === true}" class="admin-quota-user"><div class="admin-quota-user-heading"><h3>${esc(user.username)}</h3>${user.deleting ? '<span>待完成删除 · 不能修改额度</span>' : ''}</div><div class="admin-quota-fields">${fields}<button type="submit" class="secondary-button">预览并保存</button></div></form>`;
  }
  automaticMarkup(users, automatic) {
    return `<form id="admin-quota-automatic-form" class="admin-quota-automatic"><div class="admin-quota-user-heading"><h3>自动分配站内配额</h3><span>${automatic ? '已启用' : '尚未启用'}</span></div><p>选择具体用户后，两个官方窗口各自的剩余额度将平均分配；下个完整窗口按 100% ÷ 参与人数分配，无需手填 Token。</p><div class="admin-quota-member-list">${users.map((user, index) => `<label for="quota-member-${index}" data-deleting="${user.deleting === true}"><input id="quota-member-${index}" type="checkbox" data-automatic-user="${esc(user.id)}" ${this.selectedIds.has(user.id) ? 'checked' : ''} /><span>${esc(user.username)}${user.deleting ? ' · 待删除' : ''}</span></label>`).join('')}</div><p id="admin-quota-selection-summary" class="admin-quota-selection-summary" aria-live="polite"></p><details class="admin-quota-rules"><summary>分配规则</summary><p>按官方账号总用量的变化，以及各任务的 Token 占比，将消耗分摊到站内用户。这里展示站内配额记录，官方仅提供账号总量。</p><p>仅所选用户参加分配；新建账号不会自动加入。重复开启同一名单不会清除已用量。更改模式前需停止运行中的任务，变更名单需等待原有两个窗口结束。</p><p>官方数据未知时保留待分配状态。达到站内配额后暂停后续任务，正在执行的请求仍可能继续计入消耗。</p></details><div class="admin-quota-mode-actions"><button id="admin-quota-automatic-apply" class="primary-button" type="submit">${automatic ? '预览名单设置' : '预览并开启自动分配'}</button>${automatic ? '<button id="admin-quota-automatic-disable" class="secondary-button" type="button">关闭并恢复手动配额</button>' : ''}</div></form>`;
  }
  accept(view, { preserveSelection = false } = {}) {
    if (!validQuotaView(view)) throw new Error('服务器未确认额度状态，请刷新后重试。');
    const ids = preserveSelection ? [...this.selectedIds] : view.settings?.userIds || [];
    this.selectedIds = new Set(ids.filter((id) => view.users.some((user) => user.id === id && !user.deleting)));
    this.view = view; this.failed = false; this.render();
  }
  async refresh({ official = false } = {}) {
    if (!this.host || this.busy || this.locked || !this.session().authenticated) return;
    const generation = this.generation;
    this.setBusy(true); this.message('');
    try {
      const view = await this.request(official ? 'quotas/refresh' : 'quotas', ...(official ? [{ method: 'POST' }] : []));
      if (this.current(generation)) this.accept(view);
    } catch (error) { this.handleError(error, generation); }
    finally { if (this.current(generation)) this.setBusy(false); }
  }
  handleError(error, generation, automatic = false) {
    if (!this.current(generation)) return;
    if (error.status === 401) { this.expire(); return; }
    this.failed = true;
    this.message(automatic && error.status === 409 ? '当前不能更改自动配额。请先停止运行中的任务；变更名单还需等待原有两个官方窗口结束，再刷新状态后重试。' : '额度操作未完成或结果待确认，请刷新额度状态后重试。', true);
    this.controls();
  }
  async saveUser(id) {
    if (!this.host || this.busy || this.locked || this.failed || automaticMode(this.view) || !this.session().authenticated) return;
    const user = this.view?.users.find((item) => item.id === id);
    if (!user || user.deleting) return;
    const form = [...this.host.querySelectorAll('[data-quota-user]')].find((node) => node.dataset.quotaUser === id);
    let body;
    try { body = { fiveHourLimit: parseWanTokens(form.querySelector('[data-window="fiveHour"]').value), weeklyLimit: parseWanTokens(form.querySelector('[data-window="weekly"]').value) }; }
    catch (error) { this.message(error.message, true); return; }
    if (body.fiveHourLimit === user.quota.fiveHour.limit && body.weeklyLimit === user.quota.weekly.limit) { this.message('额度未改变。'); return; }
    await this.commitPreview({ path: `users/${encodeURIComponent(id)}/quota`, method: 'PUT', body, heading: `确认修改 ${user.username} 的额度`,
      rows: [{ ...body, id, username: user.username }], success: `已更新 ${user.username} 的额度，当前窗口已用量保留。` });
  }
  async distribute() {
    if (!this.host || this.busy || this.locked || this.failed || !this.view || automaticMode(this.view) || !this.session().authenticated) return;
    let body, rows;
    try {
      body = { fiveHourTotal: parseWanTokens(this.node('quota-total-five').value), weeklyTotal: parseWanTokens(this.node('quota-total-weekly').value), confirmation: 'DISTRIBUTE_QUOTA' };
      rows = quotaDistribution(this.view.users, body.fiveHourTotal, body.weeklyTotal);
    } catch (error) { this.message(error.message, true); return; }
    await this.commitPreview({ path: 'quotas/distribute', method: 'POST', body, heading: `确认均分给 ${rows.length} 个用户`, rows, refresh: true,
      success: '总额度已分配，当前窗口已用量保留。当前显示服务器实际保存的额度。' });
  }
  async changeAutomatic(enabled) {
    if (!this.host || this.busy || this.locked || this.failed || !this.view || !this.session().authenticated) return;
    const userIds = enabled ? [...this.selectedIds].sort() : [];
    if (enabled && !userIds.length) { this.message('请先选择参加分配的用户。', true); return; }
    const generation = this.generation;
    this.setBusy(true); this.message('');
    try {
      const current = await this.request('quotas');
      if (!this.current(generation)) return;
      this.accept(current, { preserveSelection: true });
      if (enabled && userIds.some((id) => !this.view.users.some((user) => user.id === id && !user.deleting))) {
        this.message('用户列表已经改变，请重新选择参与分配的用户。', true); return;
      }
      if (!enabled && !automaticMode(this.view)) { this.message('当前已经使用手动站内配额。'); return; }
      const confirmed = await this.confirmAutomatic(enabled, userIds);
      if (!confirmed || !this.current(generation)) return;
      const reply = await this.request('quotas/automatic', { method: 'PUT', body: { enabled, userIds, confirmation: 'CHANGE_AUTOMATIC_QUOTA' } });
      if (!this.current(generation)) return;
      if (!validQuotaView(reply) || (enabled ? !automaticMode(reply) || !sameIds(reply.settings.userIds, userIds) : reply.settings?.mode !== 'tokens')) {
        throw new Error('服务器未确认自动配额设置。');
      }
      this.accept(reply);
      this.message(enabled ? '自动配额已启用；额度将按官方数据分配。' : '已关闭自动分配，恢复手动站内配额。');
    } catch (error) { this.handleError(error, generation, true); }
    finally { if (this.current(generation)) this.setBusy(false); }
  }
  automaticPreviewMarkup(userIds) {
    const keepExisting = automaticMode(this.view) && sameIds(this.view.settings.userIds, userIds);
    const restoreExisting = !automaticMode(this.view) && this.view.settings?.userIds?.length > 0
      && sameIds(this.view.settings.userIds, userIds);
    const share = (key) => {
      const meter = this.view.official[key];
      const reset = meter?.resetAt;
      const time = typeof reset === 'number' ? reset * (reset < 1e12 ? 1000 : 1) : Date.parse(reset);
      const fresh = typeof meter?.stale === 'boolean' ? !meter.stale : this.view.official.stale === false;
      const idle = isIdleWindow(meter) && meter.usedPercent === 0;
      return percentageValid(meter?.usedPercent) && fresh && !this.view.official.error && (idle || time > Date.now())
        ? Math.max(0, 100 - meter.usedPercent) / userIds.length : null;
    };
    const shares = { fiveHour: share('fiveHour'), weekly: share('weekly') };
    const cells = (user) => ['fiveHour', 'weekly'].map((key) => {
      if (restoreExisting) return '<td>保留原配额，待同步</td>';
      const value = keepExisting ? user.quota[key].limit : shares[key];
      return `<td>${value === null ? '待官方同步' : percentageText(value)}${keepExisting ? `<small>已用 ${percentageText(user.quota[key].used)} · 剩余 ${percentageText(user.quota[key].remaining)}</small>` : ''}</td>`;
    }).join('');
    return `<p>仅以下 ${userIds.length} 个用户参与自动分配；未选择的用户没有自动站内配额。下个完整窗口每人 ${percentageText(100 / userIds.length)}。</p>
      <p>${keepExisting ? '保持现有名单及配额，当前窗口已用量不会清零。' : restoreExisting ? '恢复原有分配，当前窗口已用量不会清零；开启后同步余额。如果原窗口已结束，将按官方新窗口分配。' : '当前窗口按官方剩余量均分，两个窗口分别计算。数据未知时保存设置并等待官方同步；实际配额以保存后显示的状态为准。'}</p>
      ${[['fiveHour', '5 小时窗口'], ['weekly', '周窗口']].filter(([key]) => isIdleWindow(this.view.official[key]) && shares[key] !== null).map(([, label]) => `<p>${label}：${IDLE_WINDOW_TEXT}。</p>`).join('')}
      <div class="admin-quota-preview-scroll"><table class="admin-workspace-table"><thead><tr><th>参与用户</th><th>本轮 5 小时配额</th><th>本轮每周配额</th></tr></thead><tbody>${userIds.map((id) => {
        const user = this.view.users.find((item) => item.id === id);
        return `<tr><td>${esc(user.username)}</td>${cells(user)}</tr>`;
      }).join('')}</tbody></table></div>`;
  }
  confirmAutomatic(enabled, userIds) {
    return this.openConfirmation(enabled ? '确认自动分配站内配额' : '关闭自动站内配额？', enabled
      ? this.automaticPreviewMarkup(userIds)
      : '<p>关闭后恢复手动 Token 配额。请确认当前没有运行中的任务。</p>', enabled ? '确认开启自动分配' : '确认恢复手动配额');
  }
  openConfirmation(heading, markup, action) {
    return new Promise((resolve) => {
      const dialog = this.host.ownerDocument.createElement('dialog');
      dialog.className = 'admin-confirm-dialog admin-quota-confirm';
      dialog.setAttribute('aria-labelledby', 'admin-quota-confirm-heading');
      dialog.innerHTML = `<h2 id="admin-quota-confirm-heading">${esc(heading)}</h2>${markup}<div class="admin-confirm-actions"><button type="button" class="secondary-button" data-cancel>取消</button><button type="button" class="primary-button" data-confirm>${esc(action)}</button></div>`;
      let confirmed = false;
      dialog.querySelector('[data-cancel]').addEventListener('click', () => dialog.close());
      dialog.querySelector('[data-confirm]').addEventListener('click', () => { confirmed = true; dialog.close(); });
      dialog.addEventListener('close', () => { if (this.dialog === dialog) this.dialog = null; dialog.remove(); resolve(confirmed); }, { once: true });
      this.dialog = dialog; this.host.ownerDocument.body.append(dialog); dialog.showModal(); dialog.querySelector('[data-cancel]').focus();
    });
  }
  async commitPreview({ path, method, body, heading, rows, success, refresh = false }) {
    const generation = this.generation;
    this.setBusy(true); this.message('');
    try {
      if (refresh) {
        const view = await this.request('quotas');
        if (!this.current(generation)) return;
        this.accept(view);
        rows = quotaDistribution(this.view.users, body.fiveHourTotal, body.weeklyTotal);
        heading = `确认均分给 ${rows.length} 个用户`;
      }
      const confirmed = await this.confirmPreview(heading, rows);
      if (!confirmed || !this.current(generation)) return;
      const view = await this.request(path, { method, body });
      if (!this.current(generation)) return;
      this.accept(view); this.message(success);
    } catch (error) { this.handleError(error, generation); }
    finally { if (this.current(generation)) this.setBusy(false); }
  }
  confirmPreview(heading, rows) {
    return new Promise((resolve) => {
      const dialog = this.host.ownerDocument.createElement('dialog');
      dialog.className = 'admin-confirm-dialog admin-quota-confirm';
      dialog.setAttribute('aria-labelledby', 'admin-quota-confirm-heading');
      dialog.innerHTML = `<h2 id="admin-quota-confirm-heading">${esc(heading)}</h2><p>以下是保存后的新额度。已用量不清零；若新额度小于已用量，该窗口剩余为 0。</p><div class="admin-quota-preview-scroll"><table class="admin-workspace-table"><thead><tr><th>用户</th><th>5 小时：额度 / 剩余</th><th>每周：额度 / 剩余</th></tr></thead><tbody>${rows.map((row) => {
        const quota = this.view.users.find((user) => user.id === row.id).quota;
        return `<tr><td>${esc(row.username)}</td><td>${limitText(row.fiveHourLimit)}<small>剩余 ${remaining(row.fiveHourLimit, quota.fiveHour.used)}</small></td><td>${limitText(row.weeklyLimit)}<small>剩余 ${remaining(row.weeklyLimit, quota.weekly.used)}</small></td></tr>`;
      }).join('')}</tbody></table></div><div class="admin-confirm-actions"><button type="button" class="secondary-button" data-cancel>取消</button><button type="button" class="primary-button" data-confirm>确认保存额度</button></div>`;
      let confirmed = false;
      dialog.querySelector('[data-cancel]').addEventListener('click', () => dialog.close());
      dialog.querySelector('[data-confirm]').addEventListener('click', () => { confirmed = true; dialog.close(); });
      dialog.addEventListener('close', () => { if (this.dialog === dialog) this.dialog = null; dialog.remove(); resolve(confirmed); }, { once: true });
      this.dialog = dialog; this.host.ownerDocument.body.append(dialog); dialog.showModal(); dialog.querySelector('[data-cancel]').focus();
    });
  }
}
