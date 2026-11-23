import assert from 'node:assert/strict';
import test from 'node:test';
import { JSDOM } from 'jsdom';
import { parseWanTokens, quotaDistribution, officialMarkup, quotaWindowMarkup, timeText, percentageText, quotaBlockReason, quotaUnit } from '../src/quota-view.js';

test('token input converts decimal ten-thousands exactly and rejects malformed or unsafe amounts', () => {
  assert.equal(parseWanTokens(''), null);
  assert.equal(parseWanTokens('0'), 0);
  assert.equal(parseWanTokens('12.3456'), 123456);
  assert.equal(parseWanTokens('0.0001'), 1);
  assert.equal(parseWanTokens('100000000'), 1_000_000_000_000);
  for (const value of ['-1', '1e4', 'Infinity', '0.00001', '100000000.0001', '99999999999999999', '<script>']) assert.throws(() => parseWanTokens(value));
});

test('distribution excludes deleting users and assigns exact remainder by ASCII user id', () => {
  const users = [{ id: 'b', username: '二' }, { id: 'A', username: '一' }, { id: 'c', username: '待删除', deleting: true }];
  const result = quotaDistribution(users, 3, 5);
  assert.deepEqual(result, [{ id: 'A', username: '一', fiveHourLimit: 2, weeklyLimit: 3 }, { id: 'b', username: '二', fiveHourLimit: 1, weeklyLimit: 2 }]);
  assert.deepEqual(quotaDistribution(users, null, 0).map((item) => [item.fiveHourLimit, item.weeklyLimit]), [[null, 0], [null, 0]]);
  assert.throws(() => quotaDistribution([], 1, 1));
});

test('unknown official windows are explicitly unknown with no false zero-percent progress', () => {
  const document = new JSDOM(officialMarkup({ fiveHour: { usedPercent: null, status: 'unknown', updatedAt: null }, weekly: { status: 'unknown' }, updatedAt: null, stale: true, error: 'unavailable' })).window.document;
  assert.equal(document.querySelectorAll('progress').length, 0);
  assert.match(document.body.textContent, /暂未知/);
  assert.doesNotMatch(document.body.textContent, /0%/);
  assert.doesNotMatch(document.body.textContent, /上次获取/);
  assert.equal(document.querySelectorAll('.quota-status').length, 1);
  assert.match(document.querySelector('.quota-status').textContent, /暂不可用/);
  assert.equal(document.querySelectorAll('.quota-unknown').length, 2);
  const known = new JSDOM(officialMarkup({ fiveHour: { usedPercent: 0, status: 'known' }, weekly: { usedPercent: 12, status: 'unknown' }, stale: true })).window.document;
  assert.deepEqual([...known.querySelectorAll('progress')].map((item) => item.value), [0, 12]);
  assert.match(known.body.textContent, /上次获取/);
});

test('private usage authorization failures explain the administrator action without account details', () => {
  const required = new JSDOM(officialMarkup({ errorCode: 'usage_authorization_required', error: 'secret-account@example.test' })).window.document;
  assert.match(required.body.textContent, /官方额度查询授权/);
  assert.match(required.body.textContent, /共享 Claude 账号相同/);
  assert.doesNotMatch(required.body.textContent, /secret-account|启动一个工作区/);
  const busy = new JSDOM(officialMarkup({ errorCode: 'usage_authorization_busy' })).window.document;
  assert.match(busy.body.textContent, /正在完成额度查询授权/);
  assert.equal(busy.querySelectorAll('progress').length, 0);
});

test('official history requires an observed percentage or valid timestamp and never duplicates warnings', () => {
  const stale = new JSDOM(officialMarkup({ fiveHour: { usedPercent: null, updatedAt: '2026-11-17T10:00:00Z' }, stale: true, error: 'unavailable' })).window.document;
  assert.match(stale.body.textContent, /上次获取/);
  assert.equal(stale.querySelectorAll('.quota-status').length, 1);
  assert.equal(stale.querySelectorAll('progress').length, 0);
  const invalid = new JSDOM(officialMarkup({ fiveHour: { usedPercent: NaN, updatedAt: 'bad' }, updatedAt: null, stale: true })).window.document;
  assert.doesNotMatch(invalid.body.textContent, /上次获取/);
  assert.match(invalid.body.textContent, /暂不可用/);
  const fresh = new JSDOM(officialMarkup({ fiveHour: { usedPercent: 12 }, weekly: { usedPercent: 15 }, stale: false })).window.document;
  assert.equal(fresh.querySelectorAll('.quota-status').length, 0);
});

test('missing personal usage is not called zero and reset timestamps never invent a daily reset', () => {
  assert.match(quotaWindowMarkup({}, '5 小时'), /暂未知/);
  assert.match(quotaWindowMarkup({ limit: null, used: 12345, remaining: null }, '5 小时'), /不限额/);
  assert.match(quotaWindowMarkup({ limit: 10000, used: 0, remaining: 10000 }, '<img>'), /&lt;img&gt;/);
  assert.equal(timeText(null), '待官方提供');
  assert.equal(timeText('bad'), '待官方提供');
  assert.equal(timeText(1800000000), timeText(1800000000000));
});

test('official-share quotas display percentage points without treating unknown values as unlimited or zero', () => {
  const document = new JSDOM(quotaWindowMarkup({ limit: 20, used: 3.25, remaining: 16.75 }, '5 小时窗口', 'percent')).window.document;
  assert.deepEqual([...document.querySelectorAll('dt')].map((item) => item.textContent), ['分配', '已用', '剩余']);
  assert.deepEqual([...document.querySelectorAll('dd')].map((item) => item.textContent), ['20%', '3.25%', '16.75%']);
  assert.doesNotMatch(document.body.textContent, /Token|官方个人|估算/);
  const unknown = new JSDOM(quotaWindowMarkup({ limit: null, used: null, remaining: null }, '周窗口', 'percent')).window.document;
  assert.deepEqual([...unknown.querySelectorAll('dd')].map((item) => item.textContent), ['暂未知', '暂未知', '暂未知']);
  assert.doesNotMatch(unknown.body.textContent, /不限额|0%|100%/);
  assert.equal(percentageText(0), '0%'); assert.equal(percentageText(100), '100%');
  assert.equal(percentageText(0.0001), '低于 0.01%');
  for (const value of [null, undefined, '20', NaN, Infinity, -1, 101]) assert.equal(percentageText(value), '暂未知');
});

test('official-share admission explanations prefer the server reason and wait when a window is unknown', () => {
  const shared = { mode: 'official_share', unit: 'percent', exhausted: false, fiveHour: { limit: 20, remaining: 17 }, weekly: { limit: 20, remaining: 18 } };
  assert.equal(quotaUnit(shared), 'percent'); assert.equal(quotaUnit({}), 'tokens');
  assert.equal(quotaBlockReason(shared), '');
  assert.equal(quotaBlockReason({ ...shared, blockedReason: '新窗口尚未同步，请稍后刷新。' }), '新窗口尚未同步，请稍后刷新。');
  assert.match(quotaBlockReason({ ...shared, weekly: { limit: null, remaining: null } }), /等待官方额度同步/);
  assert.match(quotaBlockReason({ ...shared, exhausted: true }), /站内配额已用完/);
  assert.match(quotaBlockReason({ ...shared, weekly: { limit: null, remaining: 0 } }), /站内配额已用完/);
});

test('shared account deductions are explained separately from attributed use only for percentage quotas', () => {
  const bucket = { limit: 20, used: 3, remaining: 14.25, sharedAdjustment: 2.75 };
  const document = new JSDOM(quotaWindowMarkup(bucket, '5 小时窗口', 'percent')).window.document;
  assert.deepEqual([...document.querySelectorAll('dd')].map((item) => item.textContent), ['20%', '3%', '14.25%']);
  assert.equal(document.querySelector('.quota-adjustment').textContent, '账号共享扣减 2.75%');
  for (const value of [undefined, null, 0, -1, NaN, 101, '<script>']) {
    assert.doesNotMatch(quotaWindowMarkup({ ...bucket, sharedAdjustment: value }, '周窗口', 'percent'), /账号共享扣减/);
  }
  assert.doesNotMatch(quotaWindowMarkup(bucket, '5 小时窗口'), /账号共享扣减/);
});

test('official query errors use safe actionable messages and retain observed meters with their timestamps', () => {
  const cases = [['no_running_workspace', /先启动一个工作区再刷新/], ['query_timeout', /查询超时/], ['query_failed', /查询失败/], ['unavailable', /客户端未返回官方额度/], ['storage_failed', /保存失败.*存储/]];
  const updated = '2026-11-17T10:23:00Z';
  for (const [errorCode, message] of cases) {
    const document = new JSDOM(officialMarkup({ errorCode, error: '<script>private upstream detail</script>', stale: true, fiveHour: { usedPercent: 28, updatedAt: updated }, weekly: { usedPercent: null } })).window.document;
    assert.match(document.querySelector('.quota-status').textContent, message);
    assert.match(document.querySelector('[data-official-window="fiveHour"]').textContent, /上次获取的数据/);
    assert.equal(document.querySelector('progress').value, 28);
    assert.match(document.body.textContent, new RegExp(timeText(updated)));
    assert.doesNotMatch(document.body.textContent, /private upstream|用完|耗尽/);
    assert.equal(document.querySelector('script'), null);
  }
  const unknown = new JSDOM(officialMarkup({ errorCode: '<img src=x>', error: 'SECRET', stale: true })).window.document;
  assert.match(unknown.body.textContent, /官方额度查询暂不可用/);
  assert.doesNotMatch(unknown.body.textContent, /SECRET|img src|上次获取/);
  assert.equal(unknown.querySelectorAll('progress, img').length, 0);
});

test('official refresh retry hint is shown only for a valid future timestamp', () => {
  const now = new Date('2026-11-17T11:00:00Z').getTime();
  const valid = new JSDOM(officialMarkup({ errorCode: 'query_timeout', retryAt: '2026-11-17T11:01:00Z' }, { now })).window.document;
  assert.match(valid.querySelector('.quota-retry').textContent, /下次可刷新（查询间隔）/);
  assert.match(valid.querySelector('.quota-retry').textContent, new RegExp(timeText('2026-11-17T11:01:00Z')));
  for (const retryAt of [null, undefined, '', 'bad', '<script>', '2026-11-17T11:00:00Z', '2026-11-17T10:59:59Z', now + 60000]) {
    assert.doesNotMatch(officialMarkup({ retryAt }, { now }), /下次可刷新/);
  }
});

test('fresh idle official and personal windows keep real zero usage without inventing a recovery timestamp', () => {
  const official = { fiveHour: { usedPercent: 0, resetAt: null, state: 'idle', idleSince: '2026-11-19T10:00:00Z', updatedAt: '2026-11-19T10:10:00Z', stale: false }, weekly: { usedPercent: 20, resetAt: '2026-11-25T10:00:00Z', updatedAt: '2026-11-19T10:10:00Z', stale: false }, stale: false };
  const document = new JSDOM(officialMarkup(official)).window.document;
  const five = document.querySelector('[data-official-window="fiveHour"]'), week = document.querySelector('[data-official-window="weekly"]');
  assert.equal(five.querySelector('progress').value, 0);
  assert.match(five.textContent, /0% 已用/); assert.match(five.textContent, /尚未开始新周期；首次使用后显示恢复时间/);
  assert.doesNotMatch(five.textContent, /待官方提供|上次获取|61%/);
  assert.match(week.textContent, new RegExp(timeText(official.weekly.resetAt))); assert.equal(week.querySelector('progress').value, 20);
  const personal = new JSDOM(quotaWindowMarkup({ limit: 20, used: 0, remaining: 20, resetAt: null, state: 'idle' }, '5 小时窗口', 'percent')).window.document;
  assert.deepEqual([...personal.querySelectorAll('dd')].map((node) => node.textContent), ['20%', '0%', '20%']);
  assert.match(personal.body.textContent, /尚未开始新周期；首次使用后显示恢复时间/);
  assert.doesNotMatch(personal.body.textContent, /待官方提供/);
});

test('history labels and timestamps are scoped to their own official window', () => {
  const data = { fiveHour: { usedPercent: 61, updatedAt: '2026-11-18T10:00:00Z', stale: true }, weekly: { usedPercent: 20, resetAt: '2026-11-25T10:00:00Z', updatedAt: '2026-11-19T10:10:00Z', stale: false }, updatedAt: '2026-11-19T10:10:00Z', stale: true };
  const document = new JSDOM(officialMarkup(data)).window.document;
  assert.match(document.querySelector('[data-official-window="fiveHour"]').textContent, /上次获取的数据/);
  assert.doesNotMatch(document.querySelector('[data-official-window="weekly"]').textContent, /上次获取|待刷新/);
  assert.match(document.querySelector('[data-official-window="weekly"]').textContent, /当前同步数据/);
  assert.equal(document.querySelector('.quota-status'), null, 'one stale window does not mark all meters historical');
  data.fiveHour = { usedPercent: null, resetAt: null, state: 'unknown', updatedAt: null, stale: true };
  const unknown = new JSDOM(officialMarkup(data)).window.document.querySelector('[data-official-window="fiveHour"]');
  assert.equal(unknown.querySelector('progress'), null); assert.match(unknown.textContent, /暂未知/);
  assert.doesNotMatch(unknown.textContent, /0%|100%|尚未开始新周期/);
  assert.match(unknown.textContent, /更新：待官方提供/);
});
