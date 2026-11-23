import createDOMPurify from 'dompurify';
import { Marked, Renderer } from 'marked';

const escapeHTML = (value) => String(value ?? '').replace(/[&<>"']/g, (character) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[character]));

const plainText = (value) => escapeHTML(value).replace(/\r\n?|\n/g, '<br>');

function safeURL(value) {
  // Only explicit web links are clickable. Relative paths and protocol-relative
  // links could navigate into the dashboard or expose local project paths.
  if (!/^https?:\/\//i.test(value) || /[\u0000-\u0020\u007f]/.test(value)) return null;
  try {
    const url = new URL(value);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return null;
    return url.href;
  } catch {
    return null;
  }
}

const standardRenderer = new Renderer();
const parser = new Marked({
  gfm: true,
  breaks: true,
  async: false,
  renderer: {
    html({ text }) {
      // Code explanations often contain literal HTML. Display it without
      // interpreting it, even when the markup would normally be harmless.
      return escapeHTML(text);
    },
    code({ text, lang }) {
      const language = String(lang || '').trim().split(/\s+/)[0] || '代码';
      return `<div class="markdown-code-block"><div class="markdown-code-label">${escapeHTML(language)}</div><pre><code>${escapeHTML(text)}</code></pre></div>\n`;
    },
    link({ href, title, tokens }) {
      const label = this.parser.parseInline(tokens);
      const url = safeURL(href);
      if (!url) return label;
      const titleAttribute = title ? ` title="${escapeHTML(title)}"` : '';
      return `<a href="${escapeHTML(url)}"${titleAttribute} target="_blank" rel="noopener noreferrer nofollow">${label}</a>`;
    },
    image({ text }) {
      // Do not fetch images supplied by messages: even a benign remote image
      // would disclose the reader's IP before they choose to visit the site.
      return `<span class="markdown-image">[图片：${escapeHTML(text || '图片')}]</span>`;
    },
    checkbox({ checked }) {
      return `<span class="markdown-checkbox" aria-label="${checked ? '已完成' : '未完成'}">${checked ? '☑' : '☐'}</span> `;
    },
    table(token) {
      return `<div class="markdown-table-wrap" role="region" aria-label="表格" tabindex="0">${standardRenderer.table.call(this, token)}</div>\n`;
    },
  },
});

const sanitizeOptions = {
  ALLOWED_TAGS: [
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'br', 'strong', 'em', 'del',
    'blockquote', 'ul', 'ol', 'li', 'hr', 'pre', 'code', 'table', 'thead',
    'tbody', 'tfoot', 'tr', 'th', 'td', 'a', 'div', 'span',
  ],
  ALLOWED_ATTR: ['href', 'title', 'target', 'rel', 'class', 'start', 'align', 'role', 'tabindex', 'aria-label'],
  ALLOW_DATA_ATTR: false,
  ALLOW_ARIA_ATTR: false,
  // These formatting attributes contain fixed renderer values, not URLs.
  ADD_URI_SAFE_ATTR: ['target', 'rel', 'start', 'align', 'tabindex', 'aria-label'],
  ALLOWED_URI_REGEXP: /^https?:\/\//i,
};

/** Create an isolated renderer for a browser or a DOM-backed test environment. */
export function createMarkdownRenderer(browserWindow) {
  const purifier = createDOMPurify(browserWindow);
  if (!purifier.isSupported) return plainText;
  return (value) => {
    const text = String(value ?? '');
    try {
      return purifier.sanitize(parser.parse(text), sanitizeOptions);
    } catch {
      // Keep a malformed or exceptionally nested response readable without
      // ever falling back to inserting unsanitized HTML.
      return plainText(text);
    }
  };
}

let browserRenderer;
let rendererWindow;

/** Safe HTML for a .chat-markdown container, for both translation and original. */
export function renderMarkdown(value) {
  const browserWindow = globalThis.window;
  if (!browserWindow?.document?.implementation) return plainText(value);
  if (!browserRenderer || rendererWindow !== browserWindow) {
    browserRenderer = createMarkdownRenderer(browserWindow);
    rendererWindow = browserWindow;
  }
  return browserRenderer(value);
}
