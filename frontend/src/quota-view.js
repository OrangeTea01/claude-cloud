export const escapeQuota = (value) => String(value ?? '').replace(/[&<>"']/g, (s) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[s]));
export const validTokenCount = (value) => Number.isSafeInteger(value) && value >= 0;
export function tokenText(value) {
  return validTokenCount(value) ? `${new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 4 }).format(value / 10000)} 万 Token` : '暂未知';
}
export const validPercentage = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100;
export function percentageText(value) {
  if (!validPercentage(value)) return '暂未知';
  if (value > 0 && value < 0.01) return '低于 0.01%';
  return `${new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(value)}%`;
}
export const quotaUnit = (quota) => quota?.unit === 'percent' || quota?.mode === 'official_share' ? 'percent' : 'tokens';
export const isIdleWindow = (window) => window?.state === 'idle' && window.resetAt === null;
export const IDLE_WINDOW_TEXT = '尚未开始新周期；首次使用后显示恢复时间';
export function quotaBlockReason(quota) {
  if (!quota || typeof quota !== 'object') return '';
  if (typeof quota.blockedReason === 'string' && quota.blockedReason.trim()) return quota.blockedReason;
  if (quotaUnit(quota) === 'percent' && ['fiveHour', 'weekly'].some((key) => !validPercentage(quota[key]?.remaining))) return '正在等待官方额度同步，同步完成后才能继续任务。';
  if (quota.exhausted || ['fiveHour', 'weekly'].some((key) => (quotaUnit(quota) === 'percent' || quota[key]?.limit !== null) && quota[key]?.remaining === 0)) return '站内配额已用完，请等待窗口恢复或联系管理员调整。';
  return '';
}
export function timeText(value) {
  if (value === null || value === undefined || value === '') return '待官方提供';
  const timestamp = typeof value === 'number' ? value * (value < 1e12 ? 1000 : 1) : value;
  const date = new Date(timestamp);
  return Number.isFinite(date.getTime()) ? date.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '待官方提供';
}
export function quotaWindowMarkup(window = {}, label, unit = 'tokens') {
  const { limit, used, remaining, resetAt, sharedAdjustment = 0 } = window;
  const percent = unit === 'percent', format = percent ? percentageText : tokenText;
  const adjustment = percent && validPercentage(sharedAdjustment) && sharedAdjustment > 0 ? `<p class="quota-adjustment">账号共享扣减 ${percentageText(sharedAdjustment)}</p>` : '';
  const reset = isIdleWindow(window) ? IDLE_WINDOW_TEXT : `下次恢复：${timeText(resetAt)}`;
  return `<section class="quota-window"><strong>${escapeQuota(label)}</strong><dl><div><dt>${percent ? '分配' : '额度'}</dt><dd>${!percent && limit === null ? '不限额' : format(limit)}</dd></div><div><dt>已用</dt><dd>${format(used)}</dd></div><div><dt>剩余</dt><dd>${!percent && limit === null && remaining === null ? '不限额' : format(remaining)}</dd></div></dl>${adjustment}<p>${escapeQuota(reset)}</p></section>`;
}
export function officialMarkup(official = {}, { now = Date.now() } = {}) {
  if (!official || typeof official !== 'object') official = {};
  const knownPercent = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100;
  const hasObservation = [official.fiveHour, official.weekly].some((item) => knownPercent(item?.usedPercent)
    || timeText(item?.updatedAt) !== '待官方提供') || timeText(official.updatedAt) !== '待官方提供';
  const errors = {
    no_running_workspace: '官方额度查询需要运行中的工作区，请先启动一个工作区再刷新。',
    usage_authorization_required: '请管理员在「官方额度查询授权」中完成登录，并确认与当前共享 Claude 账号相同。',
    usage_authorization_busy: '管理员正在完成额度查询授权，请稍后刷新。',
    query_timeout: '官方额度查询超时，请稍后重试。',
    query_failed: '官方额度查询失败，请稍后重试。',
    unavailable: '当前 Claude 客户端未返回官方额度，请稍后刷新或联系管理员检查客户端。',
    storage_failed: '官方额度查询结果保存失败，请联系管理员检查服务器存储后重试。',
  };
  const knownError = typeof official.errorCode === 'string' && Object.hasOwn(errors, official.errorCode) ? errors[official.errorCode] : '';
  const failed = Boolean(knownError || official.error || official.errorCode);
  const reason = knownError || (failed ? '官方额度查询暂不可用，请稍后刷新。' : !hasObservation ? '官方额度暂不可用，可稍后刷新。' : '');
  const status = reason;
  const retryTime = typeof official.retryAt === 'string' && official.retryAt.trim() ? new Date(official.retryAt).getTime() : NaN;
  const retry = Number.isFinite(retryTime) && retryTime > now ? `<p class="quota-retry">下次可刷新（查询间隔）：${escapeQuota(timeText(official.retryAt))}</p>` : '';
  const windows = [['fiveHour', '官方 5 小时窗口'], ['weekly', '官方周窗口']].map(([key, label]) => {
    const item = official[key] || {};
    // Some official reports contain a numeric meter without a status enum.
    // Its observed value is usable; absent/invalid numbers remain unknown.
    const known = knownPercent(item.usedPercent);
    const stale = typeof item.stale === 'boolean' ? item.stale : Boolean(failed || official.stale);
    const observed = known || timeText(item.updatedAt) !== '待官方提供';
    const freshness = stale && observed ? '上次获取的数据，当前状态待刷新。' : !stale && known ? '当前同步数据' : '';
    // A fresh idle window is a real zero meter whose countdown has not begun.
    // Explicitly unknown windows still have no percentage or invented reset.
    const reset = isIdleWindow(item) && known && item.usedPercent === 0 && !stale ? IDLE_WINDOW_TEXT : `恢复：${timeText(item.resetAt)}`;
    const updatedAt = Object.hasOwn(item, 'updatedAt') ? item.updatedAt : official.updatedAt;
    return `<section class="official-window" data-official-window="${key}"><div><strong>${label}</strong><span>${known ? `${Math.round(item.usedPercent * 10) / 10}% 已用` : '暂未知'}</span></div>${known ? `<progress max="100" value="${item.usedPercent}" aria-label="${label}已用百分比"></progress>` : '<p class="quota-unknown">尚未取得官方数据，不能推算剩余额度。</p>'}${freshness ? `<p class="official-window-freshness" data-stale="${stale}">${freshness}</p>` : ''}<p>${escapeQuota(reset)} · 更新：${escapeQuota(timeText(updatedAt))}</p></section>`;
  }).join('');
  return `<div class="official-usage"><h3>官方账号总额度</h3>${status ? `<p class="quota-status">${status}</p>` : ''}${retry}${windows}<p class="quota-footnote">这是共享 Claude 账号的总用量，与个人站内配额分开统计。</p></div>`;
}
export function parseWanTokens(value) {
  const input = String(value).trim();
  if (!input) return null;
  if (!/^\d+(?:\.\d{1,4})?$/.test(input)) throw new Error('请输入非负数，单位为万 Token，最多 4 位小数；留空表示不限额。');
  const [whole, decimal = ''] = input.split('.');
  const result = Number(whole) * 10000 + Number(decimal.padEnd(4, '0'));
  if (!validTokenCount(result) || result > 1_000_000_000_000) throw new Error('额度数值过大，每个窗口最多 100,000,000 万 Token。');
  return result;
}
export function quotaDistribution(users, fiveHourTotal, weeklyTotal) {
  const eligible = users.filter((user) => !user.deleting).slice().sort((a, b) => a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
  if (!eligible.length) throw new Error('没有可分配额度的用户。');
  for (const total of [fiveHourTotal, weeklyTotal]) if (total !== null && !validTokenCount(total)) throw new Error('总额度格式不正确。');
  const share = (total, index) => total === null ? null : Math.floor(total / eligible.length) + (index < total % eligible.length ? 1 : 0);
  return eligible.map((user, index) => ({ id: user.id, username: user.username, fiveHourLimit: share(fiveHourTotal, index), weeklyLimit: share(weeklyTotal, index) }));
}
