import assert from 'node:assert/strict';
import test from 'node:test';
import { AttentionAudio } from '../src/attention-audio.js';

const permission = (id) => ({ id });
const tick = () => new Promise((resolve) => setImmediate(resolve));
function setup() {
  const calls = { created: 0, starts: 0, closed: 0, hints: [], saved: [] };
  const context = { state: 'running', currentTime: 0, destination: {}, close() { calls.closed++; }, createOscillator() { return { frequency: {}, connect() {}, disconnect() {}, start() { calls.starts++; }, stop() {} }; }, createGain() { return { gain: { setValueAtTime() {}, exponentialRampToValueAtTime() {} }, connect() {}, disconnect() {} }; } };
  const audio = new AttentionAudio({ storage: { getItem: () => null, setItem: (...args) => calls.saved.push(args) }, contextFactory: () => { calls.created++; return context; }, onHint: (hint) => calls.hints.push(hint) });
  return { audio, calls, context };
}

test('sound waits for activation, seeds old approvals and rings once for each new permission set', async () => {
  const { audio, calls } = setup();
  assert.equal(calls.created, 0);
  audio.beginView('one'); audio.observe('one', 'run1', [permission('old')]);
  assert.equal(calls.starts, 0); assert.equal(calls.hints.length, 0);
  await audio.activate();
  audio.observe('one', 'run1', [permission('old'), permission('new')]);
  assert.equal(calls.starts, 2);
  audio.observe('one', 'run1', [permission('old'), permission('new')]);
  audio.observe('one', 'run1', []); audio.observe('one', 'run1', [permission('new')]);
  assert.equal(calls.starts, 2);
  audio.beginView('one'); audio.observe('one', 'run1', [permission('new'), permission('away')]);
  assert.equal(calls.starts, 2, 'opening existing approvals does not replay them');
  audio.observe('one', 'run2', [permission('new')]);
  assert.equal(calls.starts, 4, 'same id in a new run is a new approval');
});

test('device preference suppresses sounds while off, and account reset clears audio and dedup state', async () => {
  const { audio, calls } = setup(); await audio.activate();
  audio.setEnabled(false); audio.beginView('one'); audio.observe('one', 'run1', []); audio.observe('one', 'run1', [permission('new')]);
  assert.equal(calls.starts, 0); assert.deepEqual(calls.saved.at(-1), ['claude-cloud-permission-sound', 'off']);
  audio.setEnabled(true); await tick(); audio.observe('one', 'run1', [permission('new')]);
  assert.equal(calls.starts, 0, 'enabling sound does not replay pending approvals');
  audio.reset(); assert.equal(calls.closed, 1); assert.equal(audio.context, null); assert.equal(audio.seen.size, 0);
  assert.equal(audio.enabled, true, 'only the device setting survives account changes');
});

test('unsupported or rejected audio produces one lightweight hint rather than repeated errors', async () => {
  for (const contextFactory of [() => null, () => ({ state: 'suspended', resume: async () => { throw new Error('blocked'); } })]) {
    const hints = []; const audio = new AttentionAudio({ storage: null, contextFactory, onHint: (hint) => hints.push(hint) });
    await audio.activate(); await audio.activate(); audio.beginView('one'); audio.observe('one', 'e', []);
    audio.observe('one', 'e', [permission('a')]); audio.observe('one', 'e', [permission('a'), permission('b')]);
    assert.equal(hints.length, 1); assert.match(audio.status, /确认卡片仍会显示/);
  }
});

test('late audio resume cannot restore account state after reset', async () => {
  let finish; const gate = new Promise((resolve) => { finish = resolve; });
  const audio = new AttentionAudio({ storage: null, contextFactory: () => ({ state: 'suspended', resume: () => gate, close() {} }) });
  const pending = audio.activate(); audio.reset(); finish(); await pending;
  assert.equal(audio.context, null); assert.match(audio.status, /点击页面后/);
});
