import assert from 'node:assert/strict';
import test from 'node:test';
import { JSDOM } from 'jsdom';
import { SoloSettings } from '../src/solo-settings.js';

const tick = () => new Promise((resolve) => setImmediate(resolve));
function deferred() { let resolve; const promise = new Promise((done) => { resolve = done; }); return { resolve, promise }; }
function fixture(handler = async () => ({ authenticated: true })) {
  const dom = new JSDOM('<body></body>', { url: 'https://work.test/' });
  const page = dom.window.document;
  dom.window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  dom.window.HTMLDialogElement.prototype.close = function () { this.open = false; };
  let context = { authenticated: true, mode: 'solo', admin: true, key: 'session-1', csrf: 'csrf-1' };
  const calls = [], panels = [], browsers = [], notifications = [];
  const settings = new SoloSettings({ page, getContext: () => context,
    api: async (path, options) => { calls.push([path, options]); return handler(path, options); },
    expire: () => notifications.push('expired'), passwordChanged: () => notifications.push('password-changed'),
    panelFactory: (options) => {
      const panel = { ...options, async mount(host) { this.host = host; host.textContent = '连接面板'; return this.request(`${this.namespace}/status`); }, unmount() { this.unmounted = true; } };
      panels.push(panel); return panel;
    },
    browserFactory: (options) => { const browser = { ...options, show(purpose) { this.purpose = purpose; }, close() { this.closed = true; } }; browsers.push(browser); return browser; },
  });
  return { dom, page, settings, calls, panels, browsers, notifications, setContext(value) { context = value; }, close() { settings.close(); dom.window.close(); } };
}

test('settings cannot mount in an unauthenticated, ordinary-user or unconfirmed owner context', () => {
  const c = fixture();
  for (const context of [{ authenticated: false, mode: 'solo', admin: true }, { authenticated: true, mode: 'multiuser', admin: false }, { authenticated: true, mode: 'solo', admin: false }]) {
    c.setContext(context); c.settings.show(); assert.equal(c.settings.dialog, undefined);
  }
  assert.equal(c.calls.length, 0); c.close();
});

test('settings mount only selected services and use the unified session without account provisioning', async () => {
  const c = fixture(); c.settings.show('claude'); await tick();
  assert.equal(c.panels.length, 1); assert.equal(c.panels[0].solo, true);
  assert.equal(c.calls[0][0], '/api/admin/claude/status');
  assert.equal(c.calls[0][1].csrfToken, 'csrf-1');
  assert.equal(c.page.querySelector('[data-settings-section="password"]').open, false);
  assert.doesNotMatch(c.settings.dialog.textContent, /创建用户|用户管理|平均分配|工作区密码/);
  c.settings.show('usage'); await tick();
  assert.equal(c.panels.length, 2); assert.equal(c.calls[1][0], '/api/admin/usage-auth/status');
  c.settings.show('account'); c.settings.openBrowser('billing');
  assert.equal(c.browsers[0].purpose, 'billing');
  c.settings.close();
  assert.ok(c.panels.every((panel) => panel.unmounted)); assert.equal(c.browsers[0].closed, true);
  assert.equal(c.page.querySelector('dialog'), null); c.close();
});

test('password change submits current and confirmed new password once, clears fields, and requires re-login', async () => {
  const gate = deferred(); const c = fixture(() => gate.promise); c.settings.show('password');
  c.page.querySelector('#solo-current-password').value = 'old-private-password';
  c.page.querySelector('#solo-new-password').value = 'new-private-password';
  c.page.querySelector('#solo-confirm-password').value = 'new-private-password';
  const operation = c.settings.changePassword();
  await c.settings.changePassword();
  assert.equal(c.calls.length, 1); assert.equal(c.calls[0][0], '/api/settings/password');
  assert.deepEqual(c.calls[0][1].body, { currentPassword: 'old-private-password', newPassword: 'new-private-password', confirmPassword: 'new-private-password' });
  assert.equal(c.page.querySelector('#solo-current-password').value, '');
  gate.resolve({ ok: true, sessionsRevoked: true }); await operation;
  assert.deepEqual(c.notifications, ['password-changed']); assert.equal(c.page.querySelector('dialog'), null); c.close();
});

test('invalid password confirmation stays local and stale settings cannot use a replacement session', async () => {
  const c = fixture(); c.settings.show('password');
  c.page.querySelector('#solo-current-password').value = 'old-password';
  c.page.querySelector('#solo-new-password').value = 'new-password-long';
  c.page.querySelector('#solo-confirm-password').value = 'different-password';
  await c.settings.changePassword(); assert.equal(c.calls.length, 0);
  assert.match(c.page.querySelector('[data-password-message]').textContent, /不一致/);
  const oldContext = c.settings.context;
  c.setContext({ authenticated: true, mode: 'solo', admin: true, key: 'session-2', csrf: 'csrf-2' });
  await assert.rejects(c.settings.request('/api/admin/claude/status', {}, oldContext), /登录已过期/);
  assert.equal(c.calls.length, 0); c.close();
});

test('suspension clears password and browser views without waiting on stalled service requests', async () => {
  const gate = deferred(); const c = fixture(() => gate.promise); c.settings.show('claude');
  c.settings.openBrowser('register');
  const input = c.page.querySelector('#solo-current-password'); input.value = 'must-not-stay';
  c.settings.suspend();
  assert.equal(input.value, ''); assert.equal(c.page.querySelector('dialog'), null);
  assert.equal(c.browsers[0].closed, true); assert.equal(c.panels[0].unmounted, true);
  gate.resolve({ authenticated: true }); await tick();
  assert.equal(c.page.querySelector('dialog'), null); c.close();
});

test('a partially applied model restriction is re-read and retried without claiming an unsafe rollback', async () => {
  let saved = false, retry = false;
  const c = fixture(async (_path, options) => {
    if (options.method === 'PUT') {
      saved = true;
      if (!retry) throw new Error('任务仍在停止');
      return { fableAllowed: false, enforcementReady: true };
    }
    return { fableAllowed: !saved, enforcementReady: !saved };
  });
  c.settings.show('advanced'); await tick();
  assert.equal(c.page.querySelector('[data-fable-toggle]').checked, true);
  await c.settings.changePolicy(false);
  assert.equal(c.page.querySelector('[data-fable-toggle]').checked, false);
  assert.equal(c.page.querySelector('[data-fable-toggle]').disabled, true);
  assert.equal(c.page.querySelector('[data-policy-apply]').hidden, false);
  retry = true; await c.settings.changePolicy(false);
  assert.equal(c.page.querySelector('[data-policy-apply]').hidden, true);
  assert.match(c.page.querySelector('[data-policy-message]').textContent, /已保存/); c.close();
});

test('browser cleanup keeps its captured session and cannot borrow a newly logged-in owner token', async () => {
  const c = fixture(); c.settings.show('account'); c.settings.openBrowser('register');
  const oldBrowser = c.browsers[0]; c.settings.close();
  c.setContext({ authenticated: true, mode: 'solo', admin: true, key: 'session-2', csrf: 'csrf-2' });
  c.settings.show('account');
  await assert.rejects(oldBrowser.api('/api/admin/claude-account-browser', { method: 'POST' }), /登录已过期/);
  await oldBrowser.api('/api/admin/claude-account-browser/stop', { method: 'POST' });
  assert.equal(c.calls.at(-1)[1].csrfToken, 'csrf-1'); c.close();
});
