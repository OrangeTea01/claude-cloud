export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
export const MAX_ATTACHMENTS = 5;
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (s) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[s]));
const fileIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9Z"/><path d="M14 3v6h6"/></svg>';
export const attachmentSize = (size) => size >= 1024 * 1024 ? `${(size / (1024 * 1024)).toFixed(1)} MB` : size >= 1024 ? `${Math.ceil(size / 1024)} KB` : `${size || 0} B`;
export const safeAttachmentPath = (path) => typeof path === 'string' && /^uploads\/[a-f0-9]{32}\/[^/\\\x00-\x1f:]+$/.test(path) && !['.', '..'].includes(path.split('/').at(-1));

export function rasterType(bytes) {
  const match = (start, values) => values.every((value, i) => bytes[start + i] === value);
  if (match(0, [137, 80, 78, 71, 13, 10, 26, 10])) return 'image/png';
  if (match(0, [255, 216, 255])) return 'image/jpeg';
  if (match(0, [71, 73, 70, 56]) && [55, 57].includes(bytes[4]) && bytes[5] === 97) return 'image/gif';
  if (match(0, [82, 73, 70, 70]) && match(8, [87, 69, 66, 80])) return 'image/webp';
  return null;
}

export function toBase64(bytes) {
  let binary = '';
  for (let i = 0; i < bytes.length; i += 16384) binary += String.fromCharCode(...bytes.subarray(i, i + 16384));
  return btoa(binary);
}

async function fileBytes(file, view) {
  if (file.arrayBuffer) return new Uint8Array(await file.arrayBuffer());
  return new Uint8Array(await new Promise((resolve, reject) => {
    const reader = new view.FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error('无法读取文件，请重新选择'));
    reader.readAsArrayBuffer(file);
  }));
}

function objectURL(bytes, type) {
  if (!URL.createObjectURL) return '';
  return URL.createObjectURL(new Blob([bytes], { type }));
}

/** Selected files stay bound to their original workspace, including late replies. */
export class ChatAttachments {
  constructor(chat) {
    this.chat = chat;
    this.drafts = new Map();
    this.previews = new Map();
    this.epoch = 0;
    this.counter = 0;
    this.queue = Promise.resolve();
  }

  list(id = this.chat.workspaceId) { return this.drafts.get(id) || []; }
  refresh(id) { if (this.chat.workspaceId === id && this.chat.host) { this.render(); this.chat.controls(); } }
  release(item) { if (item.url) URL.revokeObjectURL(item.url); item.url = ''; }
  clearPreviews() { for (const item of this.previews.values()) this.release(item); this.previews.clear(); }
  reset() {
    this.epoch++;
    this.queue = Promise.resolve();
    for (const items of this.drafts.values()) for (const item of items) this.release(item);
    this.drafts.clear();
    this.clearPreviews();
  }

  mount() {
    const chat = this.chat, host = chat.host, id = chat.workspaceId, generation = chat.generation;
    const active = () => chat.current(id, generation);
    const picker = host.querySelector('#chat-files');
    picker.addEventListener('change', (event) => {
      const files = [...event.target.files]; event.target.value = '';
      if (active()) this.add(files);
    });
    const composer = host.querySelector('#chat-form');
    composer.addEventListener('dragover', (event) => {
      if (![...(event.dataTransfer?.types || [])].includes('Files')) return;
      event.preventDefault(); composer.classList?.add('file-drag-over');
    });
    composer.addEventListener('dragleave', () => composer.classList?.remove('file-drag-over'));
    composer.addEventListener('drop', (event) => {
      if (!event.dataTransfer?.files?.length) return;
      event.preventDefault(); composer.classList?.remove('file-drag-over');
      if (active()) this.add([...event.dataTransfer.files]);
    });
    host.querySelector('#chat-input').addEventListener('paste', (event) => {
      const images = [...(event.clipboardData?.items || [])].filter((item) => item.kind === 'file' && item.type.startsWith('image/')).map((item) => item.getAsFile()).filter(Boolean);
      if (!images.length) return;
      event.preventDefault();
      if (active()) this.add(images);
    });
    this.render();
  }

  allowed() {
    const chat = this.chat;
    return chat.host?.isConnected && chat.isAuthenticated() && chat.selected()?.running && !chat.selected()?.terminalActive && !chat.isBusy() && !chat.actions.has('send');
  }

  add(files) {
    const chat = this.chat, id = chat.workspaceId;
    if (!this.allowed()) { chat.toast('请先启动工作区，等待当前提交完成后再上传。', true); return Promise.resolve(); }
    const items = this.list(id);
    this.drafts.set(id, items);
    for (const file of files) {
      if (items.length >= MAX_ATTACHMENTS) { chat.toast('每条消息最多添加 5 个附件。', true); break; }
      if (file.size > MAX_UPLOAD_BYTES) { chat.toast(`${file.name} 超过 10 MiB，请选择更小的文件。`, true); continue; }
      if (!file.name || /[\/\\\x00-\x1f:]/.test(file.name) || ['.', '..'].includes(file.name)) { chat.toast('文件名不受支持，请重命名后上传。', true); continue; }
      const item = { id: String(++this.counter), name: file.name, size: file.size, file, status: 'queued', url: '', error: '' };
      items.push(item);
      this.enqueue(id, item);
    }
    this.refresh(id);
    return this.queue;
  }

  enqueue(id, item) {
    const epoch = this.epoch, view = this.chat.host?.ownerDocument?.defaultView;
    const retained = () => epoch === this.epoch && this.list(id).includes(item) && this.chat.isAuthenticated();
    this.queue = this.queue.then(async () => {
      if (!retained()) return;
      item.status = 'uploading'; item.error = ''; this.refresh(id);
      try {
        const bytes = await fileBytes(item.file, view);
        if (!retained()) return;
        if (bytes.length > MAX_UPLOAD_BYTES) throw new Error('文件超过 10 MiB');
        const type = rasterType(bytes);
        if (type && !item.url) item.url = objectURL(bytes, type);
        this.refresh(id);
        const result = await this.chat.api(`/api/workspaces/${encodeURIComponent(id)}/attachments`, { method: 'POST', body: { name: item.name, content: toBase64(bytes) } });
        if (!retained()) return;
        if (!safeAttachmentPath(result.path) || typeof result.name !== 'string') throw new Error('上传结果无效，请重试');
        item.attachment = result; item.status = 'ready';
      } catch (error) {
        if (!retained()) return;
        item.status = 'failed'; item.error = error.message || '上传失败，请重试';
      }
      this.refresh(id);
    });
    return this.queue;
  }

  retry(itemId) {
    if (!this.allowed()) return;
    const id = this.chat.workspaceId, item = this.list(id).find((entry) => entry.id === itemId);
    if (!item || item.status !== 'failed') return;
    item.status = 'queued'; item.error = '';
    const pending = this.enqueue(id, item); this.refresh(id); return pending;
  }

  remove(itemId) {
    if (this.chat.actions.has('send')) return;
    const id = this.chat.workspaceId, item = this.list(id).find((entry) => entry.id === itemId);
    if (!item) return;
    this.release(item);
    this.drafts.set(id, this.list(id).filter((entry) => entry !== item)); this.refresh(id);
  }

  submitted(id, items) {
    for (const item of items) this.release(item);
    this.drafts.set(id, this.list(id).filter((item) => !items.includes(item)));
    this.refresh(id);
  }

  render() {
    const box = this.chat.host?.querySelector('#chat-attachments');
    if (!box) return;
    const items = this.list();
    box.hidden = !items.length;
    box.innerHTML = items.map((item) => `<div class="attachment-card" data-status="${item.status}">${item.url ? `<img src="${esc(item.url)}" alt="${esc(item.name)}" class="attachment-thumbnail">` : `<span class="attachment-file-icon">${fileIcon}</span>`}<div class="attachment-copy"><strong title="${esc(item.name)}">${esc(item.name)}</strong><small>${attachmentSize(item.size)} · ${item.status === 'ready' ? '已上传' : item.status === 'failed' ? '上传失败' : item.status === 'queued' ? '等待上传' : '正在上传…'}</small>${item.error ? `<small class="attachment-error">${esc(item.error)}</small>` : ''}</div>${item.status === 'failed' ? `<button type="button" class="text-button" data-chat-action="retry-attachment" data-id="${item.id}">重试</button>` : ''}<button type="button" class="attachment-remove" data-chat-action="remove-attachment" data-id="${item.id}" aria-label="移除附件 ${esc(item.name)}" title="仅取消本次发送，已上传文件保留在工作区">×</button></div>`).join('');
  }

  historyMarkup(message) {
    const attachments = (message.attachments || []).filter((item) => safeAttachmentPath(item.path)).slice(0, MAX_ATTACHMENTS);
    if (!attachments.length) return '';
    return `<div class="message-attachments">${attachments.map((item, index) => {
      const key = `${this.chat.workspaceId}:${item.path}`, preview = this.previews.get(key);
      const image = ['image/png', 'image/jpeg', 'image/gif', 'image/webp'].includes(item.mediaType);
      return `<div class="message-attachment">${preview?.url ? `<img src="${esc(preview.url)}" alt="${esc(item.name)}" class="message-image-preview">` : ''}<div class="message-attachment-row"><span class="attachment-file-icon">${fileIcon}</span><div class="attachment-copy"><strong>${esc(item.name)}</strong><small>${attachmentSize(item.size)}</small></div>${image ? `<button type="button" class="text-button" data-chat-action="preview-attachment" data-message="${esc(message.id)}" data-index="${index}">${preview?.url ? '收起图片' : '预览图片'}</button>` : ''}<button type="button" class="text-button" data-chat-action="download-attachment" data-message="${esc(message.id)}" data-index="${index}">下载</button></div></div>`;
    }).join('')}</div>`;
  }

  async open(messageId, index, preview) {
    const chat = this.chat, id = chat.workspaceId, generation = chat.generation;
    const message = chat.model?.messages.find((item) => item.id === messageId);
    const attachment = (message?.attachments || []).filter((item) => safeAttachmentPath(item.path)).slice(0, MAX_ATTACHMENTS)[Number(index)];
    if (!attachment || !chat.current(id, generation)) return;
    const key = `${id}:${attachment.path}`, existing = this.previews.get(key);
    if (preview && existing) { this.release(existing); this.previews.delete(key); chat.lastRender = ''; chat.render(); return; }
    const action = `attachment:${key}`;
    if (chat.actions.has(action)) return;
    chat.actions.add(action);
    try {
      const result = await chat.api(`/api/workspaces/${encodeURIComponent(id)}/file?path=${encodeURIComponent(attachment.path)}`);
      if (!chat.current(id, generation)) return;
      const bytes = result.encoding === 'base64' ? Uint8Array.from(atob(result.content), (char) => char.charCodeAt(0)) : new TextEncoder().encode(result.content);
      if (bytes.length > MAX_UPLOAD_BYTES) throw new Error('文件超过 10 MiB');
      const type = rasterType(bytes);
      if (preview && !type) throw new Error('此文件不支持图片预览，请下载查看');
      const url = objectURL(bytes, preview ? type : 'application/octet-stream');
      if (!url) throw new Error('当前浏览器暂不支持预览或下载');
      if (preview) { this.previews.set(key, { url }); chat.lastRender = ''; chat.render(); }
      else {
        const link = chat.host.ownerDocument.createElement('a'); link.href = url; link.download = attachment.name;
        link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      }
    } catch (error) { if (chat.current(id, generation)) chat.toast(error.message, true); }
    finally { if (chat.current(id, generation)) chat.actions.delete(action); }
  }
}
