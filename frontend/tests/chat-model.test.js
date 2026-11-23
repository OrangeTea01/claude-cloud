import assert from 'node:assert/strict';
import test from 'node:test';
import { BilingualChat } from '../src/chat.js';

// Small DOM stand-in for the chat controller; rendering/layout is checked in
// the browser. Keep listeners and input values so tests cover user action state.
class Element {
  constructor() {
    this.listeners = new Map();
    this.dataset = {};
    this.value = '';
    this.textContent = '';
    this.hidden = false;
    this.disabled = false;
    this.scrollHeight = 200;
    this.clientHeight = 100;
    this.scrollTop = 100;
  }
  addEventListener(name, fn) { this.listeners.set(name, fn); }
  removeEventListener(name) { this.listeners.delete(name); }
  querySelectorAll() { return []; }
}

class Host extends Element {
  constructor() { super(); this.isConnected = true; }
  set innerHTML(html) {
    this.html = html;
    this.elements = new Map();
    for (const match of html.matchAll(/<[^>]*\bid="([^"]+)"[^>]*>/g)) {
      const element = new Element();
      element.disabled = /\bdisabled\b/.test(match[0]);
      this.elements.set(`#${match[1]}`, element);
    }
  }
  get innerHTML() { return this.html; }
  querySelector(selector) { return this.elements.get(selector); }
}

const options = [
  { value: 'default', label: '默认' }, { value: 'sonnet', label: 'Sonnet' },
  { value: 'opus', label: 'Opus' }, { value: 'haiku', label: 'Haiku' },
];

const snapshot = (extra = {}) => ({
  epoch: 'epoch-1', revision: 1, phase: 'idle', busy: false, active: true,
  selectedModel: 'default', activeModel: 'claude-sonnet-example', modelOptions: options,
  messages: [{ id: '1', role: 'user', text: '保留历史', original: 'Keep history', sent: true }],
  permissions: [], ...extra,
});

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function setup(api = async () => snapshot()) {
  const context = { workspace: { id: 'work one', running: true, terminalActive: false }, busy: false, calls: [], toasts: [] };
  const chat = new BilingualChat({
    api: (...args) => { context.calls.push(args); return api(...args); },
    toast: (...args) => context.toasts.push(args), modal: async () => false,
    selected: () => context.workspace, isAuthenticated: () => true,
    isBusy: () => context.busy, onState: () => {}, onStopped: async () => {},
  });
  chat.poll = () => {}; // Explicit responses below stand in for the network poll.
  context.chat = chat;
  context.mount = (id = context.workspace.id) => {
    context.workspace.id = id;
    context.host = new Host();
    chat.mount(context.host, id);
  };
  context.node = (id) => context.host.querySelector(`#${id}`);
  context.accept = (value) => chat.accept(value, chat.workspaceId, chat.generation);
  context.mount();
  context.accept(snapshot());
  context.node('chat-input').value = '还没发送的草稿';
  context.node('chat-input').listeners.get('input')();
  return context;
}

test('selecting a model waits for acknowledgement without sending a message or losing draft/history', async () => {
  const response = deferred();
  const c = setup(() => response.promise);
  const transcript = c.node('chat-transcript').innerHTML;
  const select = c.node('chat-model');
  select.value = 'opus';
  const request = select.listeners.get('change')({ target: select });
  assert.deepEqual(c.calls, [['/api/workspaces/work%20one/chat/model', { method: 'PUT', body: { model: 'opus' } }]]);
  assert.equal(select.value, 'default', 'pending selection is not claimed as applied');
  assert.equal(select.disabled, true);
  assert.equal(c.node('chat-send').disabled, true);
  assert.equal(c.node('chat-active-model').hidden, true);
  assert.match(c.node('chat-model-feedback').textContent, /正在切换为 Opus/);
  assert.deepEqual(c.toasts, [], 'do not claim success before server acknowledgement');
  await c.chat.changeModel('haiku');
  await c.chat.send();
  assert.equal(c.calls.length, 1, 'double changes and sends are locked while waiting');
  response.resolve(snapshot({ revision: 3, selectedModel: 'opus', activeModel: null }));
  await request;
  assert.equal(select.value, 'opus');
  assert.equal(select.disabled, false);
  assert.equal(c.node('chat-input').value, '还没发送的草稿');
  assert.equal(c.chat.drafts.get('work one'), '还没发送的草稿');
  assert.equal(c.node('chat-transcript').innerHTML, transcript);
  assert.match(c.node('chat-model-feedback').textContent, /已选择 Opus/);
  assert.deepEqual(c.toasts, [['已选择 Opus，用于后续消息。']]);
  assert.equal(c.node('chat-active-model').hidden, true, 'do not invent the resolved model alias');
  c.accept(snapshot({ revision: 4, selectedModel: 'opus', activeModel: 'claude-opus-example' }));
  assert.equal(c.node('chat-active-model').textContent, '当前运行：claude-opus-example');
  c.chat.controls();
  assert.equal(c.toasts.length, 1, 'polls and control refreshes do not repeat the success notice');
});

test('a rejected model keeps the accepted selection, explains the error, and permits retry', async () => {
  let reject = true;
  const c = setup(async () => {
    if (reject) throw new Error('账号暂不可使用此模型');
    return snapshot({ revision: 2, selectedModel: 'haiku', activeModel: null });
  });
  c.node('chat-model').value = 'opus';
  await c.chat.changeModel('opus');
  assert.equal(c.node('chat-model').value, 'default');
  assert.equal(c.node('chat-model').disabled, false);
  assert.equal(c.node('chat-input').value, '还没发送的草稿');
  assert.match(c.node('chat-model-feedback').textContent, /未能切换模型.*账号暂不可使用/);
  assert.equal(c.node('chat-model-feedback').dataset.error, 'true');
  assert.deepEqual(c.toasts, [['未能切换模型：账号暂不可使用此模型', true]]);
  reject = false;
  await c.chat.changeModel('haiku');
  assert.equal(c.node('chat-model').value, 'haiku');
  assert.equal(c.node('chat-model-feedback').dataset.error, 'false');
  assert.doesNotMatch(c.node('chat-model-feedback').textContent, /未能/);
  assert.deepEqual(c.toasts[1], ['已选择 Haiku，用于后续消息。']);
});

test('stopped workspaces can save a model preference without starting Claude', async () => {
  const c = setup(async () => snapshot({ revision: 2, active: false, selectedModel: 'sonnet', activeModel: null }));
  c.workspace.running = false;
  c.accept(snapshot({ active: false, activeModel: 'old-model' }));
  assert.equal(c.node('chat-model').disabled, false);
  assert.equal(c.node('chat-active-model').hidden, true);
  await c.chat.changeModel('sonnet');
  assert.equal(c.calls.length, 1);
  assert.match(c.calls[0][0], /\/chat\/model$/);
  assert.match(c.node('chat-model-feedback').textContent, /下次启动会话时使用/);
  assert.deepEqual(c.toasts, [['已保存 Sonnet，下次启动会话时使用。']]);
  assert.equal(c.node('chat-send').disabled, true);
});

test('busy, terminal, loading, failure and external actions block model changes', async () => {
  for (const block of [
    (c) => { c.chat.model = null; },
    (c) => { c.chat.model.busy = true; },
    (c) => { c.workspace.terminalActive = true; },
    (c) => { c.busy = true; },
    (c) => { c.chat.failure = '连接失败'; },
    ...['send', 'stop', 'model', 'permission:1', 'retry:1'].map((key) => (c) => c.chat.actions.add(key)),
  ]) {
    const c = setup();
    block(c);
    c.chat.controls();
    assert.equal(c.node('chat-model').disabled, true);
    await c.chat.changeModel('opus');
    assert.equal(c.calls.length, 0);
  }
  const c = setup();
  await c.chat.changeModel('default');
  await c.chat.changeModel('not-an-option');
  assert.equal(c.calls.length, 0, 'unchanged or unlisted values do not make requests');
});

test('the default option is sent as default and a server switching phase blocks sends', async () => {
  const c = setup(async () => snapshot({ revision: 4, activeModel: null }));
  c.accept(snapshot({ revision: 2, selectedModel: 'opus' }));
  await c.chat.changeModel('default');
  assert.deepEqual(c.calls[0][1].body, { model: 'default' });
  c.accept(snapshot({ revision: 5, phase: 'switching_model', busy: true }));
  assert.equal(c.node('chat-model').disabled, true);
  assert.equal(c.node('chat-send').disabled, true);
  assert.match(c.node('chat-phase').textContent, /切换模型/);
});

test('late responses cannot change a newer workspace or release its model action', async () => {
  const oldResponse = deferred(), newResponse = deferred();
  const c = setup((url) => url.includes('work%20one') ? oldResponse.promise : newResponse.promise);
  const oldRequest = c.chat.changeModel('opus');
  c.mount('work two');
  c.accept(snapshot({ epoch: 'epoch-two' }));
  const newRequest = c.chat.changeModel('haiku');
  assert.deepEqual(c.toasts, []);
  oldResponse.resolve(snapshot({ revision: 3, selectedModel: 'opus' }));
  await oldRequest;
  assert.equal(c.chat.workspaceId, 'work two');
  assert.equal(c.node('chat-model').value, 'default');
  assert.equal(c.node('chat-model').disabled, true);
  assert.equal(c.chat.actions.has('model'), true);
  assert.match(c.node('chat-model-feedback').textContent, /Haiku/);
  assert.deepEqual(c.toasts, [], 'an acknowledgement from another workspace must not display a success notice');
  newResponse.resolve(snapshot({ epoch: 'epoch-two', revision: 3, selectedModel: 'haiku', activeModel: null }));
  await newRequest;
  assert.equal(c.node('chat-model').value, 'haiku');
  assert.deepEqual(c.toasts, [['已选择 Haiku，用于后续消息。']]);
});

test('old revisions and old process epochs cannot overwrite the latest model', async () => {
  for (const changeEpoch of [false, true]) {
    const response = deferred();
    const c = setup(() => response.promise);
    const request = c.chat.changeModel('opus');
    const current = snapshot({ epoch: changeEpoch ? 'epoch-new' : 'epoch-1', revision: 5, selectedModel: 'haiku' });
    assert.equal(c.accept(current), true);
    response.resolve(snapshot({ revision: 3, selectedModel: 'opus' }));
    await request;
    assert.equal(c.node('chat-model').value, 'haiku');
    assert.equal(c.chat.model, current);
    assert.doesNotMatch(c.node('chat-model-feedback').textContent, /已选择 Opus/);
    assert.deepEqual(c.toasts, [], 'a stale model revision or process epoch must not claim success');
    assert.equal(c.accept(snapshot({ revision: 2, selectedModel: 'sonnet' })), false);
    assert.equal(c.node('chat-model').value, 'haiku');
  }
});

test('stop confirmation reserves the action so a model cannot change underneath it', async () => {
  const c = setup();
  const decision = deferred();
  let modalCalls = 0;
  c.chat.modal = () => { modalCalls++; return decision.promise; };
  const stopped = c.chat.stop();
  await c.chat.stop();
  await c.chat.changeModel('opus');
  assert.equal(modalCalls, 1);
  assert.equal(c.node('chat-model').disabled, true);
  assert.equal(c.calls.length, 0);
  decision.resolve(false);
  await stopped;
  assert.equal(c.node('chat-model').disabled, false);
});

test('disabled Fable is visible but cannot be selected or used by a saved conversation', async () => {
  const c = setup(async () => snapshot({ revision: 4, selectedModel: 'opus', fableAllowed: false, modelBlocked: false }));
  const disabledOptions = [...options, { value: 'fable', label: 'Fable', disabled: true }];
  c.accept(snapshot({ revision: 2, modelOptions: disabledOptions, fableAllowed: false }));
  assert.match(c.node('chat-model').innerHTML, /value="fable" disabled>Fable（管理员已禁用）/);
  await c.chat.changeModel('fable');
  assert.equal(c.calls.length, 0);
  c.accept(snapshot({ revision: 3, selectedModel: 'fable', modelOptions: disabledOptions, fableAllowed: false, modelBlocked: true, active: false }));
  assert.equal(c.node('chat-model').value, 'fable');
  assert.equal(c.node('chat-model').disabled, false, 'the user can explicitly select another model');
  assert.equal(c.node('chat-send').disabled, true);
  assert.match(c.node('chat-model-feedback').textContent, /管理员已禁止.*请选择其他模型/);
  await c.chat.send();
  assert.equal(c.calls.length, 0);
  await c.chat.changeModel('opus');
  assert.equal(c.node('chat-model').value, 'opus');
  assert.equal(c.node('chat-send').disabled, false);
  assert.equal(c.node('chat-input').value, '还没发送的草稿');
});

test('enabled Fable uses the same acknowledged model-switch flow and a later policy revocation blocks sending', async () => {
  const fableOptions = [...options, { value: 'fable', label: 'Fable', disabled: false }];
  const c = setup(async () => snapshot({ revision: 3, selectedModel: 'fable', modelOptions: fableOptions, fableAllowed: true, activeModel: null }));
  c.accept(snapshot({ revision: 2, modelOptions: fableOptions, fableAllowed: true }));
  await c.chat.changeModel('fable');
  assert.deepEqual(c.calls[0][1].body, { model: 'fable' });
  assert.equal(c.node('chat-model').value, 'fable');
  assert.equal(c.node('chat-send').disabled, false);
  c.accept(snapshot({ revision: 4, selectedModel: 'fable', fableAllowed: false, modelBlocked: true, modelOptions: [...options, { value: 'fable', label: 'Fable', disabled: true }], active: false }));
  assert.equal(c.node('chat-send').disabled, true);
  assert.doesNotMatch(c.node('chat-model-feedback').textContent, /已选择 Fable/);
  assert.equal(c.chat.drafts.get('work one'), '还没发送的草稿');
});

test('pending policy enforcement blocks model changes and sends until the administrator finishes applying it', async () => {
  const c = setup();
  c.accept(snapshot({ revision: 2, policySyncPending: true }));
  assert.equal(c.node('chat-model').disabled, true);
  assert.equal(c.node('chat-send').disabled, true);
  assert.match(c.node('chat-model-feedback').textContent, /权限尚未完整应用/);
  await c.chat.changeModel('opus'); await c.chat.send();
  assert.equal(c.calls.length, 0);
  c.accept(snapshot({ revision: 3, policySyncPending: false }));
  assert.equal(c.node('chat-model').disabled, false);
  assert.equal(c.node('chat-send').disabled, false);
  assert.equal(c.node('chat-input').value, '还没发送的草稿');
});
