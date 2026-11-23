import assert from 'node:assert/strict';
import test from 'node:test';
import { JSDOM } from 'jsdom';
import { AdminClaudePanel } from '../src/admin-claude.js';
import { AdminPage } from '../src/admin.js';
import { ServerLoginBrowser } from '../src/login-browser.js';

const status = (extra = {}) => ({ authenticated: false, terminalActive: false, importing: false, error: '', pendingConfirmation: false, accountEmail: null, ...extra });
const tick = () => new Promise((resolve) => setImmediate(resolve));
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; };
class Browser { constructor(options) { this.options = options; } reset() {} observeOutput() {} }
class Terminal {
  constructor() { this.options = {}; this.cols = 80; this.rows = 24; }
  loadAddon() {} open() {} onData() {} onResize() {} focus() {} write() {} dispose() {}
}
class FitAddon { fit() {} }
class WebLinksAddon {}
globalThis.location = { protocol: 'https:', host: 'fixture.invalid' };
function fixture(handler = async () => status()) {
  const dom = new JSDOM('<div id="inference"></div><div id="usage"></div>');
  const calls = [], sockets = [], panels = [], events = [];
  const session = { authenticated: true, csrf: 'admin-csrf-only' };
  const options = {
    request: async (...args) => { calls.push(args); events.push(args[0]); return handler(...args); },
    session: () => session, expire: () => { session.authenticated = false; for (const panel of panels) panel.unmount(); },
    Browser, load: async () => { events.push('load'); return { Terminal, FitAddon, WebLinksAddon }; },
    observe: () => ({ observe() {}, disconnect() {} }), confirm: async () => true,
    socket: (url) => { const socket = { url, readyState: 1, sent: [], send(value) { this.sent.push(JSON.parse(value)); }, close() { this.closed = true; } }; sockets.push(socket); return socket; },
  };
  const create = (namespace = 'usage-auth') => { const panel = new AdminClaudePanel({ ...options, namespace }); panel.scheduleRefresh = () => {}; panels.push(panel); return panel; };
  const usage = create();
  return { dom, calls, sockets, session, options, events, usage, create, host: dom.window.document.getElementById('usage'),
    node: (id) => dom.window.document.getElementById(id), close: () => { for (const panel of panels) panel.unmount(); dom.window.close(); } };
}

test('inference and usage authorization have independent DOM, endpoints, sockets and clipboard contexts', async () => {
  const c = fixture(); const inference = c.create('claude');
  await inference.mount(c.node('inference')); await c.usage.mount(c.host);
  const ids = [...c.dom.window.document.querySelectorAll('[id]')].map((node) => node.id);
  assert.equal(new Set(ids).size, ids.length);
  assert.equal(c.node('admin-claude-heading').textContent, 'Claude 账号');
  assert.equal(c.node('admin-usage-auth-heading').textContent, '官方额度查询授权');
  await inference.connect(); await c.usage.connect();
  for (const socket of c.sockets) { socket.onopen(); socket.onmessage({ data: JSON.stringify({ type: 'ready' }) }); }
  assert.equal(c.sockets[0].url, 'wss://fixture.invalid/api/admin/claude/terminal');
  assert.equal(c.sockets[1].url, 'wss://fixture.invalid/api/admin/usage-auth/terminal');
  assert.deepEqual(c.sockets[1].sent[0], { type: 'auth', csrfToken: 'admin-csrf-only' });
  assert.equal(c.usage.browser.options.endpoint('', '/display'), '/api/admin/usage-auth/login-browser/display');
  assert.equal(c.usage.browser.options.domPrefix, 'usage-auth-browser');
  const originalContext = inference.browser.options.context(), usageContext = c.usage.browser.options.context();
  assert.notEqual(originalContext.workspaceId, usageContext.workspaceId);
  assert.equal(c.usage.fillCode(originalContext.workspaceId, c.usage.generation, 'code'), false);
  assert.equal(c.usage.fillCode(usageContext.workspaceId, c.usage.generation, 'code'), true);
  assert.equal(c.sockets[0].sent.some((message) => message.data === 'code'), false);
  assert.equal(c.sockets[1].sent.at(-1).data, 'code'); c.close();
});

test('new usage login prepares once before opening a terminal, while terminal reconnection does not prepare again', async () => {
  const gate = deferred(); const c = fixture((path) => path.endsWith('/prepare') ? gate.promise : status());
  await c.usage.mount(c.host);
  const connecting = c.usage.connect(); await c.usage.connect();
  assert.deepEqual(c.calls.at(-1), ['usage-auth/prepare', { method: 'POST', body: { confirmation: 'AUTHORIZE_USAGE_FOR_SHARED_ACCOUNT' } }]);
  assert.equal(c.sockets.length, 0);
  gate.resolve({ ok: true }); await connecting;
  assert.ok(c.events.indexOf('usage-auth/prepare') < c.events.indexOf('load'));
  c.sockets[0].onmessage({ data: JSON.stringify({ type: 'ready' }) }); c.sockets[0].onclose({ code: 1006 });
  await c.usage.connect();
  assert.equal(c.calls.filter(([path]) => path.endsWith('/prepare')).length, 1);
  assert.equal(c.sockets.length, 2); c.close();
});

test('official login stays pending until administrator checks the account and explicitly confirms association', async () => {
  let current = status({ pendingConfirmation: true, accountEmail: '<img src=x>@example.test' });
  const gate = deferred(); const c = fixture(async (path) => {
    if (path.endsWith('/confirm')) { await gate.promise; current = status({ authenticated: true, accountEmail: 'account@example.test' }); return current; }
    return current;
  });
  await c.usage.mount(c.host);
  assert.equal(c.node('admin-usage-auth-state').textContent, '等待管理员确认');
  assert.equal(c.node('admin-usage-auth-account-email').textContent, '<img src=x>@example.test');
  assert.equal(c.host.querySelector('img'), null);
  assert.match(c.host.textContent, /系统不会自动核实/);
  assert.equal(c.node('admin-usage-auth-confirm-association').disabled, true);
  await c.usage.confirmAssociation(); assert.equal(c.calls.filter(([path]) => path.endsWith('/confirm')).length, 0);
  c.node('admin-usage-auth-same-account').checked = true; c.usage.controls();
  const pending = c.usage.confirmAssociation(); await c.usage.confirmAssociation();
  assert.deepEqual(c.calls.at(-1), ['usage-auth/confirm', { method: 'POST', body: { confirmation: 'CONFIRM_SAME_CLAUDE_ACCOUNT' } }]);
  assert.equal(c.node('admin-usage-auth-confirm-association').disabled, true);
  gate.resolve(); await pending;
  assert.equal(c.node('admin-usage-auth-state').textContent, '已关联查询授权');
  assert.equal(c.node('admin-usage-auth-association').hidden, true);
  assert.match(c.node('admin-usage-auth-message').textContent, /已由管理员确认关联/);
  assert.doesNotMatch(c.host.textContent, /系统已验证|自动验证成功/); c.close();
});

test('changed accounts clear explicit consent and malformed or late status fails closed', async () => {
  let current = status({ pendingConfirmation: true, accountEmail: 'one@example.test' });
  const c = fixture(async () => current); await c.usage.mount(c.host);
  c.node('admin-usage-auth-same-account').checked = true;
  current = status({ pendingConfirmation: true, accountEmail: 'two@example.test' }); await c.usage.refresh();
  assert.equal(c.node('admin-usage-auth-same-account').checked, false);
  current = status({ pendingConfirmation: 'yes' }); await c.usage.refresh();
  assert.equal(c.usage.status, null); assert.equal(c.node('admin-usage-auth-confirm-association').disabled, true);
  assert.equal(c.node('admin-usage-auth-account-email').textContent, ''); c.close();
  const gate = deferred(); const d = fixture(() => gate.promise); const mounting = d.usage.mount(d.host); d.usage.unmount();
  gate.resolve(status({ pendingConfirmation: true, accountEmail: 'private@example.test' })); await mounting;
  assert.equal(d.usage.status, null); assert.doesNotMatch(d.host.textContent, /private@example/); d.close();
});

test('leaving administrator page during prepare or association cannot open a late terminal or restore private identity', async () => {
  for (const action of ['prepare', 'confirm']) {
    const gate = deferred(); const c = fixture((path) => path.endsWith(`/${action}`) ? gate.promise : status({ pendingConfirmation: action === 'confirm', accountEmail: action === 'confirm' ? 'private@example.test' : null }));
    await c.usage.mount(c.host);
    if (action === 'confirm') c.node('admin-usage-auth-same-account').checked = true;
    const pending = action === 'confirm' ? c.usage.confirmAssociation() : c.usage.connect();
    c.session.authenticated = false; c.usage.unmount(); gate.resolve(status({ authenticated: true })); await pending;
    assert.equal(c.sockets.length, 0); assert.equal(c.usage.status, null);
    assert.equal(c.node('admin-usage-auth-account-email').textContent, ''); c.close();
  }
});

test('failed prepare never opens a terminal and failed confirmation cannot claim an account association', async () => {
  const failedPrepare = fixture(async (path) => { if (path.endsWith('/prepare')) throw new Error('operation conflict'); return status(); });
  await failedPrepare.usage.mount(failedPrepare.host); await failedPrepare.usage.connect();
  assert.equal(failedPrepare.sockets.length, 0); assert.equal(failedPrepare.usage.state, 'error');
  assert.equal(failedPrepare.usage.busy, false); failedPrepare.close();
  const failedConfirm = fixture(async (path) => { if (path.endsWith('/confirm')) throw new Error('关联未完成，请刷新'); return status({ pendingConfirmation: true, accountEmail: 'account@example.test' }); });
  await failedConfirm.usage.mount(failedConfirm.host); failedConfirm.node('admin-usage-auth-same-account').checked = true;
  await failedConfirm.usage.confirmAssociation();
  assert.equal(failedConfirm.usage.status.authenticated, false);
  assert.equal(failedConfirm.node('admin-usage-auth-state').textContent, '等待管理员确认');
  assert.equal(failedConfirm.node('admin-usage-auth-message').dataset.kind, 'error');
  assert.doesNotMatch(failedConfirm.node('admin-usage-auth-message').textContent, /已由管理员确认/); failedConfirm.close();
});

test('starting replacement usage login clears the old binding display after prepare succeeds', async () => {
  const c = fixture(async () => status({ authenticated: true })); await c.usage.mount(c.host);
  assert.equal(c.node('admin-usage-auth-state').textContent, '已关联查询授权');
  await c.usage.connect();
  assert.equal(c.usage.status.authenticated, false); assert.equal(c.node('admin-usage-auth-state').dataset.allowed, 'false');
  assert.equal(c.calls.filter(([path]) => path.endsWith('/prepare')).length, 1); c.close();
});

test('two server browser dialogs keep their labels and authorization code fields separate', async () => {
  const dom = new JSDOM('<body></body>'); const oldDocument = globalThis.document; globalThis.document = dom.window.document;
  dom.window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  dom.window.HTMLDialogElement.prototype.close = function () { this.open = false; };
  const browsers = [];
  try {
    for (const [domPrefix, id] of [['server-login', 'shared-auth'], ['usage-auth-browser', 'usage-auth']]) {
      const browser = new ServerLoginBrowser({ domPrefix, api: async () => ({ active: false }), toast() {}, context: () => ({ authenticated: true, workspaceId: id, terminalConnected: true, terminalGeneration: 1 }), fillTerminal: () => true });
      browsers.push(browser); await browser.show();
    }
    const ids = [...dom.window.document.querySelectorAll('[id]')].map((node) => node.id);
    assert.equal(new Set(ids).size, ids.length);
    for (const browser of browsers) {
      assert.equal(browser.dialog.getAttribute('aria-labelledby'), `${browser.domPrefix}-title`);
      assert.ok(browser.dialog.querySelector(`#${browser.domPrefix}-url`));
      assert.equal(browser.dialog.querySelector(`label[for="${browser.domPrefix}-code"]`).control.id, `${browser.domPrefix}-code`);
    }
    browsers[0].dialog.querySelector('#server-login-code').value = 'inference-only';
    assert.equal(browsers[1].dialog.querySelector('#usage-auth-browser-code').value, '');
  } finally { for (const browser of browsers) browser.reset(); globalThis.document = oldDocument; dom.window.close(); }
});

test('administrator page owns usage authorization requests and removes both panels on expiry', async () => {
  const c = fixture(); const calls = []; let current = status(), usageOptions;
  const response = (body, code = 200) => ({ ok: code < 400, status: code, json: async () => body });
  const page = new AdminPage(c.host, {
    claudeFactory: () => ({ mount() {}, controls() {}, unmount() {} }), quotaFactory: () => ({ mount() {}, controls() {}, unmount() {}, async refresh() {} }),
    usageFactory: (options) => { usageOptions = options; const panel = new AdminClaudePanel({ ...c.options, ...options }); panel.scheduleRefresh = () => {}; return panel; },
    fetch: async (path, options) => {
      calls.push({ path, ...options });
      if (path.endsWith('/session')) return response({ enabled: true, authenticated: true, csrfToken: 'only-admin-csrf' });
      if (path.endsWith('/model-policy')) return response({ fableAllowed: false, revision: 0 });
      if (path.endsWith('/users')) return response({ users: [] });
      if (path.endsWith('/workspaces')) return response({ workspaces: [] });
      if (path.endsWith('/prepare')) return response({ ok: true });
      if (path.endsWith('/confirm')) { current = status({ authenticated: true }); return response(current); }
      if (path.endsWith('/usage-auth/status')) return response(current);
      throw new Error('Unexpected request');
    },
  });
  await page.start(); await tick(); await page.usageAuth.connect();
  current = status({ pendingConfirmation: true, accountEmail: 'admin-only@example.test' }); await page.usageAuth.refresh();
  c.node('admin-usage-auth-same-account').checked = true; await page.usageAuth.confirmAssociation();
  for (const suffix of ['/prepare', '/confirm']) {
    const request = calls.find((item) => item.path.endsWith(suffix));
    assert.equal(request.headers['X-CSRF-Token'], 'only-admin-csrf'); assert.equal(request.credentials, 'same-origin');
  }
  const privatePanel = page.usageAuth; page.expire();
  assert.equal(privatePanel.host, null); assert.equal(c.node('admin-usage-auth-card'), null); assert.equal(c.node('admin-claude-card'), null);
  const count = calls.length; await assert.rejects(usageOptions.request('usage-auth/prepare', { method: 'POST' }), /管理员登录已过期/);
  assert.equal(calls.length, count); c.close();
});
