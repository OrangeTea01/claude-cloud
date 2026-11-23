import assert from 'node:assert/strict';
import test from 'node:test';
import { JSDOM } from 'jsdom';
import { AccountUsageMenu } from '../src/account-usage.js';

const tick = () => new Promise((resolve) => setImmediate(resolve));
const usage = () => ({ quota: { fiveHour: { limit: 50000, used: 20000, remaining: 30000, resetAt: null }, weekly: { limit: 100000, used: 20000, remaining: 80000 }, concurrencyLimit: 1, exhausted: false }, official: { fiveHour: { status: 'unknown' }, weekly: { usedPercent: 54, status: 'known' } }, activeTasks: [{ workspaceId: 'one', name: '<img src=x>', phase: 'awaiting_permission' }], idleMinutes: 30 });
function setup(api = async () => usage()) {
  const dom = new JSDOM('<button id="account">账号</button>'); const user = { id: 'alice', username: 'Alice <script>' }; const calls = [], timers = [];
  const audio = { enabled: true, status: '等待点击', setEnabled(value) { this.enabled = value; this.status = value ? '开启' : '关闭'; } };
  const menu = new AccountUsageMenu({ api: (...args) => { calls.push(args); return api(...args); }, getUser: () => user, audio, page: dom.window.document, setTimer: (fn) => { timers.push(fn); return timers.length; }, clearTimer: () => {} });
  const button = dom.window.document.querySelector('#account'); menu.mount(button);
  return { dom, user, calls, timers, menu, audio, button, node: (id) => dom.window.document.getElementById(id) };
}

test('account menu opens separately from logout, displays real quotas and safely escapes account/task names', async () => {
  const c = setup(); assert.equal(c.calls.length, 0);
  c.button.click(); await tick();
  assert.equal(c.button.getAttribute('aria-expanded'), 'true');
  assert.equal(c.calls[0][0], '/api/account/usage');
  assert.match(c.menu.panel.textContent, /2 万 Token/); assert.match(c.menu.panel.textContent, /3 万 Token/);
  assert.match(c.menu.panel.textContent, /同时最多 1 个任务/); assert.match(c.menu.panel.textContent, /30 分钟/);
  assert.equal(c.menu.panel.querySelectorAll('progress').length, 1);
  assert.equal(c.menu.panel.querySelectorAll('img,script').length, 0);
  assert.ok(c.menu.panel.querySelector('[data-action="logout"]'));
  c.node('account-sound-enabled').checked = false;
  c.node('account-sound-enabled').dispatchEvent(new c.dom.window.Event('change'));
  assert.equal(c.audio.enabled, false); assert.equal(c.node('account-sound-state').textContent, '关闭');
  c.menu.unmount(); c.dom.window.close();
});

test('manual refresh uses POST, deduplicates pending requests and reports failures without fabricated usage', async () => {
  let finish; const c = setup(() => new Promise((resolve) => { finish = resolve; }));
  c.menu.open(); await c.menu.refresh(true); assert.equal(c.calls.length, 1);
  finish(usage()); await tick();
  c.menu.api = async (...args) => { c.calls.push(args); throw new Error('<b>暂不可用</b>'); };
  await c.menu.refresh(true);
  assert.deepEqual(c.calls.at(-1), ['/api/account/usage/refresh', { method: 'POST' }]);
  assert.equal(c.node('account-usage-error').textContent, '<b>暂不可用</b>');
  assert.equal(c.node('account-usage-error').querySelector('b'), null);
  assert.equal(c.node('account-usage-refresh').disabled, false);
  c.menu.unmount(); c.dom.window.close();
});

test('closing or switching accounts discards late private responses and clears menu state', async () => {
  let finish; const c = setup(() => new Promise((resolve) => { finish = resolve; }));
  c.menu.open(); c.menu.unmount(); c.user.id = 'bob'; c.user.username = 'Bob';
  finish(usage()); await tick();
  assert.equal(c.node('account-usage-menu'), null); assert.equal(c.menu.panel, null);
  c.menu.mount(c.button); c.menu.api = async () => usage(); c.menu.open(); await tick();
  assert.match(c.menu.panel.textContent, /Bob/); assert.doesNotMatch(c.menu.panel.textContent, /Alice/);
  c.dom.window.document.dispatchEvent(new c.dom.window.KeyboardEvent('keydown', { key: 'Escape' }));
  assert.equal(c.node('account-usage-menu'), null);
  c.menu.unmount(); c.dom.window.close();
});

test('percentage allocation is distinct from official account totals and reports synchronization blocks safely', async () => {
  const result = usage(); result.quota = { mode: 'official_share', unit: 'percent', exhausted: false, blockedReason: '<b>等待下一周期官方数据同步</b>', fiveHour: { limit: 20, used: 3, remaining: 17 }, weekly: { limit: 20, used: 2.25, sharedAdjustment: 2, remaining: 15.75 } };
  const c = setup(async () => result); c.menu.open(); await tick();
  assert.match(c.menu.panel.textContent, /我的站内配额/); assert.match(c.menu.panel.textContent, /官方账号总额度/);
  assert.match(c.menu.panel.textContent, /17%/); assert.match(c.menu.panel.textContent, /15.75%/);
  assert.match(c.menu.panel.textContent, /账号共享扣减 2%/);
  assert.match(c.menu.panel.textContent, /不是官方个人账单/); assert.doesNotMatch(c.menu.panel.textContent, /万 Token|估算/);
  assert.match(c.menu.panel.textContent, /等待下一周期官方数据同步/); assert.equal(c.menu.panel.querySelector('.quota-status b'), null);
  c.menu.unmount(); c.dom.window.close();
});

test('solo menu shows optional official usage without personal allocation or shared-account instructions', async () => {
  const c = setup(async () => ({ quota: null, official: { errorCode: 'usage_authorization_required' }, activeTasks: [], idleMinutes: 30 }));
  c.menu.getMode = () => 'solo'; delete c.user.username;
  c.menu.open(); await tick();
  assert.equal(c.menu.panel.querySelector('.solo-management-menu').open, false);
  assert.match(c.menu.panel.textContent, /管理与设置 → 显示官方额度/);
  assert.doesNotMatch(c.menu.panel.textContent, /管理员|共享|站内配额|Token|undefined/);
  assert.equal(c.node('account-usage-error').hidden, true);
  c.menu.unmount(); c.dom.window.close();
});
