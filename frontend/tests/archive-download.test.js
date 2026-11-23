import assert from 'node:assert/strict';
import test from 'node:test';
import { JSDOM } from 'jsdom';
import { ArchiveDownload, archiveFilename, MAX_ARCHIVE_BYTES } from '../src/archive-download.js';

const ID = 'a'.repeat(32);
const zipBytes = () => Uint8Array.from([80, 75, 5, 6, ...new Array(18).fill(0)]);
const zipResponse = (headers = {}) => new Response(zipBytes(), { headers: { 'Content-Type': 'application/zip', 'Content-Length': '22', ...headers } });
function deferred() { let resolve; const promise = new Promise((yes) => { resolve = yes; }); return { promise, resolve }; }
const flush = () => new Promise((resolve) => setImmediate(resolve));

function setup(handler = async () => zipResponse()) {
  const dom = new JSDOM('<body></body>');
  let context = { id: ID, name: '我的项目', key: 'account-a:1:workspace-a' };
  const calls = [], downloads = [], errors = [], states = [], created = [], revoked = [], httpErrors = [];
  const timers = new Map(); let timerId = 0;
  dom.window.HTMLAnchorElement.prototype.click = function () { downloads.push({ href: this.href, filename: this.download, attached: this.isConnected }); };
  const controller = new ArchiveDownload({
    getContext: () => context, document: dom.window.document,
    fetch: async (...args) => { calls.push(args); return handler(...args); },
    onState: () => states.push(controller.busy), onError: (message) => errors.push(message),
    onHttpError: (status, message) => { httpErrors.push({ status, message }); return status === 401 || status === 423; },
    urlApi: { createObjectURL(blob) { created.push(blob); return `blob:download-${created.length}`; }, revokeObjectURL(url) { revoked.push(url); } },
    setTimer(callback) { timers.set(++timerId, callback); return timerId; }, clearTimer(id) { timers.delete(id); },
  });
  return { controller, calls, downloads, errors, states, created, revoked, httpErrors, timers,
    setContext(next) { context = next; }, close() { controller.cancel(); dom.window.close(); } };
}

test('whole workspace download uses authenticated GET and a UTF-8 attachment filename, then releases its URL', async () => {
  const c = setup(async () => zipResponse({ 'Content-Disposition': "attachment; filename=workspace.zip; filename*=UTF-8''%E9%A1%B9%E7%9B%AE.zip" }));
  assert.equal(await c.controller.start(), true);
  assert.equal(c.calls[0][0], `/api/workspaces/${ID}/archive`);
  assert.equal(c.calls[0][1].method, 'GET');
  assert.equal(c.calls[0][1].credentials, 'same-origin');
  assert.equal(c.calls[0][1].cache, 'no-store');
  assert.deepEqual(c.downloads, [{ href: 'blob:download-1', filename: '项目.zip', attached: true }]);
  assert.deepEqual(c.states, [true, false]);
  assert.equal(c.created[0].size, 22); // A legitimate empty workspace ZIP is valid.
  for (const timer of [...c.timers.values()]) timer();
  assert.deepEqual(c.revoked, ['blob:download-1']);
  assert.equal(c.timers.size, 0);
  c.close();
});

test('default browser fetch and timers retain their Window receiver and URL methods retain their URL receiver', async (t) => {
  const dom = new JSDOM('<body></body>');
  const events = [], errors = [];
  dom.window.HTMLAnchorElement.prototype.click = () => events.push('download');
  t.mock.method(globalThis, 'fetch', function () {
    if (this !== globalThis) throw new TypeError('Illegal invocation');
    events.push('fetch');
    return Promise.resolve(zipResponse());
  });
  t.mock.method(globalThis, 'setTimeout', function (callback, delay) {
    if (this !== globalThis) throw new TypeError('Illegal invocation');
    assert.equal(typeof callback, 'function');
    assert.equal(delay, 30_000);
    events.push('setTimer');
    return 123;
  });
  t.mock.method(globalThis, 'clearTimeout', function (timer) {
    if (this !== globalThis) throw new TypeError('Illegal invocation');
    assert.equal(timer, 123);
    events.push('clearTimer');
  });
  const urlApi = {
    createObjectURL() { assert.equal(this, urlApi); events.push('createURL'); return 'blob:receiver-test'; },
    revokeObjectURL(url) { assert.equal(this, urlApi); assert.equal(url, 'blob:receiver-test'); events.push('revokeURL'); },
  };
  const controller = new ArchiveDownload({ getContext: () => ({ id: ID, name: 'project', key: 'same-session' }),
    document: dom.window.document, urlApi, onError: (message) => errors.push(message) });
  try {
    assert.equal(await controller.start(), true);
    controller.cancel();
    assert.deepEqual(errors, []);
    assert.deepEqual(events, ['fetch', 'createURL', 'download', 'setTimer', 'clearTimer', 'revokeURL']);
    assert.equal(controller.urls.size, 0);
  } finally {
    t.mock.restoreAll();
    controller.cancel();
    dom.window.close();
  }
});

test('double clicks cannot start duplicate exports while the first request is pending', async () => {
  const gate = deferred(); const c = setup(() => gate.promise);
  const pending = c.controller.start();
  assert.equal(c.controller.busy, true);
  assert.equal(await c.controller.start(), false);
  assert.equal(c.calls.length, 1);
  gate.resolve(zipResponse()); await pending;
  assert.equal(c.downloads.length, 1);
  c.close();
});

test('workspace switch or logout aborts and ignores a response that arrives after cancellation', async () => {
  const gate = deferred(); const c = setup(() => gate.promise);
  const pending = c.controller.start();
  c.controller.cancel();
  assert.equal(c.calls[0][1].signal.aborted, true);
  c.setContext({ id: 'b'.repeat(32), key: 'another-account:2:workspace-b' });
  gate.resolve(zipResponse());
  assert.equal(await pending, false);
  assert.equal(c.downloads.length, 0);
  assert.equal(c.errors.length, 0);
  assert.equal(c.controller.busy, false);
  c.close();
});

test('lock while receiving the ZIP discards the blob and never creates a download URL', async () => {
  const gate = deferred();
  const c = setup(async () => ({ ok: true, status: 200, headers: new Headers({ 'Content-Type': 'application/zip' }), blob: () => gate.promise }));
  const pending = c.controller.start(); await flush();
  c.controller.cancel(ID); c.setContext(null);
  gate.resolve(new Blob([zipBytes()])); await pending;
  assert.equal(c.created.length, 0);
  assert.equal(c.downloads.length, 0);
  assert.equal(c.errors.length, 0);
  assert.equal(await c.controller.start(), false);
  assert.equal(c.calls.length, 1);
  c.close();
});

test('an account generation change blocks late ZIP responses even without a cancellation callback', async () => {
  const gate = deferred(); const c = setup(() => gate.promise);
  const pending = c.controller.start();
  c.setContext({ id: ID, name: 'other', key: 'account-b:2:workspace-a' });
  gate.resolve(zipResponse()); await pending;
  assert.equal(c.created.length, 0);
  assert.equal(c.errors.length, 0);
  c.close();
});

test('a cancelled export cannot clear the loading state of a newer workspace export', async () => {
  const first = deferred(), second = deferred(); let count = 0;
  const c = setup(() => ++count === 1 ? first.promise : second.promise);
  const old = c.controller.start();
  c.controller.cancel();
  c.setContext({ id: 'b'.repeat(32), name: '第二个项目', key: 'account-a:1:workspace-b' });
  const current = c.controller.start();
  first.resolve(zipResponse()); await old;
  assert.equal(c.controller.busy, true);
  assert.equal(c.downloads.length, 0);
  second.resolve(zipResponse()); await current;
  assert.equal(c.downloads.length, 1);
  assert.equal(c.downloads[0].filename, '第二个项目.zip');
  c.close();
});

test('HTTP failures are displayed as bounded plain text and expired or locked sessions are delegated', async () => {
  for (const status of [401, 423, 429, 500]) {
    const c = setup(async () => new Response(JSON.stringify({ detail: '<img src=x onerror=alert(1)> 暂时无法下载' }), { status, headers: { 'Content-Type': 'application/json' } }));
    assert.equal(await c.controller.start(), false);
    assert.equal(c.created.length, 0);
    assert.equal(c.httpErrors[0].status, status);
    assert.equal(c.errors.length, status === 401 || status === 423 ? 0 : 1);
    assert.equal(c.controller.busy, false);
    c.close();
  }
});

test('a non-JSON size error explains all archive limits instead of saving an error response', async () => {
  const c = setup(async () => new Response('<h1>too large</h1>', { status: 413 }));
  await c.controller.start();
  assert.match(c.errors[0], /256 MiB.*512 MiB.*50,000/);
  assert.doesNotMatch(c.errors[0], /<h1>/);
  assert.equal(c.created.length, 0);
  c.close();
});

test('invalid, empty, truncated and oversized archive responses never produce a download', async () => {
  const invalid = new Uint8Array(22);
  const responses = [
    new Response('<html>login</html>', { headers: { 'Content-Type': 'text/html' } }),
    new Response('', { headers: { 'Content-Type': 'application/zip' } }),
    new Response(invalid, { headers: { 'Content-Type': 'application/zip' } }),
    zipResponse({ 'Content-Length': '30' }),
    zipResponse({ 'Content-Length': String(MAX_ARCHIVE_BYTES + 1) }),
    { ok: true, headers: new Headers({ 'Content-Type': 'application/zip' }), blob: async () => ({ size: MAX_ARCHIVE_BYTES + 1 }) },
  ];
  for (const response of responses) {
    const c = setup(async () => response);
    assert.equal(await c.controller.start(), false);
    assert.equal(c.created.length, 0);
    assert.equal(c.errors.length, 1);
    assert.equal(c.calls[0][1].signal.aborted, true);
    c.close();
  }
});

test('cancelling after download releases object URLs before another account can use them', async () => {
  const c = setup(); await c.controller.start();
  c.controller.cancel(ID);
  assert.deepEqual(c.revoked, ['blob:download-1']);
  assert.equal(c.timers.size, 0);
  c.close();
});

test('attachment filenames preserve readable UTF-8 names and remove path or control syntax', () => {
  assert.equal(archiveFilename("attachment; filename*=UTF-8''%E9%A1%B9%E7%9B%AE.zip"), '项目.zip');
  assert.equal(archiveFilename('attachment; filename="../../bad\\file.zip"'), '_.._bad_file.zip');
  assert.equal(archiveFilename("attachment; filename*=UTF-8''bad%XX; filename=backup.zip"), 'backup.zip');
  assert.equal(archiveFilename('', '自己的项目'), '自己的项目.zip');
});
