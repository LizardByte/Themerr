import { api, busy, toast } from './api.js';
import { refreshPage } from './workspace_navigation.js';

export function matchesFilters(item, filters) {
    return (!filters.search || item.title.toLocaleLowerCase().includes(filters.search.toLocaleLowerCase().trim())) &&
        (!filters.server || item.server === filters.server) &&
        (!filters.type || item.type === filters.type) &&
        (!filters.status || (filters.status === 'installed' && item.theme) ||
            (filters.status === 'missing' && !item.theme) || (filters.status === 'attention' && item.attention));
}

export function initDashboard(root = document) {
    const search = root.querySelector('#library-search');
    if (!search) return;
    const fields = ['server', 'type', 'status'].map(name => root.querySelector(`#${name}-filter`));
    const clearSearch = root.querySelector('#clear-search');
    try {
        const saved = JSON.parse(sessionStorage.getItem('themerr-refresh-filters') || 'null');
        sessionStorage.removeItem('themerr-refresh-filters');
        if (saved) {
            search.value = saved.search || '';
            fields.forEach((field, index) => { field.value = saved.fields[index] || ''; });
        }
    } catch {
        // Filtering also works when browser storage is unavailable.
    }
    function filter() {
        if (clearSearch) clearSearch.hidden = !search.value;
        let count = 0;
        const filters = { search: search.value, server: fields[0].value, type: fields[1].value, status: fields[2].value };
        root.querySelectorAll('[data-library]').forEach(library => {
            let visible = 0;
            library.querySelectorAll('[data-media-row]').forEach(row => {
                const match = matchesFilters({ ...row.dataset, server: library.dataset.server,
                    theme: row.dataset.theme === 'true', attention: row.dataset.attention === 'true' }, filters);
                row.hidden = !match;
                visible += Number(match);
            });
            library.hidden = visible === 0;
            count += visible;
        });
        root.querySelector('#visible-count').textContent = `${count} ${count === 1 ? 'item' : 'items'}`;
        root.querySelector('#filter-empty').hidden = count > 0;
    }
    search.addEventListener('input', filter);
    fields.forEach(field => field.addEventListener('change', filter));
    clearSearch?.addEventListener('click', () => {
        search.value = '';
        filter();
        search.focus();
    });
    root.querySelector('#clear-filters').addEventListener('click', () => {
        search.value = '';
        fields.forEach(field => { field.value = ''; });
        filter();
        search.focus();
    });
    filter();
}

export async function waitForRefresh(id, query, delay) {
    for (let attempt = 0; attempt < 60; attempt += 1) {
        const result = await query(); // NOSONAR javascript:S9382: Polls must finish before checking the next task state.
        const job = result.jobs.find(item => item.id === id);
        if (job && job.status !== 'running') return job.status;
        await delay(); // NOSONAR javascript:S9382: Wait between polls to avoid flooding the server.
    }
    return 'running';
}

export async function retryItem(button, request = api, notify = toast) {
    const result = await request('/api/tasks/retry', {
        body: {
            server_id: button.dataset.serverId,
            item_id: button.dataset.itemId,
        },
    });
    notify(result.message);
    return result;
}

async function refreshLibraries(button) {
    const result = await api('/api/tasks/refresh', { body: { scan: button.dataset.scan === 'true' } });
    toast(result.message);
    const status = await waitForRefresh(result.job_id, () => api('/api/tasks', { method: 'GET' }),
        () => new Promise(resolve => setTimeout(resolve, 2000)));
    if (status === 'failed') throw new Error('Refresh failed. Check Activity for details.');
    if (status === 'running') {
        toast('The refresh is still running. Follow its progress in Activity.');
        return;
    }
    if (!button.isConnected) return;
    const search = document.getElementById('library-search');
    if (search) {
        try {
            sessionStorage.setItem('themerr-refresh-filters', JSON.stringify({ search: search.value,
                fields: ['server', 'type', 'status'].map(name => document.getElementById(`${name}-filter`).value) }));
        } catch {
            // Reloading does not require browser storage.
        }
    }
    await refreshPage();
}

export function initNavigation() {
    const sidebar = document.querySelector('.app-sidebar');
    const menu = document.querySelector('.mobile-menu');
    const mobile = window.matchMedia('(max-width: 700px)');
    function syncNavigation() {
        if (sidebar) sidebar.inert = mobile.matches && !document.body.classList.contains('sidebar-open');
    }
    syncNavigation();
    document.addEventListener('themerr:navigation', syncNavigation);
    mobile.addEventListener('change', () => {
        document.body.classList.remove('sidebar-open');
        if (menu) menu.setAttribute('aria-expanded', 'false');
        syncNavigation();
    });
    document.querySelectorAll('[data-sidebar-toggle]').forEach(button => button.addEventListener('click', () => {
        const open = document.body.classList.toggle('sidebar-open');
        menu.setAttribute('aria-expanded', String(open));
        syncNavigation();
        if (open) document.querySelector('.primary-nav a').focus();
        else menu.focus();
    }));
    document.addEventListener('keydown', event => {
        if (event.key === 'Escape' && document.body.classList.contains('sidebar-open')) {
            document.body.classList.remove('sidebar-open');
            document.querySelector('.mobile-menu').setAttribute('aria-expanded', 'false');
            syncNavigation();
            document.querySelector('.mobile-menu').focus();
        }
    });
}

export function initPageControls() {
    document.querySelectorAll('[data-refresh]').forEach(button => button.addEventListener('click',
        () => busy(button, () => refreshLibraries(button))));
    document.querySelectorAll('[data-retry-item]').forEach(button => button.addEventListener('click',
        () => busy(button, () => retryItem(button))));
    document.querySelectorAll('time').forEach(element => {
        const date = new Date(element.textContent);
        if (!Number.isNaN(date.valueOf())) {
            element.dateTime = date.toISOString();
            element.textContent = date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
        }
    });
}
