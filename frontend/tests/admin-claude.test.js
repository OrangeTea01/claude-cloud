import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { AdminClaudePanel } from '../src/admin-claude.js';

class Node {
  constructor() { this.value = ''; this.textContent = ''; this.hidden = false; this.disabled = false; this.dataset = {}; this.listeners = {}; this.clientWidth = 900; this.clientHeight = 350; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  replaceChildren() { this.cleared = true; }
}
class Host {
  set innerHTML(value) { this.html = value; this.nodes = new Map([...value.matchAll(/\bid="([^"]+)"/g)].map((match) => [`#${match[1]}`, new Node()])); }
  querySelector(selector) { return this.nodes.get(selector); }
}
class Browser {
  constructor(options) { this.options = options; this.output = ''; }
  reset() { this.output = ''; this.resets = (this.resets || 0) + 1; }
  observeOutput(data) { this.output += data; }
}
class Terminal {
  constructor() { this.options = {}; this.cols = 80; this.rows = 24; this.output = ''; }
  loadAddon() {}
  open() {}
  onData(fn) { this.data = fn; }
  onResize(fn) { this.resize = fn; }
  focus() {}
  write(data) { this.output += data; }
  dispose() { this.disposed = true; this.output = ''; }
}
class FitAddon { fit() {} }
class WebLinksAddon {}
function deferred() { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; }
function setup(handler = async () => ({ authenticated: false, terminalActive: false }), options = {}) {
  const calls = [], sockets = [], host = new Host();
  const session = { authenticated: true, csrf: 'admin-only-csrf' };
  let expired = 0;
  const panel = new AdminClaudePanel({
    request: async (...args) => { calls.push(args); return handler(...args); },
    session: () => session,
    expire: () => { expired++; session.authenticated = false; panel.unmount(); },
    Browser, load: async () => ({ Terminal, FitAddon, WebLinksAddon }),
    observe: () => ({ observe() {}, disconnect() {} }),
    socket: (url) => { const ws = { url, readyState: 1, sent: [], send(value) { this.sent.push(JSON.parse(value)); }, close() { this.closed = true; } }; sockets.push(ws); return ws; },
    confirm: async () => true, ...options,
  });
  const node = (id) => host.querySelector(`#${id}`);
  return { panel, calls, sockets, host, session, node, expired: () => expired };
}
globalThis.location = { protocol: 'https:', host: 'fixture.invalid' };

test('admin authorization is independent of all workspace IDs and authenticates its socket with admin CSRF', async () => {
  const c = setup(); await c.panel.mount(c.host);
  assert.equal(c.calls[0][0], 'claude/status');
  assert.equal(c.node('admin-claude-state').textContent, '尚未授权');
  assert.equal(c.node('admin-claude-connect').textContent, '开始授权');
  await c.panel.connect(); const ws = c.sockets[0]; ws.onopen();
  assert.equal(ws.url, 'wss://fixture.invalid/api/admin/claude/terminal');
  assert.deepEqual(ws.sent[0], { type: 'auth', csrfToken: 'admin-only-csrf' });
  ws.onmessage({ data: JSON.stringify({ type: 'ready' }) });
  assert.equal(c.node('admin-claude-browser').disabled, false);
  assert.equal(c.panel.browser.options.endpoint('any-user-workspace', '/display'), '/api/admin/claude/login-browser/display');
  assert.equal(c.panel.browser.options.context().terminalConnected, true);
  assert.notEqual(c.panel.browser.options.context().workspaceId, 'any-user-workspace');
  c.panel.unmount();
});

test('saved authorization requires confirmation before a replacement starts', async () => {
  let confirms = 0;
  const c = setup(async () => ({ authenticated: true, terminalActive: false }), { confirm: async () => { confirms++; return false; } });
  await c.panel.mount(c.host);
  assert.equal(c.node('admin-claude-state').textContent, '已保存授权');
  assert.equal(c.node('admin-claude-connect').textContent, '重新授权');
  await c.panel.connect();
  assert.equal(confirms, 1); assert.equal(c.sockets.length, 0);
  assert.equal(c.panel.busy, false);
  assert.match(c.node('admin-claude-status').textContent, /实际可用性与额度/);
  c.panel.unmount();
});

test('authorization code goes only to the current admin terminal and requires explicit submission', async () => {
  const c = setup(); await c.panel.mount(c.host); await c.panel.connect();
  const ws = c.sockets[0]; ws.onmessage({ data: JSON.stringify({ type: 'ready' }) });
  const context = c.panel.browser.options.context();
  assert.equal(c.panel.fillCode('user-workspace', context.terminalGeneration, 'auth-code'), false);
  assert.equal(c.panel.fillCode(context.workspaceId, context.terminalGeneration - 1, 'auth-code'), false);
  assert.equal(c.panel.fillCode(context.workspaceId, context.terminalGeneration, 'code\ncommand'), false);
  assert.equal(c.panel.fillCode(context.workspaceId, context.terminalGeneration, 'official-auth-code'), true);
  assert.deepEqual(ws.sent.at(-1), { type: 'input', data: 'official-auth-code' });
  assert.equal(c.node('admin-claude-submit-code').hidden, false);
  c.panel.submitCode(); assert.deepEqual(ws.sent.at(-1), { type: 'input', data: '\r' });
  assert.equal(c.node('admin-claude-submit-code').hidden, true);
  c.panel.unmount();
});

test('closing admin session clears terminal output and ignores late messages', async () => {
  const c = setup(); await c.panel.mount(c.host); await c.panel.connect();
  const ws = c.sockets[0]; ws.onmessage({ data: JSON.stringify({ type: 'ready' }) });
  const term = c.panel.term;
  ws.onmessage({ data: JSON.stringify({ type: 'output', data: 'temporary authorization link' }) });
  assert.equal(term.output, 'temporary authorization link');
  ws.onclose({ code: 4403 });
  assert.equal(c.expired(), 1); assert.equal(term.disposed, true); assert.equal(term.output, '');
  assert.equal(c.panel.browser.output, ''); assert.equal(c.panel.host, null);
  ws.onmessage({ data: JSON.stringify({ type: 'output', data: 'late private output' }) });
  assert.equal(term.output, '');
});

test('pending status or component load cannot recreate authorization after leaving admin', async () => {
  const gate = deferred();
  const c = setup(() => gate.promise);
  const mount = c.panel.mount(c.host); c.panel.unmount();
  gate.resolve({ authenticated: true, terminalActive: true }); await mount;
  assert.equal(c.panel.status, null);
  const load = deferred(); const d = setup(undefined, { load: () => load.promise });
  await d.panel.mount(d.host); const connecting = d.panel.connect(); d.panel.unmount();
  load.resolve({ Terminal, FitAddon, WebLinksAddon }); await connecting;
  assert.equal(d.sockets.length, 0); assert.equal(d.panel.term, null);
});

test('cancelling authorization calls admin stop and reloads shared status without clearing saved authorization', async () => {
  const c = setup(async () => ({ authenticated: true, terminalActive: false }));
  await c.panel.mount(c.host); await c.panel.connect();
  const ws = c.sockets[0]; ws.onmessage({ data: JSON.stringify({ type: 'ready' }) });
  await c.panel.stop();
  assert.deepEqual(c.calls.find(([path]) => path.endsWith('/stop')), ['claude/terminal/stop', { method: 'POST' }]);
  assert.equal(c.panel.status.authenticated, true); assert.equal(c.node('admin-claude-state').textContent, '已保存授权');
  assert.equal(ws.closed, true); assert.equal(c.node('admin-claude-terminal-wrap').hidden, true);
  c.panel.unmount();
});

test('user entry and chat UI contain no Claude login controls, raw terminal code, or account links', async () => {
  const main = await readFile(new URL('../src/main.js', import.meta.url), 'utf8');
  const chat = await readFile(new URL('../src/chat.js', import.meta.url), 'utf8');
  assert.doesNotMatch(main, /ServerLoginBrowser|@xterm|new WebSocket|mode-terminal|connect-terminal|server-login|submit-login-code|\/login-browser|\/terminal\/stop/);
  assert.doesNotMatch(main + chat, /Pro \/ Max|登录 \/ 原始终端|打开登录终端|Claude 登录信息/);
  assert.match(main, /chat\.mount/); assert.match(main, /downloadFile/); assert.match(chat, /chat-model/);
});

test('authorization keeps polling after terminal exit until delayed credential storage is complete', async () => {
  let status = { authenticated: false, terminalActive: false, importing: false, error: '' };
  const c = setup(async () => status), scheduled = [];
  c.panel.scheduleRefresh = (delay) => scheduled.push(delay);
  await c.panel.mount(c.host); await c.panel.connect();
  const ws = c.sockets[0]; ws.onmessage({ data: JSON.stringify({ type: 'ready' }) });
  ws.onmessage({ data: JSON.stringify({ type: 'exit', code: 0 }) }); scheduled.length = 0;
  status = { ...status, importing: true };
  await c.panel.refresh({ manual: false });
  assert.equal(c.panel.state, 'exited'); assert.equal(c.node('admin-claude-state').textContent, '正在保存授权');
  assert.equal(c.node('admin-claude-connect').disabled, true); assert.equal(c.node('admin-claude-stop').disabled, true);
  assert.deepEqual(scheduled, [3000]);
  const count = c.calls.length; await c.panel.connect(); await c.panel.stop();
  assert.equal(c.calls.length, count); assert.equal(c.sockets.length, 1);
  await c.panel.refresh({ manual: false }); assert.equal(scheduled.length, 2);
  status = { ...status, authenticated: true, importing: false };
  await c.panel.refresh({ manual: false });
  assert.equal(scheduled.length, 2); assert.equal(c.node('admin-claude-state').textContent, '已保存授权');
  assert.match(c.node('admin-claude-message').textContent, /统一授权已保存/);
  assert.equal(c.node('admin-claude-connect').disabled, false);
  c.panel.unmount();
});

test('failed delayed credential storage is shown as text without a false saved status', async () => {
  let status = { authenticated: true, terminalActive: false, importing: true, error: '' };
  const c = setup(async () => status), scheduled = [];
  c.panel.scheduleRefresh = (delay) => scheduled.push(delay);
  await c.panel.mount(c.host);
  assert.equal(c.node('admin-claude-state').textContent, '正在保存授权');
  assert.equal(c.node('admin-claude-state').dataset.allowed, 'false');
  const error = '自动保存授权失败。<img src=x onerror=alert(1)>';
  status = { ...status, importing: false, error };
  await c.panel.refresh({ manual: false });
  assert.equal(c.node('admin-claude-state').textContent, '授权保存未完成');
  assert.equal(c.node('admin-claude-state').dataset.allowed, 'false');
  assert.equal(c.node('admin-claude-status').textContent, error);
  assert.equal(c.node('admin-claude-status').dataset.error, 'true');
  assert.equal(c.node('admin-claude-status').innerHTML, undefined);
  assert.equal(scheduled.length, 1); assert.equal(c.node('admin-claude-connect').disabled, false);
  c.panel.unmount();
});

test('malformed import metadata fails closed and late completion cannot restore an expired panel', async () => {
  for (const malformed of [{ importing: 'true' }, { error: { detail: 'private' } }]) {
    const c = setup(async () => ({ authenticated: true, terminalActive: false, ...malformed }));
    await c.panel.mount(c.host);
    assert.equal(c.panel.status, null); assert.equal(c.node('admin-claude-connect').disabled, true);
    assert.equal(c.node('admin-claude-stop').disabled, true); assert.equal(c.node('admin-claude-state').textContent, '状态待确认');
    c.panel.unmount();
  }
  const gate = deferred(); let pending = false;
  const c = setup(async () => pending ? gate.promise : ({ authenticated: false, terminalActive: false, importing: true, error: '' }));
  c.panel.scheduleRefresh = () => {};
  await c.panel.mount(c.host); pending = true;
  const finishing = c.panel.refresh({ manual: false }); c.session.authenticated = false; c.panel.unmount();
  gate.resolve({ authenticated: true, terminalActive: false, importing: false, error: '' }); await finishing;
  assert.equal(c.panel.status, null); assert.equal(c.panel.host, null);
});

test('automatic import polling is bounded and manual refresh resumes checks without enabling another authorization', async () => {
  const c = setup(async () => ({ authenticated: false, terminalActive: false, importing: true, error: '' })), scheduled = [];
  c.panel.scheduleRefresh = (delay) => scheduled.push(delay);
  await c.panel.mount(c.host); scheduled.length = 0; c.panel.importPolls = 99;
  await c.panel.refresh({ manual: false });
  assert.equal(scheduled.length, 0); assert.match(c.node('admin-claude-status').textContent, /刷新授权状态/);
  assert.equal(c.node('admin-claude-connect').disabled, true);
  await c.panel.refresh(); assert.deepEqual(scheduled, [3000]);
  assert.equal(c.panel.importPolls, 1); assert.equal(c.node('admin-claude-connect').disabled, true);
  c.panel.unmount();
});

test('a fast successful save after terminal exit replaces the checking message even when no importing snapshot was observed', async () => {
  let authenticated = false;
  const c = setup(async () => ({ authenticated, terminalActive: false, importing: false, error: '' }));
  c.panel.scheduleRefresh = () => {};
  await c.panel.mount(c.host); await c.panel.connect();
  c.sockets[0].onmessage({ data: JSON.stringify({ type: 'exit', code: 0 }) });
  authenticated = true; await c.panel.refresh({ manual: false });
  assert.equal(c.node('admin-claude-state').textContent, '已保存授权');
  assert.equal(c.node('admin-claude-message').textContent, '统一授权已保存，所有工作区可以使用。');
  c.panel.unmount();
});

test('solo login automatically opens one valid server authorization page and ignores repeated or stale output', async () => {
  class AutoBrowser extends Browser {
    constructor(options) { super(options); this.opened = []; }
    observeOutput(data) { this.url = data; }
    async show(url) { this.opened.push(url); }
  }
  const c = setup(undefined, { solo: true, Browser: AutoBrowser });
  c.panel.scheduleRefresh = () => {};
  await c.panel.mount(c.host); await c.panel.connect();
  assert.doesNotMatch(c.host.html, /管理员|所有工作区共用|用户直接使用/);
  assert.match(c.host.html, /官方登录页面会自动打开/);
  const ws = c.sockets[0], valid = 'https://claude.com/cai/oauth/authorize?response_type=code&client_id=client&code_challenge=challenge&state=session';
  const emit = (type, data) => ws.onmessage({ data: JSON.stringify({ type, data }) });
  emit('ready'); emit('output', 'https://claude.com/cai/oauth/authorize?response_type=code');
  assert.deepEqual(c.panel.browser.opened, []);
  emit('output', valid); emit('output', valid);
  assert.deepEqual(c.panel.browser.opened, [valid]);
  c.panel.unmount(); emit('output', valid);
  assert.deepEqual(c.panel.browser.opened, [valid]);
});

test('ordinary multiuser authorization remains manually opened', async () => {
  class ManualBrowser extends Browser {
    observeOutput(data) { this.url = data; }
    async show() { throw new Error('must not auto-open'); }
  }
  const c = setup(undefined, { Browser: ManualBrowser }); c.panel.scheduleRefresh = () => {};
  await c.panel.mount(c.host); await c.panel.connect();
  const ws = c.sockets[0]; ws.onmessage({ data: JSON.stringify({ type: 'ready' }) });
  ws.onmessage({ data: JSON.stringify({ type: 'output', data: 'https://claude.com/cai/oauth/authorize?response_type=code&client_id=client&code_challenge=challenge&state=session' }) });
  assert.notEqual(c.panel.autoBrowserOpened, true); c.panel.unmount();
});
