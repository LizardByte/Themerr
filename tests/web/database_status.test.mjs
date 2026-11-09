import assert from 'node:assert/strict';
import test from 'node:test';
import { publicationAge, initDatabaseStatus } from '../../web/js/database_status.js';

test('publication age formats seconds, minutes, hours and days in the selected locale', () => {
    const now = Date.parse('2026-10-02T12:00:00Z');
    for (const [seconds, expected] of [[20, '20 seconds ago'], [120, '2 minutes ago'],
        [7200, '2 hours ago'], [172800, '2 days ago']]) {
        assert.equal(publicationAge(new Date(now - seconds * 1000).toISOString(), now, 'en'), expected);
    }
    assert.equal(publicationAge(null, now, 'en'), null);
    assert.equal(publicationAge('invalid', now, 'en'), null);
});

function fixture(t) {
    t.mock.timers.enable({ apis: ['setTimeout', 'setInterval', 'Date'], now: Date.parse('2026-10-02T12:00:00Z') });
    const window = new EventTarget();
    globalThis.window = window;
    t.after(() => { delete globalThis.window; });
    const time = {}, link = {}, warning = {};
    const status = { dataset: { unknownLabel: 'Unknown', staleLabel: 'Check failed' },
        querySelector: selector => ({ '[data-database-time]': time, '[data-database-link]': link,
            '[data-database-stale]': warning })[selector] };
    const root = { querySelector: () => status };
    return { root, time, link, warning, window };
}

test('age updates locally, publication checks wait for the hourly deadline, and pagehide cancels them', async t => {
    const { root, time, link, warning, window } = fixture(t);
    const query = t.mock.fn(async () => ({
        updated_at: '2026-10-02T10:00:00+00:00',
        url: 'https://github.com/Themerr/ThemerrDB',
        next_check: Date.now() / 1000 + 3600,
        stale: false,
    }));
    initDatabaseStatus(root, query);
    await Promise.resolve();
    assert.equal(time.textContent, '2 hours ago');
    assert.equal(time.dateTime, '2026-10-02T10:00:00.000Z');
    assert.ok(time.title);
    assert.equal(warning.hidden, true);
    assert.equal(link.href, 'https://github.com/Themerr/ThemerrDB');
    t.mock.timers.tick(60 * 1000);
    assert.equal(query.mock.callCount(), 1);
    t.mock.timers.tick(3540 * 1000);
    await Promise.resolve();
    assert.equal(query.mock.callCount(), 2);
    assert.equal(time.textContent, '3 hours ago');
    window.dispatchEvent(new Event('pagehide'));
    t.mock.timers.tick(7200 * 1000);
    assert.equal(query.mock.callCount(), 2);
});

test('failed checks preserve the last publication and retry only after an hour', async t => {
    const { root, time, warning } = fixture(t);
    let attempt = 0;
    const query = t.mock.fn(async () => {
        if (attempt++) throw new Error('Network unavailable');
        return { updated_at: '2026-10-02T10:00:00Z', next_check: Date.now() / 1000 + 3600, stale: false };
    });
    initDatabaseStatus(root, query);
    await Promise.resolve();
    t.mock.timers.tick(3600 * 1000);
    await Promise.resolve();
    assert.equal(time.textContent, '3 hours ago');
    assert.equal(warning.hidden, false);
    assert.equal(warning.title, 'Check failed');
    t.mock.timers.tick(3599 * 1000);
    assert.equal(query.mock.callCount(), 2);
    t.mock.timers.tick(1000);
    await Promise.resolve();
    assert.equal(query.mock.callCount(), 3);
});

test('no publication displays an unknown age and pages without an indicator perform no request', async t => {
    const { root, time, warning } = fixture(t);
    const query = t.mock.fn(async () => ({ updated_at: null, stale: true }));
    initDatabaseStatus({ querySelector: () => null }, query);
    assert.equal(query.mock.callCount(), 0);
    initDatabaseStatus(root, query);
    await Promise.resolve();
    assert.equal(time.textContent, 'Unknown');
    assert.equal(warning.hidden, false);
});

test('the indicator uses the page language for relative time', async t => {
    const { root, time } = fixture(t);
    root.documentElement = { lang: 'fr' };
    initDatabaseStatus(root, async () => ({ updated_at: '2026-10-02T10:00:00Z', stale: false }));
    await Promise.resolve();
    assert.equal(time.textContent, 'il y a 2 heures');
});

test('workspace navigation cancels publication polls and a late response cannot restart them', async t => {
    const { root, time } = fixture(t);
    const controller = new AbortController();
    let resolve;
    const query = t.mock.fn(() => new Promise(done => { resolve = done; }));
    initDatabaseStatus(root, query, controller.signal);
    controller.abort();
    resolve({ updated_at: '2026-10-02T10:00:00Z', stale: false });
    await Promise.resolve();
    t.mock.timers.tick(7200 * 1000);
    assert.equal(query.mock.callCount(), 1);
    assert.equal(time.textContent, undefined);
});
