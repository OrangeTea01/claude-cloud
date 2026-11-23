import assert from 'node:assert/strict';
import test from 'node:test';
import { JSDOM } from 'jsdom';
import { AdminPage } from '../src/admin.js';

const OWNER = { id: 'a'.repeat(32), username: 'owner', createdAt: '2026-11-16T10:00:00Z' };
const ALICE = { id: 'b'.repeat(32), username: 'alice', createdAt: '2026-11-16T11:00:00Z' };
const WORKSPACE = { id: 'c'.repeat(32), name: '项目截图', ownerId: ALICE.id, username: 'alice', createdAt: '', running: true, passwordProtected: true };
const response = (data, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => data });
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; };
function setup(handler = () => undefined, initialSpaces = [WORKSPACE]) {
  const dom = new JSDOM('<!doctype html><html><body><div id="fixture"></div></body></html>', { url: 'https://fixture.test/admin/' });
  const host = dom.window.document.querySelector('#fixture');
  const calls = [], records = { users: [OWNER, ALICE], workspaces: initialSpaces };
  const page = new AdminPage(host, { usageFactory: () => ({ mount() {}, controls() {}, unmount() {} }),
    quotaFactory: () => ({ mount() {}, controls() {}, unmount() {}, async refresh() {} }), claudeFactory: () => ({ mount() {}, controls() {}, unmount() {} }),
    fetch: async (path, options) => {
      calls.push({ path, ...options });
      const custom = await handler(path, options, records);
      if (custom !== undefined) return custom;
      if (path.endsWith('/session')) return response({ enabled: true, authenticated: true, csrfToken: 'admin-fixture-csrf' });
      if (path.endsWith('/model-policy')) return response({ fableAllowed: false, revision: 0 });
      if (path.endsWith('/users') && options.method === 'GET') return response({ users: records.users });
      if (path.endsWith('/workspaces') && options.method === 'GET') return response({ workspaces: records.workspaces });
      throw new Error(`Unexpected fixture request ${path}`);
    },
  });
  const node = (id) => host.querySelector(`#${id}`);
  const createDraft = (username = 'bob') => {
    node('create-username').value = username;
    node('create-password').value = 'Fixture password 2026';
    node('create-confirm-password').value = 'Fixture password 2026';
  };
  return { dom, host, page, calls, records, node, createDraft };
}

test('admin lists independent accounts and escaped workspace metadata without user content', async () => {
  const malicious = { ...WORKSPACE, name: '<img src=x onerror=alert(1)>', username: '<script>bad()</script>' };
  const c = setup(undefined, [malicious]);
  await c.page.start();
  assert.deepEqual([...c.node('password-user').options].map((item) => item.textContent), ['owner', 'alice']);
  assert.equal(c.node('password-user').value, OWNER.id);
  assert.equal(c.node('admin-workspace-count').textContent, '1');
  assert.match(c.node('admin-workspace-list').textContent, /<img src=x onerror=alert\(1\)>/);
  assert.equal(c.node('admin-workspace-list').querySelector('img,script'), null);
  assert.equal(c.node('delete-all-workspaces').disabled, false);
  assert.ok(c.calls.filter((call) => call.path.endsWith('/users') || call.path.endsWith('/workspaces')).every((call) => call.headers['X-CSRF-Token'] === 'admin-fixture-csrf'));
  assert.equal(c.dom.window.localStorage.length, 0);
});

test('account creation uses admin CSRF, deduplicates clicks and clears password fields after confirmation', async () => {
  const gate = deferred();
  const c = setup((path, options) => path.endsWith('/users') && options.method === 'POST' ? gate.promise : undefined);
  await c.page.start(); c.createDraft('  BOB  ');
  const task = c.page.createUser();
  await c.page.createUser(); await c.page.logout();
  const writes = c.calls.filter((call) => call.method === 'POST');
  assert.equal(writes.length, 1);
  assert.equal(writes[0].headers['X-CSRF-Token'], 'admin-fixture-csrf');
  assert.deepEqual(JSON.parse(writes[0].body), { username: 'bob', password: 'Fixture password 2026', confirmPassword: 'Fixture password 2026' });
  assert.equal(c.node('create-user-submit').disabled, true);
  const bob = { id: 'd'.repeat(32), username: 'bob', createdAt: '' };
  c.records.users.push(bob); gate.resolve(response({ user: bob }, 201)); await task;
  assert.equal(c.node('create-password').value, '');
  assert.equal(c.node('create-confirm-password').value, '');
  assert.equal(c.node('create-username').value, '');
  assert.equal(c.node('password-user').options.length, 3);
  assert.match(c.node('create-user-message').textContent, /已创建 bob/);
  assert.doesNotMatch(c.host.textContent, /Fixture password/);
});

test('invalid and conflicting account creation cannot claim success or expose credentials', async () => {
  const c = setup((path, options) => path.endsWith('/users') && options.method === 'POST' ? response({ detail: 'sensitive fixture password' }, 409) : undefined);
  await c.page.start(); c.createDraft('../bad'); await c.page.createUser();
  assert.equal(c.calls.filter((call) => call.method === 'POST').length, 0);
  c.createDraft('alice'); await c.page.createUser();
  assert.match(c.node('create-user-message').textContent, /已存在/);
  assert.equal(c.node('create-user-message').dataset.kind, 'error');
  assert.equal(c.node('create-password').value, '');
  assert.equal(c.node('create-confirm-password').value, '');
  assert.doesNotMatch(c.host.textContent, /sensitive fixture password/);
});

test('password reset targets only the selected account and changing selection clears old drafts', async () => {
  const c = setup((path, options) => path.endsWith('/password') && options.method === 'PUT' ? response({ ok: true, sessionsRevoked: true }) : undefined);
  await c.page.start();
  c.node('new-password').value = 'Previous sensitive draft';
  c.node('confirm-password').value = 'Previous sensitive draft';
  c.node('password-user').value = ALICE.id;
  c.node('password-user').dispatchEvent(new c.dom.window.Event('change'));
  assert.equal(c.node('new-password').value, '');
  c.node('new-password').value = c.node('confirm-password').value = 'Alice new password 2026';
  await c.page.changePassword();
  const writes = c.calls.filter((call) => call.method === 'PUT');
  assert.equal(writes.length, 1);
  assert.equal(writes[0].path, `/api/admin/users/${ALICE.id}/password`);
  assert.match(c.node('admin-message').textContent, /alice/);
  assert.equal(c.node('new-password').value, '');
});

test('bulk deletion requires confirmation and cancelling never sends a mutation', async () => {
  const c = setup(); await c.page.start();
  const gate = deferred(); c.page.confirmWorkspaceDeletion = () => gate.promise;
  const task = c.page.deleteAllWorkspaces();
  assert.equal(c.node('delete-all-workspaces').disabled, true);
  await c.page.deleteAllWorkspaces();
  assert.equal(c.calls.filter((call) => call.method === 'POST').length, 0);
  gate.resolve(false); await task;
  assert.equal(c.calls.filter((call) => call.method === 'POST').length, 0);
  assert.equal(c.node('delete-all-workspaces').disabled, false);
});

test('bulk deletion sends the exact admin confirmation and refreshes after verified completion', async () => {
  const gate = deferred();
  const c = setup((path, options) => path.endsWith('/delete-all') && options.method === 'POST' ? gate.promise : undefined);
  await c.page.start(); c.page.confirmWorkspaceDeletion = async () => true;
  const task = c.page.deleteAllWorkspaces();
  await Promise.resolve(); await Promise.resolve();
  await c.page.deleteAllWorkspaces(); await c.page.createUser();
  const request = c.calls.find((call) => call.path.endsWith('/delete-all'));
  assert.ok(request); assert.equal(request.headers['X-CSRF-Token'], 'admin-fixture-csrf');
  assert.deepEqual(JSON.parse(request.body), { confirmation: 'DELETE_ALL_WORKSPACES' });
  assert.equal(c.calls.filter((call) => call.path.endsWith('/delete-all')).length, 1);
  assert.match(c.node('admin-delete-message').textContent, /正在停止/);
  c.records.workspaces = []; gate.resolve(response({ deleted: 1, failed: [], remaining: 0 })); await task;
  assert.equal(c.node('admin-delete-message').dataset.kind, 'success');
  assert.match(c.node('admin-delete-message').textContent, /已删除 1/);
  assert.equal(c.node('admin-workspace-count').textContent, '0');
  assert.equal(c.node('delete-all-workspaces').disabled, true);
  assert.equal(c.node('password-user').options.length, 2, 'account management remains available');
});

test('partial failures and ambiguous deletion replies never report a successful clear', async () => {
  let value = { deleted: 2, remaining: 1, failed: [{ id: WORKSPACE.id, name: WORKSPACE.name, error: 'private server details' }] };
  const c = setup((path) => path.endsWith('/delete-all') ? response(value) : undefined);
  await c.page.start(); c.page.confirmWorkspaceDeletion = async () => true;
  await c.page.deleteAllWorkspaces();
  assert.equal(c.node('admin-delete-message').dataset.kind, 'error');
  assert.match(c.node('admin-delete-message').textContent, /仍有 1/);
  assert.doesNotMatch(c.node('admin-delete-message').textContent, /private server details|当前没有工作区/);
  assert.equal(c.node('delete-all-workspaces').disabled, false);
  value = { ok: true }; await c.page.deleteAllWorkspaces();
  assert.equal(c.node('admin-delete-message').dataset.kind, 'error');
  assert.match(c.node('admin-delete-message').textContent, /待确认/);
});

test('expired admin confirmation and late management replies cannot restore private controls', async () => {
  const c = setup(); await c.page.start();
  const confirmation = deferred(); c.page.confirmWorkspaceDeletion = () => confirmation.promise;
  const deleting = c.page.deleteAllWorkspaces();
  c.page.expire(); confirmation.resolve(true); await deleting;
  assert.equal(c.calls.filter((call) => call.path.endsWith('/delete-all')).length, 0);
  assert.equal(c.node('create-password'), null);
  assert.equal(c.page.users.length, 0);
  assert.equal(c.page.workspaces.length, 0);
  const gate = deferred();
  const d = setup((path) => path.endsWith('/users') ? gate.promise : undefined);
  const loading = d.page.start();
  for (let i = 0; i < 12; i++) await Promise.resolve();
  d.page.expire(); gate.resolve(response({ users: [OWNER] })); await loading;
  assert.equal(d.page.phase, 'login'); assert.equal(d.page.users.length, 0);
  assert.equal(d.node('delete-all-workspaces'), null);
});

test('confirmation dialog names the destructive scope and defaults focus to cancel', async () => {
  const c = setup(); await c.page.start();
  const prototype = c.dom.window.HTMLDialogElement.prototype;
  prototype.showModal = function () { this.open = true; };
  prototype.close = function () { this.open = false; this.dispatchEvent(new c.dom.window.Event('close')); };
  const result = c.page.confirmWorkspaceDeletion();
  const dialog = c.dom.window.document.querySelector('dialog');
  assert.match(dialog.textContent, /上传文件、图片和聊天记录/);
  assert.match(dialog.textContent, /用户账号、密码及管理员的 Claude 统一授权会保留/);
  assert.equal(c.dom.window.document.activeElement, dialog.querySelector('[data-cancel]'));
  dialog.querySelector('[data-cancel]').click(); assert.equal(await result, false);
  assert.equal(c.dom.window.document.querySelector('dialog'), null);
});

test('user rows show owned workspace counts, pending deletion and safely rendered names', async () => {
  const c = setup();
  c.records.users = [OWNER, { ...ALICE, username: '<img src=x onerror=bad()>', deleting: true }];
  await c.page.start();
  const rows = c.node('admin-user-list').querySelectorAll('tbody tr');
  assert.equal(rows[0].querySelector('[data-user-workspace-count]').textContent, '0');
  assert.equal(rows[1].querySelector('[data-user-workspace-count]').textContent, '1');
  assert.match(rows[1].textContent, /<img src=x onerror=bad\(\)>/);
  assert.match(rows[1].textContent, /登录已停用/);
  assert.equal(rows[1].querySelector('[data-delete-user]').textContent, '重试删除');
  assert.equal(rows[1].querySelector('img'), null);
  assert.deepEqual([...c.node('password-user').options].map((item) => item.value), [OWNER.id]);
});

test('deleting a single user requires confirmation and only uses administrator authorization', async () => {
  const gate = deferred();
  const c = setup((path) => path === `/api/admin/users/${ALICE.id}/delete` ? gate.promise : undefined);
  await c.page.start();
  c.page.confirmUserDeletion = async () => false;
  await c.page.deleteUser(ALICE.id);
  assert.equal(c.calls.filter((call) => call.method === 'POST').length, 0);
  c.page.confirmUserDeletion = async () => true;
  const deleting = c.page.deleteUser(ALICE.id);
  await Promise.resolve(); await Promise.resolve();
  await c.page.deleteUser(ALICE.id); await c.page.deleteAllWorkspaces(); await c.page.logout();
  const writes = c.calls.filter((call) => call.method === 'POST');
  assert.equal(writes.length, 1);
  assert.equal(writes[0].path, `/api/admin/users/${ALICE.id}/delete`);
  assert.deepEqual(JSON.parse(writes[0].body), { confirmation: 'DELETE_USER_AND_DATA' });
  assert.equal(writes[0].headers['X-CSRF-Token'], 'admin-fixture-csrf');
  assert.equal(writes[0].credentials, 'same-origin');
  assert.equal(c.node('create-user-submit').disabled, true);
  c.records.users = [OWNER]; c.records.workspaces = [];
  gate.resolve(response({ deleted: true, userId: ALICE.id, username: 'alice', deletedWorkspaces: 1, failed: [], remaining: 0 }));
  await deleting;
  assert.match(c.node('admin-delete-user-message').textContent, /已删除用户 alice/);
  assert.equal(c.node('admin-delete-user-message').dataset.kind, 'success');
  assert.equal(c.node('admin-user-count').textContent, '1');
  assert.equal(c.node('admin-workspace-count').textContent, '0');
  assert.equal(c.node('password-user').value, OWNER.id);
});

test('partial user deletion keeps a disabled login and supports retry without exposing server errors', async () => {
  let value = { deleted: false, userId: ALICE.id, username: 'alice', deletedWorkspaces: 0, remaining: 1,
    failed: [{ id: WORKSPACE.id, name: '<script>unsafe()</script>', error: 'SECRET Docker error' }] };
  const c = setup((path) => path === `/api/admin/users/${ALICE.id}/delete` ? response(value) : undefined);
  c.records.users = [OWNER, { ...ALICE, deleting: true }];
  await c.page.start(); c.page.confirmUserDeletion = async () => true;
  await c.page.deleteUser(ALICE.id);
  assert.match(c.node('admin-delete-user-message').textContent, /尚未完成，登录已停用/);
  assert.equal(c.node('admin-delete-user-message').dataset.kind, 'error');
  assert.doesNotMatch(c.host.textContent, /SECRET|unsafe/);
  const retry = c.node('admin-user-list').querySelector(`[data-delete-user="${ALICE.id}"]`);
  assert.equal(retry.textContent, '重试删除'); assert.equal(retry.disabled, false);
  value = { deleted: true, userId: ALICE.id, username: 'alice', deletedWorkspaces: 1, remaining: 0, failed: [] };
  c.records.users = [OWNER]; c.records.workspaces = [];
  await c.page.deleteUser(ALICE.id);
  assert.equal(c.node('admin-delete-user-message').dataset.kind, 'success');
  assert.equal(c.page.users.length, 1);
});

test('single user deletion rejects ambiguous or mismatched success and discards late responses after expiry', async () => {
  let value = { deleted: true, userId: OWNER.id, username: 'owner', deletedWorkspaces: 0, remaining: 0, failed: [] };
  const c = setup((path) => path.endsWith('/delete') ? response(value) : undefined);
  await c.page.start(); c.page.confirmUserDeletion = async () => true;
  await c.page.deleteUser(ALICE.id);
  assert.equal(c.node('admin-delete-user-message').dataset.kind, 'error');
  assert.match(c.node('admin-delete-user-message').textContent, /待确认/);
  value = { deleted: true, userId: ALICE.id, username: 'alice', deletedWorkspaces: 0, remaining: 1, failed: [] };
  await c.page.deleteUser(ALICE.id);
  assert.equal(c.node('admin-delete-user-message').dataset.kind, 'error');
  const gate = deferred();
  const d = setup((path) => path.endsWith('/delete') ? gate.promise : undefined);
  await d.page.start(); d.page.confirmUserDeletion = async () => true;
  const deleting = d.page.deleteUser(ALICE.id);
  await Promise.resolve(); await Promise.resolve();
  d.page.expire();
  gate.resolve(response({ deleted: true, userId: ALICE.id, username: 'alice', deletedWorkspaces: 1, remaining: 0, failed: [] }));
  await deleting;
  assert.equal(d.page.phase, 'login'); assert.equal(d.page.users.length, 0);
  assert.equal(d.node('admin-delete-user-message'), null);
});

test('single user cleanup accepts unknown remaining counts without mislabelling account errors as failed workspaces', async () => {
  let remaining = null;
  const c = setup((path) => path === `/api/admin/users/${ALICE.id}/delete` ? response({
    deleted: false, userId: ALICE.id, username: 'alice', deletedWorkspaces: 0, remaining,
    failed: [{ id: ALICE.id, name: 'alice', error: 'SECRET account database error' }],
  }) : undefined);
  c.records.users = [OWNER, { ...ALICE, deleting: true }];
  await c.page.start(); c.page.confirmUserDeletion = async () => true;
  await c.page.deleteUser(ALICE.id);
  assert.equal(c.node('admin-delete-user-message').dataset.kind, 'error');
  assert.match(c.node('admin-delete-user-message').textContent, /剩余工作区数量待确认/);
  assert.match(c.node('admin-delete-user-message').textContent, /登录已停用/);
  assert.doesNotMatch(c.host.textContent, /null|SECRET|1 个工作区未能删除/);
  assert.equal(c.node('admin-user-list').querySelector(`[data-delete-user="${ALICE.id}"]`).disabled, false);
  remaining = 0;
  await c.page.deleteUser(ALICE.id);
  assert.match(c.node('admin-delete-user-message').textContent, /还剩 0 个工作区/);
  assert.match(c.node('admin-delete-user-message').textContent, /删除尚未完成/);
  assert.doesNotMatch(c.node('admin-delete-user-message').textContent, /1 个工作区未能删除/);
});

test('user and batch confirmation names exact targets and destructive scope with no typed secret', async () => {
  const c = setup(); await c.page.start();
  const prototype = c.dom.window.HTMLDialogElement.prototype;
  prototype.showModal = function () { this.open = true; };
  prototype.close = function () { this.open = false; this.dispatchEvent(new c.dom.window.Event('close')); };
  let pending = c.page.confirmUserDeletion({ ...ALICE, username: '<img src=x>' });
  let dialog = c.dom.window.document.querySelector('dialog');
  assert.match(dialog.textContent, /<img src=x>/);
  assert.equal(dialog.querySelector('img,input'), null);
  assert.match(dialog.textContent, /上传文件、图片和聊天记录/);
  assert.match(dialog.textContent, /不可撤销/);
  assert.equal(c.dom.window.document.activeElement, dialog.querySelector('[data-cancel]'));
  dialog.querySelector('[data-cancel]').click(); assert.equal(await pending, false);
  pending = c.page.confirmSelectedUserDeletion([OWNER, ALICE]);
  dialog = c.dom.window.document.querySelector('dialog');
  assert.match(dialog.textContent, /owner（0 个工作区）/); assert.match(dialog.textContent, /alice（1 个工作区）/);
  assert.match(dialog.textContent, /未选中的用户及其数据/);
  dialog.querySelector('[data-confirm]').click(); assert.equal(await pending, true);
});

test('selection and select all are bounded to 100 unique existing accounts and cancellation keeps data', async () => {
  const c = setup();
  c.records.users = Array.from({ length: 101 }, (_, index) => ({ id: String(index), username: `person${index}` }));
  await c.page.start(); c.page.selectAllUsers(true);
  assert.equal(c.page.selectedUserIds.size, 100);
  assert.equal(c.node('admin-selected-user-count').textContent, '已选择 100 个用户');
  const last = c.node('admin-user-list').querySelector('[data-select-user="100"]');
  assert.equal(last.disabled, true);
  c.page.selectUser('100', true); c.page.selectUser('unknown', true);
  assert.equal(c.page.selectedUserIds.size, 100);
  c.page.selectUser('0', false); c.page.selectUser('100', true);
  assert.equal(c.page.selectedUserIds.size, 100); assert.equal(c.page.selectedUserIds.has('0'), false);
  c.page.selectAllUsers(false); assert.equal(c.page.selectedUserIds.size, 0);
  c.page.selectUser('1', true);
  c.page.confirmSelectedUserDeletion = async () => false;
  await c.page.deleteSelectedUsers();
  assert.equal(c.calls.filter((call) => call.method === 'POST').length, 0);
  assert.equal(c.page.selectedUserIds.size, 1);
});

test('batch deletion sends only selected IDs once and preserves unselected users', async () => {
  const gate = deferred();
  const c = setup((path) => path.endsWith('/delete-batch') ? gate.promise : undefined);
  const bob = { id: 'd'.repeat(32), username: 'bob', createdAt: '' };
  c.records.users.push(bob);
  await c.page.start(); c.page.selectUser(ALICE.id, true); c.page.selectUser(bob.id, true);
  c.page.confirmSelectedUserDeletion = async () => true;
  const deleting = c.page.deleteSelectedUsers();
  await Promise.resolve(); await Promise.resolve();
  await c.page.deleteSelectedUsers(); await c.page.deleteUser(OWNER.id);
  c.page.selectAllUsers(true);
  assert.equal(c.page.selectedUserIds.has(OWNER.id), false);
  const writes = c.calls.filter((call) => call.method === 'POST');
  assert.equal(writes.length, 1);
  assert.equal(writes[0].path, '/api/admin/users/delete-batch');
  assert.equal(writes[0].headers['X-CSRF-Token'], 'admin-fixture-csrf');
  assert.deepEqual(JSON.parse(writes[0].body), { userIds: [ALICE.id, bob.id], confirmation: 'DELETE_USERS_AND_DATA' });
  c.records.users = [OWNER]; c.records.workspaces = [];
  gate.resolve(response({ deletedUsers: 2, failedUsers: 0, results: [
    { deleted: true, userId: ALICE.id, username: 'alice', deletedWorkspaces: 1, failed: [], remaining: 0 },
    { deleted: true, userId: bob.id, username: 'bob', deletedWorkspaces: 0, failed: [], remaining: 0 },
  ] })); await deleting;
  assert.equal(c.node('admin-delete-user-message').dataset.kind, 'success');
  assert.match(c.node('admin-delete-user-message').textContent, /已删除所选的 2 个用户/);
  assert.deepEqual(c.page.users.map((user) => user.id), [OWNER.id]);
  assert.equal(c.page.selectedUserIds.size, 0);
});

test('partial batch deletion retains only failed selection and reports unknown remaining counts safely', async () => {
  const c = setup((path) => path.endsWith('/delete-batch') ? response({ deletedUsers: 1, failedUsers: 1, results: [
    { deleted: true, userId: OWNER.id, username: 'owner', deletedWorkspaces: 0, failed: [], remaining: 0 },
    { deleted: false, userId: ALICE.id, username: 'alice', deletedWorkspaces: 0, failed: [{ id: WORKSPACE.id, name: '<script>x</script>', error: 'secret detail' }], remaining: null },
  ] }) : undefined);
  await c.page.start(); c.page.selectAllUsers(true); c.page.confirmSelectedUserDeletion = async () => true;
  c.records.users = [{ ...ALICE, deleting: true }];
  await c.page.deleteSelectedUsers();
  assert.equal(c.node('admin-delete-user-message').dataset.kind, 'error');
  assert.match(c.node('admin-delete-user-message').textContent, /alice/);
  assert.match(c.node('admin-delete-user-message').textContent, /登录已停用/);
  assert.doesNotMatch(c.host.textContent, /secret detail|null|<script>/);
  assert.deepEqual([...c.page.selectedUserIds], [ALICE.id]);
  assert.equal(c.node('delete-selected-users').disabled, false);
  assert.equal(c.node('admin-submit').disabled, true);
  assert.equal(c.node('create-user-submit').disabled, false);
});

test('batch results must exactly cover selected users and cannot silently claim missing or duplicate deletions', async () => {
  const ownerResult = { deleted: true, userId: OWNER.id, username: 'owner', deletedWorkspaces: 0, failed: [], remaining: 0 };
  let value = { results: [ownerResult, ownerResult], deletedUsers: 2, failedUsers: 0 };
  const c = setup((path) => path.endsWith('/delete-batch') ? response(value) : undefined);
  await c.page.start(); c.page.selectAllUsers(true); c.page.confirmSelectedUserDeletion = async () => true;
  await c.page.deleteSelectedUsers();
  assert.equal(c.node('admin-delete-user-message').dataset.kind, 'error');
  assert.equal(c.page.users.length, 2); assert.equal(c.page.selectedUserIds.size, 2);
  value = { results: [ownerResult], deletedUsers: 1, failedUsers: 0 };
  await c.page.deleteSelectedUsers();
  assert.match(c.node('admin-delete-user-message').textContent, /待确认/);
  assert.equal(c.page.users.length, 2);
});

test('deleting the last owner leaves administrator access and account creation available', async () => {
  const c = setup((path, options, records) => {
    if (path === `/api/admin/users/${OWNER.id}/delete`) {
      records.users = []; records.workspaces = [];
      return response({ deleted: true, userId: OWNER.id, username: 'owner', deletedWorkspaces: 0, remaining: 0, failed: [] });
    }
  }, []);
  c.records.users = [OWNER];
  await c.page.start(); c.page.confirmUserDeletion = async () => true;
  c.node('new-password').value = c.node('confirm-password').value = 'previous password draft';
  await c.page.deleteUser(OWNER.id);
  assert.equal(c.page.phase, 'ready'); assert.equal(c.page.csrf, 'admin-fixture-csrf');
  assert.equal(c.node('admin-user-count').textContent, '0');
  assert.equal(c.node('create-user-submit').disabled, false);
  assert.equal(c.node('admin-submit').disabled, true);
  assert.equal(c.node('new-password').value, '');
  assert.match(c.node('admin-user-list').textContent, /创建新用户/);
});

test('failed account refresh disables all delete controls and stale batch confirmations send nothing', async () => {
  let failRead = false;
  const c = setup((path, options) => failRead && path.endsWith('/users') && options.method === 'GET' ? response({}, 503) : undefined);
  await c.page.start(); c.page.selectAllUsers(true);
  failRead = true; await c.page.loadManagement();
  assert.equal(c.node('delete-selected-users').disabled, true);
  assert.ok([...c.node('admin-user-list').querySelectorAll('[data-delete-user]')].every((node) => node.disabled));
  await c.page.deleteUser(ALICE.id); await c.page.deleteSelectedUsers();
  assert.equal(c.calls.filter((call) => call.method === 'POST').length, 0);
  failRead = false; await c.page.loadManagement();
  const gate = deferred(); c.page.confirmSelectedUserDeletion = () => gate.promise;
  const deleting = c.page.deleteSelectedUsers(); c.page.expire(); gate.resolve(true); await deleting;
  assert.equal(c.calls.filter((call) => call.method === 'POST').length, 0);
  assert.equal(c.page.selectedUserIds.size, 0);
  assert.equal(c.node('admin-user-list'), null);
});

test('batch deletion expiry and late acknowledgements cannot restore selected accounts or admin controls', async () => {
  const c = setup((path) => path.endsWith('/delete-batch') ? response({ detail: 'private upstream error' }, 401) : undefined);
  await c.page.start(); c.page.selectAllUsers(true); c.page.confirmSelectedUserDeletion = async () => true;
  await c.page.deleteSelectedUsers();
  assert.equal(c.page.phase, 'login'); assert.equal(c.page.selectedUserIds.size, 0);
  assert.equal(c.node('admin-user-list'), null); assert.doesNotMatch(c.host.textContent, /private upstream error/);
  const gate = deferred();
  const d = setup((path) => path.endsWith('/delete-batch') ? gate.promise : undefined);
  await d.page.start(); d.page.selectUser(ALICE.id, true); d.page.confirmSelectedUserDeletion = async () => true;
  const deleting = d.page.deleteSelectedUsers();
  await Promise.resolve(); await Promise.resolve();
  d.page.expire(); gate.resolve(response({ deletedUsers: 1, failedUsers: 0, results: [
    { deleted: true, userId: ALICE.id, username: 'alice', deletedWorkspaces: 1, failed: [], remaining: 0 },
  ] })); await deleting;
  assert.equal(d.page.phase, 'login'); assert.equal(d.page.users.length, 0);
  assert.equal(d.page.selectedUserIds.size, 0); assert.equal(d.node('admin-user-list'), null);
});
