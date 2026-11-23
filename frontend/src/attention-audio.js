/** Permission sounds are device preferences; no account or conversation is stored. */
export class AttentionAudio {
  constructor({ storage, contextFactory, onChange = () => {}, onHint = () => {} } = {}) {
    try { this.storage = storage === undefined ? globalThis.localStorage : storage; } catch { this.storage = null; }
    this.contextFactory = contextFactory || (() => { const Audio = globalThis.AudioContext || globalThis.webkitAudioContext; return Audio ? new Audio() : null; });
    this.onChange = onChange; this.onHint = onHint;
    try { this.enabled = this.storage?.getItem('claude-cloud-permission-sound') !== 'off'; } catch { this.enabled = true; }
    this.status = this.enabled ? '点击页面后启用审批提示音' : '审批提示音已关闭';
    this.seen = new Map(); this.seedViews = new Set(); this.generation = 0; this.warned = false;
  }
  attach(page) {
    this.detachListeners(); this.page = page;
    this.gesture = (event) => { if (event.isTrusted && (event.type === 'pointerdown' || !event.repeat)) this.activate(); };
    page.addEventListener('pointerdown', this.gesture, true); page.addEventListener('keydown', this.gesture, true);
  }
  detachListeners() {
    if (this.page && this.gesture) { this.page.removeEventListener('pointerdown', this.gesture, true); this.page.removeEventListener('keydown', this.gesture, true); }
    this.page = null; this.gesture = null;
  }
  changed() { this.onChange({ enabled: this.enabled, status: this.status }); }
  async activate() {
    if (!this.enabled || this.activating || this.context?.state === 'running') return;
    this.activating = true; const generation = this.generation;
    try {
      if (!this.context || this.context.state === 'closed') this.context = this.contextFactory();
      if (!this.context) throw new Error('unsupported');
      if (this.context.state !== 'running') await this.context.resume();
      if (generation !== this.generation || !this.enabled) return;
      if (this.context.state !== 'running') throw new Error('blocked');
      this.status = '审批提示音已开启'; this.changed();
    } catch {
      if (generation !== this.generation) return;
      this.blocked();
    } finally { if (generation === this.generation) this.activating = false; }
  }
  blocked() {
    this.status = '浏览器尚未允许播放声音，请点击页面重试；确认卡片仍会显示。'; this.changed();
    if (!this.warned) { this.warned = true; this.onHint(this.status); }
  }
  setEnabled(value) {
    this.enabled = Boolean(value);
    try { this.storage?.setItem('claude-cloud-permission-sound', this.enabled ? 'on' : 'off'); } catch { /* Optional device preference. */ }
    this.status = this.enabled ? '点击页面后启用审批提示音' : '审批提示音已关闭'; this.changed();
    if (this.enabled) this.activate();
  }
  beginView(workspaceId) { this.seedViews.add(workspaceId); }
  observe(workspaceId, epoch, permissions = []) {
    const key = `${workspaceId}:${epoch || 'unknown'}`;
    const ids = permissions.map((permission) => permission?.id).filter((id) => typeof id === 'string' && id);
    const seed = this.seedViews.delete(workspaceId);
    if (seed) { this.seen.set(key, new Set(ids)); return; }
    if (!this.seen.has(key)) this.seen.set(key, new Set());
    const previous = this.seen.get(key);
    const fresh = ids.some((id) => !previous.has(id));
    for (const id of ids) previous.add(id);
    if (this.seen.size > 200) this.seen.delete(this.seen.keys().next().value);
    if (fresh && this.enabled) this.play();
  }
  play() {
    if (!this.context || this.context.state !== 'running') { this.blocked(); return; }
    try {
      const start = this.context.currentTime;
      for (const [offset, frequency] of [[0, 660], [0.16, 880]]) {
        const oscillator = this.context.createOscillator(), gain = this.context.createGain();
        oscillator.type = 'sine'; oscillator.frequency.value = frequency;
        gain.gain.setValueAtTime(0.0001, start + offset);
        gain.gain.exponentialRampToValueAtTime(0.12, start + offset + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, start + offset + 0.13);
        oscillator.connect(gain); gain.connect(this.context.destination);
        oscillator.onended = () => { oscillator.disconnect(); gain.disconnect(); };
        oscillator.start(start + offset); oscillator.stop(start + offset + 0.14);
      }
    } catch { this.blocked(); }
  }
  reset() {
    this.generation++; this.detachListeners(); this.seen.clear(); this.seedViews.clear(); this.activating = false; this.warned = false;
    const context = this.context; this.context = null;
    try { context?.close()?.catch?.(() => {}); } catch { /* Already closed. */ }
    this.status = this.enabled ? '点击页面后启用审批提示音' : '审批提示音已关闭'; this.changed();
  }
}
