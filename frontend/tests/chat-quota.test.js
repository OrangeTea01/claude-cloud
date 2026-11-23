import assert from 'node:assert/strict';
import test from 'node:test';
import { JSDOM } from 'jsdom';
import { BilingualChat } from '../src/chat.js';
import { AttentionAudio } from '../src/attention-audio.js';

const quota = (remaining = 0) => ({ fiveHour: { limit: 100, used: 100 - remaining, remaining, resetAt: null }, weekly: { limit: 1000, used: 100, remaining: 900 }, exhausted: !remaining });
const snapshot = (extra = {}) => ({ epoch: 'epoch1', revision: 1, phase: 'idle', busy: false, selectedModel: 'sonnet', modelOptions: [{ value: 'sonnet', label: 'Sonnet' }], messages: [{ id: 'm', role: 'user', text: '历史消息', sent: true }], permissions: [], ...extra });
function setup(api = async () => snapshot()) {
  const dom = new JSDOM('<div id="host"></div>'); const calls = [], sounds = [], notices = [];
  const audio = new AttentionAudio({ storage: null }); audio.play = () => sounds.push('sound');
  const c = { dom, workspace: { id: 'one', running: false }, calls, sounds, notices, refreshed: 0, authenticated: true };
  c.chat = new BilingualChat({ api: (...args) => { calls.push(args); return api(...args); }, toast: (...args) => notices.push(args), modal: async () => false, selected: () => c.workspace, isAuthenticated: () => c.authenticated, isBusy: () => false, onState: () => {}, onStopped: async () => { c.refreshed++; c.workspace.running = true; }, attention: audio });
  c.chat.poll = () => {};
  c.mount = (id = 'one') => { c.workspace.id = id; c.chat.mount(dom.window.document.getElementById('host'), id); };
  c.accept = (model) => c.chat.accept(model, c.workspace.id, c.chat.generation);
  c.node = (id) => dom.window.document.getElementById(id);
  c.mount(); return c;
}
const close = (c) => { c.chat.unmount(); c.dom.window.close(); };

test('running rows are inside transcript only for busy phases and do not replace messages or drafts', () => {
  const c = setup(); c.accept(snapshot());
  const history = c.node('chat-messages'); c.node('chat-input').value = '下一条草稿';
  for (const [index, phase] of ['starting', 'thinking', 'awaiting_permission', 'switching_model'].entries()) {
    c.accept(snapshot({ revision: index + 2, phase, busy: true }));
    assert.equal(c.node('chat-running').hidden, false); assert.equal(c.node('chat-running').parentElement, c.node('chat-transcript'));
    assert.equal(c.node('chat-messages'), history); assert.match(history.textContent, /历史消息/); assert.equal(c.node('chat-input').value, '下一条草稿');
  }
  c.accept(snapshot({ revision: 7, phase: 'thinking', busy: false })); assert.equal(c.node('chat-running').hidden, true);
  c.accept(snapshot({ revision: 8, phase: 'error', busy: true })); assert.equal(c.node('chat-running').hidden, true);
  close(c);
});

test('quota pause stays visible with stopped container and resumes only after both windows permit it', async () => {
  const c = setup(async () => snapshot({ revision: 10, phase: 'starting', busy: true }));
  c.accept(snapshot({ phase: 'paused_quota', quotaPaused: true, quota: quota(), canResume: false }));
  assert.equal(c.node('chat-quota-paused').hidden, false); assert.equal(c.node('chat-resume').disabled, true);
  assert.doesNotMatch(c.node('chat-notice').textContent, /先点击上方/);
  c.node('chat-input').value = '保留草稿'; await c.chat.resume(); assert.equal(c.calls.length, 0);
  c.accept(snapshot({ revision: 2, phase: 'paused_quota', quotaPaused: true, quota: { ...quota(10), weekly: { limit: 100, remaining: 0 } }, canResume: true }));
  assert.equal(c.node('chat-resume').disabled, true, 'both windows must have remaining quota');
  c.accept(snapshot({ revision: 3, phase: 'paused_quota', quotaPaused: true, quota: quota(10), canResume: true }));
  assert.equal(c.node('chat-resume').disabled, false, 'server can start a stopped container on resume');
  await c.chat.resume();
  assert.deepEqual(c.calls, [['/api/workspaces/one/chat/resume', { method: 'POST' }]]);
  assert.equal(c.refreshed, 1); assert.equal(c.node('chat-input').value, '保留草稿'); assert.equal(c.node('chat-quota-paused').hidden, true);
  close(c);
});

test('progress changes preserve a reader position away from the latest message', () => {
  const c = setup(); c.accept(snapshot());
  Object.defineProperty(c.node('chat-transcript'), 'scrollHeight', { value: 2000, configurable: true });
  Object.defineProperty(c.node('chat-transcript'), 'clientHeight', { value: 400, configurable: true });
  c.node('chat-transcript').scrollTop = 200;
  c.accept(snapshot({ revision: 2, phase: 'thinking', busy: true }));
  assert.equal(c.node('chat-transcript').scrollTop, 200);
  c.accept(snapshot({ revision: 3, phase: 'paused_quota', quotaPaused: true, canResume: false, quota: quota() }));
  assert.equal(c.node('chat-transcript').scrollTop, 200);
  close(c);
});

test('quota refresh is explicit and resume is deduplicated without applying late workspace responses', async () => {
  let finish; const c = setup((path) => path.endsWith('/resume') ? new Promise((resolve) => { finish = resolve; }) : path.endsWith('/refresh') ? {} : snapshot({ revision: 2, phase: 'paused_quota', quotaPaused: true, quota: quota(20), canResume: true }));
  c.accept(snapshot({ phase: 'paused_quota', quotaPaused: true, quota: quota(), canResume: false }));
  await c.chat.refreshQuota(); assert.deepEqual(c.calls.map(([path]) => path), ['/api/account/usage/refresh', '/api/workspaces/one/chat']);
  const pending = c.chat.resume(); await c.chat.resume(); assert.equal(c.calls.filter(([path]) => path.endsWith('/resume')).length, 1);
  c.mount('two'); c.accept(snapshot()); finish(snapshot({ revision: 99, busy: true, phase: 'thinking' })); await pending;
  assert.equal(c.chat.model.revision, 1); assert.equal(c.refreshed, 0); assert.deepEqual(c.notices, []);
  close(c);
});

test('accepted permissions sound once, initial old permissions and stale responses never sound', () => {
  const c = setup();
  c.accept(snapshot({ permissions: [{ id: 'old', tool: 'Read', input: {} }] })); assert.equal(c.sounds.length, 0);
  c.accept(snapshot({ revision: 2, permissions: [{ id: 'new', tool: 'Read', input: {} }] })); assert.equal(c.sounds.length, 1);
  c.accept(snapshot({ revision: 2, permissions: [{ id: 'new', tool: 'Read', input: {} }] }));
  c.accept(snapshot({ revision: 1, permissions: [{ id: 'stale', tool: 'Read', input: {} }] })); assert.equal(c.sounds.length, 1);
  const oldGeneration = c.chat.generation; c.mount('two');
  c.chat.accept(snapshot({ revision: 99, permissions: [{ id: 'late', tool: 'Read', input: {} }] }), 'one', oldGeneration);
  assert.equal(c.sounds.length, 1); close(c);
});

test('official synchronization pause preserves the reason and cannot resume on unknown percentages', async () => {
  const c = setup(); const percent = { mode: 'official_share', unit: 'percent', exhausted: false, blockedReason: '等待下一周期官方数据同步', fiveHour: { limit: 20, used: 3, remaining: 17 }, weekly: { limit: 20, used: 4, remaining: 16 } };
  c.accept(snapshot({ phase: 'paused_quota', quotaPaused: true, canResume: false, quota: percent }));
  assert.match(c.node('chat-quota-explanation').textContent, /等待下一周期官方数据同步/);
  assert.doesNotMatch(c.node('chat-quota-paused').textContent, /已用完/);
  assert.equal(c.node('chat-resume').disabled, true); await c.chat.resume(); assert.equal(c.calls.length, 0);
  c.accept(snapshot({ revision: 2, phase: 'paused_quota', quotaPaused: true, canResume: true, quota: { ...percent, blockedReason: null, weekly: { limit: null, remaining: null } } }));
  assert.equal(c.node('chat-resume').disabled, true); assert.match(c.node('chat-quota-explanation').textContent, /等待官方额度同步/);
  c.accept(snapshot({ revision: 3, phase: 'paused_quota', quotaPaused: true, canResume: true, quota: { ...percent, blockedReason: null } }));
  assert.equal(c.node('chat-resume').disabled, false); close(c);
});

test('new tasks wait for official synchronization without erasing the next draft', async () => {
  const c = setup(); c.workspace.running = true;
  c.accept(snapshot({ quota: { mode: 'official_share', unit: 'percent', exhausted: false, blockedReason: '官方数据尚未同步', fiveHour: { remaining: null }, weekly: { remaining: null } } }));
  c.node('chat-input').value = '保留这条任务'; c.chat.controls();
  assert.equal(c.node('chat-send').disabled, true); assert.match(c.node('chat-notice').textContent, /官方数据尚未同步/);
  await c.chat.send(); assert.equal(c.calls.length, 0); assert.equal(c.node('chat-input').value, '保留这条任务'); close(c);
});

test('an allocated idle window permits a new task and stopped-session resume without a reset timestamp', () => {
  const c = setup();
  const available = { mode: 'official_share', unit: 'percent', exhausted: false, blockedReason: null,
    fiveHour: { limit: 20, used: 0, remaining: 20, resetAt: null, state: 'idle' },
    weekly: { limit: 20, used: 4, remaining: 16, resetAt: '2026-11-25T10:00:00Z', state: 'active' } };
  c.workspace.running = true; c.accept(snapshot({ quota: available }));
  c.node('chat-input').value = '开始下一项任务'; c.chat.controls();
  assert.equal(c.node('chat-send').disabled, false);
  c.workspace.running = false;
  c.accept(snapshot({ revision: 2, phase: 'paused_quota', quotaPaused: true, canResume: true, quota: available }));
  assert.equal(c.node('chat-resume').disabled, false);
  assert.match(c.node('chat-quota-explanation').textContent, /站内配额已可用/);
  assert.equal(c.node('chat-input').value, '开始下一项任务'); close(c);
});

test('unknown, expired and globally exhausted official quotas block both new sends and paused resume', async () => {
  for (const [reason, remaining] of [['正在等待官方额度同步，请稍后刷新后继续', null], ['新窗口已开始，正在等待官方同步', null], ['官方账号总额度已用完，请等待对应窗口恢复', 0]]) {
    const c = setup(); const officialShare = { mode: 'official_share', unit: 'percent', blockedReason: reason, exhausted: true, fiveHour: { limit: 20, used: 3, remaining }, weekly: { limit: 20, used: 1, remaining: 19 } };
    c.accept(snapshot({ quota: officialShare })); c.node('chat-input').value = '保留待办'; c.chat.controls();
    assert.equal(c.node('chat-send').disabled, true); assert.equal(c.node('chat-notice').textContent, reason, 'stopped container must not hide quota cause');
    assert.equal(c.node('chat-phase').textContent, '站内配额暂不可用'); await c.chat.send();
    c.accept(snapshot({ revision: 2, phase: 'paused_quota', quotaPaused: true, canResume: true, quota: officialShare }));
    assert.equal(c.node('chat-resume').disabled, true); assert.match(c.node('chat-quota-explanation').textContent, new RegExp(reason));
    await c.chat.resume(); assert.equal(c.calls.length, 0); assert.equal(c.node('chat-input').value, '保留待办'); close(c);
  }
});
