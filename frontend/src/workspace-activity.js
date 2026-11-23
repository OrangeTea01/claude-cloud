/** Only trusted user interactions refresh the workspace idle clock; never polls. */
export class WorkspaceActivity {
  constructor({ send, now = () => Date.now(), interval = 60_000 }) { this.send = send; this.now = now; this.interval = Math.max(60_000, interval); this.sentAt = new Map(); this.generation = 0; }
  mount(host, context) {
    this.unmount(); if (!host || !context) return;
    this.host = host; this.context = context; this.generation++;
    this.listener = (event) => { if (event.isTrusted) this.record(event.type); };
    // Scroll events also fire for programmatic transcript scrolling. Observe
    // its physical gestures instead, so incoming messages never reset idle time.
    for (const type of ['pointerdown', 'click', 'input', 'keydown', 'wheel', 'touchmove']) host.addEventListener(type, this.listener, { capture: true, passive: true });
  }
  record(type) {
    if (!this.context || !['pointerdown', 'click', 'input', 'keydown', 'wheel', 'touchmove'].includes(type)) return;
    const { id, key } = this.context; const now = this.now();
    if (this.pending || (this.sentAt.has(key) && now - this.sentAt.get(key) < this.interval)) return;
    this.sentAt.set(key, now); const generation = this.generation; this.pending = true;
    Promise.resolve().then(() => generation === this.generation ? this.send(`/api/workspaces/${encodeURIComponent(id)}/activity`, { method: 'POST' }) : undefined)
      .catch(() => {}).finally(() => { if (generation === this.generation) this.pending = false; });
  }
  unmount() {
    this.generation++;
    if (this.host) for (const type of ['pointerdown', 'click', 'input', 'keydown', 'wheel', 'touchmove']) this.host.removeEventListener(type, this.listener, true);
    this.host = null; this.context = null; this.listener = null; this.pending = false;
  }
  reset() { this.unmount(); this.sentAt.clear(); }
}
