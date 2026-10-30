import assert from 'node:assert/strict';
import test from 'node:test';
import { matchesFilters, initDashboard, retryItem, waitForRefresh } from '../../web/js/dashboard.js';

test('library filters distinguish the same title on separate servers and handle Unicode search', () => {
    const media = { title: 'Léon: The Professional', server: 'a', type: 'movie', theme: true, attention: false };
    assert.equal(matchesFilters(media, { search: ' LÉON ', server: 'a', type: 'movie', status: 'installed' }), true);
    for (const filters of [{ server: 'b' }, { type: 'show' }, { status: 'missing' },
        { status: 'attention' }, { search: 'Arrival' }]) assert.equal(matchesFilters(media, filters), false);
    assert.equal(matchesFilters({ ...media, attention: true }, { status: 'attention' }), true);
    assert.equal(matchesFilters({ ...media, theme: false }, { status: 'missing' }), true);
});

class Field extends EventTarget {
    value = '';
    textContent = '';
    hidden = false;
    focus() { this.focused = true; }
}

test('typing, combining filters, and clearing restores rows, library groups, and item counts', () => {
    const fields = Object.fromEntries(['library-search', 'server-filter', 'type-filter', 'status-filter',
        'visible-count', 'filter-empty', 'clear-filters', 'clear-search'].map(id => [`#${id}`, new Field()]));
    const rows = [{ title: 'arrival', type: 'movie', theme: 'true', attention: 'false' },
        { title: 'dark', type: 'show', theme: 'false', attention: 'true' }].map(dataset => ({ dataset, hidden: false }));
    const libraries = rows.map((row, index) => ({ dataset: { server: index ? 'b' : 'a' },
        querySelectorAll: () => [row], hidden: false }));
    const root = { querySelector: id => fields[id], querySelectorAll: () => libraries };
    initDashboard(root);
    assert.equal(fields['#clear-search'].hidden, true);
    fields['#library-search'].value = 'Dark';
    fields['#library-search'].dispatchEvent(new Event('input'));
    assert.equal(rows[0].hidden, true);
    assert.equal(libraries[0].hidden, true);
    assert.equal(rows[1].hidden, false);
    assert.equal(fields['#visible-count'].textContent, '1 item');
    assert.equal(fields['#clear-search'].hidden, false);
    fields['#status-filter'].value = 'installed';
    fields['#status-filter'].dispatchEvent(new Event('change'));
    assert.equal(rows[1].hidden, true);
    assert.equal(fields['#filter-empty'].hidden, false);
    fields['#clear-search'].dispatchEvent(new Event('click'));
    assert.equal(fields['#clear-search'].hidden, true);
    assert.equal(fields['#status-filter'].value, 'installed');
    assert.equal(rows[0].hidden, false);
    assert.equal(rows[1].hidden, true);
    assert.equal(fields['#library-search'].focused, true);
    fields['#clear-filters'].dispatchEvent(new Event('click'));
    assert.equal(rows.every(row => !row.hidden), true);
    assert.equal(libraries.every(library => !library.hidden), true);
    assert.equal(fields['#filter-empty'].hidden, true);
    assert.equal(fields['#visible-count'].textContent, '2 items');
    assert.equal(fields['#library-search'].focused, true);
});

test('refresh waits for its own job and reports failures or long-running work', async () => {
    const states = [[], [{ id: 'another', status: 'finished' }, { id: 'requested', status: 'running' }],
        [{ id: 'requested', status: 'finished' }]];
    let waits = 0;
    assert.equal(await waitForRefresh('requested', async () => ({ jobs: states.shift() }),
        async () => { waits += 1; }), 'finished');
    assert.equal(waits, 2);
    assert.equal(await waitForRefresh('requested', async () => ({ jobs: [{ id: 'requested', status: 'failed' }] }),
        async () => {}), 'failed');
    assert.equal(await waitForRefresh('requested', async () => ({ jobs: [] }),
        async () => {}), 'running');
});

test('retry posts the selected server item and reports the result', async () => {
    const calls = [];
    const messages = [];
    const button = { dataset: { serverId: 'server-1', itemId: '42' } };
    const result = await retryItem(button, async (path, options) => {
        calls.push([path, options]);
        return { message: 'Retry queued. Follow progress in Activity.' };
    }, message => messages.push(message));
    assert.deepEqual(calls, [['/api/tasks/retry', { body: { server_id: 'server-1', item_id: '42' } }]]);
    assert.deepEqual(messages, ['Retry queued. Follow progress in Activity.']);
    assert.equal(result.message, messages[0]);
});
