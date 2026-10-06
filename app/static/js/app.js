'use strict';

const PAGE_SIZE = 120;
const QUEUE_POLL_MS = 1500;
const BADGE_POLL_MS = 5000;

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const enc = encodeURIComponent;
const view = $('#view');

const state = {
    servers: [],
    serversLoadedAt: 0,
    items: new Map(),        // "serverId:key" -> item, for every card rendered
    selection: new Map(),    // "serverId:key" -> item
    route: 0,                // bumped on navigation so stale responses are dropped
    observer: null,
    pollTimer: null,
    queueToolbar: '',
    focusSearch: false,
    files: {path: '', items: [], sort: {col: 'name', asc: true}, selected: new Set()},
};

// ---------------------------------------------------------------- formatting

const ESCAPES = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ESCAPES[c]);

function formatBytes(bytes, decimals = 1) {
    if (!+bytes) return '';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
    return `${parseFloat((bytes / 1024 ** i).toFixed(decimals))} ${units[i]}`;
}

function formatDuration(ms) {
    const minutes = Math.round((ms || 0) / 60000);
    if (!minutes) return '';
    return minutes >= 60 ? `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, '0')}m` : `${minutes} min`;
}

function formatEta(seconds) {
    if (!isFinite(seconds) || seconds <= 0) return '';
    if (seconds < 60) return `${Math.round(seconds)}s left`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m left`;
    return `${Math.floor(seconds / 3600)}h ${Math.round((seconds % 3600) / 60)}m left`;
}

const initials = name => (name || '?').trim().split(/\s+/).slice(0, 2).map(w => w[0]).join('').toUpperCase();

function hue(text) {
    let h = 0;
    for (const c of text || '') h = (h * 31 + c.charCodeAt(0)) % 360;
    return h;
}

// ---------------------------------------------------------------- ui helpers

const spinner = (text = 'Loading…') => `<div class="spinner"><i class="fa-solid fa-circle-notch fa-spin"></i><span>${esc(text)}</span></div>`;
const skeletons = (count = 18) => '<div class="media-card skeleton"></div>'.repeat(count);
const emptyState = (text, icon = 'fa-box-open') => `<div class="empty"><i class="fa-solid ${icon}"></i>${esc(text)}</div>`;
const errorState = text => `<div class="empty error"><i class="fa-solid fa-triangle-exclamation"></i>${esc(text)}</div>`;

function toast(message, type = 'success') {
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.textContent = message;
    $('#toasts').appendChild(el);
    requestAnimationFrame(() => el.classList.add('show'));
    setTimeout(() => { el.classList.remove('show'); setTimeout(() => el.remove(), 350); }, type === 'error' ? 5000 : 3000);
}

function storageGet(key) { try { return localStorage.getItem(key); } catch { return null; } }
function storageSet(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } }

async function api(path, {method = 'GET', body} = {}) {
    const options = {method, headers: {}};
    if (body !== undefined) {
        options.headers['Content-Type'] = 'application/json';
        options.body = JSON.stringify(body);
    }
    const res = await fetch(path, options);
    if (res.status === 401) {
        window.location.href = '/login';
        throw new Error('Session expired');
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Request failed (${res.status})`);
    return data;
}

// ---------------------------------------------------------------- layout

function setHeader(title, {back = null, toolbar = ''} = {}) {
    setTitle(title);
    const backBtn = $('#back-btn');
    backBtn.hidden = !back;
    backBtn.dataset.href = back || '';
    $('#toolbar').innerHTML = toolbar;
}

function setTitle(title) {
    $('#page-title').textContent = title;
    document.title = `${title} · SyncPlex`;
}

function setNav(name) {
    $$('[data-nav]').forEach(el => el.classList.toggle('active', el.dataset.nav === name));
}

function toggleMenu(open = !$('#sidebar').classList.contains('open')) {
    $('#sidebar').classList.toggle('open', open);
    $('#backdrop').hidden = !open;
}

function closeModals() {
    $$('.modal').forEach(m => { m.hidden = true; });
}

function openConfirm(title, message, onConfirm) {
    $('#confirm-title').textContent = title;
    $('#confirm-msg').textContent = message;
    $('#confirm-yes').onclick = () => { closeModals(); onConfirm(); };
    $('#confirm-modal').hidden = false;
}

// ---------------------------------------------------------------- router

const routes = [
    {re: /^\/servers$/, nav: 'servers', render: renderServers},
    {re: /^\/server\/([^/]+)$/, nav: 'servers', render: renderLibraries},
    {re: /^\/server\/([^/]+)\/library\/(\d+)$/, nav: 'servers', render: renderLibrary},
    {re: /^\/server\/([^/]+)\/item\/(\d+)$/, nav: 'servers', render: renderChildren},
    {re: /^\/recent$/, nav: 'recent', render: renderRecent},
    {re: /^\/search$/, nav: 'search', render: renderSearch},
    {re: /^\/downloads$/, nav: 'downloads', render: renderDownloads},
    {re: /^\/files$/, nav: 'files', render: renderFiles},
];

function parseHash() {
    const [path, query] = (window.location.hash.slice(1) || '/servers').split('?');
    return {path, params: new URLSearchParams(query || '')};
}

function setParams(updates, {replace = false} = {}) {
    const {path, params} = parseHash();
    Object.entries(updates).forEach(([k, v]) => (v ? params.set(k, v) : params.delete(k)));
    const query = params.toString();
    const hash = `#${path}${query ? `?${query}` : ''}`;
    if (replace) {
        history.replaceState(null, '', hash);
        router();
    } else {
        window.location.hash = hash;
    }
}

const isStale = token => token !== state.route;

async function router() {
    const token = ++state.route;
    const {path, params} = parseHash();
    state.observer?.disconnect();
    state.observer = null;
    clearInterval(state.pollTimer);
    state.queueToolbar = '';
    clearSelection();
    closeModals();
    toggleMenu(false);
    if (!state.focusSearch) $('#main').scrollTop = 0;

    const route = routes.find(r => r.re.test(path));
    if (!route) {
        window.location.hash = '#/servers';
        return;
    }
    setNav(route.nav);
    try {
        await route.render(token, params, ...path.match(route.re).slice(1).map(decodeURIComponent));
    } catch (e) {
        if (!isStale(token)) view.innerHTML = errorState(e.message);
    }
}

// ---------------------------------------------------------------- servers

async function loadServers(force = false) {
    if (force || Date.now() - state.serversLoadedAt > 120000) {
        state.servers = await api('/api/servers');
        state.serversLoadedAt = Date.now();
    }
    return state.servers;
}

const serverName = id => state.servers.find(s => s.id === id)?.name || 'Server';

function serverCard(s) {
    const h = hue(s.name);
    const status = s.online
        ? '<span class="pill pill-ok"><span class="dot"></span>Online</span>'
        : '<span class="pill pill-off"><span class="dot"></span>Offline</span>';
    const connection = s.forced
        ? '<span class="pill pill-plex"><i class="fa-solid fa-thumbtack"></i>Pinned</span>'
        : '<span class="pill pill-blue"><i class="fa-solid fa-wand-magic-sparkles"></i>Auto</span>';
    const owner = s.owned ? 'Your server' : `Shared by ${esc(s.owner)}`;
    const version = s.version ? ` · v${esc(s.version.split('-')[0])}` : '';
    return `
        <a href="#/server/${enc(s.id)}" class="panel server-card${s.online ? '' : ' offline'}">
            <div class="server-banner" style="background: linear-gradient(135deg, hsl(${h} 50% 26%), hsl(${(h + 60) % 360} 40% 13%))"></div>
            <button class="btn-icon" data-action="connections" data-server="${esc(s.id)}" title="Connection settings"><i class="fa-solid fa-gear"></i></button>
            <span class="avatar">${esc(initials(s.name))}</span>
            <div class="server-body">
                <h3 class="truncate">${esc(s.name)}</h3>
                <p class="small muted">${owner}${version}</p>
                <div class="row" style="justify-content: center">${status}${connection}</div>
            </div>
        </a>`;
}

async function renderServers(token) {
    setHeader('Servers', {toolbar: '<button class="btn" data-action="reload"><i class="fa-solid fa-rotate-right"></i> Refresh</button>'});
    view.innerHTML = spinner('Loading your Plex servers…');
    const servers = await loadServers(true);
    if (isStale(token)) return;
    view.innerHTML = servers.length
        ? `<div class="card-grid">${servers.map(serverCard).join('')}</div>`
        : emptyState('No Plex server is available on this account yet.', 'fa-server');
}

async function openConnections(serverId) {
    const list = $('#conn-list');
    list.innerHTML = spinner('Scanning…');
    $('#conn-modal').hidden = false;
    try {
        const data = await api(`/api/servers/${enc(serverId)}/connections`);
        const option = (uri, active, title, detail) => `
            <button class="conn${active ? ' active' : ''}" data-action="set-connection" data-server="${esc(serverId)}" data-uri="${esc(uri)}">
                ${title}<div class="tiny faint row" style="margin-top: 4px">${detail}</div>
            </button>`;
        list.innerHTML = option('', !data.forced,
            '<b><i class="fa-solid fa-wand-magic-sparkles c-plex"></i> Automatic</b>',
            'Fastest reachable address: direct first, relay last.')
            + data.connections.map(c => option(c.uri, c.active && data.forced,
                `<div class="mono small">${esc(c.uri)}</div>`,
                `<span>${c.relay ? 'Relay (speed limited)' : c.local ? 'Local network' : 'Remote'}</span><span>·</span><span>${esc(c.address)}:${esc(c.port)}</span>
                 ${c.active ? '<span class="c-green" style="margin-left: auto"><i class="fa-solid fa-check"></i> In use</span>' : ''}`)).join('');
    } catch (e) {
        list.innerHTML = errorState(e.message);
    }
}

async function setConnection(serverId, uri) {
    try {
        await api(`/api/servers/${enc(serverId)}/connection`, {method: 'PUT', body: {uri: uri || null}});
        closeModals();
        toast(uri ? 'Connection pinned' : 'Automatic connection restored');
        state.serversLoadedAt = 0;
        router();
    } catch (e) {
        toast(e.message, 'error');
    }
}

// ---------------------------------------------------------------- media cards

const itemId = item => `${item.server_id}:${item.key}`;
const itemRoute = item => `#/server/${enc(item.server_id)}/item/${enc(item.key)}`;

function mediaCard(item, {showServer = false} = {}) {
    const id = esc(itemId(item));
    state.items.set(itemId(item), item);
    const tags = item.tags?.length ? `<span class="badge badge-tag">${esc(item.tags.join(' · '))}</span>` : '<span></span>';
    const res = item.resolution ? `<span class="badge badge-res">${esc(item.resolution)}</span>` : '';
    const size = item.size ? ` · ${formatBytes(item.size)}` : '';
    const action = (name, icon, title, cls = '') => `<button class="action ${cls}" data-action="${name}" data-id="${id}" title="${title}"><i class="fa-solid ${icon}"></i></button>`;
    return `
        <div class="media-card${state.selection.has(itemId(item)) ? ' selected' : ''}" data-card="${id}">
            <div class="placeholder"><i class="fa-solid ${item.type === 'movie' ? 'fa-film' : 'fa-tv'}"></i></div>
            ${item.thumb ? `<img loading="lazy" src="${esc(item.thumb)}" alt="" onerror="this.remove()">` : ''}
            <div class="card-badges">${tags}${res}</div>
            <div class="card-check"><i class="fa-solid fa-check"></i></div>
            <div class="card-info">
                <div class="card-title" title="${esc(item.title)}">${esc(item.title)}</div>
                <div class="card-sub">${esc(item.subtitle)}${size}</div>
                ${showServer && item.server_name ? `<div class="card-server"><i class="fa-solid fa-server"></i> ${esc(item.server_name)}</div>` : ''}
            </div>
            <div class="card-actions">
                ${item.browsable ? action('open', 'fa-folder-open', 'Open') : ''}
                ${action('info', 'fa-info', 'Details')}
                ${action('sync', 'fa-cloud-arrow-down', 'Sync to server', 'blue')}
                ${item.browsable ? '' : action('device-download', 'fa-laptop', 'Download to this device', 'blue')}
            </div>
        </div>`;
}

const mediaGrid = (items, options) => items.map(i => mediaCard(i, options)).join('');

// ---------------------------------------------------------------- selection & sync

function toggleSelect(card) {
    const id = card.dataset.card;
    const item = state.items.get(id);
    if (!item) return;
    if (state.selection.has(id)) state.selection.delete(id);
    else state.selection.set(id, item);
    card.classList.toggle('selected', state.selection.has(id));
    updateFab();
}

function selectAll() {
    const cards = $$('[data-card]', view);
    const select = cards.some(c => !state.selection.has(c.dataset.card));
    cards.forEach(card => {
        const id = card.dataset.card;
        if (select) state.selection.set(id, state.items.get(id));
        else state.selection.delete(id);
        card.classList.toggle('selected', select);
    });
    updateFab();
}

function clearSelection() {
    state.selection.clear();
    $$('.media-card.selected').forEach(c => c.classList.remove('selected'));
    updateFab();
}

function updateFab() {
    $('#fab-count').textContent = state.selection.size;
    $('#fab').classList.toggle('visible', state.selection.size > 0);
}

async function syncItems(items) {
    if (!items.length) return;
    const byServer = new Map();
    items.forEach(i => byServer.set(i.server_id, [...(byServer.get(i.server_id) || []), i.key]));
    toast(items.some(i => i.browsable) ? 'Resolving episodes…' : 'Adding to the sync queue…');

    let queued = 0, skipped = 0;
    const results = await Promise.allSettled([...byServer].map(([serverId, keys]) =>
        api('/api/downloads', {method: 'POST', body: {server_id: serverId, keys}})));
    results.forEach(r => {
        if (r.status === 'fulfilled') {
            queued += r.value.queued;
            skipped += r.value.skipped;
        } else {
            toast(r.reason.message, 'error');
        }
    });
    if (queued || skipped) toast(`${queued} file(s) queued${skipped ? ` · ${skipped} already in queue` : ''}`);
    refreshQueueBadge();
}

function downloadToDevice(item) {
    const link = document.createElement('a');
    link.href = `/api/servers/${enc(item.server_id)}/items/${enc(item.key)}/file`;
    link.click();
    toast('Download starting in your browser…');
}

// ---------------------------------------------------------------- item details

async function openInfo(id) {
    const base = state.items.get(id);
    if (!base) return;
    const body = $('#info-body');
    $('#info-poster').src = base.thumb || '';
    body.innerHTML = spinner();
    $('#info-modal').hidden = false;

    let item;
    try {
        item = await api(`/api/servers/${enc(base.server_id)}/items/${enc(base.key)}`);
    } catch (e) {
        body.innerHTML = errorState(e.message);
        return;
    }
    state.items.set(id, {...base, ...item});
    if (item.poster) $('#info-poster').src = item.poster;

    const chips = list => `<div class="chips">${list.map(v => `<span class="chip">${esc(v)}</span>`).join('')}</div>`;
    const meta = [item.year, formatDuration(item.duration), item.resolution, formatBytes(item.size), item.rating && `★ ${Number(item.rating).toFixed(1)}`]
        .filter(Boolean).map(esc).join('<span class="sep">•</span>');
    const specs = [
        ['Video', [item.video_codec, item.resolution].filter(Boolean).join(' · ')],
        ['Audio', [...(item.audio || []), item.audio_codec].filter(Boolean).join(', ')],
        ['Subtitles', (item.subtitles || []).join(', ')],
        ['File', item.file],
        ['Director', (item.directors || []).join(', ')],
    ].filter(([, v]) => v);
    const eid = esc(id);

    body.innerHTML = `
        <div class="small c-plex"><i class="fa-solid fa-server"></i> ${esc(item.server_name)}</div>
        <div>
            <h2>${esc(item.title)}</h2>
            ${item.subtitle && item.type !== 'movie' ? `<div class="muted" style="margin-top: 4px">${esc(item.subtitle)}</div>` : ''}
        </div>
        ${meta ? `<div class="detail-meta">${meta}</div>` : ''}
        ${item.genres?.length ? chips(item.genres) : ''}
        <p class="detail-summary">${esc(item.summary || 'No summary available.')}</p>
        ${specs.length ? `<dl class="specs">${specs.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join('')}</dl>` : ''}
        ${item.cast?.length ? chips(item.cast) : ''}
        <div class="detail-actions">
            <button class="btn btn-primary" data-action="sync" data-id="${eid}"><i class="fa-solid fa-cloud-arrow-down"></i> ${item.browsable ? `Sync all (${item.leaf_count || 0})` : 'Sync to server'}</button>
            ${item.browsable
                ? `<button class="btn" data-action="open" data-id="${eid}"><i class="fa-solid fa-folder-open"></i> Open</button>`
                : `<button class="btn btn-blue" data-action="device-download" data-id="${eid}"><i class="fa-solid fa-laptop"></i> Download</button>`}
            <a class="btn" href="${esc(item.plex_url)}" target="_blank" rel="noopener"><i class="fa-solid fa-play"></i> Watch on Plex</a>
        </div>`;
}

// ---------------------------------------------------------------- libraries

async function renderLibraries(token, params, serverId) {
    await loadServers();
    const connectionBtn = `<button class="btn" data-action="connections" data-server="${esc(serverId)}"><i class="fa-solid fa-gear"></i> Connection</button>`;
    setHeader(serverName(serverId), {back: '#/servers', toolbar: connectionBtn});
    view.innerHTML = spinner(`Connecting to ${serverName(serverId)}…`);

    const data = await api(`/api/servers/${enc(serverId)}/libraries`);
    if (isStale(token)) return;
    setTitle(data.server);

    const libraries = data.libraries.map(l => `
        <a href="#/server/${enc(serverId)}/library/${enc(l.id)}" class="lib-card ${l.type}">
            <i class="fa-solid ${l.type === 'movie' ? 'fa-film' : 'fa-tv'} lib-icon"></i>
            <small>${l.type === 'movie' ? 'Movies' : 'TV Shows'}</small>
            <h3>${esc(l.title)}</h3>
        </a>`).join('');

    view.innerHTML = `
        <div class="card-grid">${libraries || emptyState('No movie or TV library on this server.')}</div>
        <h2 class="section-title spaced"><i class="fa-solid fa-clock-rotate-left"></i> Recently added</h2>
        <div id="recent-grid" class="media-grid">${skeletons(12)}</div>`;

    try {
        const items = await api(`/api/servers/${enc(serverId)}/recent`);
        if (!isStale(token)) $('#recent-grid').innerHTML = items.length ? mediaGrid(items) : emptyState('Nothing new here.');
    } catch (e) {
        if (!isStale(token)) $('#recent-grid').innerHTML = errorState(e.message);
    }
}

const SORT_OPTIONS = [['addedAt', 'Date added'], ['title', 'Title'], ['year', 'Year'], ['released', 'Release date'], ['rating', 'Rating']];

async function renderLibrary(token, params, serverId, sectionId) {
    await loadServers();
    const sort = params.get('sort') || 'addedAt';
    const direction = params.get('dir') || 'desc';
    const query = params.get('q') || '';

    setHeader(serverName(serverId), {
        back: `#/server/${enc(serverId)}`,
        toolbar: `
            <label class="search"><i class="fa-solid fa-magnifying-glass"></i>
                <input id="lib-search" type="search" class="input" placeholder="Filter titles…" value="${esc(query)}"></label>
            <select id="lib-sort" class="input" title="Sort by">${SORT_OPTIONS.map(([v, label]) => `<option value="${v}"${v === sort ? ' selected' : ''}>${label}</option>`).join('')}</select>
            <button class="btn" data-action="toggle-direction" title="Sort direction"><i class="fa-solid ${direction === 'asc' ? 'fa-arrow-up-short-wide' : 'fa-arrow-down-wide-short'}"></i></button>
            <button class="btn" data-action="select-all" title="Select all"><i class="fa-solid fa-check-double"></i></button>`,
    });

    const search = $('#lib-search');
    let debounce;
    search.addEventListener('input', () => {
        clearTimeout(debounce);
        debounce = setTimeout(() => { state.focusSearch = true; setParams({q: search.value.trim()}, {replace: true}); }, 400);
    });
    $('#lib-sort').addEventListener('change', e => setParams({sort: e.target.value}));
    if (state.focusSearch) {
        state.focusSearch = false;
        search.focus();
        search.setSelectionRange(search.value.length, search.value.length);
    }

    view.innerHTML = `<div id="grid" class="media-grid">${skeletons()}</div><div id="sentinel" style="height: 1px"></div>`;
    const grid = $('#grid'), sentinel = $('#sentinel');
    let start = 0, total = Infinity, loading = false;

    const loadMore = async () => {
        if (loading || start >= total || isStale(token)) return;
        loading = true;
        if (start) grid.insertAdjacentHTML('beforeend', skeletons(12));
        try {
            const qs = new URLSearchParams({sort, direction, start, size: PAGE_SIZE, q: query});
            const data = await api(`/api/servers/${enc(serverId)}/libraries/${enc(sectionId)}?${qs}`);
            if (isStale(token)) return;
            $$('.skeleton', grid).forEach(el => el.remove());
            total = data.items.length ? data.total : start;
            start += data.items.length;
            setTitle(`${data.title}${total ? ` (${total})` : ''}`);
            grid.insertAdjacentHTML('beforeend', mediaGrid(data.items));
            if (!total) grid.innerHTML = emptyState(query ? `No title matches “${query}”.` : 'This library is empty.');
        } finally {
            loading = false;
        }
        // Keep filling while the end of the grid is still on screen (large displays).
        if (sentinel.getBoundingClientRect().top < window.innerHeight + 600) loadMore();
    };

    state.observer = new IntersectionObserver(entries => entries[0].isIntersecting && loadMore(), {root: $('#main'), rootMargin: '600px'});
    state.observer.observe(sentinel);
    await loadMore();
}

async function renderChildren(token, params, serverId, key) {
    await loadServers();
    setHeader(serverName(serverId), {back: 'history'});
    view.innerHTML = `<div class="media-grid">${skeletons(12)}</div>`;
    const data = await api(`/api/servers/${enc(serverId)}/items/${enc(key)}/children`);
    if (isStale(token)) return;

    const parentId = `${serverId}:${key}`;
    if (!state.items.has(parentId)) state.items.set(parentId, {server_id: serverId, key, browsable: true, title: data.title});
    setHeader(data.title || serverName(serverId), {
        back: 'history',
        toolbar: `
            <button class="btn" data-action="info" data-id="${esc(parentId)}"><i class="fa-solid fa-info"></i> Details</button>
            <button class="btn" data-action="select-all" title="Select all"><i class="fa-solid fa-check-double"></i></button>
            <button class="btn btn-primary" data-action="sync" data-id="${esc(parentId)}"><i class="fa-solid fa-cloud-arrow-down"></i> Sync all</button>`,
    });
    view.innerHTML = data.items.length ? `<div class="media-grid">${mediaGrid(data.items)}</div>` : emptyState('Nothing in here.');
}

// ---------------------------------------------------------------- recently added & search

async function renderRecent(token) {
    setHeader('Recently Added', {toolbar: '<button class="btn" data-action="reload"><i class="fa-solid fa-rotate-right"></i> Refresh</button>'});
    view.innerHTML = `<p id="recent-status" class="small muted" style="margin-bottom: 16px"></p><div id="grid" class="media-grid">${skeletons()}</div>`;
    const servers = (await loadServers()).filter(s => s.online);
    if (isStale(token)) return;
    if (!servers.length) {
        view.innerHTML = emptyState('No server is online.', 'fa-server');
        return;
    }

    const all = [], failed = [];
    let pending = servers.length;
    const status = () => {
        $('#recent-status').textContent = pending
            ? `Waiting for ${pending} server(s)…`
            : failed.length ? `Unreachable: ${failed.join(', ')}` : `${all.length} items from ${servers.length} server(s)`;
    };
    status();

    await Promise.all(servers.map(async s => {
        try {
            const items = await api(`/api/servers/${enc(s.id)}/recent`);
            if (isStale(token)) return;
            all.push(...items);
            all.sort((a, b) => b.added_at - a.added_at);
            $('#grid').innerHTML = mediaGrid(all, {showServer: true});
        } catch {
            failed.push(s.name);
        } finally {
            pending--;
            if (!isStale(token)) status();
        }
    }));
    if (!isStale(token) && !all.length) $('#grid').innerHTML = emptyState('Nothing recently added.');
}

async function renderSearch(token, params) {
    const query = (params.get('q') || '').trim();
    setHeader('Search all servers');
    view.innerHTML = `
        <form id="search-form" class="search search-lg">
            <i class="fa-solid fa-magnifying-glass"></i>
            <input id="search-input" type="search" class="input" placeholder="Movie, show, episode…" value="${esc(query)}" autocomplete="off">
        </form>
        <div id="search-results"></div>`;
    $('#search-form').addEventListener('submit', e => {
        e.preventDefault();
        setParams({q: $('#search-input').value.trim()});
    });
    $('#search-input').focus();

    const results = $('#search-results');
    if (!query) {
        results.innerHTML = emptyState('Search every server shared with you at once.', 'fa-magnifying-glass');
        return;
    }
    results.innerHTML = `<div class="media-grid">${skeletons(12)}</div>`;
    const servers = (await loadServers()).filter(s => s.online);
    const sections = await Promise.all(servers.map(server =>
        api(`/api/servers/${enc(server.id)}/search?q=${enc(query)}`)
            .then(items => ({server, items}))
            .catch(() => ({server, items: []}))));
    if (isStale(token)) return;

    const found = sections.filter(s => s.items.length);
    results.innerHTML = found.length
        ? found.map(({server, items}) => `
            <h2 class="section-title"><i class="fa-solid fa-server c-plex"></i>${esc(server.name)} <span class="muted">${items.length} result(s)</span></h2>
            <div class="media-grid" style="margin-bottom: 36px">${mediaGrid(items)}</div>`).join('')
        : emptyState(`No result for “${query}”.`, 'fa-magnifying-glass');
}

// ---------------------------------------------------------------- sync queue

const STATUS_LABELS = {queued: 'Queued', downloading: 'Downloading', completed: 'Completed', failed: 'Failed', cancelled: 'Cancelled'};

function taskShell(t) {
    const button = (op, icon, title, cls = '') => `<button class="btn-icon ${cls}" data-action="task" data-task="${esc(t.id)}" data-op="${op}" title="${title}"><i class="fa-solid ${icon}"></i></button>`;
    const active = t.status === 'queued' || t.status === 'downloading';
    return `
        <div class="task-thumb">${t.thumb ? `<img src="${esc(t.thumb)}" alt="" onerror="this.remove()">` : ''}</div>
        <div class="grow">
            <div class="row"><span class="task-title truncate" title="${esc(t.path)}">${esc(t.title)}</span><span class="task-status">${STATUS_LABELS[t.status] || esc(t.status)}</span></div>
            <div class="tiny faint truncate"><i class="fa-solid fa-server c-plex"></i> ${esc(t.server_name || '…')} · <i class="fa-solid fa-user"></i> ${esc(t.user)}${t.path ? ` · ${esc(t.path)}` : ''}</div>
            <div class="progress"><div></div></div>
            <div class="task-detail truncate"></div>
        </div>
        <div class="row">
            ${active ? button('cancel', 'fa-stop', 'Cancel', 'danger') : ''}
            ${t.status === 'failed' || t.status === 'cancelled' ? button('retry', 'fa-rotate-right', 'Retry', 'ok') : ''}
            ${button('remove', 'fa-xmark', 'Remove from list')}
        </div>`;
}

function taskDetail(t) {
    if (t.status === 'failed') return t.error;
    const eta = t.status === 'downloading' && t.speed ? formatEta((t.total - t.downloaded) / t.speed) : '';
    return [
        t.progress && t.status !== 'completed' ? `${t.progress}%` : '',
        t.speed ? `${formatBytes(t.speed)}/s` : '',
        eta,
        t.total ? `${formatBytes(t.downloaded) || '0 B'} / ${formatBytes(t.total)}` : '',
    ].filter(Boolean).join(' · ');
}

/** Updates rows in place: the markup (and its buttons) is rebuilt only when a task changes state. */
function updateQueue(data) {
    const busy = data.active + data.queued;
    const toolbar = `
        ${data.paused
            ? '<button class="btn btn-primary" data-action="queue" data-op="resume"><i class="fa-solid fa-play"></i> Resume</button>'
            : '<button class="btn" data-action="queue" data-op="pause"><i class="fa-solid fa-pause"></i> Pause</button>'}
        <button class="btn btn-ghost-danger" data-action="queue" data-op="cancel-all"${busy ? '' : ' disabled'}><i class="fa-solid fa-ban"></i> Cancel all</button>
        <button class="btn" data-action="queue" data-op="clear-finished"><i class="fa-solid fa-broom"></i> Clear finished</button>`;
    if (toolbar !== state.queueToolbar) $('#toolbar').innerHTML = state.queueToolbar = toolbar;

    $('#queue-summary').innerHTML = data.paused
        ? '<b class="c-plex"><i class="fa-solid fa-pause"></i> Queue paused</b> — downloads in progress are suspended and resume where they stopped.'
        : `${data.active} downloading · ${data.queued} waiting`;

    const list = $('#queue-list');
    if (!data.tasks.length) {
        list.innerHTML = emptyState('The sync queue is empty.', 'fa-cloud-arrow-down');
        return;
    }
    $$('.empty, .spinner', list).forEach(el => el.remove());

    const rows = new Map($$('[data-task-row]', list).map(row => [row.dataset.taskRow, row]));
    data.tasks.forEach((t, index) => {
        let row = rows.get(t.id);
        rows.delete(t.id);
        if (!row) {
            row = document.createElement('div');
            row.dataset.taskRow = t.id;
        }
        const signature = [t.status, t.title, t.thumb, t.server_name, t.path].join('|');
        if (row.dataset.signature !== signature) {
            row.className = `panel task ${t.status}`;
            row.innerHTML = taskShell(t);
            row.dataset.signature = signature;
        }
        row.querySelector('.progress > div').style.width = `${t.progress}%`;
        row.querySelector('.task-detail').textContent = taskDetail(t);
        if (list.children[index] !== row) list.insertBefore(row, list.children[index] || null);
    });
    rows.forEach(row => row.remove());
}

async function renderDownloads(token) {
    setHeader('Sync Queue');
    view.innerHTML = `<div id="queue-summary" class="queue-summary"></div><div id="queue-list" class="queue">${spinner()}</div>`;
    const refresh = async () => {
        try {
            const data = await api('/api/downloads');
            if (isStale(token)) return;
            updateQueueBadge(data);
            updateQueue(data);
        } catch { /* keep polling */ }
    };
    await refresh();
    state.pollTimer = setInterval(refresh, QUEUE_POLL_MS);
}

function updateQueueBadge(data) {
    const badge = $('#queue-badge');
    const count = data.active + data.queued;
    badge.textContent = count;
    badge.hidden = !count;
}

async function refreshQueueBadge() {
    try { updateQueueBadge(await api('/api/downloads')); } catch { /* ignore */ }
}

async function queueAction(op) {
    try {
        const data = await api(`/api/downloads/${op}`, {method: 'POST'});
        if (parseHash().path === '/downloads') updateQueue(data);
        updateQueueBadge(data);
    } catch (e) { toast(e.message, 'error'); }
}

async function taskAction(taskId, op) {
    try {
        await api(`/api/downloads/tasks/${enc(taskId)}/${op}`, {method: 'POST'});
        const data = await api('/api/downloads');
        if (parseHash().path === '/downloads') updateQueue(data);
        updateQueueBadge(data);
    } catch (e) { toast(e.message, 'error'); }
}

// ---------------------------------------------------------------- files

const FILE_ICONS = {folder: 'fa-folder c-plex', video: 'fa-film c-blue', partial: 'fa-hourglass-half faint', file: 'fa-file faint'};
const FILE_COLUMNS = [['name', 'Name', 'name'], ['type', 'Type', ''], ['size', 'Size', 'num'], ['modified', 'Modified', 'num']];

async function renderFiles(token, params) {
    const path = params.get('path') || '';
    const files = state.files;
    if (files.path !== path) files.selected.clear();
    files.path = path;
    setHeader('Files & Storage', {back: path ? `#/files?path=${enc(path.split('/').slice(0, -1).join('/'))}` : null});
    view.innerHTML = spinner();

    const [listing, storage] = await Promise.all([api(`/api/files?path=${enc(path)}`), api('/api/storage')]);
    if (isStale(token)) return;
    files.items = listing.items;
    updateStorage(storage);

    const parts = path ? path.split('/') : [];
    const breadcrumb = ['<a href="#/files"><i class="fa-solid fa-house"></i> downloads</a>']
        .concat(parts.map((part, i) => i === parts.length - 1
            ? `<b>${esc(part)}</b>`
            : `<a href="#/files?path=${enc(parts.slice(0, i + 1).join('/'))}">${esc(part)}</a>`))
        .join('<span class="sep">/</span>');
    const stat = (label, value, cls = '') => `<div class="panel stat"><small>${label}</small><strong class="${cls}">${formatBytes(value) || '0 B'}</strong></div>`;

    view.innerHTML = `
        <div class="stats">
            ${stat('Used', storage.used)}
            ${stat('Free', storage.free, storage.percent > 90 ? 'c-red' : 'c-green')}
            ${stat('Total', storage.total, 'muted')}
        </div>
        <div class="panel files">
            <div class="breadcrumb">${breadcrumb}</div>
            <div class="table-wrap">
                <table class="table">
                    <thead><tr>
                        <th class="check"><input type="checkbox" id="files-all" title="Select all"></th>
                        ${FILE_COLUMNS.map(([col, label, cls]) => `<th class="sortable ${cls}" data-action="file-sort" data-col="${col}">${label}<i data-sort-icon="${col}"></i></th>`).join('')}
                        <th></th>
                    </tr></thead>
                    <tbody id="files-body"></tbody>
                </table>
            </div>
        </div>`;
    $('#files-all').addEventListener('change', e => {
        files.items.forEach(f => (e.target.checked ? files.selected.add(f.path) : files.selected.delete(f.path)));
        renderFileRows();
    });
    renderFileRows();
}

function renderFileRows() {
    const files = state.files;
    const {col, asc} = files.sort;
    const value = (f, key) => (key === 'name' ? f.name.toLowerCase() : f[key]);
    const sorted = [...files.items].sort((a, b) =>
        (b.type === 'folder') - (a.type === 'folder')
        || (value(a, col) < value(b, col) ? -1 : value(a, col) > value(b, col) ? 1 : 0) * (asc ? 1 : -1));

    $('#files-body').innerHTML = sorted.length ? sorted.map(f => `
        <tr>
            <td class="check"><input type="checkbox" data-file-check="${esc(f.path)}"${files.selected.has(f.path) ? ' checked' : ''}></td>
            <td class="name">${f.type === 'folder'
                ? `<a class="file-name" href="#/files?path=${enc(f.path)}"><i class="fa-solid ${FILE_ICONS.folder}"></i><span class="truncate">${esc(f.name)}</span></a>`
                : `<div class="file-name"><i class="fa-solid ${FILE_ICONS[f.type]}"></i><span class="truncate" title="${esc(f.name)}">${esc(f.name)}</span></div>`}</td>
            <td class="tiny muted">${esc(f.type).toUpperCase()}</td>
            <td class="num mono small">${formatBytes(f.size) || '—'}</td>
            <td class="num small muted">${new Date(f.modified * 1000).toLocaleString()}</td>
            <td class="num"><button class="btn-icon danger" data-action="file-delete" data-path="${esc(f.path)}" title="Delete"><i class="fa-solid fa-trash"></i></button></td>
        </tr>`).join('') : `<tr><td colspan="6">${emptyState('This folder is empty.', 'fa-folder-open')}</td></tr>`;

    $$('[data-sort-icon]').forEach(icon => {
        const active = icon.dataset.sortIcon === col;
        icon.className = `fa-solid ${active ? (asc ? 'fa-sort-up' : 'fa-sort-down') : 'fa-sort'}`;
        icon.parentElement.classList.toggle('sorted', active);
    });
    const count = files.selected.size;
    $('#toolbar').innerHTML = count ? `<button class="btn btn-danger" data-action="files-delete-selected"><i class="fa-solid fa-trash"></i> Delete ${count} item(s)</button>` : '';
    $('#files-all').checked = sorted.length > 0 && sorted.every(f => files.selected.has(f.path));
}

function deleteFiles(paths) {
    const label = paths.length === 1 ? `“${paths[0].split('/').pop()}”` : `${paths.length} items`;
    openConfirm('Delete files?', `Permanently delete ${label} from the server disk? This cannot be undone.`, async () => {
        try {
            const res = await api('/api/files/delete', {method: 'POST', body: {paths}});
            paths.forEach(p => state.files.selected.delete(p));
            toast(`${res.deleted} item(s) deleted`);
            router();
        } catch (e) { toast(e.message, 'error'); }
    });
}

function updateStorage(data) {
    $('#storage-text').textContent = `${formatBytes(data.free) || '0 B'} free`;
    const bar = $('#storage-bar');
    bar.firstElementChild.style.width = `${data.percent}%`;
    bar.classList.toggle('warn', data.percent > 90);
}

async function refreshStorage() {
    try { updateStorage(await api('/api/storage')); } catch { /* ignore */ }
}

// ---------------------------------------------------------------- events

const withItem = fn => el => {
    const item = state.items.get(el.dataset.id);
    if (item) fn(item, el);
};

const actions = {
    'toggle-menu': () => toggleMenu(),
    'toggle-sidebar': () => storageSet('syncplex.sidebar', $('#sidebar').classList.toggle('collapsed') ? 'collapsed' : ''),
    'logout': async () => {
        await api('/api/auth/logout', {method: 'POST'}).catch(() => {});
        window.location.href = '/login';
    },
    'back': el => (el.dataset.href === 'history' ? history.back() : (window.location.hash = el.dataset.href)),
    'close-modal': closeModals,
    'reload': () => { state.serversLoadedAt = 0; router(); },
    'connections': el => openConnections(el.dataset.server),
    'set-connection': el => setConnection(el.dataset.server, el.dataset.uri),
    'open': withItem(item => { window.location.hash = itemRoute(item); }),
    'info': el => openInfo(el.dataset.id),
    'sync': withItem(item => syncItems([item])),
    'device-download': withItem(downloadToDevice),
    'sync-selected': () => { syncItems([...state.selection.values()]); clearSelection(); },
    'select-all': selectAll,
    'toggle-direction': () => setParams({dir: parseHash().params.get('dir') === 'asc' ? 'desc' : 'asc'}),
    'queue': el => queueAction(el.dataset.op),
    'task': el => taskAction(el.dataset.task, el.dataset.op),
    'file-sort': el => {
        const sort = state.files.sort;
        const col = el.dataset.col;
        sort.asc = sort.col === col ? !sort.asc : col === 'name' || col === 'type';
        sort.col = col;
        renderFileRows();
    },
    'file-delete': el => deleteFiles([el.dataset.path]),
    'files-delete-selected': () => deleteFiles([...state.files.selected]),
};

document.addEventListener('click', e => {
    // [data-stop] marks modal boxes, so clicks inside them never reach the backdrop's close action.
    const target = e.target.closest('[data-action], [data-stop]');
    if (target?.dataset.action) {
        e.preventDefault();
        e.stopPropagation();
        actions[target.dataset.action]?.(target, e);
        return;
    }
    const card = e.target.closest('[data-card]');
    if (card && !card.classList.contains('skeleton')) toggleSelect(card);
});

document.addEventListener('change', e => {
    const path = e.target.dataset?.fileCheck;
    if (path === undefined) return;
    if (e.target.checked) state.files.selected.add(path);
    else state.files.selected.delete(path);
    renderFileRows();
});

document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeModals();
});

window.addEventListener('hashchange', router);

// ---------------------------------------------------------------- boot

(async function init() {
    if (storageGet('syncplex.sidebar') === 'collapsed') $('#sidebar').classList.add('collapsed');
    let me;
    try {
        me = await api('/api/auth/me');
    } catch {
        return;
    }
    $('#user-name').textContent = me.user.username;
    const avatar = $('#user-avatar');
    avatar.textContent = initials(me.user.username);
    if (me.user.thumb) {
        const img = new Image();
        img.onload = () => avatar.replaceChildren(img);
        img.src = me.user.thumb;
    }

    router();
    refreshStorage();
    refreshQueueBadge();
    setInterval(refreshStorage, 60000);
    setInterval(() => { if (parseHash().path !== '/downloads') refreshQueueBadge(); }, BADGE_POLL_MS);
})();
