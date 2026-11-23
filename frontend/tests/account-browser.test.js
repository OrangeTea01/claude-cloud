import assert from 'node:assert/strict';
import test from 'node:test';
import { JSDOM } from 'jsdom';
import { AccountBrowser, remainingBrowserTime, formatBrowserTime } from '../src/account-browser.js';
import { AccountUsageMenu } from '../src/account-usage.js';

const ENDPOINT = '/api/admin/claude-account-browser';
const flush = () => new Promise((resolve) => setImmediate(resolve));
const deferred = () => { let resolve; const promise = new Promise((yes) => { resolve = yes; }); return { promise, resolve }; };

function fixture(handler) {
  const dom = new JSDOM('<body><button id="account">账号</button></body>', { url: 'https://work.example/' });
  dom.window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  dom.window.HTMLDialogElement.prototype.close = function () { this.open = false; };
  let clock = Date.parse('2026-11-19T12:00:00Z');
  let context = { authenticated: true, key: 'alice:session-one', csrf: 'csrf-one' };
  let remote = null;
  const calls = [], sockets = [], rfbs = [], hints = [], timers = new Map();
  let timerId = 0;
  class Socket {
    constructor(url) { this.url = url; this.listeners = {}; this.sent = []; sockets.push(this); }
    addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
    emit(name, event = {}) { for (const callback of this.listeners[name] || []) callback(event); }
    open() { this.onopen?.(); }
    send(value) { this.sent.push(value); }
    close() { this.closed = true; this.emit('close', { code: 1000 }); }
  }
  class RFB {
    constructor(screen, socket) { this.screen = screen; this.socket = socket; this.listeners = {}; this.keys = []; rfbs.push(this); }
    addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
    emit(name, event = {}) { for (const callback of this.listeners[name] || []) callback(event); }
    disconnect() { this.disconnected = true; }
    focus() { this.focused = true; }
    sendKey(...args) { this.keys.push(args); }
  }
  const status = (purpose = 'register', id = 'browser-one') => ({ active: true, browserId: id, purpose, expiresAt: new Date(clock + 1800000).toISOString() });
  const api = async (path, options = {}) => {
    calls.push([path, options]);
    if (handler) return handler(path, options, { status });
    if (path.endsWith('/stop')) { remote = null; return { ok: true }; }
    if (options.method === 'POST') { remote = remote ? { ...remote, purpose: options.body.purpose } : status(options.body.purpose); return remote; }
    return remote || { active: false, browserId: null, purpose: null, expiresAt: null };
  };
  const browser = new AccountBrowser({ api, getContext: () => context, toast: (...args) => hints.push(args), page: dom.window.document,
    location: dom.window.location, createSocket: (url) => new Socket(url), loadRFB: async () => RFB, now: () => clock,
    setTimer: (callback) => { timers.set(++timerId, callback); return timerId; }, clearTimer: (id) => timers.delete(id) });
  return { dom, browser, calls, sockets, rfbs, hints, timers, status,
    setRemote: (value) => { remote = value; }, setContext: (value) => { context = value; }, advance: (ms) => { clock += ms; },
    connect() { sockets.at(-1).open(); rfbs.at(-1).emit('connect'); },
    async close() { await browser.close({ quiet: true }); dom.window.close(); } };
}

test('registration opens a fixed server purpose and authenticates the display, without local official links or OAuth fields', async () => {
  const c = fixture(); await c.browser.show('register');
  assert.equal(c.calls[0][0], ENDPOINT);
  assert.deepEqual(c.calls[1][1].body, { purpose: 'register' });
  assert.equal(c.calls[1][1].csrfToken, 'csrf-one');
  assert.equal(c.browser.dialog.querySelectorAll('a[href],form,[data-login-code]').length, 0);
  assert.equal(c.sockets[0].url, 'wss://work.example/api/admin/claude-account-browser/display');
  c.connect();
  assert.deepEqual(JSON.parse(c.sockets[0].sent[0]), { type: 'auth', csrfToken: 'csrf-one', browserId: 'browser-one' });
  assert.equal(c.rfbs[0].showDotCursor, true);
  assert.equal(c.rfbs[0].scaleViewport, true);
  assert.equal(c.browser.connected, true);
  assert.equal(c.browser.dialog.querySelector('[data-browser-countdown]').textContent, '30:00');
  assert.match(c.browser.dialog.textContent, /官方页面为准/);
  assert.equal(c.rfbs[0].listeners.clipboard, undefined, 'remote clipboard must not be captured into the page');
  await c.close();
});

test('billing and extra usage navigation retain the current lease and its original deadline', async () => {
  const c = fixture(); await c.browser.show('register'); c.connect();
  const deadline = c.browser.expiresAt;
  c.advance(90000);
  await c.browser.navigate('billing'); await c.browser.navigate('usage');
  assert.deepEqual(c.calls.slice(-2).map(([, options]) => options.body), [{ purpose: 'billing', browserId: 'browser-one' }, { purpose: 'usage', browserId: 'browser-one' }]);
  assert.equal(c.browser.expiresAt, deadline);
  assert.equal(c.sockets.length, 1, 'changing official pages must not replace the remote session');
  assert.equal(c.browser.dialog.querySelector('[data-browser-purpose="usage"]').getAttribute('aria-pressed'), 'true');
  assert.match(c.browser.dialog.textContent, /并非 API 账户余额/);
  await c.browser.navigate('https://untrusted.example');
  assert.equal(c.calls.length, 4, 'unknown purpose must not reach the API');
  await c.close();
});

test('an existing same-session browser reconnects without creating a fresh profile', async () => {
  const c = fixture(); c.setRemote(c.status('billing', 'existing-browser'));
  await c.browser.show('billing');
  assert.equal(c.calls.length, 1);
  assert.equal(c.browser.browserId, 'existing-browser');
  c.connect();
  assert.equal(JSON.parse(c.sockets[0].sent[0]).browserId, 'existing-browser');
  await c.close();
});

test('anonymous access and unknown purpose do not create a dialog or request', async () => {
  const c = fixture(); c.setContext({ authenticated: false });
  await c.browser.show('register'); await c.browser.show('foreign');
  assert.equal(c.calls.length, 0); assert.equal(c.browser.dialog, null);
  assert.match(c.hints[0][0], /先登录/);
  await c.close();
});

test('closing during startup stops the late returned lease and never paints its contents', async () => {
  const pending = deferred();
  const c = fixture(async (path, options, { status }) => {
    if (path.endsWith('/stop')) return { ok: true };
    if (options.method === 'POST') { await pending.promise; return status(); }
    return { active: false };
  });
  const opening = c.browser.show('register'); await flush();
  const closing = c.browser.close({ quiet: true });
  assert.equal(c.browser.dialog, null);
  pending.resolve(); await opening; await closing;
  assert.equal(c.sockets.length, 0);
  assert.deepEqual(c.calls.at(-1)[1].body, { browserId: 'browser-one' });
  assert.equal(c.calls.at(-1)[0], `${ENDPOINT}/stop`);
  await c.close();
});

test('closing while status is pending cleans the existing lease returned afterward', async () => {
  const pending = deferred();
  const c = fixture(async (path, options, { status }) => {
    if (path.endsWith('/stop')) return { ok: true };
    await pending.promise; return status();
  });
  const opening = c.browser.show(); await flush();
  const closing = c.browser.close({ quiet: true });
  pending.resolve(); await opening; await closing;
  assert.equal(c.calls.filter(([path]) => path.endsWith('/stop')).length, 1);
  assert.equal(c.sockets.length, 0);
  await c.close();
});

test('switching identities drops late results and clears transient mobile input', async () => {
  const c = fixture(); await c.browser.show(); c.connect();
  const input = c.browser.dialog.querySelector('[data-browser-text]'); input.value = 'private';
  c.setContext({ authenticated: true, key: 'bob:session-two', csrf: 'different' });
  c.browser.tick(); await c.browser.operation;
  assert.equal(c.browser.dialog, null); assert.equal(input.value, '');
  assert.equal(c.sockets[0].closed, true);
  assert.equal(c.calls.at(-1)[1].csrfToken, 'csrf-one', 'cleanup must retain the original request context');
  await c.close();
});

test('another-session busy error is readable without showing remote data or opening local websites', async () => {
  const c = fixture(async () => { throw new Error('浏览器正由另一个会话使用'); });
  await c.browser.show('billing');
  assert.match(c.browser.dialog.querySelector('[data-browser-status]').textContent, /另一个会话/);
  assert.equal(c.browser.dialog.querySelector('.account-browser-screen').hidden, true);
  assert.equal(c.sockets.length, 0);
  assert.equal(c.browser.dialog.querySelector('[data-browser-purpose="billing"]').disabled, false);
  await c.close();
});

test('five-minute warning occurs once and the absolute deadline ends the browser lease', async () => {
  const c = fixture(); await c.browser.show();
  c.advance(25 * 60000); c.browser.tick(); c.browser.tick();
  assert.equal(c.hints.filter(([message]) => message.includes('不足 5 分钟')).length, 1);
  assert.equal(c.browser.dialog.querySelector('[data-browser-warning]').hidden, false);
  assert.equal(c.browser.dialog.querySelector('[data-browser-countdown]').textContent, '05:00');
  c.advance(5 * 60000); c.browser.tick(); await c.browser.operation;
  assert.equal(c.browser.dialog, null);
  assert.equal(c.timers.size, 0);
  assert.equal(c.calls.at(-1)[0], `${ENDPOINT}/stop`);
  await c.close();
});

test('refresh suspension preserves the server session and resume verifies its original ID', async () => {
  const c = fixture(); await c.browser.show('billing'); c.connect();
  const input = c.browser.dialog.querySelector('[data-browser-text]'); input.value = 'temporary';
  c.browser.suspend();
  assert.equal(c.browser.browserId, 'browser-one'); assert.equal(input.value, '');
  assert.equal(c.calls.length, 2); assert.equal(c.timers.size, 0);
  await c.browser.resume();
  assert.equal(c.calls.length, 3); assert.equal(c.calls.at(-1)[1].method, undefined);
  assert.equal(c.sockets.length, 2); c.connect();
  assert.equal(JSON.parse(c.sockets.at(-1).sent[0]).browserId, 'browser-one');
  await c.close();
});

test('suspending pending startup does not destroy its session or attach a hidden display', async () => {
  const pending = deferred();
  const c = fixture(async (path, options, { status }) => {
    if (options.method === 'POST') { await pending.promise; return status(); }
    return { active: false };
  });
  const opening = c.browser.show(); await flush(); c.browser.suspend();
  pending.resolve(); await opening;
  assert.equal(c.sockets.length, 0); assert.equal(c.browser.browserId, 'browser-one');
  assert.equal(c.calls.filter(([path]) => path.endsWith('/stop')).length, 0);
  await c.close();
});

test('mobile input is transient and display shortcuts never send arbitrary navigation requests', async () => {
  const c = fixture(); await c.browser.show(); c.connect();
  const input = c.browser.dialog.querySelector('[data-browser-text]');
  assert.equal(input.type, 'password'); assert.equal(input.autocomplete, 'off');
  input.value = '验证A\n'; c.browser.dialog.querySelector('[data-browser-type]').click();
  assert.equal(input.value, '');
  assert.equal(c.rfbs[0].keys.length, 3, 'control characters cannot be typed through the paste field');
  c.browser.dialog.querySelector('[data-browser-address]').click();
  assert.deepEqual(c.rfbs[0].keys.slice(-3), [[0xffe3, 'ControlLeft', true], [108, 'KeyL'], [0xffe3, 'ControlLeft', false]]);
  assert.equal(c.calls.length, 2);
  await c.close();
});

test('expired display auth cannot be mistaken for a successful connection or payment', async () => {
  const c = fixture(); await c.browser.show(); c.connect();
  c.sockets[0].emit('close', { code: 4403 });
  assert.equal(c.browser.connected, false);
  assert.equal(c.browser.dialog.querySelector('[data-browser-reconnect]').hidden, true);
  assert.equal(c.browser.dialog.querySelector('.account-browser-screen').hidden, true);
  assert.equal(c.browser.dialog.querySelector('[data-browser-input]').hidden, true);
  assert.match(c.browser.dialog.querySelector('[data-browser-status]').textContent, /会话已结束/);
  assert.doesNotMatch(c.browser.dialog.textContent, /支付成功|充值成功/);
  await c.close();
});

test('resuming an expired browser clears its last picture and enables a fresh explicit start', async () => {
  const c = fixture(); await c.browser.show(); c.connect();
  const screen = c.browser.dialog.querySelector('.account-browser-screen');
  screen.textContent = 'old private page';
  c.browser.suspend(); c.setRemote(null); await c.browser.resume();
  assert.equal(screen.hidden, true); assert.equal(screen.textContent, '');
  assert.equal(c.browser.browserId, null);
  assert.equal(c.calls.filter(([, options]) => options.method === 'POST').length, 1);
  await c.browser.navigate('billing');
  assert.deepEqual(c.calls.at(-1)[1].body, { purpose: 'billing' });
  await c.close();
});

test('suspension clears private pixels and restore keeps them hidden until the lease is validated', async () => {
  const restored = deferred(); let statusReads = 0;
  const c = fixture((path, options, { status }) => {
    if (path.endsWith('/stop')) return { ok: true };
    if (options.method === 'POST') return status();
    return ++statusReads === 1 ? { active: false } : restored.promise;
  });
  await c.browser.show(); c.connect();
  const screen = c.browser.dialog.querySelector('.account-browser-screen');
  screen.textContent = 'private account page pixels';
  c.browser.dialog.querySelector('[data-browser-text]').value = 'private input';
  c.browser.suspend();
  assert.equal(screen.hidden, true);
  assert.equal(screen.textContent, '');
  assert.equal(c.browser.dialog.querySelector('[data-browser-input]').hidden, true);
  assert.equal(c.browser.dialog.querySelector('[data-browser-text]').value, '');
  const resuming = c.browser.resume(); await flush();
  assert.equal(screen.hidden, true);
  assert.equal(screen.textContent, '');
  assert.equal(c.sockets.length, 1, 'no restored display may attach before validation');
  restored.resolve(c.status()); await resuming;
  assert.equal(c.sockets.length, 2);
  assert.equal(screen.hidden, false);
  await c.close();
});

test('countdown parsing handles malformed dates and clamps expired sessions', () => {
  assert.equal(remainingBrowserTime('invalid'), null);
  assert.equal(remainingBrowserTime('2026-11-19T12:00:00Z', Date.parse('2026-11-19T12:00:01Z')), 0);
  assert.equal(formatBrowserTime(61), '01:01');
});

test('ordinary account menu only exposes usage and account controls', async () => {
  const c = fixture();
  const menu = new AccountUsageMenu({ api: async () => { throw new Error('quota unavailable'); }, getUser: () => ({ id: 'alice', username: 'Alice' }),
    audio: { enabled: false, status: '关闭' }, page: c.dom.window.document, setTimer: () => 1, clearTimer: () => {} });
  menu.mount(c.dom.window.document.querySelector('#account')); menu.open(); await flush();
  assert.equal(menu.panel.querySelector('[data-account-browser]'), null);
  assert.doesNotMatch(menu.panel.textContent, /注册 Claude|订阅 \/ 充值/);
  assert.ok(menu.panel.querySelector('#account-usage-refresh'));
  menu.unmount(); await c.close();
});
