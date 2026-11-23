import assert from 'node:assert/strict';
import test from 'node:test';
import { AdminPage, passwordValidation } from '../src/admin.js';

// DOM stand-in keeps real controller requests and input state observable.
// Page layout and native labels are independently checked in a browser.
class Node {
  constructor(tag = '') { this.tag = tag; this.value = ''; this.disabled = false; this.hidden = false; this.textContent = ''; this.dataset = {}; this.listeners = {}; this.attributes = {}; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  setAttribute(name, value) { this.attributes[name] = value; }
}
class Host {
  set innerHTML(value) {
    this.html = value; this.nodes = new Map();
    for (const match of value.matchAll(/<([\w-]+)[^>]*\bid="([^"]+)"[^>]*>/g)) this.nodes.set(`#${match[2]}`, new Node(match[1]));
  }
  get innerHTML() { return this.html; }
  querySelector(selector) { return this.nodes?.get(selector); }
  querySelectorAll(selector) { return [...(this.nodes?.values() || [])].filter((node) => selector.split(', ').includes(node.tag)); }
}
function deferred() { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; }
function response(data, status = 200) { return { ok: status >= 200 && status < 300, status, json: async () => data }; }
function setup(handler, policyHandler = () => response({ fableAllowed: false, revision: 0 })) {
  const calls = [], policyCalls = [], host = new Host();
  const page = new AdminPage(host, { usageFactory: () => ({ mount() {}, controls() {}, unmount() {} }), quotaFactory: () => ({ mount() {}, controls() {}, unmount() {}, async refresh() {} }), claudeFactory: () => ({ mount() {}, controls() {}, unmount() {} }), fetch: async (...args) => {
    if (args[0].endsWith('/model-policy')) { policyCalls.push(args); return policyHandler(...args); }
    if (args[0].endsWith('/users') && args[1].method === 'GET') return response({ users: [{ id: 'owner-id', username: 'owner', createdAt: '' }] });
    if (args[0].endsWith('/workspaces') && args[1].method === 'GET') return response({ workspaces: [] });
    calls.push(args); return handler(...args);
  } });
  const node = (id) => host.querySelector(`#${id}`);
  return { page, calls, policyCalls, host, node };
}
const loggedIn = { enabled: true, authenticated: true, csrfToken: 'admin-csrf-token' };
const loginState = { enabled: true, authenticated: false };
const fillNew = (c, value = 'New dashboard password 2026') => { c.node('new-password').value = value; c.node('confirm-password').value = value; };

test('solo administration entry redirects to the unified website without a separate login', async () => {
  for (const authenticated of [false, true]) {
    const c = setup(() => response({ enabled: true, authenticated, mode: 'solo' }));
    const redirects = [];
    c.host.ownerDocument = { defaultView: { location: { replace: (url) => redirects.push(url) } } };
    await c.page.start();
    assert.deepEqual(redirects, ['/#settings']);
    assert.equal(c.node('admin-password'), undefined);
    assert.equal(c.calls.length, 1);
  }
});

test('disabled admin setup cannot submit or masquerade as an authenticated page', async () => {
  const c = setup(() => response({ enabled: false, authenticated: false }));
  await c.page.start();
  assert.equal(c.page.phase, 'disabled');
  assert.equal(c.node('admin-password'), undefined);
  assert.equal(c.node('new-password'), undefined);
  await c.page.login(); await c.page.changePassword();
  assert.equal(c.calls.length, 1);
  assert.match(c.host.innerHTML, /configure_admin\.py/);
});

test('admin login is independent, clears submitted password, and prevents duplicate requests', async () => {
  const gate = deferred();
  const c = setup((path) => path.endsWith('/session') ? response(loginState) : gate.promise);
  await c.page.start();
  c.node('admin-password').value = 'Admin fixture password';
  const login = c.page.login();
  assert.equal(c.node('admin-password').value, '');
  assert.equal(c.node('admin-submit').disabled, true);
  await c.page.login();
  assert.equal(c.calls.length, 2);
  const [path, options] = c.calls[1];
  assert.equal(path, '/api/admin/login');
  assert.equal(options.method, 'POST');
  assert.equal(options.credentials, 'same-origin');
  assert.equal(options.cache, 'no-store');
  assert.equal(options.headers['X-CSRF-Token'], undefined);
  assert.deepEqual(JSON.parse(options.body), { password: 'Admin fixture password' });
  gate.resolve(response({ authenticated: true, csrfToken: 'new-admin-csrf' }));
  await login;
  assert.equal(c.page.phase, 'ready');
  assert.equal(c.page.csrf, 'new-admin-csrf');
  assert.equal(c.node('admin-password'), undefined);
  assert.equal(c.node('new-password').value, '');
});

test('invalid or mismatched passwords never reach the server and validation does not expose them', async () => {
  const c = setup(() => response(loggedIn));
  await c.page.start();
  fillNew(c, 'short');
  await c.page.changePassword();
  assert.equal(c.calls.length, 1);
  assert.match(c.node('admin-message').textContent, /12/);
  fillNew(c); c.node('confirm-password').value = 'Different new password';
  await c.page.changePassword();
  assert.equal(c.calls.length, 1);
  assert.match(c.node('admin-message').textContent, /不一致/);
  assert.doesNotMatch(c.node('admin-message').textContent, /Different/);
  assert.notEqual(passwordValidation('😀'.repeat(6), '😀'.repeat(6)), '');
});

test('password update uses admin CSRF, waits for confirmed success, clears fields and does not resubmit', async () => {
  const gate = deferred();
  const c = setup((path) => path.endsWith('/session') ? response(loggedIn) : gate.promise);
  await c.page.start(); fillNew(c);
  const save = c.page.changePassword();
  await c.page.changePassword(); await c.page.logout();
  assert.equal(c.calls.length, 2);
  assert.equal(c.node('admin-submit').disabled, true);
  assert.equal(c.node('admin-logout').disabled, true);
  assert.equal(c.node('admin-message').textContent, '');
  const [path, request] = c.calls[1];
  assert.equal(path, '/api/admin/users/owner-id/password');
  assert.equal(request.method, 'PUT');
  assert.equal(request.headers['X-CSRF-Token'], 'admin-csrf-token');
  assert.deepEqual(JSON.parse(request.body), { newPassword: 'New dashboard password 2026', confirmPassword: 'New dashboard password 2026' });
  gate.resolve(response({ ok: true, sessionsRevoked: true })); await save;
  assert.equal(c.node('new-password').value, '');
  assert.equal(c.node('confirm-password').value, '');
  assert.equal(c.page.phase, 'ready');
  assert.equal(c.node('admin-message').dataset.kind, 'success');
  assert.match(c.node('admin-message').textContent, /旧密码已失效/);
  assert.equal(c.node('admin-submit').disabled, false);
});

test('expired admin session clears new passwords and returns to a clean admin login', async () => {
  const c = setup((path) => path.endsWith('/session') ? response(loggedIn) : response({ detail: 'never show submitted secret' }, 401));
  await c.page.start(); fillNew(c);
  await c.page.changePassword();
  assert.equal(c.page.phase, 'login');
  assert.equal(c.page.csrf, '');
  assert.equal(c.node('new-password'), undefined);
  assert.equal(c.node('admin-password').value, '');
  assert.match(c.node('admin-message').textContent, /登录已过期/);
  assert.doesNotMatch(c.node('admin-message').textContent, /secret/);
});

test('server failure and malformed success never claim that a password was saved', async () => {
  let status = 503;
  const c = setup((path) => path.endsWith('/session') ? response(loggedIn) : response({ detail: 'sensitive backend details' }, status));
  await c.page.start(); fillNew(c);
  await c.page.changePassword();
  assert.equal(c.node('admin-message').dataset.kind, 'error');
  assert.doesNotMatch(c.node('admin-message').textContent, /sensitive|已更新/);
  assert.match(c.node('admin-message').textContent, /存储/);
  assert.equal(c.node('new-password').value, '', 'submitted passwords are cleared even when the response fails');
  assert.equal(c.node('admin-submit').disabled, false);
  fillNew(c); status = 200;
  await c.page.changePassword();
  assert.equal(c.node('admin-message').dataset.kind, 'error');
  assert.match(c.node('admin-message').textContent, /未确认密码/);
});

test('logout uses admin CSRF and clears editable passwords only after acknowledgement', async () => {
  const c = setup((path) => path.endsWith('/session') ? response(loggedIn) : response({ ok: true }));
  await c.page.start(); fillNew(c);
  await c.page.logout();
  assert.equal(c.calls[1][0], '/api/admin/logout');
  assert.equal(c.calls[1][1].headers['X-CSRF-Token'], 'admin-csrf-token');
  assert.equal(c.calls[1][1].body, undefined);
  assert.equal(c.page.phase, 'login');
  assert.equal(c.page.csrf, '');
  assert.equal(c.node('new-password'), undefined);
});

test('malformed logout acknowledgement does not claim that the admin session was revoked', async () => {
  const c = setup((path) => path.endsWith('/session') ? response(loggedIn) : response({}));
  await c.page.start();
  await c.page.logout();
  assert.equal(c.page.phase, 'ready');
  assert.equal(c.page.csrf, 'admin-csrf-token');
  assert.match(c.node('admin-message').textContent, /未确认退出/);
  assert.equal(c.node('admin-logout').disabled, false);
});

test('broken initial session responses can be retried without enabling password operations', async () => {
  let valid = false;
  const c = setup(() => response(valid ? loginState : {}));
  await c.page.start();
  assert.equal(c.page.phase, 'unavailable');
  assert.equal(c.node('new-password'), undefined);
  valid = true; await c.page.start();
  assert.equal(c.page.phase, 'login');
  assert.equal(c.node('admin-password').disabled, false);
});

test('model permission is loaded independently and refreshing preserves password drafts', async () => {
  const c = setup(() => response(loggedIn));
  await c.page.start();
  assert.equal(c.policyCalls.length, 1);
  assert.equal(c.policyCalls[0][0], '/api/admin/model-policy');
  assert.equal(c.policyCalls[0][1].method, 'GET');
  assert.equal(c.policyCalls[0][1].body, undefined);
  assert.equal(c.node('fable-allowed').checked, false);
  assert.equal(c.node('fable-allowed').disabled, false);
  fillNew(c);
  await c.page.loadPolicy();
  assert.equal(c.node('new-password').value, 'New dashboard password 2026');
  assert.equal(c.node('confirm-password').value, 'New dashboard password 2026');
  assert.equal(c.node('fable-policy-state').textContent, '已禁止');
});

test('allowing Fable waits for the policy ACK and uses admin authorization without duplicate operations', async () => {
  const gate = deferred();
  const c = setup(() => response(loggedIn), (path, request) => request.method === 'GET' ? response({ fableAllowed: false, revision: 0 }) : gate.promise);
  await c.page.start(); fillNew(c);
  c.node('fable-allowed').checked = true;
  const change = c.page.changePolicy(true);
  assert.equal(c.node('fable-allowed').checked, false, 'show the last confirmed value until server acknowledgement');
  assert.equal(c.node('fable-allowed').disabled, true);
  assert.equal(c.node('admin-logout').disabled, true);
  await c.page.changePolicy(true); await c.page.changePassword(); await c.page.logout();
  assert.equal(c.calls.length, 1);
  assert.equal(c.policyCalls.length, 2);
  const [path, request] = c.policyCalls[1];
  assert.equal(path, '/api/admin/model-policy');
  assert.equal(request.method, 'PUT');
  assert.equal(request.headers['X-CSRF-Token'], 'admin-csrf-token');
  assert.deepEqual(JSON.parse(request.body), { fableAllowed: true });
  assert.match(c.node('fable-policy-message').textContent, /正在允许/);
  gate.resolve(response({ fableAllowed: true, revision: 1 })); await change;
  assert.equal(c.node('fable-allowed').checked, true);
  assert.equal(c.node('fable-allowed').disabled, false);
  assert.match(c.node('fable-policy-message').textContent, /已允许/);
  assert.equal(c.node('new-password').value, 'New dashboard password 2026');
  assert.equal(c.node('admin-message').textContent, '');
});

test('disabling Fable reflects the confirmed restriction without changing credentials', async () => {
  const c = setup(() => response(loggedIn), (path, request) => response(request.method === 'GET' ? { fableAllowed: true, revision: 2 } : { fableAllowed: false, revision: 3 }));
  await c.page.start();
  await c.page.changePolicy(false);
  assert.equal(c.node('fable-allowed').checked, false);
  assert.equal(c.node('fable-policy-state').textContent, '已禁止');
  assert.match(c.node('fable-policy-message').textContent, /已禁止/);
  assert.equal(c.calls.length, 1, 'policy updates never submit or change a password');
});

test('a failed disable reads the committed restriction and permits applying the same policy again', async () => {
  let allowed = true, ready = true, shouldFail = true;
  const c = setup(() => response(loggedIn), (path, request) => {
    if (request.method === 'GET') return response({ fableAllowed: allowed, revision: allowed ? 0 : 1, enforcementReady: ready });
    allowed = false; ready = false;
    if (shouldFail) return response({ detail: 'sensitive internal information' }, 503);
    ready = true; return response({ fableAllowed: false, revision: 1, enforcementReady: true });
  });
  await c.page.start(); fillNew(c);
  await c.page.changePolicy(false);
  assert.equal(c.policyCalls.length, 3, 'failed write automatically reads the durable policy');
  assert.equal(c.node('fable-allowed').checked, false, 'the persisted restriction must not revert to the old allowed value');
  assert.equal(c.node('fable-allowed').disabled, true);
  assert.equal(c.node('fable-policy-state').textContent, '权限尚未完整应用');
  assert.equal(c.node('fable-policy-apply').hidden, false);
  assert.equal(c.node('fable-policy-apply').disabled, false);
  assert.doesNotMatch(c.node('fable-policy-message').textContent, /sensitive|已禁止/);
  shouldFail = false;
  await c.page.changePolicy(false);
  assert.equal(c.policyCalls.length, 4);
  assert.deepEqual(JSON.parse(c.policyCalls[3][1].body), { fableAllowed: false });
  assert.equal(c.node('fable-policy-state').textContent, '已禁止');
  assert.equal(c.node('fable-policy-apply').hidden, true);
  assert.equal(c.node('fable-allowed').disabled, false);
  assert.equal(c.node('new-password').value, 'New dashboard password 2026');
});

test('a successful response with incomplete enforcement never claims the permission is active', async () => {
  const c = setup(() => response(loggedIn), (path, request) => response({ fableAllowed: request.method === 'PUT', revision: request.method === 'PUT' ? 1 : 0, enforcementReady: request.method === 'GET' }));
  await c.page.start();
  await c.page.changePolicy(true);
  assert.equal(c.node('fable-allowed').checked, true);
  assert.equal(c.node('fable-policy-state').textContent, '权限尚未完整应用');
  assert.equal(c.node('fable-policy-apply').hidden, false);
  assert.doesNotMatch(c.node('fable-policy-message').textContent, /已允许/);
});

test('an unknown write outcome with failed refresh blocks toggles until a successful reload', async () => {
  let failure = false;
  const c = setup(() => response(loggedIn), () => failure ? response({}, 503) : response({ fableAllowed: false, revision: 0 }));
  await c.page.start(); failure = true;
  await c.page.changePolicy(true);
  assert.equal(c.node('fable-policy-state').textContent, '状态待确认');
  assert.equal(c.node('fable-allowed').disabled, true);
  const before = c.policyCalls.length;
  await c.page.changePolicy(true);
  assert.equal(c.policyCalls.length, before);
  failure = false; await c.page.loadPolicy();
  assert.equal(c.node('fable-policy-state').textContent, '已禁止');
  assert.equal(c.node('fable-allowed').disabled, false);
});

test('expired policy requests clear credentials and late snapshots cannot restore administrator controls', async () => {
  const c = setup(() => response(loggedIn), (path, request) => request.method === 'GET' ? response({ fableAllowed: false, revision: 0 }) : response({}, 401));
  await c.page.start(); fillNew(c);
  const oldGeneration = c.page.generation;
  await c.page.changePolicy(true);
  assert.equal(c.page.phase, 'login');
  assert.equal(c.page.csrf, '');
  assert.equal(c.node('new-password'), undefined);
  assert.equal(c.node('fable-allowed'), undefined);
  assert.equal(c.page.acceptPolicy({ fableAllowed: true, revision: 99 }, oldGeneration), false);
  assert.equal(c.node('admin-password').value, '');
  assert.equal(c.node('admin-submit').disabled, false);
});

test('stale and malformed policy snapshots cannot overwrite the latest confirmed setting', async () => {
  const c = setup(() => response(loggedIn), () => response({ fableAllowed: true, revision: 5 }));
  await c.page.start();
  assert.equal(c.page.acceptPolicy({ fableAllowed: false, revision: 4 }, c.page.generation), false);
  assert.equal(c.page.policy.fableAllowed, true);
  assert.throws(() => c.page.acceptPolicy({ fableAllowed: 'true', revision: 6 }, c.page.generation), /未确认模型权限/);
  assert.equal(c.page.policy.revision, 5);
});

test('Claude authorization is mounted only for administrators and its requests use the separate admin session', async () => {
  const host = new Host(), calls = [], panels = [];
  const page = new AdminPage(host, { usageFactory: () => ({ mount() {}, controls() {}, unmount() {} }),
    quotaFactory: () => ({ mount() {}, controls() {}, unmount() {}, async refresh() {} }),
    claudeFactory: (options) => { const panel = { options, mounted: false, closed: false, mount() { this.mounted = true; }, controls() {}, unmount() { this.closed = true; } }; panels.push(panel); return panel; },
    fetch: async (path, options) => {
      calls.push([path, options]);
      if (path.endsWith('/session')) return response(loginState);
      if (path.endsWith('/login')) return response(loggedIn);
      if (path.endsWith('/model-policy')) return response({ fableAllowed: false, revision: 0 });
      if (path.endsWith('/users')) return response({ users: [{ id: 'owner-id', username: 'owner', createdAt: '' }] });
      if (path.endsWith('/workspaces')) return response({ workspaces: [] });
      if (path.endsWith('/claude/status')) return response({ authenticated: true, terminalActive: false });
      return response({ ok: true });
    },
  });
  await page.start();
  assert.equal(panels.length, 0); assert.equal(host.querySelector('#admin-claude-card'), undefined);
  host.querySelector('#admin-password').value = 'Fixture admin password'; await page.login();
  assert.equal(panels.length, 1); assert.equal(panels[0].mounted, true);
  host.querySelector('#new-password').value = 'Keep user password draft';
  await panels[0].options.request('claude/status');
  const [, options] = calls.find(([path]) => path.endsWith('/claude/status'));
  assert.equal(options.headers['X-CSRF-Token'], 'admin-csrf-token');
  assert.equal(options.credentials, 'same-origin'); assert.equal(options.cache, 'no-store');
  assert.equal(host.querySelector('#new-password').value, 'Keep user password draft');
  await page.logout();
  assert.equal(panels[0].closed, true); assert.equal(host.querySelector('#admin-claude-card'), undefined);
  const count = calls.length;
  await assert.rejects(panels[0].options.request('claude/status'), /管理员登录已过期/);
  assert.equal(calls.length, count);
  host.querySelector('#admin-password').value = 'Fixture admin password'; await page.login();
  const freshCount = calls.length;
  await assert.rejects(panels[0].options.request('claude/login-browser/stop', { method: 'POST' }), /管理员登录已过期/);
  assert.equal(calls.length, freshCount);
  assert.equal(panels[0].options.session().authenticated, false);
  panels[0].options.expire(); assert.equal(page.phase, 'ready');
});

test('an expired Claude authorization request removes the private panel and clears administrator drafts', async () => {
  const host = new Host(); let panel;
  const page = new AdminPage(host, { usageFactory: () => ({ mount() {}, controls() {}, unmount() {} }),
    quotaFactory: () => ({ mount() {}, controls() {}, unmount() {}, async refresh() {} }),
    claudeFactory: (options) => (panel = { options, closed: false, mount() {}, controls() {}, unmount() { this.closed = true; } }),
    fetch: async (path) => path.endsWith('/session') ? response(loggedIn) : path.endsWith('/model-policy') ? response({ fableAllowed: false, revision: 0 }) : path.endsWith('/users') ? response({ users: [{ id: 'owner-id', username: 'owner', createdAt: '' }] }) : path.endsWith('/workspaces') ? response({ workspaces: [] }) : response({}, 401),
  });
  await page.start(); host.querySelector('#new-password').value = 'Sensitive draft';
  const privatePanel = panel;
  await assert.rejects(privatePanel.options.request('claude/status'), /已过期/);
  assert.equal(privatePanel.closed, true); assert.equal(page.phase, 'login'); assert.equal(page.csrf, '');
  assert.equal(host.querySelector('#new-password'), undefined); assert.equal(host.querySelector('#admin-claude-card'), undefined);
  assert.equal(host.querySelector('#admin-password').value, '');
});
