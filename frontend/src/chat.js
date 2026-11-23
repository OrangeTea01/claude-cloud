import { renderMarkdown } from './markdown.js';
import { ChatAttachments } from './attachments.js';
import { conversationMode, CHAT_ATTACHMENT_HINT, CHAT_MODE_HELP } from './conversation-mode.js';
import { timeText, quotaUnit, quotaBlockReason, validPercentage } from './quota-view.js';

const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (s) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[s]));

const phases = {
  idle: '可以开始对话', starting: '正在连接 Claude', thinking: 'Claude 正在处理',
  awaiting_permission: '等待你的确认', switching_model: '正在切换模型…', paused_quota: '任务已暂停', error: '需要处理', stopped: '会话已停止',
};

/** HTTP polling keeps the structured conversation independent of terminal ANSI. */
export class BilingualChat {
  constructor({ api, toast, modal, selected, isAuthenticated, isBusy, onState, onStopped, attention = null, getMode = () => 'multiuser' }) {
    Object.assign(this, { api, toast, modal, selected, isAuthenticated, isBusy, onState, onStopped, attention, getMode });
    this.generation = 0;
    this.drafts = new Map();
    this.model = null;
    this.workspaceId = null;
    this.actions = new Set();
    this.attachments = new ChatAttachments(this);
  }

  unmount({ forgetDrafts = false, forgetAttachments = forgetDrafts } = {}) {
    clearTimeout(this.timer);
    if (this.host && this.clickHandler) this.host.removeEventListener('click', this.clickHandler);
    this.generation++;
    this.workspaceId = null;
    this.host = null;
    this.model = null;
    this.actions.clear();
    this.draftRevision = 0;
    this.attachments.clearPreviews();
    if (forgetDrafts) this.drafts.clear();
    if (forgetAttachments) this.attachments.reset();
  }

  mount(host, workspaceId) {
    this.unmount();
    this.host = host;
    this.workspaceId = workspaceId;
    this.attention?.beginView(workspaceId);
    this.model = null;
    this.lastRender = '';
    this.lastPermissions = '';
    this.previousEpochs = new Set();
    this.failure = '';
    this.pendingModel = null;
    this.modelNotice = '';
    this.modelNoticeError = false;
    this.modelOptionsSignature = '';
    host.innerHTML = `<div class="chat-header"><div><span class="chat-symbol" aria-hidden="true">✳</span><h2>用中文，和 Claude 一起做。</h2></div><button class="chat-refresh-button" id="chat-refresh" type="button" data-chat-action="refresh" title="重新读取当前对话">刷新对话</button></div>
      <div class="chat-model-bar"><div class="chat-model-picker"><label for="chat-model">Claude 模型</label><select id="chat-model" aria-describedby="chat-model-hint chat-model-feedback" disabled><option value="">正在读取…</option></select></div><div class="chat-model-copy"><p id="chat-model-hint">切换后用于后续消息；可用模型由管理员设置。</p><p id="chat-active-model" hidden></p><p id="chat-model-feedback" role="status" hidden></p></div></div>
      <div class="chat-transcript" id="chat-transcript" tabindex="0" aria-label="对话记录"><div id="chat-messages"><div class="chat-loading">正在读取对话…</div></div><div class="chat-running-row" id="chat-running" role="status" hidden><span class="chat-running-mark" aria-hidden="true">✳</span><strong id="chat-running-text"></strong></div><section class="chat-quota-paused" id="chat-quota-paused" role="status" hidden><strong>任务已暂停</strong><p id="chat-quota-explanation"></p><p id="chat-quota-reset"></p><div><button class="button primary small" id="chat-resume" type="button" data-chat-action="resume">继续任务</button><button class="button secondary small" id="chat-quota-refresh" type="button" data-chat-action="quota-refresh">刷新额度</button></div></section></div>
      <div class="chat-permissions" id="chat-permissions" aria-label="待确认的工具操作" hidden></div>
      <div class="chat-notice" id="chat-notice" role="status" hidden></div>
      <form class="chat-composer" id="chat-form"><div class="chat-upload-toolbar"><button class="chat-upload-button" type="button" id="chat-upload" data-chat-action="upload"><span aria-hidden="true">＋</span> 上传文件 / 图片</button><input id="chat-files" type="file" multiple hidden aria-label="选择文件或图片"><span class="chat-upload-hint">每个文件最多 10 MiB · 每条最多 5 个</span></div><div class="chat-attachments" id="chat-attachments" aria-label="待发送附件" aria-live="polite" hidden></div><label class="chat-draft-label" for="chat-input">消息内容</label><textarea id="chat-input" maxlength="16000" rows="3" placeholder="直接告诉 Claude 你想完成什么…也可拖入文件或粘贴图片" autocomplete="off" spellcheck="false"></textarea>
        <div class="chat-composer-footer"><span id="chat-phase" role="status">正在连接工作区</span><div><span class="chat-shortcut">Ctrl / ⌘ + Enter</span><button class="button primary small" id="chat-send" type="submit" disabled>发送</button></div></div></form>
      <div class="chat-footnote"><span>直接与 Claude 对话 · 支持 Markdown</span><button class="text-button" type="button" data-chat-action="help">使用说明</button></div>`;
    const input = host.querySelector('#chat-input');
    input.value = this.drafts.get(workspaceId) || '';
    input.addEventListener('input', () => { this.drafts.set(workspaceId, input.value); this.draftRevision++; this.controls(); });
    input.addEventListener('keydown', (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key === 'Enter' && !event.isComposing) {
        event.preventDefault();
        this.send();
      }
    });
    host.querySelector('#chat-form').addEventListener('submit', (event) => { event.preventDefault(); this.send(); });
    host.querySelector('#chat-model').addEventListener('change', (event) => this.changeModel(event.target.value));
    host.addEventListener('click', this.clickHandler = (event) => {
      const button = event.target.closest('[data-chat-action]');
      if (!button || button.disabled) return;
      const action = button.dataset.chatAction;
      if (action === 'help') this.help();
      else if (action === 'allow' || action === 'deny') this.permission(button.dataset.id, action === 'allow');
      else if (action === 'refresh') this.refreshNow();
      else if (action === 'resume') this.resume();
      else if (action === 'quota-refresh') this.refreshQuota();
      else if (action === 'upload') host.querySelector('#chat-files').click();
      else if (action === 'remove-attachment') this.attachments.remove(button.dataset.id);
      else if (action === 'retry-attachment') this.attachments.retry(button.dataset.id);
      else if (action === 'preview-attachment' || action === 'download-attachment') this.attachments.open(button.dataset.message, button.dataset.index, action === 'preview-attachment');
    });
    this.attachments.mount();
    this.renderMode();
    this.poll(this.generation);
  }

  current(id, generation) {
    return this.host?.isConnected && this.workspaceId === id && this.selected()?.id === id && this.generation === generation && this.isAuthenticated();
  }

  endpoint(id = this.workspaceId, suffix = '') {
    return `/api/workspaces/${encodeURIComponent(id)}/chat${suffix}`;
  }

  accept(model, id, generation) {
    if (!this.current(id, generation)) return false;
    // Ignore old poll responses that race an approval/send/stop/model response.
    if (this.model?.epoch && model.epoch && this.model.epoch !== model.epoch) {
      if (this.previousEpochs.has(model.epoch)) return false;
      this.previousEpochs.add(this.model.epoch);
    } else if (this.model && Number.isFinite(model.revision) && model.revision < this.model.revision) return false;
    const completed = this.model?.busy && !model.busy;
    this.model = model;
    this.renderMode();
    this.attention?.observe(id, model.epoch, model.permissions || []);
    this.failure = '';
    this.onState(model, { completed });
    this.render();
    return true;
  }

  async poll(generation) {
    const id = this.workspaceId;
    if (!this.current(id, generation)) return;
    try {
      if (document.visibilityState !== 'hidden') {
        const model = await this.api(this.endpoint(id));
        this.accept(model, id, generation);

      }
    } catch (error) {
      if (this.current(id, generation)) {
        this.failure = error.message;
        this.controls();
        if (!this.model) this.host.querySelector('#chat-messages').innerHTML = '<div class="chat-loading">暂时无法读取对话。请检查连接。<button class="text-button" type="button" data-chat-action="refresh">重新连接</button></div>';
      }
    } finally {
      if (this.current(id, generation)) this.timer = setTimeout(() => this.poll(generation), 1500);
    }
  }

  async refreshNow() {
    if (!this.workspaceId) return;
    const id = this.workspaceId, generation = this.generation;
    try {
      const model = await this.api(this.endpoint(id));
      if (!this.current(id, generation)) return;
      this.accept(model, id, generation);
    } catch (error) { if (this.current(id, generation)) { this.failure = error.message; this.controls(); } }
  }

  render() {
    if (!this.host || !this.model) return;
    const transcript = this.host.querySelector('#chat-transcript');
    const followBottom = !this.lastRender || transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 100;
    const messages = this.model.messages || [];
    const signature = JSON.stringify(messages);
    if (signature !== this.lastRender) {
      const nearBottom = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 100;
      const firstRender = !this.lastRender;
      const expanded = new Set([...transcript.querySelectorAll('details[open]')].map((node) => node.dataset.messageId));
      const scrollTop = transcript.scrollTop;
      this.host.querySelector('#chat-messages').innerHTML = messages.length ? messages.map((message) => {
        const isUser = message.role === 'user';
        const legacyUnsent = ['failed', 'pending'].includes(message.translationStatus);
        const originalOnly = !message.text && Boolean(message.original);
        const displayText = message.text || message.original || '';
        const explanation = isUser ? (message.sent === false || legacyUnsent ? '未发送' : '已发送') : originalOnly ? '历史原文' : '';
        const showOriginal = Boolean(message.original && message.original !== displayText);
        const attachmentMarkup = this.attachments.historyMarkup(message);
        return `<article class="chat-message ${isUser ? 'user-message' : 'assistant-message'}" data-message-id="${esc(message.id)}"><div class="chat-message-author"><span>${isUser ? '你' : '✳ Claude'}</span>${explanation ? `<small>${esc(explanation)}</small>` : ''}</div>${displayText ? `<div class="chat-message-text chat-markdown">${renderMarkdown(displayText)}</div>` : attachmentMarkup ? '' : '<div class="chat-message-text message-placeholder">暂无文本内容。</div>'}${attachmentMarkup}${showOriginal ? `<details class="message-original" data-message-id="${esc(message.id)}" ${expanded.has(message.id) ? 'open' : ''}><summary>查看历史原文</summary><div class="chat-markdown markdown-original">${renderMarkdown(message.original)}</div></details>` : ''}</article>`;
      }).join('') : `<div class="chat-empty"><span class="chat-empty-symbol" aria-hidden="true">✳</span><h3>想法用中文说，工作交给 Claude。</h3><p>描述你想完成的任务，<br>直接和 Claude 一起推进项目。</p><div class="chat-first-use"><strong>从一个具体任务开始</strong><span>描述目标，按需上传文件。Claude 请求执行工具时，会请你确认。</span></div></div>`;
      this.lastRender = signature;
      transcript.scrollTop = firstRender || nearBottom ? transcript.scrollHeight : scrollTop;
    }
    const permissions = this.model.permissions || [];
    const permissionBox = this.host.querySelector('#chat-permissions');
    const permissionSignature = JSON.stringify(permissions);
    if (permissionSignature !== this.lastPermissions) {
      permissionBox.hidden = !permissions.length;
      permissionBox.innerHTML = permissions.map((permission) => `<section class="chat-permission"><div class="permission-heading"><span>需要你的确认</span><strong>${esc(permission.tool)}</strong></div><p>Claude 请求执行下面的操作。请核对原始参数；确认仅对这一次请求生效，5 分钟未确认将拒绝。</p>${permission.title ? `<div class="permission-title">${esc(permission.title)}</div>` : ''}<pre>${esc(JSON.stringify(permission.input, null, 2))}</pre><div class="permission-actions"><button class="button secondary small" type="button" data-chat-action="deny" data-id="${esc(permission.id)}">拒绝</button><button class="button primary small" type="button" data-chat-action="allow" data-id="${esc(permission.id)}">仅允许这一次</button></div></section>`).join('');
      this.lastPermissions = permissionSignature;
    }
    this.controls();
    // Keep the running/paused row in view only while following the latest
    // messages; readers looking through history retain their scroll position.
    if (followBottom) transcript.scrollTop = transcript.scrollHeight;
  }

  controls() {
    if (!this.host?.isConnected) return;
    const workspace = this.selected();
    const activeTerminal = workspace?.terminalActive;
    const solo = this.getMode() === 'solo';
    const sendBusy = this.actions.has('send');
    const modelBusy = this.actions.has('model');
    const modelBlocked = this.model?.modelBlocked || (this.model?.selectedModel === 'fable' && this.model?.fableAllowed === false);
    const busy = this.model?.busy || sendBusy || modelBusy || this.actions.has('stop') || this.actions.has('resume') || this.isBusy();
    const input = this.host.querySelector('#chat-input');
    const quotaReason = quotaBlockReason(this.model?.quota);
    const unavailable = busy || Boolean(quotaReason) || this.model?.quotaPaused || this.model?.phase === 'paused_quota' || modelBlocked || this.model?.policySyncPending || !workspace?.running || !this.model || activeTerminal || Boolean(this.failure);
    const send = this.host.querySelector('#chat-send');
    const attachments = this.attachments.list();
    const attachmentsPending = attachments.some((item) => item.status !== 'ready');
    send.disabled = Boolean((!input.value.trim() && !attachments.length) || attachmentsPending || input.value.length > 16000 || unavailable);
    send.textContent = sendBusy ? '正在发送…' : '发送';
    this.host.querySelector('#chat-upload').disabled = !this.attachments.allowed() || attachments.length >= 5;
    this.host.querySelectorAll('[data-chat-action="remove-attachment"], [data-chat-action="retry-attachment"]').forEach((button) => { button.disabled = sendBusy; });
    // Keep the editor usable during a request so the next draft isn't lost.
    const phase = sendBusy ? '正在提交消息…' : this.actions.has('stop') ? '正在停止会话…' : modelBusy ? phases.switching_model : !this.model?.busy && quotaReason && !this.model?.quotaPaused && this.model?.phase !== 'paused_quota' ? '站内配额暂不可用' : phases[this.model?.phase] || '正在读取对话';
    if (this.host.querySelector('#chat-phase').textContent !== phase) this.host.querySelector('#chat-phase').textContent = phase;
    const modelSelect = this.host.querySelector('#chat-model');
    const options = this.model?.modelOptions || [];
    const optionSignature = JSON.stringify(options);
    if (optionSignature !== this.modelOptionsSignature) {
      modelSelect.innerHTML = options.length ? options.map((option) => `<option value="${esc(option.value)}"${option.disabled ? ' disabled' : ''}>${esc(option.label)}${option.disabled ? solo ? '（尚未启用）' : '（管理员已禁用）' : ''}</option>`).join('') : '<option value="">正在读取…</option>';
      this.modelOptionsSignature = optionSignature;
    }
    // A change event alters the native select immediately. Restore the last
    // server-accepted value while the requested model awaits its acknowledgement.
    modelSelect.value = this.model?.selectedModel || '';
    modelSelect.disabled = this.modelChangeBlocked();
    const actualModel = this.host.querySelector('#chat-active-model');
    actualModel.hidden = !this.model?.active || !this.model?.activeModel || modelBusy || this.model?.phase === 'switching_model';
    actualModel.textContent = actualModel.hidden ? '' : `当前运行：${this.model.activeModel}`;
    const modelFeedback = this.host.querySelector('#chat-model-feedback');
    const pendingLabel = options.find((option) => option.value === this.pendingModel)?.label;
    const feedback = modelBusy ? `正在切换为 ${pendingLabel || '所选模型'}…` : this.model?.policySyncPending ? '管理员模型权限尚未完整应用，请等待管理员完成设置后继续。草稿和对话会保留。' : modelBlocked ? '管理员已禁止使用 Fable。请选择其他模型后继续，已有对话和文件会保留。' : this.modelNotice;
    modelFeedback.textContent = solo ? feedback.replaceAll('管理员模型权限尚未完整应用，请等待管理员完成设置后继续。', '模型设置尚未生效，请稍后刷新或在「更多设置」重试。').replaceAll('管理员已禁止使用 Fable。', 'Fable 尚未启用，可在菜单的「更多设置」中调整。') : feedback;
    modelFeedback.hidden = !feedback;
    modelFeedback.dataset.error = String(!modelBusy && (modelBlocked || this.model?.policySyncPending || this.modelNoticeError));
    let notice = this.failure || this.model?.error || ((!this.model?.quotaPaused && this.model?.phase !== 'paused_quota') ? quotaReason : '') || '';
    if (!workspace?.running && !this.model?.quotaPaused && this.model?.phase !== 'paused_quota') notice = quotaReason || '工作区已停止。先点击上方「启动工作区」，再开始中文对话。';
    else if (activeTerminal) notice = '当前工作区正在维护，请稍后再试或联系管理员。';
    const box = this.host.querySelector('#chat-notice');
    box.hidden = !notice;
    box.textContent = notice;
    this.host.querySelectorAll('[data-chat-action="allow"], [data-chat-action="deny"]').forEach((button) => { button.disabled = this.actions.has(`permission:${button.dataset.id}`) || modelBusy || this.isBusy(); });
    this.progressControls();
  }

  canResume() {
    const quota = this.model?.quota;
    return Boolean((this.model?.quotaPaused || this.model?.phase === 'paused_quota') && this.model?.canResume && quota && !quotaBlockReason(quota) && ['fiveHour', 'weekly'].every((key) => quotaUnit(quota) === 'percent' ? validPercentage(quota[key]?.remaining) && quota[key].remaining > 0 : quota[key]?.limit === null || (Number.isFinite(quota[key]?.remaining) && quota[key].remaining > 0)));
  }

  progressControls() {
    const paused = this.model?.quotaPaused || this.model?.phase === 'paused_quota';
    const running = this.host.querySelector('#chat-running');
    running.hidden = !this.model?.busy || paused || !['starting', 'thinking', 'awaiting_permission', 'switching_model'].includes(this.model?.phase);
    this.host.querySelector('#chat-running-text').textContent = this.model?.phase === 'awaiting_permission' ? 'Claude 等待你的确认，请查看下方操作。' : phases[this.model?.phase] || '';
    this.host.querySelector('#chat-quota-paused').hidden = !paused;
    const resuming = this.actions.has('resume'), refreshing = this.actions.has('quota-refresh');
    const resume = this.host.querySelector('#chat-resume');
    resume.disabled = !this.canResume() || this.model?.busy || this.actions.size > 0 || this.isBusy();
    resume.textContent = resuming ? '正在继续…' : '继续任务';
    this.host.querySelector('#chat-quota-refresh').disabled = refreshing || resuming;
    this.host.querySelector('#chat-quota-refresh').textContent = refreshing ? '正在刷新…' : '刷新额度';
    this.host.querySelector('#chat-quota-explanation').textContent = this.canResume() ? '站内配额已可用。点击「继续任务」后从原会话接着做；工作区会自动启动。' : `${quotaBlockReason(this.model?.quota) || '当前暂不能继续，请检查其他工作区的运行任务与模型权限后刷新。'} 对话、草稿和文件已保留。`;
    this.host.querySelector('#chat-quota-reset').textContent = paused ? `5 小时窗口恢复：${timeText(this.model?.quota?.fiveHour?.resetAt)} · 周窗口恢复：${timeText(this.model?.quota?.weekly?.resetAt)}` : '';
  }

  async resume() {
    const id = this.workspaceId, generation = this.generation;
    if (!this.current(id, generation) || !this.canResume() || this.actions.size || this.model?.busy || this.isBusy()) return;
    this.actions.add('resume'); this.controls();
    try {
      const model = await this.api(this.endpoint(id, '/resume'), { method: 'POST' });
      if (this.accept(model, id, generation)) await this.onStopped();
    } catch (error) { if (this.current(id, generation)) { this.toast(error.message, true); await this.refreshNow(); } }
    finally { if (this.current(id, generation)) { this.actions.delete('resume'); this.controls(); } }
  }

  async refreshQuota() {
    const id = this.workspaceId, generation = this.generation;
    if (!this.current(id, generation) || this.actions.has('quota-refresh') || this.actions.has('resume')) return;
    this.actions.add('quota-refresh'); this.controls();
    try {
      await this.api('/api/account/usage/refresh', { method: 'POST' });
      if (this.current(id, generation)) await this.refreshNow();
    } catch (error) { if (this.current(id, generation)) this.toast(error.message, true); }
    finally { if (this.current(id, generation)) { this.actions.delete('quota-refresh'); this.controls(); } }
  }

  modelChangeBlocked() {
    return Boolean(!this.model || !this.model.modelOptions?.length || this.model.busy || this.model.policySyncPending || this.actions.size || this.isBusy() || this.selected()?.terminalActive || this.failure);
  }

  async changeModel(value) {
    const id = this.workspaceId, generation = this.generation;
    if (!this.current(id, generation)) return;
    const option = this.model?.modelOptions?.find((item) => item.value === value);
    if (this.modelChangeBlocked() || !option || option.disabled || (value === 'fable' && this.model.fableAllowed === false) || value === this.model.selectedModel) { this.controls(); return; }
    this.actions.add('model');
    this.pendingModel = value;
    this.modelNotice = '';
    this.modelNoticeError = false;
    this.controls();
    try {
      const model = await this.api(this.endpoint(id, '/model'), { method: 'PUT', body: { model: value } });
      if (this.accept(model, id, generation)) {
        this.modelNotice = model.active ? `已选择 ${option.label}，用于后续消息。` : `已保存 ${option.label}，下次启动会话时使用。`;
        this.toast(this.modelNotice);
      }
    } catch (error) {
      if (this.current(id, generation)) {
        this.modelNotice = `未能切换模型：${error.message}`;
        this.modelNoticeError = true;
        this.toast(this.modelNotice, true);
      }
    } finally {
      if (this.current(id, generation)) { this.actions.delete('model'); this.pendingModel = null; this.controls(); }
    }
  }

  async send() {
    const id = this.workspaceId, generation = this.generation;
    if (!this.current(id, generation)) return;
    this.controls();
    if (this.host.querySelector('#chat-send').disabled) return;
    const input = this.host.querySelector('#chat-input');
    const text = input.value;
    const attachments = [...this.attachments.list(id)];
    if ((!text.trim() && !attachments.length) || text.length > 16000 || attachments.some((item) => item.status !== 'ready')) return;
    const revision = this.draftRevision;
    const attachmentEpoch = this.attachments.epoch;
    this.actions.add('send');
    this.controls();
    try {
      const body = { text };
      if (attachments.length) body.attachments = attachments.map((item) => ({ path: item.attachment.path, name: item.attachment.name }));
      const model = await this.api(this.endpoint(id, '/message'), { method: 'POST', body });
      if (attachmentEpoch === this.attachments.epoch) this.attachments.submitted(id, attachments);
      if (!this.current(id, generation)) return;
      if (revision === this.draftRevision && input.value === text) { input.value = ''; this.drafts.delete(id); }
      this.accept(model, id, generation);
    } catch (error) {
      if (this.current(id, generation)) this.toast(error.message, true);
      // Draft intentionally remains untouched on a rejected submission.
    } finally {
      if (this.current(id, generation)) { this.actions.delete('send'); this.controls(); }
    }
  }

  async permission(requestId, allow) {
    const id = this.workspaceId, generation = this.generation;
    const key = `permission:${requestId}`;
    if (!this.current(id, generation) || this.actions.has('model') || this.actions.has(key) || !this.model?.permissions.some((permission) => permission.id === requestId)) return;
    this.actions.add(key);
    this.controls();
    try {
      const model = await this.api(this.endpoint(id, `/permissions/${encodeURIComponent(requestId)}`), { method: 'POST', body: { allow } });
      this.accept(model, id, generation);
    } catch (error) { if (this.current(id, generation)) { this.toast(error.message, true); this.refreshNow(); } }
    finally { if (this.current(id, generation)) { this.actions.delete(key); this.controls(); } }
  }

  async stop() {
    const id = this.workspaceId, generation = this.generation;
    if (!this.current(id, generation) || this.actions.has('stop') || this.actions.has('model') || this.isBusy()) return;
    this.actions.add('stop');
    this.controls();
    try {
      if (!(await this.modal({ title: '停止中文会话？', text: '这会终止 Claude 和当前工作区内运行的其他任务。对话记录和已保存的文件会保留。', submit: '停止会话', danger: true }))) return;
      if (!this.current(id, generation)) return;
      const model = await this.api(this.endpoint(id, '/stop'), { method: 'POST' });
      if (this.accept(model, id, generation)) { this.toast('中文会话已停止，记录已保留'); await this.onStopped(); }
    } catch (error) { if (this.current(id, generation)) this.toast(error.message, true); }
    finally { if (this.current(id, generation)) { this.actions.delete('stop'); this.controls(); } }
  }

  async help() {
    if (conversationMode(this.selected(), this.model) === 'chat') {
      await this.modal({ title: '普通聊天', text: CHAT_MODE_HELP, submit: '我知道了' });
      return;
    }
    await this.modal({ title: '和 Claude 直接对话', text: '输入消息后点击「发送」，内容会原样交给 Claude。默认使用中文回复，你也可以要求使用其他语言。\n\n点击「上传文件 / 图片」，也可把文件拖入输入区，或直接粘贴截图。每个文件最多 10 MiB，每条最多 5 个。上传完成后可以连同说明一起发送，也可以只发送附件。\n\n附件保存在当前工作区的 uploads 文件夹。移除待发送附件只取消本次引用，已上传文件仍保留，可在「工作区文件」中管理。图片支持 PNG、JPEG、GIF、WebP 预览；其他文件可下载。\n\n消息支持 Markdown 标题、列表、表格和代码块。工具操作仍需确认；命令和工具参数保持原文。切换页面不会停止会话；需要停止时点击「停止会话」。', submit: '我知道了' });
  }

  renderMode() {
    if (!this.host) return;
    const chatting = conversationMode(this.selected(), this.model) === 'chat';
    this.host.dataset.conversationMode = chatting ? 'chat' : 'code';
    const heading = this.host.querySelector('.chat-header h2');
    if (heading) heading.textContent = chatting ? '普通聊天' : '编程 · 和 Claude 一起做';
    const hint = this.host.querySelector('.chat-upload-hint');
    if (hint) hint.textContent = chatting ? CHAT_ATTACHMENT_HINT : '每个文件最多 10 MiB · 每条最多 5 个';
    const footnote = this.host.querySelector('.chat-footnote > span');
    if (footnote) footnote.textContent = chatting ? '普通聊天 · 不执行工具 · 支持 Markdown' : '编程模式 · 支持 Markdown';
    const input = this.host.querySelector('#chat-input');
    if (input) input.placeholder = chatting ? '聊点什么…也可以发送图片或文本文件' : '直接告诉 Claude 你想完成什么…也可拖入文件或粘贴图片';
  }
}
