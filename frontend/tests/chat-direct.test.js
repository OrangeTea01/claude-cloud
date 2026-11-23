import assert from 'node:assert/strict';
import test from 'node:test';
import { JSDOM } from 'jsdom';
import { BilingualChat } from '../src/chat.js';

const state = (extra = {}) => ({ epoch: 'direct-chat', revision: 1, phase: 'idle', busy: false,
  selectedModel: 'sonnet', modelOptions: [{ value: 'sonnet', label: 'Sonnet' }], messages: [], permissions: [], ...extra });
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function setup(respond = async () => state({ revision: 2 })) {
  const dom = new JSDOM('<div id="host"></div>');
  const context = { dom, workspace: { id: 'one', running: true }, calls: [], notices: [] };
  const chat = context.chat = new BilingualChat({
    api: (...args) => { context.calls.push(args); return respond(...args); },
    toast: (...args) => context.notices.push(args), modal: async () => false,
    selected: () => context.workspace, isAuthenticated: () => true, isBusy: () => false,
    onState: () => {}, onStopped: async () => {},
  });
  context.poll = chat.poll.bind(chat);
  chat.poll = () => {};
  context.mount = (id = context.workspace.id) => {
    context.workspace.id = id;
    chat.mount(dom.window.document.querySelector('#host'), id);
    chat.accept(state(), id, chat.generation);
  };
  context.node = (id) => dom.window.document.getElementById(id);
  context.edit = (value) => { context.node('chat-input').value = value; context.node('chat-input').dispatchEvent(new dom.window.Event('input')); };
  context.mount();
  return context;
}

test('one click sends exact original Chinese and code without any translation request', async () => {
  const c = setup();
  const original = '  不要删除文件。\n\n```python\n    print("你好")\n```\n  ';
  c.edit(original);
  assert.equal(c.node('chat-send').textContent, '发送');
  assert.equal(c.node('chat-send').disabled, false, 'no translator status is needed');
  await c.chat.send();
  assert.deepEqual(c.calls, [['/api/workspaces/one/chat/message', { method: 'POST', body: { text: original } }]]);
  assert.equal(c.node('chat-input').value, '');
  assert.equal(c.chat.drafts.has('one'), false);
  assert.equal(c.dom.window.document.querySelector('dialog'), null);
  assert.equal(c.node('chat-review-before-send'), null);
  assert.equal(c.node('translation-status'), null);
  assert.doesNotMatch(c.dom.window.document.body.textContent, /翻译|回译/);
});

test('whitespace-only and oversized raw input never reach Claude', async () => {
  const c = setup();
  for (const value of [' \n\t ', ' '.repeat(16000) + '中']) {
    c.edit(value); await c.chat.send();
    assert.equal(c.node('chat-send').disabled, true);
  }
  assert.deepEqual(c.calls, []);
});

test('duplicate send is blocked and editing the next draft while waiting is safe', async () => {
  const result = deferred(); const c = setup(() => result.promise);
  c.edit('第一条'); const pending = c.chat.send();
  await c.chat.send(); c.edit('下一条');
  assert.equal(c.node('chat-send').disabled, true);
  result.resolve(state({ revision: 2, busy: true, phase: 'thinking' })); await pending;
  assert.equal(c.calls.length, 1);
  assert.equal(c.node('chat-input').value, '下一条');
  assert.equal(c.chat.drafts.get('one'), '下一条');
  assert.equal(c.node('chat-send').disabled, true, 'server task still blocks another submission');
});

test('a failed send preserves the exact draft and permits an explicit retry', async () => {
  const c = setup(async () => { throw new Error('服务暂时不可用'); });
  c.edit('  保留这份草稿\n'); await c.chat.send();
  assert.equal(c.node('chat-input').value, '  保留这份草稿\n');
  assert.equal(c.chat.drafts.get('one'), '  保留这份草稿\n');
  assert.equal(c.node('chat-send').disabled, false);
  assert.equal(c.notices[0][0], '服务暂时不可用');
});

test('editing away and back does not erase a deliberately revised draft on acknowledgement', async () => {
  const result = deferred(); const c = setup(() => result.promise);
  c.edit('同一段文字'); const pending = c.chat.send();
  c.edit('修改中'); c.edit('同一段文字');
  result.resolve(state({ revision: 2 })); await pending;
  assert.equal(c.node('chat-input').value, '同一段文字');
  assert.equal(c.chat.drafts.get('one'), '同一段文字');
});

test('late workspace responses cannot overwrite the new conversation or release its pending send', async () => {
  const one = deferred(), two = deferred();
  const c = setup((url) => url.includes('/one/') ? one.promise : two.promise);
  c.edit('项目一'); const first = c.chat.send();
  c.mount('two'); c.edit('项目二'); const second = c.chat.send();
  one.resolve(state({ revision: 99, error: 'old response' })); await first;
  assert.equal(c.chat.model.revision, 1);
  assert.equal(c.chat.actions.has('send'), true);
  assert.equal(c.node('chat-input').value, '项目二');
  two.resolve(state({ revision: 2 })); await second;
  assert.equal(c.node('chat-input').value, '');
  assert.equal(c.chat.actions.has('send'), false);
});

test('refresh and polling fetch only chat state', async () => {
  const c = setup();
  await c.chat.refreshNow();
  const previousDocument = globalThis.document;
  globalThis.document = { visibilityState: 'visible' };
  try { await c.poll(c.chat.generation); }
  finally { c.chat.unmount(); globalThis.document = previousDocument; }
  assert.deepEqual(c.calls, [['/api/workspaces/one/chat'], ['/api/workspaces/one/chat']]);
});

test('new replies display directly while translated and untranslated historic records remain readable', () => {
  const c = setup();
  c.chat.accept(state({ revision: 2, messages: [
    { id: 'direct', role: 'assistant', text: '## 原样中文回复', original: '', translationStatus: 'not_needed' },
    { id: 'old-good', role: 'assistant', text: '旧版中文内容', original: 'Historic English response', translationStatus: 'done' },
    { id: 'old-fail', role: 'assistant', text: '', original: 'Previously untranslated response', translationStatus: 'failed' },
    { id: 'old-unsent', role: 'user', text: '没有发出的旧消息', original: '', translationStatus: 'failed', sent: false },
  ] }), 'one', c.chat.generation);
  const transcript = c.node('chat-transcript');
  assert.match(transcript.textContent, /原样中文回复/);
  assert.match(transcript.textContent, /旧版中文内容/);
  assert.match(transcript.textContent, /Previously untranslated response/);
  assert.match(transcript.textContent, /历史原文/);
  assert.match(transcript.textContent, /未发送/);
  assert.equal(transcript.querySelectorAll('details').length, 1);
  assert.equal(c.node('chat-messages').querySelector('button'), null, 'no obsolete translate retry action in message history');
  assert.doesNotMatch(transcript.textContent, /翻译|回译/);
});

test('stopped workspaces, blocked models and unapplied policy still block direct submission', async () => {
  const c = setup(); c.edit('请开始');
  for (const blocked of [{ modelBlocked: true }, { policySyncPending: true }, { selectedModel: 'fable', fableAllowed: false }]) {
    c.chat.accept(state({ revision: c.chat.model.revision + 1, ...blocked }), 'one', c.chat.generation);
    await c.chat.send();
    assert.equal(c.node('chat-send').disabled, true);
  }
  c.workspace.running = false;
  c.chat.accept(state({ revision: c.chat.model.revision + 1 }), 'one', c.chat.generation);
  await c.chat.send();
  assert.deepEqual(c.calls, []);
});

test('logout clears pending drafts without accepting late responses', async () => {
  const result = deferred(); const c = setup(() => result.promise);
  c.edit('私有草稿'); const pending = c.chat.send();
  c.chat.unmount({ forgetDrafts: true });
  result.resolve(state({ revision: 2 })); await pending;
  assert.equal(c.chat.host, null);
  assert.equal(c.chat.model, null);
  assert.equal(c.chat.drafts.size, 0);
});
