import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { JSDOM } from 'jsdom';
import { ArchiveDownload, ARCHIVE_LIMIT_HINT } from '../src/archive-download.js';
import { AttentionAudio } from '../src/attention-audio.js';
import { WorkspaceActivity } from '../src/workspace-activity.js';
import { AccountUsageMenu } from '../src/account-usage.js';
import { SoloSettings } from '../src/solo-settings.js';
import { createSoloWorkspaceForm } from '../src/workspace-create.js';

const mainSource = (await readFile(new URL('../src/main.js', import.meta.url), 'utf8')).replace(/^import .*;\r?\n/gm, '').replace(/\ninit\(\);\s*$/, '');
const accessSource = (await readFile(new URL('../src/workspace-access.js', import.meta.url), 'utf8')).replace(/export /g, '');
const A = 'a'.repeat(32), B = 'b'.repeat(32);
const workspace = (extra = {}) => ({ id: A, name: '私人工作区', running: true, passwordProtected: true, locked: true, ...extra });
const response = (body, status = 200) => ({ ok: status < 400, status, json: async () => body });
const flush = () => new Promise((resolve) => setImmediate(resolve));
function deferred() { let resolve; const promise = new Promise((yes) => { resolve = yes; }); return { promise, resolve }; }

function setup(handler = async (path) => path === '/api/workspaces' ? response({ workspaces: [workspace()] }) : response({ docker: true, workerImage: true })) {
  const dom = new JSDOM('<div id="app"></div><div id="toasts"></div>', { url: 'http://localhost', runScripts: 'outside-only' });
  const { window } = dom;
  window.createSoloWorkspaceForm = () => createSoloWorkspaceForm(window.document);
  window.ArchiveDownload = ArchiveDownload;
  window.ARCHIVE_LIMIT_HINT = ARCHIVE_LIMIT_HINT;
  Object.assign(window, { AttentionAudio, WorkspaceActivity, AccountUsageMenu });
  window.SoloSettings = class extends SoloSettings {
    constructor(options) { super({ ...options, panelFactory: () => ({ mount(host) { host.textContent = '连接你的 Claude'; }, unmount() {} }) }); }
  };
  window.setInterval = () => 1;
  window.clearInterval = () => {};
  window.setTimeout = () => 1;
  window.TextEncoder = TextEncoder;
  window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  window.HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new window.Event('close')); };
  const calls = [];
  window.fetch = async (path, options) => { calls.push([path, options]); return handler(path, options); };
  window.BilingualChat = class {
    constructor(options) {
      Object.assign(this, options); this.drafts = new Map(); this.actions = new Set(); this.mounts = [];
      this.attachments = { drafts: new Map(), list(id) { return this.drafts.get(id) || []; }, release(item) { item.released = true; }, clearPreviews() {}, reset() { this.drafts.clear(); } };
    }
    mount(host, id) { this.workspaceId = id; this.mounts.push(id); host.textContent = 'PRIVATE CHAT'; }
    unmount(options = {}) { this.workspaceId = null; if (options.forgetDrafts) this.drafts.clear(); if (options.forgetAttachments || options.forgetDrafts) this.attachments.reset(); }
    controls() {}
  };
  window.eval(`window.Access = (() => { ${accessSource}\nreturn { workspacePasswordForm, lockedWorkspaceMarkup }; })();`);
  window.eval(`const {workspacePasswordForm,lockedWorkspaceMarkup}=window.Access;\n${mainSource}\nwindow.subject={state,chat,api,showLogin,adoptSession,showDashboard,refreshWorkspaces,selectWorkspace,clearWorkspacePrivate,markWorkspaceLocked,lockWorkspace,changeWorkspacePassword,loadFiles,archiveDownload,updateWorkspaceControls,renderEditor,accountMenu,attention,activity,init};`);
  const subject = window.subject;
  subject.adoptSession({ csrfToken: 'user-csrf', user: { id: 'user-one', username: 'alice' } });
  return { dom, window, subject, calls, node: (id) => window.document.getElementById(id), close: () => dom.window.close() };
}

test('locked workspace displays password form without requesting private chat or files', async () => {
  const c = setup();
  await c.subject.showDashboard();
  assert.ok(c.node('workspace-unlock-form'));
  assert.equal(c.node('chat-panel'), null);
  assert.equal(c.node('file-editor'), null);
  assert.deepEqual(c.subject.chat.mounts, []);
  assert.deepEqual(c.calls.map(([path]) => path).sort(), ['/api/status', '/api/workspaces']);
  assert.match(c.window.document.body.textContent, /alice/);
  c.close();
});

test('ordinary user dashboard and account menu never expose administrator Claude account services', async () => {
  for (const workspaces of [[], [workspace()]]) {
    const c = setup(async (path) => path === '/api/workspaces' ? response({ workspaces }) : response({ docker: true, workerImage: true }));
    await c.subject.showDashboard();
    assert.equal(c.window.document.querySelector('[data-action="account-register"], [data-action="account-billing"], .account-services-nav, .account-services-shortcut'), null);
    assert.doesNotMatch(c.window.document.body.textContent, /注册 Claude|订阅 \/ 充值|Claude 个人账号/);
    c.node('account-button').click(); await flush();
    assert.ok(c.node('account-usage-menu'));
    assert.equal(c.node('account-usage-menu').querySelector('[data-account-browser]'), null);
    assert.equal(c.calls.some(([path]) => path.includes('claude-browser') || path.includes('claude-account-browser')), false);
    c.close();
  }
  assert.doesNotMatch(mainSource, /AccountBrowser|account-browser|openAccountBrowser/);
});

test('incomplete sessions return to blank login instead of inventing an owner identity', async () => {
  for (const session of [{ csrfToken: 'csrf' }, { csrfToken: 'csrf', user: { id: 'user-one', username: '' } }, { user: { id: 'user-one', username: 'alice' } }]) {
    const c = setup();
    assert.throws(() => c.subject.adoptSession(session), /登录信息不完整/);
    assert.equal(c.subject.state.user, null);
    assert.equal(c.subject.state.authenticated, false);
    assert.equal(c.subject.state.csrf, '');
    assert.equal(c.node('username').value, '');
    assert.equal(c.node('password').value, '');
    assert.doesNotMatch(c.window.document.body.textContent, /owner/);
    assert.match(c.window.document.querySelector('.login-note').textContent, /管理员创建/);
    await c.subject.showDashboard();
    assert.ok(c.node('login-form'));
    assert.equal(c.calls.some(([path]) => path === '/api/workspaces' || path === '/api/account/claude-browser'), false);
    c.close();
  }
});

test('authenticated session response without a user cannot render a dashboard during initialization', async () => {
  const c = setup(async () => response({ authenticated: true, csrfToken: 'csrf' }));
  await c.subject.init();
  assert.ok(c.node('login-form'));
  assert.equal(c.window.document.querySelector('.workspace-app'), null);
  assert.equal(c.subject.state.user, null);
  assert.doesNotMatch(c.window.document.body.textContent, /owner/);
  assert.deepEqual(c.calls.map(([path]) => path), ['/api/session']);
  c.close();
});

test('solo initial login asks for one password and shares a verified owner session with collapsed management', async () => {
  const c = setup(async (path, options = {}) => {
    if (path === '/api/session') return response({ authenticated: false, mode: 'solo' });
    if (path === '/api/login') return response({ authenticated: true, mode: 'solo', admin: true, csrfToken: 'solo-csrf', user: { id: 'solo-id' } });
    if (path === '/api/workspaces') return response({ workspaces: [] });
    if (path === '/api/account/usage') return response({ mode: 'solo', quota: null, official: {}, activeTasks: [], idleMinutes: 30 });
    return response({ docker: true, workerImage: true, claudeAuthenticated: false });
  });
  await c.subject.init();
  assert.equal(c.node('username'), null);
  assert.equal(c.node('password').value, '');
  assert.equal(c.window.document.querySelector('a[href="/admin/"]'), null);
  c.node('password').value = 'private-site-password';
  c.node('login-form').dispatchEvent(new c.window.Event('submit', { cancelable: true }));
  await flush(); await flush();
  const login = c.calls.find(([path]) => path === '/api/login');
  assert.deepEqual(JSON.parse(login[1].body), { password: 'private-site-password' });
  assert.ok(c.window.document.querySelector('.workspace-app'));
  assert.doesNotMatch(c.node('account-button').textContent, /owner|solo-id|undefined/);
  assert.ok(c.node('server-warning').querySelector('[data-action="solo-settings"]'));
  c.node('account-button').click(); await flush();
  const menu = c.node('account-usage-menu');
  assert.equal(menu.querySelector('.solo-management-menu').open, false);
  assert.match(menu.textContent, /修改网站密码/);
  assert.doesNotMatch(menu.textContent, /我的站内配额|个人站内配额|创建用户|用户管理/);
  menu.querySelector('[data-action="solo-settings"]').click(); await flush();
  assert.equal(c.node('account-usage-menu'), null);
  assert.ok(c.window.document.querySelector('.solo-settings-dialog'));
  c.subject.showLogin();
  assert.equal(c.window.document.querySelector('.solo-settings-dialog'), null);
  assert.equal(c.node('username'), null);
  c.close();
});

test('solo workspaces expose no password controls and creation sends a name and fixed mode', async () => {
  let created;
  const c = setup(async (path, options = {}) => {
    if (path === '/api/workspaces' && options.method === 'POST') { created = JSON.parse(options.body); return response(workspace({ name: created.name, locked: false, passwordProtected: false })); }
    if (path === '/api/workspaces') return response({ workspaces: [] });
    if (path.includes('/files?')) return response({ path: '', entries: [] });
    return response({ docker: true, workerImage: true, claudeAuthenticated: true });
  });
  c.subject.adoptSession({ mode: 'solo', admin: true, csrfToken: 'solo-csrf', user: { id: 'single' } });
  await c.subject.showDashboard();
  c.window.document.querySelector('[data-action="create-workspace"]').click(); await flush();
  const dialog = c.window.document.querySelector('dialog');
  assert.equal(dialog.querySelector('input[type="password"]'), null);
  dialog.querySelector('input').value = '我的项目';
  dialog.querySelector('form').dispatchEvent(new c.window.Event('submit', { cancelable: true }));
  await flush(); await flush();
  assert.deepEqual(created, { name: '我的项目', mode: 'code' });
  assert.equal(c.window.document.querySelector('[data-action="workspace-password"], [data-action="lock-workspace"]'), null);
  await c.subject.lockWorkspace(); await c.subject.changeWorkspacePassword();
  assert.equal(c.calls.some(([path]) => /\/(password|lock|unlock)$/.test(path)), false);
  c.close();
});

test('solo mode never grants settings without a server-confirmed owner session', () => {
  const c = setup();
  assert.throws(() => c.subject.adoptSession({ mode: 'solo', csrfToken: 'solo-csrf', user: { id: 'single' } }));
  assert.equal(c.subject.state.authenticated, false);
  assert.equal(c.window.document.querySelector('.solo-settings-dialog'), null);
  c.close();
});

test('cleanup from a closed solo browser cannot expire a newer website session', async () => {
  const c = setup(async () => response({ detail: 'Expired previous session' }, 401));
  c.subject.adoptSession({ mode: 'solo', admin: true, csrfToken: 'new-solo-csrf', user: { id: 'single' } });
  c.subject.state.authenticated = true;
  await assert.rejects(c.subject.api('/api/admin/claude-account-browser/stop', { method: 'POST', csrfToken: 'old-solo-csrf' }));
  assert.equal(c.subject.state.authenticated, true);
  assert.equal(c.subject.state.csrf, 'new-solo-csrf');
  assert.equal(c.calls.at(-1)[1].headers['X-CSRF-Token'], 'old-solo-csrf');
  await assert.rejects(c.subject.api('/api/status'));
  assert.equal(c.subject.state.authenticated, false); c.close();
});

test('account button opens usage menu instead of logging out and logout clears account-specific state', async () => {
  const c = setup(async (path) => path === '/api/workspaces' ? response({ workspaces: [workspace({ locked: false })] }) : path === '/api/account/usage' ? response({ quota: { fiveHour: { limit: 20000, used: 10000, remaining: 10000 }, weekly: { limit: null, used: 10000, remaining: null } }, official: {}, activeTasks: [] }) : path.includes('/files?') ? response({ path: '', entries: [] }) : response({ docker: true, workerImage: true }));
  await c.subject.showDashboard();
  c.node('account-button').click(); await flush();
  assert.ok(c.node('account-usage-menu')); assert.equal(c.subject.state.authenticated, true);
  assert.equal(c.calls.some(([path]) => path === '/api/logout'), false);
  assert.equal(c.subject.activity.context.id, A);
  c.subject.attention.beginView(A); c.subject.attention.observe(A, 'run', [{ id: 'private-approval' }]);
  c.subject.showLogin();
  assert.equal(c.node('account-usage-menu'), null); assert.equal(c.subject.activity.context, null); assert.equal(c.subject.attention.seen.size, 0);
  assert.equal(c.subject.attention.page, null); assert.equal(c.subject.accountMenu.button, null);
  c.close();
});

test('workspace ZIP is available without selecting a file, while stopped workspaces explain how to enable it', async () => {
  const c = setup(async (path) => path === '/api/workspaces' ? response({ workspaces: [workspace({ locked: false })] }) : path.includes('/files?') ? response({ path: '', entries: [] }) : response({ docker: true, workerImage: true }));
  await c.subject.showDashboard();
  assert.equal(c.subject.state.selectedFile, null);
  assert.equal(c.node('archive-download').disabled, false);
  assert.match(c.node('archive-download').textContent, /下载工作区 ZIP/);
  assert.match(c.node('archive-hint').textContent, /已保存文件.*256 MiB.*512 MiB.*50,000/);
  c.subject.state.selectedFile = { path: 'readme.txt', content: 'saved text', encoding: 'utf-8', size: 10 };
  c.subject.renderEditor();
  assert.match(c.window.document.querySelector('[data-action="download-file"]').textContent, /下载当前文件/);
  c.subject.state.workspaces[0].running = false;
  c.subject.updateWorkspaceControls();
  assert.equal(c.node('archive-download').disabled, true);
  assert.match(c.node('archive-hint').textContent, /先启动工作区.*无需启动 Claude/);
  c.close();
});

test('workspace switching, locking and logout cancel an in-flight archive without downloading its late response', async () => {
  for (const reason of ['switch', 'lock', 'logout']) {
    const gate = deferred(); let archiveSignal;
    const c = setup(async (path, options) => {
      if (path === '/api/workspaces') return response({ workspaces: [workspace({ locked: false }), workspace({ id: B, locked: false })] });
      if (path.endsWith('/archive')) { archiveSignal = options.signal; return gate.promise; }
      if (path.includes('/files?')) return response({ path: '', entries: [] });
      return response({ docker: true, workerImage: true });
    });
    await c.subject.showDashboard();
    const pending = c.subject.archiveDownload.start();
    assert.equal(c.node('archive-download').disabled, true);
    assert.match(c.node('archive-download').textContent, /正在打包并下载/);
    if (reason === 'switch') await c.subject.selectWorkspace(B);
    else if (reason === 'lock') c.subject.markWorkspaceLocked(A);
    else c.subject.showLogin();
    assert.equal(archiveSignal.aborted, true, reason);
    gate.resolve({ ok: true, status: 200, headers: { get() { throw new Error('A cancelled response must never be read'); } } });
    assert.equal(await pending, false);
    assert.equal(c.subject.archiveDownload.busy, false);
    c.close();
  }
});

test('archive server errors appear as text and a 423 response locks the workspace', async () => {
  let archiveStatus = 429;
  const c = setup(async (path) => {
    if (path === '/api/workspaces') return response({ workspaces: [workspace({ locked: false })] });
    if (path.endsWith('/archive')) return response({ detail: '<img src=x onerror=alert(1)> 请稍后重试' }, archiveStatus);
    if (path.includes('/files?')) return response({ path: '', entries: [] });
    return response({ docker: true, workerImage: true });
  });
  await c.subject.showDashboard();
  await c.subject.archiveDownload.start();
  assert.equal(c.window.document.querySelector('.toast img'), null);
  assert.match(c.window.document.querySelector('.toast').textContent, /<img src=x/);
  assert.equal(c.node('archive-download').disabled, false);
  archiveStatus = 423;
  await c.subject.archiveDownload.start();
  assert.ok(c.node('workspace-unlock-form'));
  assert.equal(c.node('archive-download'), null);
  c.close();
});

test('unlock submits password without storing it and only then mounts content', async () => {
  const c = setup(async (path, options) => {
    if (path === '/api/workspaces') return response({ workspaces: [workspace()] });
    if (path.endsWith('/unlock')) { assert.equal(JSON.parse(options.body).password, 'workspace-secret'); return response(workspace({ locked: false })); }
    if (path.includes('/files?')) return response({ path: '', entries: [] });
    return response({ docker: true, workerImage: true });
  });
  await c.subject.showDashboard();
  const input = c.node('workspace-unlock-password'); input.value = 'workspace-secret';
  c.node('workspace-unlock-form').dispatchEvent(new c.window.Event('submit', { bubbles: true, cancelable: true }));
  await flush(); await flush();
  assert.equal(input.value, '');
  assert.equal(c.subject.state.workspaces[0].locked, false);
  assert.deepEqual(c.subject.chat.mounts, [A]);
  assert.ok(c.calls.find(([path]) => path.includes('/files?')));
  assert.doesNotMatch(JSON.stringify({ ...c.window.localStorage }), /workspace-secret/);
  c.close();
});

test('server relock immediately removes cached files, drafts, attachment previews and chat', async () => {
  const gate = deferred();
  let locked = false;
  const c = setup(async (path) => {
    if (path === '/api/workspaces') return response({ workspaces: [workspace({ locked })] });
    if (path.includes('/files?')) return gate.promise;
    return response({ docker: true, workerImage: true });
  });
  const dashboard = c.subject.showDashboard();
  await flush();
  const attachment = { url: 'blob:test' };
  c.subject.chat.drafts.set(A, 'PRIVATE DRAFT');
  c.subject.chat.attachments.drafts.set(A, [attachment]);
  c.subject.state.selectedFile = { path: 'secret.txt', content: 'PRIVATE FILE' };
  locked = true;
  await c.subject.refreshWorkspaces();
  assert.ok(c.node('workspace-unlock-form'));
  assert.equal(c.node('chat-panel'), null);
  assert.equal(c.subject.state.selectedFile, null);
  assert.equal(c.subject.chat.drafts.has(A), false);
  assert.equal(c.subject.chat.attachments.drafts.has(A), false);
  assert.equal(attachment.released, true);
  gate.resolve(response({ path: '', entries: [{ name: 'secret.txt', type: 'file', size: 10 }] }));
  await dashboard;
  assert.deepEqual([...c.subject.state.entries], []);
  assert.doesNotMatch(c.window.document.body.textContent, /PRIVATE|secret.txt/);
  c.close();
});

test('423 response clears private content and keeps background tasks running', async () => {
  const c = setup(async (path) => {
    if (path === '/api/workspaces') return response({ workspaces: [workspace({ locked: false })] });
    if (path.includes('/files?')) return response({ path: '', entries: [] });
    if (path.endsWith('/chat')) return response({ detail: '工作区已锁定' }, 423);
    return response({ docker: true, workerImage: true });
  });
  await c.subject.showDashboard();
  await assert.rejects(c.subject.api(`/api/workspaces/${A}/chat`), /工作区已锁定/);
  assert.ok(c.node('workspace-unlock-form'));
  assert.equal(c.subject.state.workspaces[0].running, true);
  assert.equal(c.calls.some(([path]) => path.endsWith('/stop')), false);
  c.close();
});

test('administrator deletion discovered by refresh removes all deleted workspace content', async () => {
  let deleted = false;
  const c = setup(async (path) => path === '/api/workspaces' ? response({ workspaces: deleted ? [] : [workspace({ locked: false })] }) : path.includes('/files?') ? response({ path: '', entries: [] }) : response({ docker: true, workerImage: true }));
  await c.subject.showDashboard();
  c.subject.chat.drafts.set(A, 'PRIVATE'); deleted = true;
  await c.subject.refreshWorkspaces();
  assert.equal(c.subject.state.selectedId, null);
  assert.equal(c.subject.chat.drafts.has(A), false);
  assert.equal(c.node('chat-panel'), null);
  assert.match(c.window.document.body.textContent, /从一个工作区开始/);
  c.close();
});

test('expired login clears user drafts and rejects responses from an earlier account', async () => {
  const gate = deferred();
  const c = setup(async (path) => path === '/late' ? gate.promise : path === '/expire' ? response({ detail: 'Expired' }, 401) : response({}));
  c.subject.state.authenticated = true;
  c.subject.chat.drafts.set(A, 'PRIVATE DRAFT');
  c.subject.chat.attachments.drafts.set(A, [{ file: 'PRIVATE FILE' }]);
  const late = c.subject.api('/late');
  await assert.rejects(c.subject.api('/expire'));
  assert.equal(c.subject.chat.drafts.size, 0);
  assert.equal(c.subject.chat.attachments.drafts.size, 0);
  assert.equal(c.subject.state.user, null);
  assert.equal(c.node('username').value, '');
  assert.equal(c.node('password').value, '');
  assert.equal(c.node('username').hasAttribute('placeholder'), false);
  assert.equal(c.node('password').hasAttribute('placeholder'), false);
  c.subject.adoptSession({ csrfToken: 'second-csrf', user: { id: 'two', username: 'bob' } });
  gate.resolve(response({ private: 'alice-data' }));
  await assert.rejects(late, /登录状态已改变/);
  assert.equal(c.subject.state.user.username, 'bob');
  c.close();
});

test('workspace selection is scoped to the signed-in account', async () => {
  const c = setup(async (path) => path === '/api/workspaces' ? response({ workspaces: [workspace(), workspace({ id: B })] }) : response({ docker: true, workerImage: true }));
  c.window.localStorage.setItem('claude-cloud-workspace:other-user', B);
  c.window.localStorage.setItem('claude-cloud-workspace:user-one', A);
  c.subject.adoptSession({ csrfToken: 'csrf', user: { id: 'user-one', username: 'alice' } });
  await c.subject.showDashboard();
  assert.equal(c.subject.state.selectedId, A);
  await c.subject.selectWorkspace(B);
  assert.equal(c.window.localStorage.getItem('claude-cloud-workspace:user-one'), B);
  c.close();
});

test('account change in another tab clears the old private workspace immediately', async () => {
  const c = setup();
  await c.subject.showDashboard();
  c.subject.chat.drafts.set(A, 'PRIVATE OLD ACCOUNT DRAFT');
  c.window.dispatchEvent(new c.window.StorageEvent('storage', { key: 'claude-cloud-session-event', newValue: 'new-login' }));
  assert.equal(c.subject.state.authenticated, false);
  assert.equal(c.subject.state.user, null);
  assert.equal(c.subject.chat.drafts.size, 0);
  assert.equal(c.node('workspace-unlock-form'), null);
  assert.ok(c.node('login-form'));
  c.close();
});

test('creation password form validates matching passwords and clears inputs on close', async () => {
  const c = setup();
  const pending = c.window.Access.workspacePasswordForm({ create: true });
  c.node('workspace-create-name').value = '测试项目';
  const password = c.node('workspace-new-password'); password.value = '12345678';
  const confirm = c.node('workspace-confirm-password'); confirm.value = 'not-matching';
  const form = password.closest('form');
  form.dispatchEvent(new c.window.Event('submit', { cancelable: true }));
  assert.match(form.querySelector('.form-error').textContent, /不一致/);
  confirm.value = password.value;
  form.dispatchEvent(new c.window.Event('submit', { cancelable: true }));
  assert.deepEqual(JSON.parse(JSON.stringify(await pending)), { name: '测试项目', password: '12345678' });
  assert.equal(password.value, ''); assert.equal(confirm.value, '');
  assert.equal(c.window.document.querySelector('dialog'), null);
  c.close();
});

test('removing a workspace password still requires its current password', async () => {
  const c = setup();
  const pending = c.window.Access.workspacePasswordForm({ protectedWorkspace: true });
  const current = c.node('workspace-current-password');
  const form = current.closest('form');
  form.dispatchEvent(new c.window.Event('submit', { cancelable: true }));
  assert.match(form.querySelector('.form-error').textContent, /当前/);
  current.value = 'current-secret';
  form.dispatchEvent(new c.window.Event('submit', { cancelable: true }));
  assert.deepEqual(JSON.parse(JSON.stringify(await pending)), { currentPassword: 'current-secret', newPassword: '', confirmPassword: '' });
  assert.equal(current.value, '');
  c.close();
});
