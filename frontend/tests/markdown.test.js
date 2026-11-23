import test from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
import { marked } from 'marked';
import { createMarkdownRenderer, renderMarkdown } from '../src/markdown.js';

const dom = new JSDOM('', { url: 'https://dashboard.example/' });
const render = createMarkdownRenderer(dom.window);
const fragment = (source) => JSDOM.fragment(render(source));

function assertSafe(root) {
  assert.equal(root.querySelector('script,iframe,svg,math,object,embed,img,video,audio,source,form,input,style,link,meta'), null);
  for (const element of root.querySelectorAll('*')) {
    for (const attribute of element.attributes) {
      assert.ok(!/^on/i.test(attribute.name), attribute.name);
      assert.ok(!['src', 'srcdoc', 'style', 'id', 'name'].includes(attribute.name), attribute.name);
    }
  }
  for (const anchor of root.querySelectorAll('a')) {
    assert.match(anchor.getAttribute('href'), /^https?:\/\//);
    assert.equal(anchor.getAttribute('target'), '_blank');
    assert.equal(anchor.getAttribute('rel'), 'noopener noreferrer nofollow');
  }
}

test('renders GFM headings, emphasis, lists, quotes, tables, and code', () => {
  const root = fragment('# 标题\n\n**重点**、*强调*、~~删除~~、`inline()`\n\n> 引用\n\n- 第一项\n- [x] 完成\n- [ ] 待办\n\n3. 第三项\n\n| 名称 | 数量 |\n| :--- | ---: |\n| 中文 | 2 |\n\n```js\nconst value = "<safe>";\n```');
  assert.equal(root.querySelector('h1').textContent, '标题');
  assert.equal(root.querySelector('strong').textContent, '重点');
  assert.equal(root.querySelector('em').textContent, '强调');
  assert.equal(root.querySelector('del').textContent, '删除');
  assert.equal(root.querySelector('blockquote').textContent.trim(), '引用');
  assert.equal(root.querySelector('ol').getAttribute('start'), '3');
  assert.equal(root.querySelectorAll('.markdown-checkbox').length, 2);
  assert.equal(root.querySelector('.markdown-table-wrap').getAttribute('tabindex'), '0');
  assert.equal(root.querySelector('tbody td[align="right"]').textContent, '2');
  assert.equal(root.querySelector('.markdown-code-label').textContent, 'js');
  assert.equal(root.querySelector('pre code').textContent, 'const value = "<safe>";');
  assertSafe(root);
});

test('links allow only explicit HTTP(S) URLs and escape quoted attributes', () => {
  const root = fragment('[站点](https://example.com/path?q=1&x=2 "a &quot; onclick=alert(1)")\n\n[邮箱](mailto:user@example.com) [本地](/api/logout) [文件](file:///etc/passwd) [相对](//tracker.example/path) [凭据](https://user:password@example.com/)');
  const anchors = root.querySelectorAll('a');
  assert.equal(anchors.length, 1);
  assert.equal(anchors[0].getAttribute('href'), 'https://example.com/path?q=1&x=2');
  assert.equal(anchors[0].getAttribute('title'), 'a &quot; onclick=alert(1)');
  assert.match(root.textContent, /邮箱/);
  assert.match(root.textContent, /本地/);
  assertSafe(root);
});

test('script schemes, entity obfuscation, and control characters cannot create links', () => {
  const attacks = [
    '[点我](javascript:alert%281%29)',
    '[点我](JaVaScRiPt:alert%281%29)',
    '[点我](jav&#x61;script:alert%281%29)',
    '[点我](&#106;avascript:alert%281%29)',
    '[点我](javascript&#58;alert%281%29)',
    '[点我](java&#x09;script:alert%281%29)',
    '[点我](%6aavascript:alert%281%29)',
    '[点我](data:text/html;base64,PHNjcmlwdD4=)',
    '[点我](vbscript:msgbox%281%29)',
    '[点我](java\tscript:alert%281%29)',
    '<javascript:alert(1)>',
    '[点我](https://example.com\u0001evil)',
  ];
  for (const attack of attacks) {
    const root = fragment(attack);
    assert.equal(root.querySelector('a'), null, attack);
    assertSafe(root);
  }
});

test('raw HTML including safe-looking markup stays literal text', () => {
  const attack = '<script>alert(1)</script>\n<img src=x onerror=alert(1)>\n<iframe srcdoc="evil"></iframe>\n<svg onload=alert(1)></svg>\n<strong>不是粗体</strong>';
  const root = fragment(attack);
  assert.equal(root.textContent, attack);
  assert.equal(root.querySelector('strong'), null);
  assertSafe(root);
});

test('Markdown images never load external resources, including nested image links', () => {
  const root = fragment('![图片说明](https://tracking.example/pixel.png "追踪")\n\n[![徽章](https://tracking.example/badge.svg)](https://example.com)\n\n![坏图](data:image/svg+xml;base64,AAAA)');
  assert.equal(root.querySelectorAll('.markdown-image').length, 3);
  assert.match(root.textContent, /\[图片：图片说明\]/);
  assert.equal(root.querySelectorAll('a').length, 1);
  assertSafe(root);
});

test('HTML-like code and fence language remain exact, non-executable text', () => {
  const code = '<img src="x" onerror="alert(1)">\n</code></pre><script>alert(1)</script>\nconst x = "&quot;";';
  const root = fragment('```html"onclick="alert(1)\n' + code + '\n```');
  assert.equal(root.querySelector('pre code').textContent, code);
  assert.equal(root.querySelector('.markdown-code-label').textContent, 'html"onclick="alert(1)');
  assertSafe(root);
});

test('malformed fences, entities, and mixed HTML cannot escape sanitizer', () => {
  const attacks = [
    '```html\n<script>alert(1)</script>',
    '`</code><img src=x onerror=alert(1)>`',
    '&lt;img src=x onerror=alert(1)&gt;',
    '<table><tr><td><svg><style><img src=x onerror=alert(1)>',
    '<a href="javascript:alert(1)">test</a>',
    '[x](https://example.com/\"onmouseover=\"alert%281%29)',
    '| 内容 |\n| --- |\n| <img src=x onerror=alert(1)> |',
  ];
  for (const attack of attacks) assertSafe(fragment(attack));
});

test('browser entry point falls back to escaped plain text without a DOM', () => {
  assert.equal(renderMarkdown('<script>x</script>\n**文字**'), '&lt;script&gt;x&lt;/script&gt;<br>**文字**');
  assert.equal(renderMarkdown(null), '');
});

test('browser entry point sanitizes with the active browser DOM', () => {
  const previousWindow = globalThis.window;
  try {
    globalThis.window = dom.window;
    const root = JSDOM.fragment(renderMarkdown('**内容** <img src=x onerror=alert(1)>'));
    assert.equal(root.querySelector('strong').textContent, '内容');
    assert.match(root.textContent, /<img src=x onerror=alert\(1\)>/);
    assertSafe(root);
  } finally {
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
  }
});

test('private parser does not alter the global Marked renderer', () => {
  assert.equal(marked.parse('<b>hello</b>'), '<p><b>hello</b></p>\n');
});
