import assert from 'node:assert/strict';
import test from 'node:test';
import { JSDOM } from 'jsdom';
import { BilingualChat } from '../src/chat.js';
import { MAX_UPLOAD_BYTES, rasterType, safeAttachmentPath } from '../src/attachments.js';

const pathFor = (name) => `uploads/${'a'.repeat(32)}/${name}`;
const png = Uint8Array.from([137, 80, 78, 71, 13, 10, 26, 10, 0, 255]);
const snapshot = (extra = {}) => ({ epoch: 'attachments', revision: 1, phase: 'idle', busy: false,
  selectedModel: 'sonnet', modelOptions: [{ value: 'sonnet', label: 'Sonnet' }], messages: [], permissions: [], ...extra });
const file = (name, bytes = new TextEncoder().encode('测试内容'), type = '') => ({ name, size: bytes.length, type, arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) });
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }
const flush = () => new Promise((resolve) => setImmediate(resolve));
function setup(respond = async (url, options) => url.endsWith('/attachments')
  ? { path: pathFor(options.body.name), name: options.body.name, size: atob(options.body.content).length, mediaType: 'application/octet-stream' }
  : snapshot({ revision: 2 })) {
  const dom = new JSDOM('<div id="host"></div>');
  const context = { dom, workspace: { id: 'one', running: true }, calls: [], notices: [], authenticated: true };
  const chat = context.chat = new BilingualChat({ api: (...args) => { context.calls.push(args); return respond(...args); }, toast: (...args) => context.notices.push(args), modal: async () => false,
    selected: () => context.workspace, isAuthenticated: () => context.authenticated, isBusy: () => false, onState: () => {}, onStopped: async () => {} });
  chat.poll = () => {};
  context.node = (id) => dom.window.document.getElementById(id);
  context.edit = (text) => { context.node('chat-input').value = text; context.node('chat-input').dispatchEvent(new dom.window.Event('input')); };
  context.mount = (id = context.workspace.id) => { context.workspace.id = id; chat.mount(context.node('host'), id); chat.accept(snapshot(), id, chat.generation); };
  context.mount(); return context;
}

test('binary and Chinese filenames upload exactly, then attachment-only messages send references', async () => {
  const c = setup();
  await c.chat.attachments.add([file('设计图.png', png, 'image/png')]);
  assert.equal(c.calls[0][0], '/api/workspaces/one/attachments');
  assert.deepEqual(Buffer.from(c.calls[0][1].body.content, 'base64'), Buffer.from(png));
  assert.equal(c.calls[0][1].body.name, '设计图.png');
  assert.equal(c.node('chat-send').disabled, false);
  assert.match(c.node('chat-attachments').querySelector('img').src, /^blob:/);
  await c.chat.send();
  assert.deepEqual(c.calls[1][1].body, { text: '', attachments: [{ path: pathFor('设计图.png'), name: '设计图.png' }] });
  assert.equal(c.chat.attachments.list().length, 0);
  assert.equal(c.node('chat-attachments').hidden, true);
  c.chat.unmount({ forgetDrafts: true });
});

test('uploads run sequentially and pending or failed files block sending until retry succeeds', async () => {
  const first = deferred(); let attempt = 0;
  const c = setup(async (url, options) => {
    if (url.endsWith('/attachments')) {
      attempt++;
      if (attempt === 1) return first.promise;
      if (attempt === 2) throw new Error('连接中断');
      return { path: pathFor(options.body.name), name: options.body.name, size: 3 };
    }
    return snapshot({ revision: 2 });
  });
  const pending = c.chat.attachments.add([file('one.txt'), file('two.txt')]);
  await flush();
  assert.equal(c.calls.length, 1);
  assert.equal(c.node('chat-send').disabled, true);
  await c.chat.send(); assert.equal(c.calls.length, 1);
  first.resolve({ path: pathFor('one.txt'), name: 'one.txt', size: 3 }); await pending;
  assert.equal(c.chat.attachments.list()[1].status, 'failed');
  assert.match(c.node('chat-attachments').textContent, /连接中断/);
  assert.equal(c.node('chat-send').disabled, true);
  await c.chat.attachments.retry(c.chat.attachments.list()[1].id);
  assert.equal(c.node('chat-send').disabled, false);
  c.chat.unmount({ forgetDrafts: true });
});

test('oversized files and path names are rejected before reading; five attachment limit is enforced', async () => {
  const c = setup(); let read = false;
  await c.chat.attachments.add([{ name: 'large.bin', size: MAX_UPLOAD_BYTES + 1, arrayBuffer: async () => { read = true; } }, file('../escape.txt')]);
  assert.equal(read, false); assert.equal(c.calls.length, 0);
  await c.chat.attachments.add(Array.from({ length: 7 }, (_, i) => file(`file${i}.txt`)));
  assert.equal(c.chat.attachments.list().length, 5);
  assert.equal(c.calls.length, 5);
  assert.match(c.notices.map((value) => value[0]).join(' '), /10 MiB.*文件名.*最多添加 5/);
  c.chat.unmount({ forgetDrafts: true });
});

test('failed sends preserve exact text and attachments, removal never deletes stored files', async () => {
  const c = setup(async (url, options) => {
    if (url.endsWith('/attachments')) return { path: pathFor(options.body.name), name: options.body.name, size: 1 };
    throw new Error('暂时不能发送');
  });
  await c.chat.attachments.add([file('note.txt')]);
  c.edit('  请查看\n'); await c.chat.send();
  assert.equal(c.node('chat-input').value, '  请查看\n');
  assert.equal(c.chat.attachments.list().length, 1);
  assert.equal(c.node('chat-send').disabled, false);
  c.chat.attachments.remove(c.chat.attachments.list()[0].id);
  assert.equal(c.chat.attachments.list().length, 0);
  assert.equal(c.calls.length, 2);
  assert.equal(c.calls.some((call) => call[1]?.method === 'DELETE'), false);
});

test('late upload stays in its original workspace and cannot enter another conversation', async () => {
  const result = deferred(); const c = setup(() => result.promise);
  const pending = c.chat.attachments.add([file('private.png', png)]); await flush();
  c.mount('two');
  assert.equal(c.chat.attachments.list().length, 0);
  result.resolve({ path: pathFor('private.png'), name: 'private.png', size: png.length, mediaType: 'image/png' }); await pending;
  assert.equal(c.node('chat-attachments').hidden, true);
  assert.equal(c.chat.attachments.list('one')[0].status, 'ready');
  c.mount('one'); assert.match(c.node('chat-attachments').textContent, /private.png/);
  c.chat.unmount({ forgetDrafts: true });
});

test('expired login clears attachments even with text preserved and cannot resume old queued uploads', async () => {
  const result = deferred(); const c = setup(() => result.promise);
  c.edit('保留文字草稿');
  const pending = c.chat.attachments.add([file('first.txt'), file('second.txt')]); await flush();
  c.authenticated = false; c.chat.unmount({ forgetDrafts: false, forgetAttachments: true });
  c.authenticated = true; c.mount();
  result.resolve({ path: pathFor('first.txt'), name: 'first.txt', size: 1 }); await pending;
  assert.equal(c.chat.drafts.get('one'), '保留文字草稿');
  assert.equal(c.chat.attachments.list().length, 0);
  assert.equal(c.calls.length, 1, 'queued second file never resumes after relogin');
});

test('successful late send clears only submitted attachments in the original workspace', async () => {
  const result = deferred(); const c = setup(async (url, options) => url.endsWith('/attachments') ? { path: pathFor(options.body.name), name: options.body.name, size: 1 } : result.promise);
  await c.chat.attachments.add([file('first.txt')]);
  const pending = c.chat.send(); c.mount('two');
  await c.chat.attachments.add([file('second.txt')]);
  result.resolve(snapshot({ revision: 2 })); await pending;
  assert.equal(c.chat.attachments.list('one').length, 0);
  assert.equal(c.chat.attachments.list('two')[0].name, 'second.txt');
  c.chat.unmount({ forgetDrafts: true });
});

test('new login can upload without waiting for an old stalled request', async () => {
  const oldRequest = deferred();
  const c = setup(async (url, options) => options.body.name === 'old.txt' ? oldRequest.promise : { path: pathFor(options.body.name), name: options.body.name, size: 1 });
  const oldPending = c.chat.attachments.add([file('old.txt')]); await flush();
  c.authenticated = false; c.chat.unmount({ forgetAttachments: true });
  c.authenticated = true; c.mount();
  await c.chat.attachments.add([file('new.txt')]);
  assert.equal(c.chat.attachments.list()[0].status, 'ready');
  oldRequest.resolve({ path: pathFor('old.txt'), name: 'old.txt', size: 1 }); await oldPending;
  assert.deepEqual(c.chat.attachments.list().map((item) => item.name), ['new.txt']);
  c.chat.unmount({ forgetDrafts: true });
});

test('browser FileReader fallback preserves real File bytes', async () => {
  const c = setup();
  const browserFile = new c.dom.window.File([png], '截图.png', { type: 'image/png' });
  await c.chat.attachments.add([browserFile]);
  assert.deepEqual(Buffer.from(c.calls[0][1].body.content, 'base64'), Buffer.from(png));
  assert.equal(c.chat.attachments.list()[0].status, 'ready');
  c.chat.unmount({ forgetDrafts: true });
});

test('removed uploads ignore late replies and revoke their local image preview', async () => {
  const result = deferred(); const c = setup(() => result.promise);
  const pending = c.chat.attachments.add([file('image.png', png)]); await flush();
  const item = c.chat.attachments.list()[0];
  assert.match(item.url, /^blob:/);
  c.chat.attachments.remove(item.id); assert.equal(item.url, '');
  result.resolve({ path: pathFor('image.png'), name: 'image.png', size: 10 }); await pending;
  assert.equal(c.chat.attachments.list().length, 0);
});

test('previews inspect bytes rather than file names or claimed media types', async () => {
  assert.equal(rasterType(new TextEncoder().encode('<svg onload="alert(1)">')), null);
  assert.equal(rasterType(Uint8Array.from([255, 216, 255])), 'image/jpeg');
  assert.equal(safeAttachmentPath('https://evil.example/a.png'), false);
  assert.equal(safeAttachmentPath('uploads/../secret'), false);
  const c = setup(async (url) => url.includes('/file?') ? { encoding: 'utf-8', content: '<svg onload="alert(1)">', size: 23 } : {});
  c.chat.accept(snapshot({ revision: 2, messages: [{ id: 'unsafe', role: 'user', text: '', attachments: [{ path: pathFor('image.png'), name: '<img src=x onerror=alert(1)>.png', size: 23, mediaType: 'image/png' }] }] }), 'one', c.chat.generation);
  assert.equal(c.node('chat-transcript').querySelector('img'), null);
  await c.chat.attachments.open('unsafe', '0', true);
  assert.equal(c.node('chat-transcript').querySelector('img'), null);
  assert.match(c.notices[0][0], /不支持图片预览/);
});

test('history image preview loads only on request and closes without another network request', async () => {
  const c = setup(async () => ({ encoding: 'base64', content: Buffer.from(png).toString('base64'), size: png.length }));
  c.chat.accept(snapshot({ revision: 2, messages: [{ id: 'picture', role: 'user', text: '', attachments: [{ path: pathFor('photo.png'), name: 'photo.png', size: png.length, mediaType: 'image/png' }] }] }), 'one', c.chat.generation);
  assert.equal(c.calls.length, 0);
  await c.chat.attachments.open('picture', '0', true);
  assert.match(c.node('chat-transcript').querySelector('img').src, /^blob:/);
  assert.equal(c.calls.length, 1);
  await c.chat.attachments.open('picture', '0', true);
  assert.equal(c.node('chat-transcript').querySelector('img'), null);
  assert.equal(c.calls.length, 1);
});

test('image paste uploads files while normal text paste remains untouched; drop prevents navigation', async () => {
  const c = setup();
  const plain = new c.dom.window.Event('paste', { cancelable: true });
  Object.defineProperty(plain, 'clipboardData', { value: { items: [{ kind: 'string', type: 'text/plain' }] } });
  c.node('chat-input').dispatchEvent(plain); assert.equal(plain.defaultPrevented, false);
  const image = new c.dom.window.Event('paste', { cancelable: true });
  Object.defineProperty(image, 'clipboardData', { value: { items: [{ kind: 'file', type: 'image/png', getAsFile: () => file('screenshot.png', png) }] } });
  c.node('chat-input').dispatchEvent(image); assert.equal(image.defaultPrevented, true);
  await c.chat.attachments.queue;
  const drop = new c.dom.window.Event('drop', { cancelable: true });
  Object.defineProperty(drop, 'dataTransfer', { value: { files: [file('notes.txt')] } });
  c.node('chat-form').dispatchEvent(drop); assert.equal(drop.defaultPrevented, true);
  await c.chat.attachments.queue;
  assert.equal(c.chat.attachments.list().length, 2);
  c.chat.unmount({ forgetDrafts: true });
});
