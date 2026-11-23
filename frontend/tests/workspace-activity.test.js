import assert from 'node:assert/strict';
import test from 'node:test';
import { WorkspaceActivity } from '../src/workspace-activity.js';

const tick = () => new Promise((resolve) => setImmediate(resolve));
function setup(send) {
  let now = 0; const listeners = new Map(), calls = [];
  const host = { addEventListener: (type, fn) => listeners.set(type, fn), removeEventListener: (type) => listeners.delete(type) };
  const activity = new WorkspaceActivity({ send: send || ((...args) => calls.push(args)), now: () => now, interval: 10 });
  activity.mount(host, { id: 'one', key: 'user1:one' });
  return { activity, host, listeners, calls, advance: (value) => { now += value; }, emit: (type, isTrusted = true) => listeners.get(type)?.({ type, isTrusted }) };
}

test('only real interactions count, with a minimum one-minute throttle and no polling scroll heartbeats', async () => {
  const c = setup();
  c.emit('click', false); c.emit('scroll'); await tick(); assert.equal(c.calls.length, 0);
  c.emit('wheel'); await tick(); assert.equal(c.calls.length, 1);
  assert.deepEqual(c.calls[0], ['/api/workspaces/one/activity', { method: 'POST' }]);
  c.advance(59000); c.emit('input'); await tick(); assert.equal(c.calls.length, 1);
  c.advance(1000); c.emit('keydown'); await tick(); assert.equal(c.calls.length, 2);
  c.advance(60000); await tick(); assert.equal(c.calls.length, 2, 'time and polling alone do not send activity');
});

test('workspace changes discard queued activity and account reset removes all listeners', async () => {
  const c = setup(); c.emit('click'); c.activity.unmount(); await tick(); assert.equal(c.calls.length, 0);
  assert.equal(c.listeners.size, 0);
  c.activity.mount(c.host, { id: 'two', key: 'user2:two' }); c.emit('touchmove'); await tick();
  assert.equal(c.calls[0][0], '/api/workspaces/two/activity');
  c.activity.reset(); c.emit('click'); await tick(); assert.equal(c.calls.length, 1); assert.equal(c.activity.sentAt.size, 0);
});

test('activity failures do not create a retry storm or unhandled rejection', async () => {
  let calls = 0; const c = setup(async () => { calls++; throw new Error('offline'); });
  c.emit('input'); await tick(); c.emit('input'); await tick(); assert.equal(calls, 1);
  c.advance(60000); c.emit('input'); await tick(); assert.equal(calls, 2);
});
