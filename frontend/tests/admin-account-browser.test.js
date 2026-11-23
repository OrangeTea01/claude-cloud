import assert from 'node:assert/strict';
import test from 'node:test';
import { JSDOM } from 'jsdom';
import { AdminPage } from '../src/admin.js';
import { AccountBrowser } from '../src/account-browser.js';

const ENDPOINT = '/api/admin/claude-account-browser';
const response = (value, status = 200) => ({ ok: status < 400, status, json: async () => value });
const flush = () => new Promise((resolve) => setImmediate(resolve));
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; };
const panel = () => ({ mount() {}, unmount() {}, controls() {}, async refresh() {} });

function fixture({ authenticated = true, handler } = {}) {
  const dom = new JSDOM('<body><div id="admin-app"></div></body>', { url: 'https://work.example/admin' });
  const { window } = dom;
  window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  window.HTMLDialogElement.prototype.close = function () { this.open = false; };
  const calls = [], sockets = [], timers = new Set();
  let remote = null, serial = 0;
  const lease = (purpose = 'register', browserId = 'admin-browser') => ({ active: true, purpose, browserId, expiresAt: new Date(Date.now() + 1800000).toISOString() });
  class Socket {
    constructor(url) { this.url = url; this.listeners = {}; this.sent = []; sockets.push(this); }
    addEventListener(name, fn) { this.listeners[name] = fn; }
    send(value) { this.sent.push(value); }
    close() { this.closed = true; }
  }
  class RFB { constructor() {} addEventListener() {} disconnect() {} }
  const page = new AdminPage(window.document.querySelector('#admin-app'), {
    claudeFactory: panel, usageFactory: panel, quotaFactory: panel,
    accountBrowserFactory: (options) => new AccountBrowser({ ...options, createSocket: (url) => new Socket(url), loadRFB: async () => RFB,
      setTimer: () => { timers.add(++serial); return serial; }, clearTimer: (id) => timers.delete(id) }),
    fetch: async (path, options) => {
      calls.push([path, options]);
      const result = await handler?.(path, options, { lease });
      if (result !== undefined) return result;
      if (path.endsWith('/session')) return response({ enabled: true, authenticated, csrfToken: authenticated ? 'admin-csrf' : '' });
      if (path.endsWith('/model-policy')) return response({ fableAllowed: false, revision: 0 });
      if (path.endsWith('/users')) return response({ users: [] });
      if (path.endsWith('/workspaces')) return response({ workspaces: [] });
      if (path.endsWith('/logout')) return response({ ok: true });
      if (path === ENDPOINT) {
        if (options.method === 'POST') remote = lease(JSON.parse(options.body).purpose);
        return response(remote || { active: false });
      }
      if (path === `${ENDPOINT}/stop`) { remote = null; return response({ ok: true }); }
      throw new Error(`Unexpected route: ${path}`);
    },
  });
  return { dom, window, page, calls, sockets, timers, lease, node: (id) => window.document.getElementById(id),
    setRemote(value) { remote = value; },
    async close() { await page.accountBrowser?.close({ quiet: true }); dom.window.close(); } };
}

test('registration and billing card is administrator-only, visible before management cards and independent of workspaces', async () => {
  const c = fixture(); await c.page.start();
  const card = c.node('admin-account-services');
  assert.ok(card);
  assert.equal(c.node('admin-account-register').textContent, '注册 Claude');
  assert.equal(c.node('admin-account-billing').textContent, '订阅 / 充值');
  assert.ok(card.compareDocumentPosition(c.node('create-user-card')) & c.window.Node.DOCUMENT_POSITION_FOLLOWING);
  assert.ok(c.window.document.querySelector('.admin-nav a[href="#admin-account-services"]'));
  assert.match(card.textContent, /此入口仅管理员可用/);
  assert.doesNotMatch(card.textContent, /个人账号/);
  assert.equal(c.calls.some(([path]) => path === ENDPOINT), false, 'do not start a browser during page load');
  c.node('admin-account-register').click(); await flush(); await flush();
  const started = c.calls.find(([path, options]) => path === ENDPOINT && options.method === 'POST');
  assert.deepEqual(JSON.parse(started[1].body), { purpose: 'register' });
  assert.equal(started[1].headers['X-CSRF-Token'], 'admin-csrf');
  assert.equal(started[1].credentials, 'same-origin');
  assert.equal(c.sockets[0].url, 'wss://work.example/api/admin/claude-account-browser/display');
  c.sockets[0].onopen();
  assert.deepEqual(JSON.parse(c.sockets[0].sent[0]), { type: 'auth', csrfToken: 'admin-csrf', browserId: 'admin-browser' });
  assert.match(c.page.accountBrowser.dialog.textContent, /不会自动更改工作区的 Claude 授权/);
  await c.close();

  const anonymous = fixture({ authenticated: false }); await anonymous.page.start();
  assert.equal(anonymous.node('admin-account-services'), null);
  await anonymous.page.openAccountBrowser('register');
  assert.equal(anonymous.page.accountBrowser, null);
  assert.equal(anonymous.calls.some(([path]) => path === ENDPOINT), false);
  await anonymous.close();
});

test('billing button uses the fixed official purpose and logout disconnects and clears its browser', async () => {
  const c = fixture(); await c.page.start();
  c.node('admin-account-billing').click(); await flush(); await flush();
  const started = c.calls.find(([path, options]) => path === ENDPOINT && options.method === 'POST');
  assert.deepEqual(JSON.parse(started[1].body), { purpose: 'billing' });
  await c.page.logout();
  const stopIndex = c.calls.findIndex(([path]) => path === `${ENDPOINT}/stop`);
  const logoutIndex = c.calls.findIndex(([path]) => path === '/api/admin/logout');
  assert.ok(stopIndex >= 0 && logoutIndex >= 0);
  assert.deepEqual(JSON.parse(c.calls[stopIndex][1].body), { browserId: 'admin-browser' });
  assert.equal(c.calls[stopIndex][1].headers['X-CSRF-Token'], 'admin-csrf');
  assert.equal(c.page.accountBrowser.dialog, null);
  assert.equal(c.node('admin-account-services'), null);
  assert.equal(c.timers.size, 0);
  assert.ok(c.sockets.every((socket) => socket.closed));
  await c.close();
});

test('administrator logout dispatches immediately while browser status or creation is stalled', async () => {
  for (const pendingMethod of ['GET', 'POST']) {
    const gate = deferred();
    const c = fixture({ handler: (path, options) => path === ENDPOINT && options.method === pendingMethod ? gate.promise : undefined });
    await c.page.start();
    const opening = c.page.openAccountBrowser('register'); await flush();
    assert.ok(c.page.accountBrowser.dialog);
    const logout = c.page.logout();
    assert.equal(c.page.accountBrowser.dialog, null, 'private display closes synchronously');
    assert.ok(c.calls.some(([path]) => path === '/api/admin/logout'), 'revocation must not wait for the stalled browser request');
    await logout;
    assert.equal(c.page.phase, 'login');
    gate.resolve(response(c.lease('register', 'late-browser')));
    await opening; await c.page.accountBrowser.operation;
    const cleanup = c.calls.find(([path]) => path === `${ENDPOINT}/stop`);
    assert.equal(cleanup[1].headers['X-CSRF-Token'], 'admin-csrf');
    assert.deepEqual(JSON.parse(cleanup[1].body), { browserId: 'late-browser' });
    assert.equal(c.page.accountBrowser.dialog, null);
    assert.equal(c.sockets.length, 0);
    await c.close();
  }
});

test('a browser authorization failure expires administrator controls and removes the dialog', async () => {
  const c = fixture({ handler: (path) => path === ENDPOINT ? response({}, 401) : undefined });
  await c.page.start(); await c.page.openAccountBrowser('register');
  assert.equal(c.page.phase, 'login');
  assert.equal(c.page.csrf, '');
  assert.equal(c.page.accountBrowser.dialog, null);
  assert.equal(c.node('admin-account-services'), null);
  assert.match(c.node('admin-message').textContent, /已过期/);
  await c.close();
});

test('late creation cleanup retains old CSRF and lease without invalidating a new administrator session', async () => {
  const gate = deferred(); let createCount = 0;
  const c = fixture({ handler: (path, options) => {
    if (path === ENDPOINT && options.method === 'POST' && ++createCount === 1) return gate.promise;
    if (path === `${ENDPOINT}/stop` && options.headers['X-CSRF-Token'] === 'admin-csrf') return response({}, 401);
    return undefined;
  } });
  await c.page.start();
  const pending = c.page.openAccountBrowser('register'); await flush();
  c.page.expire();
  assert.equal(c.page.accountBrowser.dialog, null);
  c.page.phase = 'ready'; c.page.csrf = 'new-admin-csrf'; c.page.render();
  const replacement = c.page.openAccountBrowser('billing');
  gate.resolve(response(c.lease('register', 'old-browser')));
  await pending; await replacement;
  const cleanup = c.calls.find(([path]) => path === `${ENDPOINT}/stop`);
  assert.equal(cleanup[1].headers['X-CSRF-Token'], 'admin-csrf');
  assert.deepEqual(JSON.parse(cleanup[1].body), { browserId: 'old-browser' });
  assert.equal(c.page.phase, 'ready');
  assert.equal(c.page.csrf, 'new-admin-csrf');
  const latestCreate = c.calls.filter(([path, options]) => path === ENDPOINT && options.method === 'POST').at(-1);
  assert.equal(latestCreate[1].headers['X-CSRF-Token'], 'new-admin-csrf');
  assert.equal(c.page.accountBrowser.browserId, 'admin-browser');
  assert.equal(c.sockets.length, 1, 'stale creation must not attach a display');
  await c.close();
});

test('pagehide during browser creation suspends the display and BFCache resume checks the same lease', async () => {
  const gate = deferred();
  const c = fixture({ handler: (path, options) => path === ENDPOINT && options.method === 'POST' ? gate.promise : undefined });
  await c.page.start();
  const pending = c.page.openAccountBrowser('register'); await flush();
  c.window.dispatchEvent(new c.window.Event('pagehide'));
  const lease = c.lease(); c.setRemote(lease); gate.resolve(response(lease)); await pending;
  assert.equal(c.sockets.length, 0);
  assert.equal(c.timers.size, 0);
  assert.equal(c.calls.some(([path]) => path.endsWith('/stop')), false);
  const restored = new c.window.Event('pageshow'); Object.defineProperty(restored, 'persisted', { value: true });
  c.window.dispatchEvent(restored); await flush(); await flush();
  assert.equal(c.sockets.length, 1);
  assert.equal(c.calls.filter(([path, options]) => path === ENDPOINT && options.method === 'POST').length, 1);
  await c.close();
});
