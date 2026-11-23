const ENDPOINT = '/api/admin/claude-account-browser';
const PURPOSES = new Set(['register', 'billing', 'usage']);

export function remainingBrowserTime(expiresAt, now = Date.now()) {
  const deadline = Date.parse(expiresAt);
  if (!Number.isFinite(deadline)) return null;
  return Math.max(0, Math.ceil((deadline - now) / 1000));
}

export function formatBrowserTime(seconds) {
  return `${Math.floor(seconds / 60).toString().padStart(2, '0')}:${(seconds % 60).toString().padStart(2, '0')}`;
}

/** A private server display; page contents, credentials and clipboard stay remote. */
export class AccountBrowser {
  constructor({ api, getContext, toast, solo = false, page = globalThis.document,
    location = globalThis.location, createSocket = (url) => new WebSocket(url),
    loadRFB = async () => (await import('@novnc/novnc')).default,
    now = () => Date.now(), setTimer = (...args) => globalThis.setInterval(...args),
    clearTimer = (...args) => globalThis.clearInterval(...args) }) {
    Object.assign(this, { api, getContext, toast, solo, page, location, createSocket, loadRFB, now, setTimer, clearTimer });
    this.generation = 0;
    this.operation = Promise.resolve();
    this.dialog = null;
    this.browserId = null;
  }

  enqueue(operation) {
    this.operation = this.operation.catch(() => {}).then(operation);
    return this.operation;
  }

  current(generation, context) {
    const current = this.getContext();
    return Boolean(this.dialog && generation === this.generation && current?.authenticated && current.key === context.key);
  }

  request(path, options, context) {
    return this.api(path, { ...options, csrfToken: context.csrf });
  }

  status(message, error = false) {
    if (!this.dialog) return;
    const node = this.dialog.querySelector('[data-browser-status]');
    node.textContent = message;
    node.classList.toggle('is-error', error);
  }

  busy(value) {
    this.starting = value;
    this.dialog?.querySelectorAll('[data-browser-purpose]').forEach((button) => { button.disabled = value; });
  }

  mount() {
    const dialog = this.page.createElement('dialog');
    dialog.className = 'account-browser-dialog';
    dialog.setAttribute('aria-labelledby', 'account-browser-title');
    dialog.innerHTML = `<header class="account-browser-header"><div><span class="account-browser-eyebrow">管理员 · CLAUDE 账号管理</span><h2 id="account-browser-title">注册、订阅与用量充值</h2></div><button type="button" class="button secondary small" data-browser-close>关闭并清除登录</button></header>
      <p class="account-browser-intro">在服务器浏览器中的 Claude 官方页面操作，使用当前服务器出口。请登录需要管理的 Claude 账号，付款前核对账号、金额与续费规则。</p>
      <nav class="account-browser-tabs" aria-label="Claude 官方账号页面"><button type="button" data-browser-purpose="register" aria-pressed="false">注册 / 登录</button><button type="button" data-browser-purpose="billing" aria-pressed="false">订阅管理</button><button type="button" data-browser-purpose="usage" aria-pressed="false">用量充值</button></nav>
      <div class="account-browser-toolbar"><p data-browser-status role="status">正在准备服务器浏览器…</p><div class="account-browser-display-actions"><span class="account-browser-time" aria-label="浏览器剩余时间" hidden>剩余 <strong data-browser-countdown>30:00</strong></span><button type="button" class="text-button" data-browser-scale disabled>原始大小</button><button type="button" class="text-button" data-browser-reconnect hidden>重连画面</button></div></div>
      <p class="account-browser-warning" data-browser-warning role="status" hidden></p>
      <div class="account-browser-screen" aria-label="服务器上的 Claude 账号浏览器" hidden></div>
      <details class="account-browser-input" data-browser-input hidden><summary>手机输入 / 粘贴文字</summary><p>先点选画面中的输入框。邮箱验证链接请粘贴到远程地址栏，不要直接在本机打开。</p><div class="account-browser-entry"><input class="text-input" type="password" data-browser-text maxlength="4096" autocomplete="off" autocapitalize="none" spellcheck="false" aria-label="要发送到远程浏览器的文字"><button type="button" class="button secondary small" data-browser-type>发送文字</button></div><div class="account-browser-keys"><button type="button" class="text-button" data-browser-key="Tab">下一项</button><button type="button" class="text-button" data-browser-key="Return">确认</button><button type="button" class="text-button" data-browser-key="BackSpace">退格</button><button type="button" class="text-button" data-browser-select>全选</button><button type="button" class="text-button" data-browser-address>选中远程地址栏</button></div></details>
      <p class="account-browser-footnote">本次浏览器最多使用 30 分钟；切换上方页面或刷新管理员页面会保留本次登录。点击“关闭并清除登录”、到期或退出管理员页面后会清理登录信息。用量充值用于官方提供的额外用量，并非 API 账户余额；是否可用以官方页面为准。注册或付款不会自动更改工作区的 Claude 授权；需要关联账号时，请在「Claude 授权」中单独完成。</p>`;
    if (this.solo) dialog.innerHTML = dialog.innerHTML.replaceAll('管理员 · CLAUDE 账号管理', 'CLAUDE 账号管理').replaceAll('管理员页面', '管理与设置').replaceAll('「Claude 授权」', '「连接 Claude」');
    this.dialog = dialog;
    this.page.body.append(dialog);
    dialog.querySelector('[data-browser-close]').addEventListener('click', () => this.close());
    dialog.addEventListener('cancel', (event) => { event.preventDefault(); this.close(); });
    dialog.querySelectorAll('[data-browser-purpose]').forEach((button) => button.addEventListener('click', () => this.navigate(button.dataset.browserPurpose)));
    dialog.querySelector('[data-browser-reconnect]').addEventListener('click', () => this.connect(this.generation, this.context));
    dialog.querySelector('[data-browser-scale]').addEventListener('click', (event) => {
      if (!this.rfb) return;
      this.rfb.scaleViewport = !this.rfb.scaleViewport;
      event.currentTarget.textContent = this.rfb.scaleViewport ? '原始大小' : '适应屏幕';
    });
    dialog.querySelector('[data-browser-type]').addEventListener('click', () => {
      const field = dialog.querySelector('[data-browser-text]');
      const text = field.value;
      field.value = '';
      if (!this.connected || !this.rfb) return;
      this.rfb.focus();
      for (const character of text) {
        const point = character.codePointAt(0);
        if (point >= 32 && point !== 127) this.rfb.sendKey(point < 256 ? point : 0x01000000 | point);
      }
    });
    dialog.querySelectorAll('[data-browser-key]').forEach((button) => button.addEventListener('click', () => {
      if (!this.connected || !this.rfb) return;
      this.rfb.focus();
      this.rfb.sendKey({ Tab: 0xff09, Return: 0xff0d, BackSpace: 0xff08 }[button.dataset.browserKey]);
    }));
    const shortcut = (key) => {
      if (!this.connected || !this.rfb) return;
      this.rfb.focus();
      this.rfb.sendKey(0xffe3, 'ControlLeft', true);
      this.rfb.sendKey(key.charCodeAt(0), `Key${key.toUpperCase()}`);
      this.rfb.sendKey(0xffe3, 'ControlLeft', false);
    };
    dialog.querySelector('[data-browser-select]').addEventListener('click', () => shortcut('a'));
    dialog.querySelector('[data-browser-address]').addEventListener('click', () => shortcut('l'));
    dialog.showModal();
    dialog.querySelector('[data-browser-close]').focus();
    this.timer = this.setTimer(() => this.tick(), 1000);
  }

  async show(purpose = 'register') {
    if (!PURPOSES.has(purpose)) return;
    const context = this.getContext();
    if (!context?.authenticated) { this.toast('请先登录管理员页面。', true); return; }
    if (this.dialog) { this.dialog.focus(); return this.navigate(purpose); }
    const generation = ++this.generation;
    this.context = { ...context };
    this.browserId = null;
    this.expiresAt = null;
    this.warned = false;
    this.expiring = false;
    this.suspended = false;
    this.mount();
    this.busy(true);
    return this.enqueue(async () => {
      try {
        const status = await this.request(ENDPOINT, {}, context);
        if (!this.current(generation, context)) { await this.stopStaleStatus(status, context); return; }
        if (status.active) {
          this.adopt(status);
          if (status.purpose !== purpose) await this.openPurpose(purpose, generation, context);
          else await this.connect(generation, context);
        } else {
          await this.openPurpose(purpose, generation, context);
        }
      } catch (error) {
        if (this.current(generation, context)) this.status(error.message || '暂时无法打开服务器浏览器。', true);
      } finally {
        if (this.current(generation, context)) this.busy(false);
      }
    });
  }

  adopt(status) {
    if (status?.active !== true || typeof status.browserId !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(status.browserId)
        || !PURPOSES.has(status.purpose) || remainingBrowserTime(status.expiresAt, this.now()) === null) {
      throw new Error('浏览器状态无法识别，请关闭后重新打开。');
    }
    this.browserId = status.browserId;
    this.expiresAt = status.expiresAt;
    this.purpose = status.purpose;
    this.dialog.querySelectorAll('[data-browser-purpose]').forEach((button) => button.setAttribute('aria-pressed', String(button.dataset.browserPurpose === status.purpose)));
    this.dialog.querySelector('.account-browser-time').hidden = false;
    this.tick();
  }

  async navigate(purpose) {
    if (!this.dialog || this.starting || !PURPOSES.has(purpose)) return;
    const generation = this.generation;
    const context = this.context;
    if (!this.current(generation, context)) return;
    this.busy(true);
    return this.enqueue(async () => {
      try { await this.openPurpose(purpose, generation, context); }
      catch (error) { if (this.current(generation, context)) this.status(error.message || '页面暂时无法打开。', true); }
      finally { if (this.current(generation, context)) this.busy(false); }
    });
  }

  async openPurpose(purpose, generation, context) {
    if (!this.current(generation, context)) return;
    this.status(this.browserId ? '正在切换官方页面，本次登录会保留…' : '正在启动服务器浏览器，通常需要片刻…');
    const body = { purpose, ...(this.browserId ? { browserId: this.browserId } : {}) };
    const status = await this.request(ENDPOINT, { method: 'POST', body }, context);
    if (!this.current(generation, context)) {
      await this.stopStaleStatus(status, context);
      return;
    }
    this.adopt(status);
    if (!this.current(generation, context)) return;
    if (!this.connected) await this.connect(generation, context);
    else this.status('已打开官方页面，请在下方画面中操作。');
  }

  tick() {
    if (!this.dialog || !this.expiresAt || this.expiring || this.suspended) return;
    if (!this.current(this.generation, this.context)) { this.close({ quiet: true }); return; }
    const remaining = remainingBrowserTime(this.expiresAt, this.now());
    if (remaining === null) return;
    this.dialog.querySelector('[data-browser-countdown]').textContent = formatBrowserTime(remaining);
    if (remaining <= 300 && remaining > 0) {
      const warning = this.dialog.querySelector('[data-browser-warning]');
      warning.hidden = false;
      warning.textContent = '本次浏览器将在 5 分钟内关闭。请留意官方页面中的付款进度；切换页面不会延长时间。';
      if (!this.warned) { this.warned = true; this.toast('服务器浏览器剩余不足 5 分钟，请留意操作时间。'); }
    }
    if (remaining === 0) {
      this.expiring = true;
      this.toast('浏览器使用时间已到，正在结束本次会话。需要时可从「Claude 账号管理」重新打开。');
      this.close({ quiet: true });
    }
  }

  async connect(generation, context) {
    if (!this.current(generation, context) || !this.browserId || this.suspended) return;
    const browserId = this.browserId;
    let RFB;
    try { RFB = await this.loadRFB(); }
    catch { if (this.current(generation, context)) this.status('画面组件加载失败，请刷新网页后重试。', true); return; }
    if (!this.current(generation, context) || browserId !== this.browserId || this.suspended) return;
    this.disconnect();
    const dialog = this.dialog;
    const screen = dialog.querySelector('.account-browser-screen');
    screen.hidden = false;
    screen.replaceChildren();
    dialog.querySelector('[data-browser-reconnect]').hidden = true;
    this.status('正在连接服务器浏览器画面…');
    const socket = this.createSocket(`${this.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${this.location.host}${ENDPOINT}/display`);
    this.socket = socket;
    socket.binaryType = 'arraybuffer';
    const valid = () => this.current(generation, context) && !this.suspended && this.socket === socket && this.browserId === browserId;
    socket.addEventListener('close', (event) => {
      if (!valid()) return;
      this.connected = false;
      const messages = { 4403: '此浏览器会话已结束或登录状态已变化，请关闭后重新打开。', 4409: '画面正在另一个窗口中使用，请先断开另一个窗口。' };
      this.status(messages[event.code] || '画面连接已断开，可在剩余时间内重连。', true);
      dialog.querySelector('[data-browser-reconnect]').hidden = event.code === 4403;
      if (event.code === 4403) { this.disconnect(); this.clearDisplay(); }
    });
    socket.addEventListener('error', () => { if (valid()) this.status('画面连接失败，请检查网络后重连。', true); });
    socket.onopen = () => {
      if (!valid()) { socket.close(); return; }
      socket.send(JSON.stringify({ type: 'auth', csrfToken: context.csrf, browserId }));
      let rfb;
      try { rfb = new RFB(screen, socket, { shared: false }); }
      catch { socket.close(); if (valid()) this.status('画面初始化失败，请关闭后重新打开。', true); return; }
      this.rfb = rfb;
      rfb.showDotCursor = true;
      rfb.scaleViewport = true;
      rfb.resizeSession = false;
      rfb.background = '#f0eee7';
      rfb.addEventListener('connect', () => {
        if (!valid() || this.rfb !== rfb) return;
        this.connected = true;
        dialog.querySelector('[data-browser-input]').hidden = false;
        dialog.querySelector('[data-browser-scale]').disabled = false;
        this.status('已连接服务器浏览器。账号注册和支付结果请以官方页面为准。');
      });
      rfb.addEventListener('credentialsrequired', () => {
        if (!valid() || this.rfb !== rfb) return;
        this.status('远程画面配置异常，请联系管理员检查服务。', true);
        rfb.disconnect();
      });
    };
  }

  disconnect() {
    this.connected = false;
    const socket = this.socket, rfb = this.rfb;
    this.socket = null;
    this.rfb = null;
    rfb?.disconnect();
    socket?.close();
  }

  clearDisplay() {
    if (!this.dialog) return;
    const screen = this.dialog.querySelector('.account-browser-screen');
    screen.replaceChildren(); screen.hidden = true;
    this.dialog.querySelector('[data-browser-input]').hidden = true;
    this.dialog.querySelector('[data-browser-scale]').disabled = true;
    this.dialog.querySelectorAll('input').forEach((field) => { field.value = ''; });
  }

  async stopStaleStatus(status, context) {
    if (status?.active !== true || typeof status.browserId !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(status.browserId)) return;
    try { await this.request(`${ENDPOINT}/stop`, { method: 'POST', body: { browserId: status.browserId } }, context); }
    catch { /* Revoked sessions and the independent server TTL also clean up. */ }
  }

  suspend() {
    this.suspended = true;
    this.disconnect();
    this.clearDisplay();
    if (this.timer !== undefined) this.clearTimer(this.timer);
    this.timer = undefined;
  }

  async resume() {
    if (!this.dialog) return;
    this.suspended = false;
    const generation = this.generation, context = this.context;
    if (this.timer !== undefined) this.clearTimer(this.timer);
    this.timer = this.setTimer(() => this.tick(), 1000);
    return this.enqueue(async () => {
      try {
        const status = await this.request(ENDPOINT, {}, context);
        if (!this.current(generation, context)) return;
        if (status.active) { this.adopt(status); await this.connect(generation, context); }
        else {
          this.browserId = null; this.expiresAt = null;
          this.clearDisplay();
          this.dialog.querySelector('.account-browser-time').hidden = true;
          this.dialog.querySelector('[data-browser-warning]').hidden = true;
          this.dialog.querySelector('[data-browser-reconnect]').hidden = true;
          this.status('该浏览器会话已结束，请点击上方页面开启新会话。');
        }
      } catch (error) { if (this.current(generation, context)) this.status(error.message || '暂时无法恢复浏览器。', true); }
    });
  }

  async close({ quiet = false } = {}) {
    if (!this.dialog) return;
    const browserId = this.browserId, context = this.context;
    ++this.generation;
    this.disconnect();
    if (this.timer !== undefined) this.clearTimer(this.timer);
    this.timer = undefined;
    this.dialog.querySelectorAll('input').forEach((field) => { field.value = ''; });
    this.dialog.close();
    this.dialog.remove();
    this.dialog = null;
    this.browserId = null;
    this.expiresAt = null;
    this.starting = false;
    const stop = async () => {
      if (!browserId) return;
      try { await this.request(`${ENDPOINT}/stop`, { method: 'POST', body: { browserId } }, context); }
      catch (error) { if (!quiet) this.toast(`画面已关闭，未能立即确认服务器清理：${error.message}`, true); }
    };
    return this.enqueue(stop);
  }
}
