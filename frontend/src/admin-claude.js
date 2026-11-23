import { ServerLoginBrowser, isClaudeAuthLink, isClaudeLoginUrl } from './login-browser.js';

const CONTEXT = 'administrator-claude-authorization';
const titles = { idle: '未开始', connecting: '正在连接', connected: '授权终端已连接', disconnected: '连接已断开', exited: '授权流程已结束', error: '连接未完成' };

async function loadTerminal() {
  const [{ Terminal }, { FitAddon }, { WebLinksAddon }] = await Promise.all([
    import('@xterm/xterm'), import('@xterm/addon-fit'), import('@xterm/addon-web-links'),
  ]);
  return { Terminal, FitAddon, WebLinksAddon };
}

/** An administrator-only authorization context, independent of any workspace. */
export class AdminClaudePanel {
  constructor({ request, session, expire, load = loadTerminal, Browser = ServerLoginBrowser,
    socket = (url) => new WebSocket(url), observe = (fn) => new ResizeObserver(fn),
    confirm = (message) => window.confirm(message), namespace = 'claude', solo = false }) {
    if (!['claude', 'usage-auth'].includes(namespace)) throw new Error('Unsupported administrator authorization context');
    Object.assign(this, { request, session, expire, load, socket, observe, confirm, solo });
    this.namespace = namespace;
    this.usageAuthorization = namespace === 'usage-auth';
    this.prefix = this.usageAuthorization ? 'admin-usage-auth' : 'admin-claude';
    this.contextId = this.usageAuthorization ? 'administrator-usage-authorization' : CONTEXT;
    this.generation = 0;
    this.state = 'idle';
    this.locked = false;
    this.busy = false;
    this.status = null;
    this.importPolls = 0;
    this.browser = new Browser({
      api: (path, options) => this.request(path.slice('/api/admin/'.length), options),
      endpoint: (_id, suffix = '') => `/api/admin/${this.namespace}/login-browser${suffix}`,
      toast: (message, error) => this.message(message, error),
      terminalLabel: solo ? '登录终端' : this.usageAuthorization ? '额度查询授权终端' : '授权终端',
      domPrefix: this.usageAuthorization ? 'usage-auth-browser' : 'server-login',
      contextHint: solo ? '请先点击「连接 Claude」，等待官方登录页面。' : '请先点击「开始授权」，等待授权终端显示官方登录链接。',
      onAuthExpired: () => this.expire(),
      context: () => ({ workspaceId: this.contextId, authenticated: Boolean(this.host && this.session().authenticated), csrf: this.session().csrf,
        terminalConnected: this.state === 'connected', terminalGeneration: this.generation }),
      fillTerminal: (id, generation, code) => this.fillCode(id, generation, code),
    });
  }

  wording(text) {
    if (!this.solo) return text;
    return String(text).replaceAll('管理员在这里完成一次官方授权，所有工作区共用。用户直接使用中文聊天，无需登录 Claude。', '连接一次你的 Claude，之后即可在所有工作区聊天。')
      .replaceAll('授权页面从服务器访问。等待终端显示链接后，点击上方按钮，在服务器浏览器内完成官方登录。', '官方登录页面会自动打开。若未出现，请点击「在服务器浏览器登录」。')
      .replaceAll('使用当前共享 Claude 账号，再完成一次官方登录，用于读取官方账号总额度。账号与授权资料仅管理员可见。', '使用同一个 Claude 账号完成登录，显示官方 5 小时与每周额度。')
      .replaceAll('管理员专用', '可选').replaceAll('共享 Claude 账号', '用于聊天的 Claude 账号')
      .replaceAll('管理员', '你').replaceAll('统一授权', 'Claude 登录').replaceAll('授权终端', '登录终端')
      .replaceAll('重新授权', '重新连接 Claude').replaceAll('开始授权', '连接 Claude').replaceAll('取消授权', '取消连接');
  }

  maybeOpenBrowser(generation) {
    if (!this.solo || this.autoBrowserOpened || !this.current(generation) || this.state !== 'connected' || !isClaudeLoginUrl(this.browser.url)) return;
    this.autoBrowserOpened = true;
    this.browser.show(this.browser.url).catch((error) => { if (this.current(generation)) this.message(error.message, true); });
  }

  node(id) { return this.host?.querySelector(`#${id.replace(/^admin-claude/, this.prefix)}`); }
  current(generation) { return Boolean(this.host && generation === this.generation && this.session().authenticated); }

  mount(host) {
    this.host = host;
    host.innerHTML = this.wording(`<div class="policy-heading"><div><p class="policy-eyebrow">${this.usageAuthorization ? '管理员专用' : '统一授权'}</p><h2 id="admin-claude-heading">${this.usageAuthorization ? '官方额度查询授权' : 'Claude 账号'}</h2></div><span class="policy-state" id="admin-claude-state">正在检查</span></div>
      <p class="card-description">${this.usageAuthorization ? '使用当前共享 Claude 账号，再完成一次官方登录，用于读取官方账号总额度。账号与授权资料仅管理员可见。' : '管理员在这里完成一次官方授权，所有工作区共用。用户直接使用中文聊天，无需登录 Claude。'}</p>
      <p class="policy-description">${this.usageAuthorization ? '登录后请核对账号，并明确确认与现有共享 Claude 账号一致；系统不会自动核实两份授权属于同一个账号。此处管理额度查询授权。' : '重新授权会停止正在运行的任务，文件和对话记录保留。授权过期或被撤销后，请在这里重新授权。'}</p>
      <p id="admin-claude-status" class="claude-account-status" role="status">正在检查服务器保存的授权状态…</p>
      <div class="claude-account-actions"><button id="admin-claude-connect" class="primary-button" type="button" disabled>开始授权</button><button id="admin-claude-refresh" class="secondary-button" type="button">刷新授权状态</button><button id="admin-claude-stop" class="secondary-button" type="button" disabled>取消授权</button></div>
      ${this.usageAuthorization ? '<section class="usage-auth-association" id="admin-claude-association" hidden><strong>登录完成，请核对账号</strong><p>本次登录账号：<span id="admin-claude-account-email"></span></p><label><input type="checkbox" id="admin-claude-same-account">我已核对，本次登录使用的就是现有共享 Claude 账号。</label><button id="admin-claude-confirm-association" class="primary-button" type="button" disabled>确认并关联额度查询授权</button></section>' : ''}
      <section id="admin-claude-terminal-wrap" class="admin-claude-terminal-wrap" aria-label="管理员 Claude 授权终端" hidden><div class="admin-claude-terminal-toolbar"><span id="admin-claude-terminal-state">未开始</span><button id="admin-claude-browser" class="button secondary small" type="button" disabled>在服务器浏览器登录</button><button id="admin-claude-submit-code" class="button primary small" type="button" hidden>提交授权码</button></div><div id="admin-claude-terminal" class="admin-claude-terminal"></div><p class="admin-claude-terminal-note">授权页面从服务器访问。等待终端显示链接后，点击上方按钮，在服务器浏览器内完成官方登录。</p></section>
      <p id="admin-claude-message" class="admin-message" role="status" aria-live="polite" hidden></p>`.replaceAll('id="admin-claude', `id="${this.prefix}`));
    this.node('admin-claude-connect').addEventListener('click', () => this.connect());
    this.node('admin-claude-refresh').addEventListener('click', () => this.refresh());
    this.node('admin-claude-stop').addEventListener('click', () => this.stop());
    this.node('admin-claude-browser').addEventListener('click', () => this.browser.show().catch((error) => this.message(error.message, true)));
    this.node('admin-claude-submit-code').addEventListener('click', () => this.submitCode());
    this.node('admin-claude-same-account')?.addEventListener('change', () => this.controls());
    this.node('admin-claude-confirm-association')?.addEventListener('click', () => this.confirmAssociation());
    this.controls();
    return this.refresh();
  }

  message(text, error = false) {
    const node = this.node('admin-claude-message');
    if (!node) return;
    node.textContent = this.wording(text); node.hidden = !text; node.dataset.kind = error ? 'error' : 'success';
  }

  controls() {
    if (!this.host) return;
    const blocked = this.locked || this.busy;
    const badge = this.node('admin-claude-state');
    badge.textContent = this.wording(!this.status ? '状态待确认' : this.status.importing ? '正在保存授权' : this.status.error ? '授权保存未完成' : this.status.pendingConfirmation ? '等待管理员确认' : this.status.authenticated ? this.usageAuthorization ? '已关联查询授权' : '已保存授权' : '尚未授权');
    badge.dataset.allowed = String(this.status?.authenticated === true && !this.status.importing && !this.status.error && !this.status.pendingConfirmation);
    const connect = this.node('admin-claude-connect');
    connect.textContent = this.wording(this.status?.importing ? '正在保存授权…' : this.state === 'connecting' ? '正在连接…' : this.status?.terminalActive || this.state === 'disconnected' ? '重新连接授权终端' : this.status?.authenticated ? '重新授权' : '开始授权');
    connect.disabled = blocked || !this.status || this.status.importing || this.status.pendingConfirmation || this.state === 'connecting' || this.state === 'connected';
    this.node('admin-claude-refresh').disabled = blocked || this.refreshing;
    this.node('admin-claude-stop').disabled = blocked || !this.status || this.status.importing || !(this.status.pendingConfirmation || this.status.terminalActive || ['connecting', 'connected', 'disconnected'].includes(this.state));
    this.node('admin-claude-browser').disabled = blocked || !this.status || this.status.importing || this.state !== 'connected';
    const submit = this.node('admin-claude-submit-code');
    submit.hidden = !this.pendingCode;
    submit.disabled = blocked || !this.status || this.status.importing || this.state !== 'connected';
    this.node('admin-claude-terminal-state').textContent = this.wording(titles[this.state]);
    if (this.usageAuthorization) {
      const pending = Boolean(this.status?.pendingConfirmation);
      this.node('admin-claude-association').hidden = !pending;
      this.node('admin-claude-account-email').textContent = pending ? this.status.accountEmail || '官方未提供邮箱，请对照刚才登录的账号核对。' : '';
      this.node('admin-claude-same-account').disabled = blocked || !pending || this.status.importing || Boolean(this.status.error);
      this.node('admin-claude-confirm-association').disabled = blocked || !pending || this.status.importing || Boolean(this.status.error) || !this.node('admin-claude-same-account').checked;
    }
  }

  async refresh({ manual = true } = {}) {
    if (!this.host || this.refreshing || !this.session().authenticated) return;
    if (manual) this.importPolls = 0;
    const generation = this.generation;
    this.refreshing = true; this.controls();
    try {
      const status = await this.request(`${this.namespace}/status`);
      if (!this.current(generation)) return;
      if (typeof status?.authenticated !== 'boolean' || typeof status?.terminalActive !== 'boolean' ||
        (status.importing !== undefined && typeof status.importing !== 'boolean') ||
        (status.error != null && (typeof status.error !== 'string' || status.error.length > 2048)) ||
        (this.usageAuthorization && (typeof status.pendingConfirmation !== 'boolean' ||
          (status.accountEmail != null && (typeof status.accountEmail !== 'string' || status.accountEmail.length > 512))))) throw new Error('服务器未确认授权状态，请刷新后重试。');
      const previouslyImporting = this.status?.importing;
      if (this.usageAuthorization && (this.status?.pendingConfirmation !== status.pendingConfirmation || this.status?.accountEmail !== status.accountEmail)) this.node('admin-claude-same-account').checked = false;
      this.status = { authenticated: status.authenticated, terminalActive: status.terminalActive, importing: status.importing === true, error: status.error || '',
        ...(this.usageAuthorization ? { pendingConfirmation: status.pendingConfirmation, accountEmail: status.accountEmail || null } : {}) };
      if (this.usageAuthorization && this.status.pendingConfirmation && !this.status.terminalActive) this.state = 'exited';
      if (this.status.importing) this.importPolls++; else this.importPolls = 0;
      const statusNode = this.node('admin-claude-status');
      statusNode.dataset.error = String(Boolean(this.status.error));
      statusNode.textContent = this.wording(this.usageAuthorization ? this.usageStatusText() : this.status.importing
        ? this.importPolls >= 100 ? '服务器仍在保存授权，等待时间较长。请点击「刷新授权状态」继续检查；保存期间不能开始新的授权。' : '正在保存统一授权并更新工作区，请稍候。保存完成后会自动更新状态。'
        : this.status.error || (status.authenticated
          ? '服务器已保存统一授权，所有工作区使用这份授权。实际可用性与额度以 Claude 账号为准。'
          : status.terminalActive ? '官方授权流程进行中。请在授权终端和服务器浏览器中完成登录。' : '服务器尚未保存统一授权。请点击「开始授权」。'));
      if ((previouslyImporting || this.state === 'exited') && !this.status.importing && !this.status.terminalActive) {
        this.state = 'exited'; this.pendingCode = false; this.browser.reset();
        this.message(this.status.error ? '本次授权未能完成保存，请按上方提示处理后重新授权。' : this.usageAuthorization
          ? this.status.pendingConfirmation ? '官方登录已完成，请核对下方账号并确认关联。' : status.authenticated ? '额度查询授权已关联，可刷新官方额度。' : '本次额度查询授权未保存，请重新开始。'
          : status.authenticated ? '统一授权已保存，所有工作区可以使用。' : '本次授权未保存，请重新开始授权。', Boolean(this.status.error || (!status.authenticated && !this.status.pendingConfirmation)));
      }
    } catch (error) {
      if (this.current(generation)) { this.status = null; this.node('admin-claude-status').textContent = '暂时无法确认授权状态，请刷新后重试。'; this.message(error.message, true); }
    } finally {
      this.refreshing = false;
      if (this.current(generation)) {
        this.controls();
        if (this.status?.importing) { if (this.importPolls < 100) this.scheduleRefresh(3000); }
        else if (!this.status?.error && (this.status?.terminalActive || this.state === 'connected')) this.scheduleRefresh(4000);
      }
    }
  }

  usageStatusText() {
    if (this.status.importing) return this.importPolls >= 100 ? '服务器仍在保存额度查询授权，请点击「刷新授权状态」继续检查。' : '正在保存额度查询登录资料，请稍候。保存后仍需管理员核对并确认关联。';
    if (this.status.error) return this.status.error;
    if (this.status.pendingConfirmation) return '官方登录已完成，尚未关联。请核对显示的账号，再明确确认与现有共享 Claude 账号一致。';
    if (this.status.authenticated) return '额度查询授权已由管理员确认关联。可在官方账号总额度区域刷新查询；关联确认不代表系统已自动核实账号身份。';
    return this.status.terminalActive ? '额度查询官方登录进行中。请在此授权终端和服务器浏览器中完成。' : '尚未关联额度查询授权。点击「开始授权」，使用现有共享 Claude 账号完成官方登录。';
  }

  async confirmAssociation() {
    if (!this.usageAuthorization || this.locked || this.busy || !this.host || !this.session().authenticated || !this.status?.pendingConfirmation || this.status.importing || this.status.error || !this.node('admin-claude-same-account').checked) return;
    const generation = this.generation;
    this.busy = true; this.controls();
    try {
      await this.request(`${this.namespace}/confirm`, { method: 'POST', body: { confirmation: 'CONFIRM_SAME_CLAUDE_ACCOUNT' } });
      if (!this.current(generation)) return;
      this.node('admin-claude-same-account').checked = false;
      await this.refresh();
      if (this.current(generation) && this.status?.authenticated && !this.status.pendingConfirmation && !this.status.error) this.message('已由管理员确认关联，可刷新官方账号总额度。');
    } catch (error) { if (this.current(generation)) this.message(error.message, true); }
    finally { if (this.current(generation)) { this.busy = false; this.controls(); } }
  }

  scheduleRefresh(delay = 750) {
    if (this.refreshTimer || !this.host) return;
    this.refreshTimer = setTimeout(() => { this.refreshTimer = null; this.refresh({ manual: false }); }, delay);
  }

  async connect() {
    if (this.locked || this.busy || !this.host || !this.status || this.status.importing || this.status.pendingConfirmation || !this.session().authenticated || ['connecting', 'connected'].includes(this.state)) return;
    this.busy = true; this.controls();
    const before = this.generation;
    if (this.status.authenticated && !this.status.terminalActive && !(await this.confirm(this.wording(this.usageAuthorization ? '重新进行额度查询授权后，需要再次确认使用现有共享 Claude 账号。继续重新授权？' : '重新授权会停止所有工作区正在运行的任务。文件和对话记录会保留。继续重新授权？')))) { this.busy = false; this.controls(); return; }
    if (!this.current(before)) return;
    const newLogin = !this.status.terminalActive;
    this.disconnect();
    const generation = this.generation;
    this.autoBrowserOpened = false;
    this.state = 'connecting'; this.message(''); this.controls();
    this.node('admin-claude-terminal-wrap').hidden = false;
    try {
      if (this.usageAuthorization && newLogin) {
        await this.request(`${this.namespace}/prepare`, { method: 'POST', body: { confirmation: 'AUTHORIZE_USAGE_FOR_SHARED_ACCOUNT' } });
        if (!this.current(generation)) return;
        this.status = { ...this.status, authenticated: false, pendingConfirmation: false, accountEmail: null };
        this.controls();
      }
      const { Terminal, FitAddon, WebLinksAddon } = await this.load();
      if (!this.current(generation)) return;
      this.term = new Terminal({ cursorBlink: true, fontFamily: '"Cascadia Code", Consolas, monospace', fontSize: 13, lineHeight: 1.3, scrollback: 3000, minimumContrastRatio: 7,
        theme: { background: '#faf9f5', foreground: '#171717', cursor: '#171717', cursorAccent: '#faf9f5',
          selectionBackground: '#efd0c0', selectionForeground: '#171717',
          black: '#171717', red: '#171717', green: '#171717', yellow: '#171717',
          blue: '#171717', magenta: '#171717', cyan: '#171717', white: '#171717',
          brightBlack: '#171717', brightRed: '#171717', brightGreen: '#171717', brightYellow: '#171717',
          brightBlue: '#171717', brightMagenta: '#171717', brightCyan: '#171717', brightWhite: '#171717' } });
      this.fit = new FitAddon(); this.term.loadAddon(this.fit);
      const openLink = (event, uri) => {
        event.preventDefault();
        if (isClaudeAuthLink(uri)) this.browser.show(uri).catch((error) => this.message(error.message, true));
        else this.message('请在服务器浏览器内打开官方登录页面。');
      };
      this.term.options.linkHandler = { activate: openLink };
      this.term.loadAddon(new WebLinksAddon(openLink));
      const host = this.node('admin-claude-terminal');
      this.term.open(host);
      this.term.onData((data) => { if (this.send({ type: 'input', data })) { this.pendingCode = false; this.controls(); } });
      this.term.onResize(({ cols, rows }) => this.send({ type: 'resize', cols, rows }));
      this.observer = this.observe(() => { if (host.clientWidth && host.clientHeight) this.fit?.fit(); }); this.observer.observe(host);
      const ws = this.socket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/api/admin/${this.namespace}/terminal`);
      this.ws = ws;
      ws.onopen = () => { if (this.current(generation) && this.ws === ws) ws.send(JSON.stringify({ type: 'auth', csrfToken: this.session().csrf })); else ws.close(); };
      ws.onmessage = (event) => {
        if (!this.current(generation) || this.ws !== ws) return;
        let message; try { message = JSON.parse(event.data); } catch { return; }
        if (message.type === 'ready') {
          this.status ||= { authenticated: false, terminalActive: false };
          this.state = 'connected'; this.status.terminalActive = true; this.fit.fit();
          this.send({ type: 'resize', cols: this.term.cols, rows: this.term.rows }); this.term.focus(); this.scheduleRefresh(); this.maybeOpenBrowser(generation);
        } else if (message.type === 'output' && typeof message.data === 'string') {
          this.term.write(message.data); this.browser.observeOutput(message.data); this.scheduleRefresh(); this.maybeOpenBrowser(generation);
        } else if (message.type === 'exit') {
          this.state = 'exited'; this.pendingCode = false;
          if (this.status) this.status.terminalActive = false;
          this.browser.reset(); this.scheduleRefresh();
          this.message('授权流程已结束，正在重新检查服务器保存的授权状态。');
        } else if (message.type === 'error') {
          this.state = 'error'; this.message('授权连接未完成，请刷新状态并检查服务器后重试。', true); this.scheduleRefresh();
        }
        this.controls();
      };
      ws.onclose = (event) => {
        if (!this.current(generation) || this.ws !== ws) return;
        this.ws = null; this.pendingCode = false; this.browser.reset();
        if ([4001, 4003, 4401, 4403].includes(event.code)) { this.expire(); return; }
        if (!['exited', 'error'].includes(this.state)) { this.state = 'disconnected'; this.message('授权终端已断开，可重新连接后继续。', true); }
        this.scheduleRefresh(); this.controls();
      };
      ws.onerror = () => { if (this.current(generation) && this.ws === ws) this.message('无法连接授权终端，请检查网络后重试。', true); };
    } catch (error) {
      if (this.current(generation)) { this.state = 'error'; this.message(this.usageAuthorization ? '额度查询授权未能开始，请刷新授权状态并检查服务器后重试。' : '授权终端加载失败，请刷新页面后重试。', true); }
    } finally { if (this.current(generation)) { this.busy = false; this.controls(); } }
  }

  send(message) {
    if (!this.host || !this.session().authenticated || !this.status || this.status.importing || this.state !== 'connected' || this.ws?.readyState !== 1) return false;
    this.ws.send(JSON.stringify(message)); return true;
  }

  fillCode(id, generation, code) {
    if (id !== this.contextId || generation !== this.generation || typeof code !== 'string' || !code || code.length > 4096 || /[\s\x00-\x1f\x7f]/.test(code) || !this.send({ type: 'input', data: code })) {
      this.message('授权终端已变化或授权码无效，请回到生成链接的授权终端重试。', true); return false;
    }
    this.pendingCode = true; this.controls(); this.term?.focus();
    this.message('授权码已填入。请点击「提交授权码」，然后等待服务器确认授权状态。'); return true;
  }

  submitCode() {
    if (this.locked || this.busy || !this.pendingCode) return;
    if (this.send({ type: 'input', data: '\r' })) { this.pendingCode = false; this.controls(); this.message('授权码已提交，请等待服务器确认。'); }
  }

  async stop() {
    if (this.locked || this.busy || !this.host || !this.status || this.status.importing || !this.session().authenticated) return;
    const generation = this.generation; this.busy = true; this.controls();
    try {
      await this.request(`${this.namespace}/terminal/stop`, { method: 'POST' });
      if (!this.current(generation)) return;
      this.disconnect(); this.state = 'idle'; this.busy = false;
      this.node('admin-claude-terminal-wrap').hidden = true;
      this.message(this.usageAuthorization ? '本次额度查询授权流程已取消，关联状态以服务器显示为准。' : '本次授权流程已取消。已有的统一授权仍以服务器状态为准。');
      await this.refresh();
    } catch (error) { if (this.current(generation)) this.message(error.message, true); }
    finally { if (this.host) { this.busy = false; this.controls(); } }
  }

  disconnect() {
    this.generation++;
    clearTimeout(this.refreshTimer); this.refreshTimer = null;
    this.browser.reset(); this.pendingCode = false;
    const ws = this.ws; this.ws = null; ws?.close();
    this.observer?.disconnect(); this.observer = null;
    this.term?.dispose(); this.term = null; this.fit = null;
    this.node('admin-claude-terminal')?.replaceChildren();
  }

  unmount() {
    this.disconnect();
    if (this.usageAuthorization && this.host) { this.node('admin-claude-account-email').textContent = ''; this.node('admin-claude-same-account').checked = false; this.node('admin-claude-association').hidden = true; }
    this.host = null; this.status = null; this.busy = false;
  }
}
