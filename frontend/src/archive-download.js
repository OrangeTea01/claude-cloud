export const MAX_ARCHIVE_BYTES = 256 * 1024 * 1024;
export const ARCHIVE_LIMIT_HINT = '仅包含已保存文件 · ZIP 最多 256 MiB · 原文件合计最多 512 MiB · 最多 50,000 项';

export function archiveFilename(disposition, workspaceName = 'workspace') {
  let candidate = '';
  const encoded = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(disposition || '');
  if (encoded) {
    try { candidate = decodeURIComponent(encoded[1].trim()); } catch { /* Fall back to the plain filename. */ }
  }
  if (!candidate) {
    const plain = /filename\s*=\s*(?:"([^"\r\n]*)"|([^;\r\n]*))/i.exec(disposition || '');
    candidate = plain?.[1] || plain?.[2] || `${workspaceName}.zip`;
  }
  candidate = candidate.replace(/[\x00-\x1f\x7f/\\<>:"|?*]/g, '_').trim().replace(/^[. ]+|[. ]+$/g, '');
  const stem = candidate.replace(/\.zip$/i, '').slice(0, 180) || 'workspace';
  return `${stem}.zip`;
}

function downloadError(message, status) {
  return Object.assign(new Error(message), { status });
}

export class ArchiveDownload {
  constructor({ getContext, onState = () => {}, onError = () => {}, onHttpError = () => false,
    fetch: fetcher = (...args) => globalThis.fetch(...args), document: page = globalThis.document, urlApi = globalThis.URL,
    setTimer = (...args) => globalThis.setTimeout(...args), clearTimer = (...args) => globalThis.clearTimeout(...args) }) {
    Object.assign(this, { getContext, onState, onError, onHttpError, fetcher, page, urlApi, setTimer, clearTimer });
    this.active = null;
    this.urls = new Map();
  }

  get busy() { return Boolean(this.active); }

  current(request) {
    return this.active === request && !request.controller.signal.aborted && this.getContext()?.key === request.context.key;
  }

  release(url) {
    const record = this.urls.get(url);
    if (!record) return;
    this.clearTimer(record.timer);
    this.urlApi.revokeObjectURL(url);
    this.urls.delete(url);
  }

  cancel(workspaceId) {
    if (this.active && (!workspaceId || this.active.context.id === workspaceId)) {
      const request = this.active;
      this.active = null;
      request.controller.abort();
      this.onState();
    }
    for (const [url, record] of this.urls) {
      if (!workspaceId || record.workspaceId === workspaceId) this.release(url);
    }
  }

  async start() {
    const context = this.getContext();
    if (!context || this.active) return false;
    const request = { context, controller: new AbortController() };
    this.active = request;
    this.onState();
    try {
      const response = await this.fetcher(`/api/workspaces/${encodeURIComponent(context.id)}/archive`, {
        method: 'GET', credentials: 'same-origin', cache: 'no-store',
        headers: { Accept: 'application/zip' }, signal: request.controller.signal,
      });
      if (!this.current(request)) return false;
      if (!response.ok) {
        let data;
        try { data = await response.json(); } catch { /* Never display an HTML error response. */ }
        const fallback = response.status === 413
          ? '打包超过限制：ZIP 最多 256 MiB、原文件合计最多 512 MiB、最多 50,000 项，请减少文件后重试。'
          : response.status === 409 ? '工作区暂时无法打包，请先启动工作区，再重试。' : `打包下载失败（HTTP ${response.status}），请重试。`;
        throw downloadError(typeof data?.detail === 'string' ? data.detail.slice(0, 1000) : fallback, response.status);
      }
      const mediaType = (response.headers.get('Content-Type') || '').split(';')[0].trim().toLowerCase();
      if (!['application/zip', 'application/x-zip-compressed'].includes(mediaType)) {
        throw downloadError('服务器未返回有效的 ZIP 文件，请刷新页面后重试。');
      }
      const lengthHeader = response.headers.get('Content-Length');
      const declaredLength = lengthHeader === null ? null : Number(lengthHeader);
      if (declaredLength > MAX_ARCHIVE_BYTES) {
        throw downloadError('ZIP 超过 256 MiB 下载限制，请减少工作区文件后重试。');
      }
      const blob = await response.blob();
      if (!this.current(request)) return false;
      if (declaredLength !== null && (!Number.isSafeInteger(declaredLength) || declaredLength !== blob.size)) {
        throw downloadError('ZIP 传输不完整，未保存文件，请重试。');
      }
      if (blob.size > MAX_ARCHIVE_BYTES) throw downloadError('ZIP 超过 256 MiB 下载限制，请减少工作区文件后重试。');
      if (blob.size < 22) throw downloadError('收到的 ZIP 文件为空或不完整，请重试。');
      const signature = new Uint8Array(await blob.slice(0, 4).arrayBuffer());
      if (!this.current(request)) return false;
      if (signature[0] !== 0x50 || signature[1] !== 0x4b ||
          !((signature[2] === 3 && signature[3] === 4) || (signature[2] === 5 && signature[3] === 6))) {
        throw downloadError('收到的文件不是有效的 ZIP，请重试。');
      }
      const url = this.urlApi.createObjectURL(blob);
      this.urls.set(url, { workspaceId: context.id, timer: null });
      const link = this.page.createElement('a');
      try {
        link.href = url;
        link.download = archiveFilename(response.headers.get('Content-Disposition'), context.name);
        link.hidden = true;
        this.page.body.append(link);
        link.click();
        this.urls.get(url).timer = this.setTimer(() => this.release(url), 30_000);
      } catch (error) {
        this.release(url);
        throw error;
      } finally { link.remove(); }
      return true;
    } catch (error) {
      if (this.current(request) && error.name !== 'AbortError') {
        if (!this.onHttpError(error.status, error.message)) this.onError(error.message || '打包下载失败，请重试。');
      }
      return false;
    } finally {
      request.controller.abort();
      if (this.active === request) { this.active = null; this.onState(); }
    }
  }
}
