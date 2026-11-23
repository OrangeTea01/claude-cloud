import assert from 'node:assert/strict';
import test from 'node:test';
import { JSDOM } from 'jsdom';
import { BilingualChat } from '../src/chat.js';
import { createSoloWorkspaceForm } from '../src/workspace-create.js';

function page() {
  const dom = new JSDOM('<div id="host"></div>');
  dom.window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  dom.window.HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new dom.window.Event('close')); };
  return dom;
}

test('creation returns explicit chat mode without a workspace password', async () => {
  const dom = page(), document = dom.window.document;
  const result = createSoloWorkspaceForm(document);
  document.querySelector('#workspace-create-name').value = ' 阅读与写作 ';
  document.querySelector('[value="chat"]').checked = true;
  document.querySelector('form').dispatchEvent(new dom.window.Event('submit', { cancelable: true }));
  assert.deepEqual(await result, { name: '阅读与写作', mode: 'chat' });
  assert.equal(document.querySelector('dialog'), null);
});

test('creation cancellation returns no payload and old behavior defaults to code', async () => {
  const dom = page(), document = dom.window.document;
  const result = createSoloWorkspaceForm(document);
  assert.equal(document.querySelector('[name="mode"]:checked').value, 'code');
  document.querySelector('[data-dismiss]').click();
  assert.equal(await result, null);
});

test('chat mode presents correct attachment limits and keeps send body compatible', async () => {
  const dom = page(), document = dom.window.document;
  const workspace = { id: 'chat-workspace', mode: 'chat', running: true };
  const calls = [], help = [];
  const model = { epoch: 'one', revision: 1, mode: 'chat', busy: false, phase: 'idle', selectedModel: 'sonnet', modelOptions: [{ value: 'sonnet', label: 'Sonnet' }], permissions: [], messages: [] };
  const chat = new BilingualChat({ api: async (...args) => { calls.push(args); return { ...model, revision: 2 }; }, toast: () => {}, modal: async (value) => { help.push(value); }, selected: () => workspace, isAuthenticated: () => true, isBusy: () => false, onState: () => {}, onStopped: () => {} });
  chat.poll = () => {};
  chat.mount(document.querySelector('#host'), workspace.id);
  chat.accept(model, workspace.id, chat.generation);
  assert.match(document.querySelector('.chat-header h2').textContent, /普通聊天/);
  assert.match(document.querySelector('.chat-upload-hint').textContent, /256 KiB/);
  assert.match(document.querySelector('.chat-footnote').textContent, /不执行工具/);
  document.querySelector('#chat-input').value = '帮我润色这段话。';
  document.querySelector('#chat-input').dispatchEvent(new dom.window.Event('input'));
  await chat.send();
  assert.deepEqual(calls[0], ['/api/workspaces/chat-workspace/chat/message', { method: 'POST', body: { text: '帮我润色这段话。' } }]);
  await chat.help();
  assert.match(help[0].text, /Claude Code/);
  assert.match(help[0].text, /PDF、Office 和 ZIP/);
  chat.unmount();
});
