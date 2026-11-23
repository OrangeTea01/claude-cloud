import assert from 'node:assert/strict';
import test from 'node:test';
import { isClaudeLoginUrl, isClaudeAuthLink, normalizeClaudeLoginUrl, loginUrlError, ServerLoginBrowser } from '../src/login-browser.js';

const valid = 'https://claude.com/cai/oauth/authorize?response_type=code&client_id=client&code_challenge=challenge&state=session';
const current = 'https://claude.com/cai/oauth/authorize?code=true&client_id=client&response_type=code&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&scope=org%3Acreate_api_key+user%3Aprofile+user%3Ainference+user%3Asessions%3Aclaude_code&code_challenge=challenge_-123&code_challenge_method=S256&state=session_-123';

test('official complete authorization URLs are accepted', () => {
  assert.equal(isClaudeLoginUrl(valid), true);
  assert.equal(isClaudeLoginUrl(valid.replace('claude.com/cai', 'claude.ai')), true);
  assert.equal(isClaudeLoginUrl(valid.replace('claude.com/cai', 'platform.claude.com')), true);
  assert.equal(isClaudeLoginUrl(valid.replace('claude.com/cai', 'console.anthropic.com')), true);
  assert.equal(isClaudeLoginUrl(current), true);
  assert.equal(isClaudeLoginUrl(valid + '&redirect_uri=' + encodeURIComponent('http://localhost:80/callback')), true);
});

test('untrusted, malformed, ambiguous, and truncated URLs never start a browser', () => {
  for (const value of [
    valid.replace('claude.com', 'claude.com.evil.test'),
    valid.replace('https:', 'http:'), valid.replace('https://', 'https://user:password@'),
    valid.replace('https://', 'https://@'),
    valid.replace('claude.com', 'claude.com:444'), valid + '#fragment',
    valid + '&state=other', valid.replace('&state=session', ''),
    valid.replace('challenge', ''), valid.replace('/cai/oauth', '/cai\\oauth'),
    valid + '\n', valid.replace('/cai/oauth/authorize', '/login'),
    valid + '&state=%0A', valid + '&extra=%', valid + '&extra=%gg',
    valid + '&extra=\x1b', valid + '&extra=\u200b', valid + '&extra=中文',
    current.replace('platform.claude.com', 'evil.test'),
    current.replace('platform.claude.com', '%40platform.claude.com'),
    valid + '&redirect_uri=' + encodeURIComponent('http://localhost:0/callback'),
    current.replace('S256', 'plain'), valid + 'https://evil.test/',
  ]) assert.equal(isClaudeLoginUrl(value), false, value);
});

test('copied wraps and indentation are removed without altering OAuth bytes', () => {
  const wrapped = ' \t' + current.replace('%3A%2F', '%3\r\n  A%2F').replace('challenge_-123', 'chal\n\tlenge_-123').replace('&state=', ' &state=') + '\r\n';
  assert.equal(normalizeClaudeLoginUrl(wrapped), current);
  assert.equal(isClaudeLoginUrl(normalizeClaudeLoginUrl(wrapped)), true);
  assert.ok(normalizeClaudeLoginUrl(wrapped).includes('create_api_key+user%3Aprofile'));
  assert.match(loginUrlError(current.replace('&client_id=', 'client_id=')), /client_id/);
  assert.match(loginUrlError(valid + '&state=other'), /重复的 state/);
  assert.match(loginUrlError(valid + '&extra=%'), /% 编码/);
  assert.equal(normalizeClaudeLoginUrl(valid + '\u200b'), valid + '\u200b');
});

test('official malformed authorization links are intercepted rather than opened locally', () => {
  assert.equal(isClaudeAuthLink(valid.replace('&state=session', '')), true);
  assert.equal(isClaudeAuthLink(valid.replace('https:', 'http:')), true);
  assert.equal(isClaudeAuthLink('https://claude.com/pricing'), false);
});

test('authorization link detection handles terminal ANSI output and split chunks', () => {
  const browser = new ServerLoginBrowser({});
  const split = valid.indexOf('&state=');
  browser.observeOutput('\x1b[32m' + valid.slice(0, split));
  assert.equal(browser.url, '');
  browser.observeOutput(valid.slice(split) + '\x1b[0m\r\nPaste code here if prompted > ');
  assert.equal(browser.url, valid);
  browser.observeOutput('x'.repeat(40000));
  assert.ok(browser.output.length <= 32768);
});

test('a partial final OAuth value is not cached at chunk or newline boundaries', () => {
  const browser = new ServerLoginBrowser({});
  const split = current.indexOf('&state=') + '&state=s'.length;
  browser.observeOutput(current.slice(0, split));
  assert.equal(browser.url, '');
  browser.observeOutput('\r\n  ' + current.slice(split));
  assert.equal(browser.url, '');
  browser.observeOutput('\r\n\r\nPaste code here if prompted > ');
  assert.equal(browser.url, current);
  browser.observeOutput('\r\n' + current.slice(0, split));
  assert.equal(browser.url, '', 'new incomplete link must clear the previous cached link');
});

test('complete OSC 8 target survives wrapped or truncated visible labels', () => {
  for (const end of ['\x07', '\x1b\\']) {
    const browser = new ServerLoginBrowser({});
    const target = '\x1b]8;;' + current;
    browser.observeOutput(target.slice(0, -3));
    assert.equal(browser.url, '');
    browser.observeOutput(target.slice(-3) + end + current.slice(0, 120));
    assert.equal(browser.url, current);
    browser.observeOutput('\r\n  ' + current.slice(120) + '\x1b]8;;' + end);
    assert.equal(browser.url, current);
    browser.observeOutput('\r\n' + current.slice(0, -3));
    assert.equal(browser.url, '');
  }
});

test('terminal prompts and multiple links are never merged into an OAuth target', () => {
  const browser = new ServerLoginBrowser({});
  browser.observeOutput(current + '\r\nUnexpected text\r\nPaste code here if prompted > ');
  assert.equal(browser.url, '', 'prose between link and expected prompt must not be joined');
  assert.equal(isClaudeLoginUrl(normalizeClaudeLoginUrl(current + '\r\nhttps://evil.test/')), false);
});

test('browser operations remain ordered after a failed start', async () => {
  const browser = new ServerLoginBrowser({});
  const events = [];
  const failed = browser.enqueue(async () => { events.push('start'); throw new Error('failed'); });
  const stopped = browser.enqueue(async () => { events.push('stop'); });
  const reopened = browser.enqueue(async () => { events.push('reopen'); });
  await assert.rejects(failed);
  await stopped;
  await reopened;
  assert.deepEqual(events, ['start', 'stop', 'reopen']);
});
