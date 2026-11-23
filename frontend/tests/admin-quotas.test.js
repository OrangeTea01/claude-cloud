import assert from 'node:assert/strict';
import test from 'node:test';
import { JSDOM } from 'jsdom';
import { AdminQuotasPanel, validQuotaView } from '../src/admin-quotas.js';
import { AdminPage } from '../src/admin.js';

const bucket = (limit = 10000, used = 2000) => ({ limit, used, remaining: limit === null ? null : Math.max(0, limit - used), resetAt: null });
const account = (id, username, deleting = false) => ({ id, username, deleting, quota: { fiveHour: bucket(), weekly: bucket(null) } });
const view = () => ({ users: [account('b', 'bob'), account('a', 'alice'), account('z', 'old', true)], official: { fiveHour: { usedPercent: null, resetAt: null, status: 'unknown' }, weekly: { usedPercent: null, resetAt: null, status: 'unknown' }, updatedAt: null, stale: true } });
const automaticFixture = () => ({ ...view(), settings: { mode: 'tokens', userIds: [] },
  users: ['owner', 'alice', 'bob', 'carol', 'david', 'erica'].map((name) => account(name, name)),
  official: { fiveHour: { usedPercent: 40, stale: false, resetAt: new Date(Date.now() + 3600000).toISOString() },
    weekly: { usedPercent: 20, stale: false, resetAt: new Date(Date.now() + 7 * 86400000).toISOString() }, stale: false } });
const automaticReply = (data, ids, { pending = false, used = 0 } = {}) => ({ ...data, settings: { mode: 'official_share', userIds: ids },
  users: data.users.map((user) => {
    const included = ids.includes(user.id), amount = pending && included ? null : included ? 12 : 0;
    const window = { limit: amount, used: amount === null ? null : used, remaining: amount === null ? null : Math.max(0, amount - used), resetAt: null };
    return { ...user, quota: { unit: 'percent', mode: 'official_share', fiveHour: { ...window }, weekly: { ...window }, blockedReason: !included ? '未列入站内配额分配名单' : pending ? '等待官方额度更新' : null } };
  }) });
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; };
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
function fixture(handler = () => view()) {
  const dom = new JSDOM('<!doctype html><body><section id="host"></section>', { url: 'https://fixture.test/admin/' });
  dom.window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  dom.window.HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new dom.window.Event('close')); };
  const host = dom.window.document.querySelector('#host'), calls = [], busy = [];
  let authenticated = true, expired = 0;
  const panel = new AdminQuotasPanel({ request: async (...args) => { calls.push(args); return handler(...args); }, session: () => ({ authenticated }),
    expire: () => { authenticated = false; expired++; panel.unmount(); }, onBusy: (value) => busy.push(value) });
  panel.mount(host);
  const node = (id) => host.querySelector(`#${id}`);
  const form = (id) => [...host.querySelectorAll('[data-quota-user]')].find((item) => item.dataset.quotaUser === id);
  const field = (id, key) => form(id).querySelector(`[data-window="${key}"]`);
  const confirm = () => dom.window.document.querySelector('[data-confirm]').click();
  const cancel = () => dom.window.document.querySelector('[data-cancel]').click();
  const select = (ids) => host.querySelectorAll('[data-automatic-user]').forEach((input) => {
    input.checked = ids.includes(input.dataset.automaticUser); input.dispatchEvent(new dom.window.Event('change'));
  });
  return { dom, host, panel, node, form, field, confirm, cancel, select, calls, busy, expired: () => expired };
}

test('unknown official usage has no zero progress; quota values and hostile names render safely', async () => {
  const data = view(); data.users[0].username = '<img src=x onerror=bad()>';
  const c = fixture(() => data); await c.panel.refresh();
  assert.equal(c.host.querySelectorAll('.official-window progress').length, 0);
  assert.match(c.host.textContent, /暂未知/);
  assert.match(c.form('b').textContent, /已用 0.2 万 Token/);
  assert.match(c.form('b').textContent, /剩余 0.8 万 Token/);
  assert.match(c.form('b').textContent, /<img src=x onerror=bad\(\)>/);
  assert.equal(c.host.querySelector('img'), null);
  assert.equal(c.field('b', 'weekly').value, '');
  assert.equal(c.form('z').querySelector('button').disabled, true);
});

test('single user changes require confirmation, convert units exactly and preserve used values', async () => {
  const data = view();
  const c = fixture((path, options) => {
    if (options?.method === 'PUT') { data.users[0].quota.fiveHour = bucket(options.body.fiveHourLimit); data.users[0].quota.weekly = bucket(options.body.weeklyLimit); }
    return data;
  });
  await c.panel.refresh(); c.field('b', 'fiveHour').value = '0.1234'; c.field('b', 'weekly').value = '0';
  const saving = c.panel.saveUser('b');
  assert.equal(c.calls.length, 1);
  assert.match(c.dom.window.document.querySelector('dialog').textContent, /0.1234 万 Token/);
  assert.match(c.dom.window.document.querySelector('dialog').textContent, /剩余 0 万 Token/);
  await c.panel.saveUser('b'); c.confirm(); await saving;
  assert.deepEqual(c.calls[1], ['users/b/quota', { method: 'PUT', body: { fiveHourLimit: 1234, weeklyLimit: 0 } }]);
  assert.match(c.node('admin-quota-message').textContent, /已更新 bob/);
  assert.equal(c.panel.view.users[0].quota.fiveHour.used, 2000);
  assert.equal(c.panel.busy, false);
});

test('cancel and invalid input never write any quota', async () => {
  const c = fixture(); await c.panel.refresh();
  c.field('b', 'fiveHour').value = '-1'; await c.panel.saveUser('b');
  assert.match(c.node('admin-quota-message').textContent, /非负数/);
  c.field('b', 'fiveHour').value = '2';
  const saving = c.panel.saveUser('b'); c.cancel(); await saving;
  assert.equal(c.calls.length, 1);
  assert.equal(c.panel.busy, false);
});

test('distribution refreshes eligible users then previews exact integer remainder and keeps usage', async () => {
  const c = fixture(); await c.panel.refresh();
  c.node('quota-total-five').value = '0.0005'; c.node('quota-total-weekly').value = '';
  const saving = c.panel.distribute(); await tick();
  assert.deepEqual(c.calls.map((item) => item[0]), ['quotas', 'quotas']);
  const rows = [...c.dom.window.document.querySelectorAll('dialog tbody tr')];
  assert.equal(rows.length, 2);
  assert.match(rows[0].textContent, /alice.*0.0003 万 Token/);
  assert.match(rows[1].textContent, /bob.*0.0002 万 Token/);
  assert.match(rows[0].textContent, /不限额/);
  c.confirm(); await saving;
  assert.deepEqual(c.calls[2], ['quotas/distribute', { method: 'POST', body: { fiveHourTotal: 5, weeklyTotal: null, confirmation: 'DISTRIBUTE_QUOTA' } }]);
  assert.match(c.node('admin-quota-message').textContent, /已用量保留/);
});

test('distribution uses freshly loaded users rather than stale preview membership', async () => {
  let reads = 0;
  const c = fixture(() => { const data = view(); if (++reads > 1) data.users.push(account('c', 'carol')); return data; });
  await c.panel.refresh(); c.node('quota-total-five').value = '0.0005';
  const saving = c.panel.distribute(); await tick();
  assert.match(c.dom.window.document.querySelector('dialog h2').textContent, /3 个用户/);
  assert.equal(c.dom.window.document.querySelectorAll('dialog tbody tr').length, 3);
  c.cancel(); await saving;
});

test('failed write has no success message, redacts errors and requires fresh read before retry', async () => {
  const c = fixture((path, options) => { if (options) throw new Error('private OAuth secret'); return view(); });
  await c.panel.refresh(); c.field('b', 'fiveHour').value = '2';
  const saving = c.panel.saveUser('b'); c.confirm(); await saving;
  assert.equal(c.node('admin-quota-message').dataset.kind, 'error');
  assert.doesNotMatch(c.host.textContent, /private OAuth secret|已更新 bob/);
  assert.equal(c.form('b').querySelector('button').disabled, true);
  assert.equal(c.node('admin-quota-refresh').disabled, false);
  await c.panel.refresh(); assert.equal(c.form('b').querySelector('button').disabled, false);
});

test('malformed response cannot claim success or enable editing', async () => {
  const c = fixture(() => ({ ...view(), users: [{ ...account('a', 'alice'), quota: { fiveHour: bucket(), weekly: bucket(-1) } }] }));
  await c.panel.refresh();
  assert.equal(c.panel.view, null); assert.equal(c.node('admin-quota-distribute').disabled, true);
  assert.equal(c.node('admin-quota-message').dataset.kind, 'error');
  assert.equal(validQuotaView({ ...view(), users: [account('a', 'alice'), account('a', 'another')] }), false);
});

test('session expiry unmounts panel and closes pending confirmation without saving', async () => {
  const c = fixture(); await c.panel.refresh(); c.field('b', 'fiveHour').value = '2';
  const saving = c.panel.saveUser('b'); c.panel.unmount(); await saving;
  assert.equal(c.dom.window.document.querySelector('dialog'), null);
  assert.equal(c.calls.length, 1); assert.equal(c.panel.view, null);
});

test('late successful write after unmount cannot restore account quota details', async () => {
  const gate = deferred();
  const c = fixture((path, options) => options ? gate.promise : view()); await c.panel.refresh();
  c.field('b', 'fiveHour').value = '2'; const saving = c.panel.saveUser('b'); c.confirm(); await tick();
  c.panel.unmount(); gate.resolve(view()); await saving;
  assert.equal(c.panel.view, null); assert.equal(c.panel.host, null);
});

test('unauthorized fetch expires administrator access', async () => {
  const c = fixture(() => { const error = new Error('private'); error.status = 401; throw error; });
  await c.panel.refresh(); assert.equal(c.expired(), 1); assert.equal(c.panel.host, null);
});

test('manual official refresh uses the read-only query route and waits for the result', async () => {
  const gate = deferred();
  const c = fixture((path) => path === 'quotas/refresh' ? gate.promise : view());
  await c.panel.refresh();
  const refresh = c.panel.refresh({ official: true });
  assert.deepEqual(c.calls[1], ['quotas/refresh', { method: 'POST' }]);
  assert.equal(c.node('admin-quota-refresh').disabled, true);
  await c.panel.refresh({ official: true }); assert.equal(c.calls.length, 2);
  gate.resolve(view()); await refresh;
  assert.equal(c.node('admin-quota-refresh').disabled, false);
});

test('page integration sends admin CSRF and prevents account actions during quota confirmation', async () => {
  const c = fixture(), calls = [];
  const response = (body) => ({ ok: true, status: 200, json: async () => body });
  const page = new AdminPage(c.host, { usageFactory: () => ({ mount() {}, controls() {}, unmount() {} }), claudeFactory: () => ({ mount() {}, controls() {}, unmount() {} }), fetch: async (path, options) => {
    calls.push({ path, ...options });
    if (path.endsWith('/session')) return response({ enabled: true, authenticated: true, csrfToken: 'only-admin-csrf' });
    if (path.endsWith('/model-policy')) return response({ fableAllowed: false, revision: 0 });
    if (path.endsWith('/users')) return response({ users: view().users });
    if (path.endsWith('/workspaces')) return response({ workspaces: [] });
    if (path.endsWith('/quotas/automatic')) return response(automaticReply(view(), JSON.parse(options.body).userIds));
    if (path.endsWith('/quotas') || path.endsWith('/quota') || path.endsWith('/quotas/refresh')) return response(view());
    throw new Error('Unexpected request');
  } });
  await page.start();
  await page.quotas.refresh({ official: true });
  const officialRefresh = calls.find((item) => item.path.endsWith('/quotas/refresh'));
  assert.equal(officialRefresh.method, 'POST'); assert.equal(officialRefresh.headers['X-CSRF-Token'], 'only-admin-csrf');
  const form = c.host.querySelector('[data-quota-user="b"]'); form.querySelector('[data-window="fiveHour"]').value = '2';
  const saving = page.quotas.saveUser('b');
  assert.equal(c.host.querySelector('#create-user-submit').disabled, true);
  assert.equal(c.host.querySelector('#admin-logout').disabled, true);
  await page.logout(); await page.createUser();
  c.confirm(); await saving;
  const write = calls.find((item) => item.method === 'PUT');
  assert.equal(write.path, '/api/admin/users/b/quota'); assert.equal(write.headers['X-CSRF-Token'], 'only-admin-csrf');
  assert.equal(write.credentials, 'same-origin');
  assert.equal(c.host.querySelector('#create-user-submit').disabled, false);
  c.select(['a']);
  const automatic = page.quotas.changeAutomatic(true); await tick(); c.confirm(); await automatic;
  const automaticWrite = calls.find((item) => item.path.endsWith('/quotas/automatic'));
  assert.equal(automaticWrite.method, 'PUT'); assert.equal(automaticWrite.headers['X-CSRF-Token'], 'only-admin-csrf');
  assert.deepEqual(JSON.parse(automaticWrite.body), { enabled: true, userIds: ['a'], confirmation: 'CHANGE_AUTOMATIC_QUOTA' });
  page.expire(); assert.equal(c.host.querySelector('#admin-quotas-card'), null);
});

test('automatic mode selects five explicit people, previews remaining windows and sends no Token limits', async () => {
  const data = automaticFixture(), ids = ['alice', 'bob', 'carol', 'david', 'erica'];
  const c = fixture((path, options) => path === 'quotas/automatic' ? automaticReply(data, options.body.userIds) : data);
  await c.panel.refresh();
  assert.equal(c.host.querySelectorAll('[data-automatic-user]:checked').length, 0);
  assert.equal(c.node('admin-quota-automatic-apply').disabled, true);
  c.select(ids);
  assert.match(c.node('admin-quota-selection-summary').textContent, /5 个用户.*20%/);
  const saving = c.panel.changeAutomatic(true); await tick();
  const dialog = c.dom.window.document.querySelector('dialog');
  assert.equal(dialog.querySelectorAll('tbody tr').length, 5);
  assert.match(dialog.textContent, /12%/); assert.match(dialog.textContent, /16%/); assert.match(dialog.textContent, /20%/);
  assert.doesNotMatch(dialog.textContent, /owner/);
  c.confirm(); await saving;
  assert.deepEqual(c.calls.at(-1), ['quotas/automatic', { method: 'PUT', body: { enabled: true, userIds: ids, confirmation: 'CHANGE_AUTOMATIC_QUOTA' } }]);
  assert.equal(c.node('admin-quota-distribution-form'), null);
  assert.equal(c.host.querySelectorAll('[data-window]').length, 0);
  assert.match(c.form('owner').textContent, /未参与分配/);
  assert.match(c.form('alice').textContent, /站内配额.*12%/s);
  assert.match(c.host.textContent, /Token 占比/);
  assert.doesNotMatch(c.host.textContent, /估算|精准/);
});

test('automatic participant count is selectable and owner is only included explicitly', async () => {
  const c = fixture(() => automaticFixture()); await c.panel.refresh();
  c.select(['owner', 'alice']);
  const saving = c.panel.changeAutomatic(true); await tick();
  const dialog = c.dom.window.document.querySelector('dialog');
  assert.match(dialog.textContent, /2 个用户.*50%/s);
  assert.equal(dialog.querySelectorAll('tbody tr').length, 2);
  assert.match(dialog.textContent, /owner/);
  c.cancel(); await saving; assert.equal(c.calls.filter(([, options]) => options?.method === 'PUT').length, 0);
});

test('unknown official meters keep automatic allocation pending instead of reporting a funded quota', async () => {
  const data = automaticFixture(); data.official = view().official;
  const c = fixture((path, options) => path === 'quotas/automatic' ? automaticReply(data, options.body.userIds, { pending: true }) : data);
  await c.panel.refresh(); c.select(['alice', 'bob']);
  const saving = c.panel.changeAutomatic(true); await tick();
  const dialog = c.dom.window.document.querySelector('dialog');
  assert.match(dialog.textContent, /待官方同步/);
  assert.doesNotMatch(dialog.textContent, /本轮.*0%/s);
  c.confirm(); await saving;
  assert.equal(c.panel.view.settings.mode, 'official_share');
  assert.match(c.node('admin-quota-message').textContent, /额度将按官方数据分配/);
  assert.match(c.form('alice').textContent, /等待官方额度更新/);
  assert.doesNotMatch(c.form('alice').textContent, /不限额/);
});

test('fresh idle official window with no reset time previews a complete share and can enable allocation', async () => {
  const data = automaticFixture(), ids = ['alice', 'bob', 'carol', 'david', 'erica'];
  data.official.fiveHour = { usedPercent: 0, resetAt: null, state: 'idle', idleSince: new Date().toISOString(), updatedAt: new Date().toISOString(), stale: false };
  const c = fixture((path, options) => {
    if (path !== 'quotas/automatic') return data;
    const reply = automaticReply(data, options.body.userIds);
    for (const user of reply.users.filter((user) => ids.includes(user.id))) user.quota.fiveHour = { limit: 20, used: 0, remaining: 20, resetAt: null, state: 'idle' };
    return reply;
  });
  await c.panel.refresh(); c.select(ids);
  const saving = c.panel.changeAutomatic(true); await tick();
  const dialog = c.dom.window.document.querySelector('dialog');
  assert.deepEqual([...dialog.querySelectorAll('tbody tr')].map((row) => row.cells[1].textContent), Array(5).fill('20%'));
  assert.deepEqual([...dialog.querySelectorAll('tbody tr')].map((row) => row.cells[2].textContent), Array(5).fill('16%'), 'normal future weekly reset still previews official remaining share');
  assert.match(dialog.textContent, /尚未开始新周期；首次使用后显示恢复时间/);
  assert.doesNotMatch(dialog.querySelector('tbody').textContent, /待官方同步/);
  c.confirm(); await saving;
  assert.equal(c.panel.view.settings.mode, 'official_share');
  assert.equal(c.calls.at(-1)[1].body.enabled, true);
  assert.match(c.form('alice').textContent, /尚未开始新周期；首次使用后显示恢复时间/);
});

test('null reset remains pending without a fresh explicit idle zero meter', async () => {
  for (const meter of [{ usedPercent: 0, stale: false, resetAt: null }, { usedPercent: null, stale: false, resetAt: null, state: 'idle' }, { usedPercent: 0, stale: true, resetAt: null, state: 'idle' }, { usedPercent: 20, stale: false, resetAt: null, state: 'idle' }]) {
    const data = automaticFixture(); data.official.fiveHour = meter;
    const c = fixture(() => data); await c.panel.refresh(); c.select(['alice']);
    const saving = c.panel.changeAutomatic(true); await tick();
    const cell = c.dom.window.document.querySelector('dialog tbody tr').cells[1];
    assert.equal(cell.textContent, '待官方同步'); c.cancel(); await saving;
    assert.equal(c.calls.filter(([, options]) => options?.method === 'PUT').length, 0);
  }
});

test('same-list repeat preview keeps existing allocations and usage rather than reallocating remaining balance', async () => {
  const data = automaticReply(automaticFixture(), ['alice', 'bob'], { used: 3 });
  const c = fixture(() => data); await c.panel.refresh();
  const saving = c.panel.changeAutomatic(true); await tick();
  const dialog = c.dom.window.document.querySelector('dialog');
  assert.match(dialog.textContent, /已用量不会清零/);
  assert.match(dialog.textContent, /12%.*已用 3%.*剩余 9%/s);
  c.cancel(); await saving;
  assert.equal(c.calls.filter(([, options]) => options?.method === 'PUT').length, 0);
});

test('new accounts are not silently added to an active automatic allocation', async () => {
  const data = automaticReply(automaticFixture(), ['alice', 'bob']);
  const c = fixture(() => data); await c.panel.refresh();
  assert.deepEqual([...c.host.querySelectorAll('[data-automatic-user]:checked')].map((input) => input.dataset.automaticUser), ['alice', 'bob']);
  assert.equal(c.host.querySelector('[data-automatic-user="owner"]').checked, false);
  assert.equal(c.host.querySelector('[data-automatic-user="carol"]').checked, false);
  assert.match(c.form('carol').textContent, /未列入站内配额分配名单/);
  await c.panel.saveUser('alice'); await c.panel.distribute(); assert.equal(c.calls.length, 1);
});

test('reopening the saved member list does not preview a fresh grant for the same window', async () => {
  const data = automaticFixture(); data.settings.userIds = ['alice', 'bob'];
  const c = fixture(() => data); await c.panel.refresh();
  const saving = c.panel.changeAutomatic(true); await tick();
  const dialog = c.dom.window.document.querySelector('dialog');
  assert.match(dialog.textContent, /恢复原有分配，当前窗口已用量不会清零/);
  assert.match(dialog.textContent, /原窗口已结束/);
  assert.deepEqual([...dialog.querySelectorAll('tbody tr')].map((row) => row.cells[1].textContent), ['保留原配额，待同步', '保留原配额，待同步']);
  assert.doesNotMatch(dialog.textContent, /30%/);
  c.cancel(); await saving;
  assert.equal(c.calls.filter(([, options]) => options?.method === 'PUT').length, 0);
});

test('automatic setting conflicts show actionable guidance without claiming changes or revealing server text', async () => {
  const data = automaticFixture();
  const c = fixture((path) => {
    if (path === 'quotas/automatic') { const error = new Error('private server token'); error.status = 409; throw error; }
    return data;
  });
  await c.panel.refresh(); c.select(['alice']);
  const saving = c.panel.changeAutomatic(true); await tick(); c.confirm(); await saving;
  assert.equal(c.panel.view.settings.mode, 'tokens');
  assert.match(c.node('admin-quota-message').textContent, /停止运行中的任务/);
  assert.match(c.node('admin-quota-message').textContent, /两个官方窗口结束/);
  assert.doesNotMatch(c.host.textContent, /private server token|自动配额已启用/);
});

test('automatic disable requires confirmation and only restores manual controls after server acknowledgement', async () => {
  const manual = automaticFixture(), enabled = automaticReply(manual, ['alice', 'bob']);
  const gate = deferred();
  const c = fixture((path) => path === 'quotas/automatic' ? gate.promise : enabled);
  await c.panel.refresh(); const saving = c.panel.changeAutomatic(false); await tick();
  assert.match(c.dom.window.document.querySelector('dialog').textContent, /恢复手动 Token 配额/);
  c.confirm(); await tick();
  assert.equal(c.node('admin-quota-distribution-form'), null);
  assert.deepEqual(c.calls.at(-1)[1].body, { enabled: false, userIds: [], confirmation: 'CHANGE_AUTOMATIC_QUOTA' });
  gate.resolve(manual); await saving;
  assert.ok(c.node('admin-quota-distribution-form'));
  assert.match(c.node('admin-quota-message').textContent, /恢复手动站内配额/);
});

test('removed selected accounts abort the automatic preview, and expiry prevents its pending write', async () => {
  const data = automaticFixture(); let reads = 0;
  const c = fixture(() => { const current = structuredClone(data); if (++reads > 1) current.users = current.users.filter((user) => user.id !== 'alice'); return current; });
  await c.panel.refresh(); c.select(['alice']); await c.panel.changeAutomatic(true);
  assert.match(c.node('admin-quota-message').textContent, /用户列表已经改变/);
  assert.equal(c.dom.window.document.querySelector('dialog'), null);
  assert.equal(c.calls.filter(([, options]) => options?.method === 'PUT').length, 0);
  const d = fixture(() => data); await d.panel.refresh(); d.select(['bob']);
  const saving = d.panel.changeAutomatic(true); await tick(); d.panel.unmount(); await saving;
  assert.equal(d.dom.window.document.querySelector('dialog'), null);
  assert.equal(d.calls.filter(([, options]) => options?.method === 'PUT').length, 0);
});

test('malformed or mismatched automatic acknowledgements never claim the chosen policy was saved', async () => {
  const data = automaticFixture();
  assert.equal(validQuotaView({ ...automaticReply(data, ['alice']), settings: { mode: 'official_share', userIds: ['alice', 'alice'] } }), false);
  const malformed = automaticReply(data, ['alice']); malformed.users[0].quota.fiveHour.used = -1;
  assert.equal(validQuotaView(malformed), false);
  const c = fixture((path) => path === 'quotas/automatic' ? automaticReply(data, ['owner']) : data);
  await c.panel.refresh(); c.select(['alice']); const saving = c.panel.changeAutomatic(true); await tick(); c.confirm(); await saving;
  assert.equal(c.panel.view.settings.mode, 'tokens');
  assert.equal(c.node('admin-quota-message').dataset.kind, 'error');
  assert.doesNotMatch(c.node('admin-quota-message').textContent, /已启用/);
});
