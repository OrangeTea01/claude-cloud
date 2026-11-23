const LOGIN_PATHS = new Map([
  ['claude.com', '/cai/oauth/authorize'],
  ['claude.ai', '/oauth/authorize'],
  ['platform.claude.com', '/oauth/authorize'],
  ['console.anthropic.com', '/oauth/authorize'],
]);

// Remove only whitespace introduced by terminal selection/wrapping. Preserve
// percent escapes, parameter order, '+', '&', and opaque OAuth values exactly.
export function normalizeClaudeLoginUrl(value) {
  return typeof value === 'string' ? value.replace(/[ \t\r\n]/g, '') : '';
}

export function loginUrlError(value) {
  if (typeof value !== 'string' || !value || value.length > 16384) return '请粘贴终端中的一条完整授权链接。';
  if (/[^\x21-\x7e]|\\/.test(value) || /%(?![\da-f]{2})/i.test(value)) return '链接中有无法识别的字符或不完整的 % 编码，请重新复制链接。';
  if ((value.match(/https?:\/\//gi) || []).length !== 1 || /[<>"']/.test(value)) return '请只粘贴一条授权链接，不要包含终端提示文字。';
  try {
    const url = new URL(value);
    const authority = /^[a-z][a-z\d+.-]*:\/\/([^/?#]*)/i.exec(value)?.[1] || '';
    if (url.protocol !== 'https:' || authority.includes('@') || url.username || url.password || url.hash ||
        (url.port && url.port !== '443') || LOGIN_PATHS.get(url.hostname) !== url.pathname) return '请使用 Claude 终端显示的官方 HTTPS 授权链接。';
    for (const key of ['response_type', 'client_id', 'code_challenge', 'state']) {
      const values = url.searchParams.getAll(key);
      if (!values.length || !values[0]) return `链接缺少 ${key} 参数；请重新复制完整链接，并保留参数之间的 & 符号。`;
      if (values.length !== 1) return `链接包含重复的 ${key} 参数，请只复制一条授权链接。`;
    }
    if (url.searchParams.get('response_type') !== 'code') return '链接的 response_type 参数不是 code，请重新复制终端中的授权链接。';
    if (url.searchParams.has('code_challenge_method') &&
        (url.searchParams.getAll('code_challenge_method').length !== 1 || url.searchParams.get('code_challenge_method') !== 'S256')) return '链接的 code_challenge_method 参数无效，请重新复制授权链接。';
    if ([...url.searchParams.values()].some((part) => /[\x00-\x1f\x7f]/.test(part))) return '链接参数含有控制字符，请重新复制授权链接。';
    if ([...url.searchParams].length > 32) return '链接参数过多，请只复制一条官方授权链接。';
    if (url.searchParams.has('redirect_uri')) {
      if (url.searchParams.getAll('redirect_uri').length !== 1) return '链接包含重复的 redirect_uri 参数，请重新复制授权链接。';
      const redirectValue = url.searchParams.get('redirect_uri');
      const redirect = new URL(redirectValue);
      const redirectAuthority = /^[a-z][a-z\d+.-]*:\/\/([^/?#]*)/i.exec(redirectValue)?.[1] || '';
      const explicitPort = /:(\d+)$/.exec(redirectAuthority)?.[1];
      const official = redirect.protocol === 'https:' && (!redirect.port || redirect.port === '443') &&
        ['platform.claude.com', 'console.anthropic.com'].includes(redirect.hostname) && redirect.pathname === '/oauth/code/callback';
      const local = redirect.protocol === 'http:' && ['localhost', '127.0.0.1'].includes(redirect.hostname) &&
        explicitPort && Number(explicitPort) >= 1 && Number(explicitPort) <= 65535 && redirect.pathname === '/callback';
      if ((!official && !local) || redirectAuthority.includes('@') || redirect.username || redirect.password || redirect.hash || redirect.search) return '链接的授权回调地址无效，请重新复制当前终端中的链接。';
    }
    return '';
  } catch { return '授权链接格式无法识别，请重新复制终端中的完整链接。'; }
}

export function isClaudeLoginUrl(value) {
  return !loginUrlError(value);
}

// Recognize authorization-looking official links even when malformed, so they
// cannot accidentally fall back to opening on the user's own network.
export function isClaudeAuthLink(value) {
  try {
    const url = new URL(value);
    return LOGIN_PATHS.has(url.hostname) && /oauth|authorize/i.test(url.pathname);
  } catch { return false; }
}

export class ServerLoginBrowser {
  constructor({ api, toast, context, fillTerminal, endpoint, terminalLabel = '原始终端', contextHint, onAuthExpired, domPrefix = 'server-login' }) {
    Object.assign(this, { api, toast, context, fillTerminal, endpoint, terminalLabel, contextHint, onAuthExpired });
    this.domPrefix = /^[a-z][a-z0-9-]{0,60}$/.test(domPrefix) ? domPrefix : 'server-login';
    this.dialog = null;
    this.generation = 0;
    this.url = '';
    this.output = '';
    this.workspaceId = null;
    this.rfb = null;
    this.socket = null;
    this.operation = Promise.resolve();
  }

  path(id, suffix = '') { return this.endpoint ? this.endpoint(id, suffix) : `/api/workspaces/${encodeURIComponent(id)}/login-browser${suffix}`; }
  selector(suffix) { return `#${this.domPrefix}-${suffix}`; }
  enqueue(fn) { this.operation = this.operation.catch(() => {}).then(fn); return this.operation; }

  observeOutput(data) {
    // Only retain a small transient tail; never save authorization URLs to storage.
    this.output = (this.output + data).slice(-32768);
    const candidates = [];
    // OSC 8 carries the complete hyperlink target even if its visible label wraps.
    for (const match of this.output.matchAll(/\x1b\]8;[^;\x07\x1b]*;([^\x07\x1b]*)(?:\x07|\x1b\\)/g)) {
      if (isClaudeLoginUrl(match[1])) {
        const labelStart = match.index + match[0].length;
        const close = /\x1b\]8;[^;\x07\x1b]*;(?:\x07|\x1b\\)/.exec(this.output.slice(labelStart));
        candidates.push({ index: close ? labelStart + close.index : this.output.length, value: match[1] });
      }
    }
    // Replace terminal decoration with spaces to preserve source offsets. Never
    // treat the end of a WebSocket chunk as the end of an opaque state/token.
    const plain = this.output.replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g, (part) => ' '.repeat(part.length))
      .replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, (part) => ' '.repeat(part.length));
    const starts = [...plain.matchAll(/https:\/\/(?:claude\.com\/cai|claude\.ai|platform\.claude\.com|console\.anthropic\.com)\/oauth\/authorize\?/g)];
    for (const match of starts) {
      const after = plain.slice(match.index);
      const end = /(?:\r?\n)[ \t]*Paste code here if prompted\s*>/.exec(after);
      if (!end) continue;
      const rendered = this.output.slice(match.index, match.index + end.index)
        .replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g, '')
        .replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '');
      // The CLI wraps URL-only lines; an internally spaced line is prose, not
      // another URL segment. Do not append it to an otherwise valid state token.
      if (rendered.split(/\r?\n/).some((line) => /\S[ \t]+\S/.test(line.trim()))) continue;
      const candidate = normalizeClaudeLoginUrl(rendered);
      if (isClaudeLoginUrl(candidate)) candidates.push({ index: match.index, value: candidate });
    }
    const last = candidates.sort((a, b) => a.index - b.index).at(-1);
    const newestStart = starts.at(-1)?.index ?? -1;
    this.url = last && last.index >= newestStart ? last.value : '';
  }

  reset() {
    this.close({ quiet: true });
    this.url = '';
    this.output = '';
  }

  async show(value = '') {
    const context = this.context();
    if (!context.authenticated || !context.workspaceId || !context.terminalConnected) {
      this.toast(this.contextHint || '请先连接当前工作区的原始终端，等待 Claude 显示官方登录链接。', true);
      return;
    }
    if (this.dialog) { this.dialog.focus(); return; }
    const id = context.workspaceId;
    const generation = ++this.generation;
    this.workspaceId = id;
    this.terminalGeneration = context.terminalGeneration;
    const dialog = document.createElement('dialog');
    this.dialog = dialog;
    dialog.className = 'server-login-dialog';
    dialog.setAttribute('aria-labelledby', `${this.domPrefix}-title`);
    dialog.innerHTML = `<header class="server-login-header"><div><span class="eyebrow">SERVER BROWSER</span><h2 id="server-login-title">在网页中登录 Claude</h2></div><button type="button" class="button secondary small" data-login-close>关闭登录浏览器</button></header>
      <p class="server-login-note">下面是服务器上浏览器的实时画面。授权页面从服务器出网；请在画面中的官方页面完成登录。如果需要打开邮件中的验证链接，请复制链接到这个远程浏览器的地址栏。临时浏览器最多保留 30 分钟。</p>
      <form class="server-login-start"><label class="field-label" for="server-login-url">Claude 终端显示的官方授权链接</label><div><input id="server-login-url" type="text" inputmode="url" class="text-input" maxlength="16384" autocomplete="off" autocapitalize="none" spellcheck="false" placeholder="粘贴终端中的完整 https://… 授权链接" required><button class="button primary" type="submit">打开登录页面</button></div><small>可直接粘贴跨行链接，空格与换行会自动清理；请保留 &amp; 等链接符号。点击终端中的授权链接也会在这里打开。</small></form>
      <div class="server-login-toolbar"><span class="server-login-status" role="status">正在检查登录浏览器…</span><div><button type="button" class="text-button" data-login-scale disabled>原始大小</button><button type="button" class="text-button" data-login-reconnect hidden>重新连接画面</button></div></div>
      <div class="server-login-screen" aria-label="服务器上的 Claude 官方登录页面" hidden></div>
      <div class="server-login-controls" hidden><details class="server-login-input"><summary>手机输入 / 粘贴文字</summary><p>先点选远程画面里的输入框，再在这里输入并发送。</p><div><input class="text-input" data-login-text type="password" maxlength="2048" autocomplete="off" autocapitalize="none" spellcheck="false" aria-label="要输入到远程浏览器的文字"><button type="button" class="button secondary small" data-login-type>发送文字</button></div><div class="server-login-keys"><button type="button" class="text-button" data-login-key="Tab">Tab 下一项</button><button type="button" class="text-button" data-login-key="Return">Enter 确认</button><button type="button" class="text-button" data-login-key="BackSpace">退格</button><button type="button" class="text-button" data-login-select>全选</button></div></details>
      <div class="server-login-code"><label class="field-label" for="server-login-code">授权码</label><p>若官方页面显示授权码，在远程画面里点「复制」；授权码会出现在这里。点击填入后，回到${this.terminalLabel}点击「提交授权码」或按 Enter。</p><div><input id="server-login-code" class="text-input" type="password" maxlength="4096" autocomplete="off" autocapitalize="none" spellcheck="false" placeholder="等待从远程页面复制，或手动粘贴"><button type="button" class="button primary small" data-login-fill>填入${this.terminalLabel}</button></div><small>仅在${this.terminalLabel}正在等待授权码时填入；若已自动登录成功，可直接关闭此窗口。</small></div></div>`.replaceAll('id="server-login-', `id="${this.domPrefix}-`).replaceAll('for="server-login-', `for="${this.domPrefix}-`);
    document.body.append(dialog);
    const input = dialog.querySelector(this.selector('url'));
    input.value = value || this.url;
    dialog.querySelector('[data-login-close]').addEventListener('click', () => this.close());
    dialog.addEventListener('cancel', (event) => { event.preventDefault(); this.close(); });
    dialog.querySelector('form').addEventListener('submit', (event) => { event.preventDefault(); this.start(input.value); });
    dialog.querySelector('[data-login-reconnect]').addEventListener('click', () => this.connect(generation, id));
    dialog.querySelector('[data-login-scale]').addEventListener('click', (event) => {
      if (!this.rfb) return;
      this.rfb.scaleViewport = !this.rfb.scaleViewport;
      event.currentTarget.textContent = this.rfb.scaleViewport ? '原始大小' : '适应屏幕';
    });
    dialog.querySelector('[data-login-type]').addEventListener('click', () => {
      if (!this.connected) return;
      const field = dialog.querySelector('[data-login-text]');
      const text = field.value;
      field.value = '';
      this.rfb.focus();
      for (const character of text) {
        const point = character.codePointAt(0);
        if (point >= 32 && point !== 127) this.rfb.sendKey(point < 256 ? point : 0x01000000 | point);
      }
    });
    dialog.querySelectorAll('[data-login-key]').forEach((button) => button.addEventListener('click', () => {
      if (!this.connected) return;
      const keys = { Tab: 0xff09, Return: 0xff0d, BackSpace: 0xff08 };
      this.rfb.focus();
      this.rfb.sendKey(keys[button.dataset.loginKey]);
    }));
    dialog.querySelector('[data-login-select]').addEventListener('click', () => {
      if (!this.connected) return;
      this.rfb.focus();
      this.rfb.sendKey(0xffe3, 'ControlLeft', true);
      this.rfb.sendKey(97, 'KeyA');
      this.rfb.sendKey(0xffe3, 'ControlLeft', false);
    });
    dialog.querySelector('[data-login-fill]').addEventListener('click', async () => {
      const field = dialog.querySelector(this.selector('code'));
      const code = field.value.trim();
      if (!code || code.length > 4096 || /[\s\u0000-\u001f\u007f]/.test(code)) {
        this.toast('请输入官方页面给出的单行授权码。', true);
        return;
      }
      if (this.fillTerminal(id, this.terminalGeneration, code)) {
        field.value = '';
        await this.close();
      }
    });
    const codeTools = document.createElement('div');
    codeTools.className = 'server-login-keys';
    codeTools.innerHTML = '<button type="button" class="text-button" data-code-reveal>显示授权码</button><button type="button" class="text-button" data-code-copy>复制到本机</button>';
    dialog.querySelector('.server-login-code').append(codeTools);
    codeTools.querySelector('[data-code-reveal]').addEventListener('click', (event) => {
      const field = dialog.querySelector(this.selector('code'));
      field.type = field.type === 'password' ? 'text' : 'password';
      event.currentTarget.textContent = field.type === 'password' ? '显示授权码' : '隐藏授权码';
    });
    codeTools.querySelector('[data-code-copy]').addEventListener('click', async () => {
      const text = dialog.querySelector(this.selector('code')).value;
      if (!text) return;
      try { await navigator.clipboard.writeText(text); this.toast('授权码已复制。'); }
      catch { this.toast('无法写入本机剪贴板，可点击「显示授权码」后手动复制。', true); }
    });
    dialog.showModal();
    input.focus();
    try {
      const status = await this.enqueue(() => this.api(this.path(id)));
      if (!this.current(generation, id)) return;
      if (status.active) await this.connect(generation, id);
      else if (value || this.url) await this.start(input.value);
      else this.status('请等待终端显示登录链接，再粘贴到上方。');
    } catch (error) { if (this.current(generation, id)) this.status(error.message, true); }
  }

  current(generation, id) {
    const context = this.context();
    return this.dialog && generation === this.generation && id === this.workspaceId && context.workspaceId === id && context.authenticated;
  }

  status(text, error = false) {
    if (!this.dialog) return;
    const status = this.dialog.querySelector('.server-login-status');
    status.textContent = text;
    status.classList.toggle('error', error);
  }

  async start(value) {
    if (!this.dialog || this.starting) return;
    if (/Paste\s+code\s+here|Browser\s+didn.t\s+open|Welcome\s+to\s+Claude/i.test(value)) {
      this.status('请只复制授权链接，不要包含终端提示文字。', true); return;
    }
    const normalized = normalizeClaudeLoginUrl(value);
    const error = loginUrlError(normalized);
    if (error) { this.status(error, true); return; }
    this.dialog.querySelector(this.selector('url')).value = normalized;
    const id = this.workspaceId;
    const generation = this.generation;
    this.starting = true;
    const button = this.dialog.querySelector('form button');
    button.disabled = true;
    this.status(value !== normalized ? '已清理复制时的空格和换行，正在启动服务器浏览器…' : '正在启动服务器浏览器，请稍候…');
    try {
      await this.enqueue(() => this.api(this.path(id), { method: 'POST', body: { url: normalized } }));
      if (this.current(generation, id)) {
        this.dialog.querySelector(this.selector('url')).value = '';
        await this.connect(generation, id);
      }
    } catch (error) { if (this.current(generation, id)) this.status(error.message, true); }
    finally {
      if (this.current(generation, id)) { this.starting = false; button.disabled = false; }
    }
  }

  async connect(generation, id) {
    if (!this.current(generation, id)) return;
    let RFB;
    try { ({ default: RFB } = await import('@novnc/novnc')); }
    catch { if (this.current(generation, id)) this.status('登录画面组件加载失败，请刷新网页重试。', true); return; }
    if (!this.current(generation, id)) return;
    this.disconnect();
    const dialog = this.dialog;
    const screen = dialog.querySelector('.server-login-screen');
    screen.hidden = false;
    screen.replaceChildren();
    dialog.querySelector('[data-login-reconnect]').hidden = true;
    this.status('正在连接服务器浏览器画面…');
    const socket = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}${this.path(id, '/display')}`);
    this.socket = socket;
    socket.binaryType = 'arraybuffer';
    socket.addEventListener('close', (event) => {
      if (!this.current(generation, id) || this.socket !== socket) return;
      if ([4401, 4403].includes(event.code) && this.onAuthExpired) { this.onAuthExpired(); return; }
      const messages = { 4403: '网页登录已过期，请重新登录工作台。', 4409: '登录画面正在被另一个窗口控制，请先关闭另一个窗口。' };
      this.status(messages[event.code] || '登录画面已断开。可重连；临时浏览器到期后需要重新打开授权链接。', true);
      this.connected = false;
      dialog.querySelector('[data-login-reconnect]').hidden = false;
    });
    socket.addEventListener('error', () => {
      if (this.current(generation, id) && this.socket === socket) this.status('无法连接登录画面，请检查服务状态后重连。', true);
    });
    socket.onopen = () => {
      if (!this.current(generation, id) || this.socket !== socket) { socket.close(); return; }
      socket.send(JSON.stringify({ type: 'auth', csrfToken: this.context().csrf }));
      // noVNC 1.7 attaches a pre-opened channel synchronously, before the next
      // network message; the backend sends only binary RFB after this auth frame.
      let rfb;
      try { rfb = new RFB(screen, socket, { shared: false }); }
      catch {
        socket.close();
        this.status('登录画面初始化失败，请刷新网页后重试。', true);
        return;
      }
      this.rfb = rfb;
      // Keep a visible marker when the remote server sends an empty cursor.
      rfb.showDotCursor = true;
      rfb.scaleViewport = true;
      rfb.resizeSession = false;
      rfb.background = '#f0eee7';
      rfb.addEventListener('connect', () => {
        if (!this.current(generation, id) || this.rfb !== rfb) return;
        this.connected = true;
        dialog.querySelector('form').hidden = true;
        dialog.querySelector('.server-login-controls').hidden = false;
        dialog.querySelector('[data-login-scale]').disabled = false;
        this.status('已连接服务器浏览器。请在画面中的官方页面操作。');
      });
      rfb.addEventListener('clipboard', (event) => {
        if (!this.current(generation, id) || this.rfb !== rfb) return;
        const text = event.detail.text;
        if (typeof text === 'string' && text.length <= 4096) dialog.querySelector(this.selector('code')).value = text;
      });
      rfb.addEventListener('credentialsrequired', () => {
        if (!this.current(generation, id) || this.rfb !== rfb) return;
        this.status('登录浏览器服务配置异常，请检查服务器日志。', true);
        rfb.disconnect();
      });
    };
  }

  disconnect() {
    this.connected = false;
    const socket = this.socket;
    const rfb = this.rfb;
    this.socket = null;
    this.rfb = null;
    rfb?.disconnect();
    socket?.close();
  }

  async close({ quiet = false } = {}) {
    if (!this.dialog) return;
    const id = this.workspaceId;
    ++this.generation;
    this.disconnect();
    this.dialog.querySelectorAll('input').forEach((input) => { input.value = ''; });
    this.dialog.close();
    this.dialog.remove();
    this.dialog = null;
    this.workspaceId = null;
    this.starting = false;
    try { await this.enqueue(() => this.api(this.path(id, '/stop'), { method: 'POST' })); }
    catch (error) { if (!quiet) this.toast(`画面已关闭；服务器浏览器未能立即停止：${error.message}`, true); }
  }
}
