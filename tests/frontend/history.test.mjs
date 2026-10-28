// No network, browser packages, or personal Studio data. The DOM double below
// exercises controller behavior; a real-browser layout check is still needed.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

const sourceURL = (name) => new URL(`../../co_studio/frontend/js/${name}`, import.meta.url);
const api = await import(sourceURL('api.js'));
const { describeExecutionEvent } = await import(sourceURL('execution-timeline.js'));
const promptVersion = '0123456789abcdef0123456789abcdef';
const promptBinding = (overrides = {}) => ({
  status: 'bound', version_id: promptVersion, base_sha256: 'a'.repeat(64), reason: 'recorded', ...overrides,
});
const provenancePayload = (overrides = {}) => ({
  ...promptBinding(), provenance_version: 1, system_sha256: 'b'.repeat(64),
  launch_version_id: promptVersion, llm_call_id: 'call-1', model: 'recorded-model', reason: 'new_session', ...overrides,
});

test('timeline labels reflect recorded facts without inferring success or a running state', () => {
  for (const kind of ['llm_call', 'tool_call', 'llm_result', 'unknown']) {
    const item = describeExecutionEvent({ kind, payload: {}, duration_ms: null });
    assert.equal(item.failed, false);
    assert.equal(item.note, '');
    assert.doesNotMatch(item.label, /success|running/i);
  }
  assert.equal(describeExecutionEvent({ kind: 'tool_result', payload: { error: 'failed' }, duration_ms: 0 }).failed, true);
  assert.equal(describeExecutionEvent({ kind: 'llm_call', payload: { status: 'running' }, duration_ms: null }).note, '');
  assert.match(describeExecutionEvent({ kind: 'tool_result', payload: { name: '<script>' }, duration_ms: 0 }).label, /<script>/);
  assert.match(describeExecutionEvent({ kind: 'llm_result', payload: { status: 'timeout' }, duration_ms: 10 }).note, /10 ms/);
});

test('timeline provenance distinguishes recorded versions from unknown and malformed bindings', () => {
  const cases = [
    [provenancePayload(), '01234567'],
    [provenancePayload({ version_id: 'baseline' }), 'Original'],
    [provenancePayload({ reason: 'restored_session', launch_version_id: null, model: null }), '01234567'],
    [provenancePayload({ launch_version_id: 'baseline', llm_call_id: 'x'.repeat(512), model: 'm'.repeat(512) }), '01234567'],
    [provenancePayload({ status: 'unknown', version_id: null, base_sha256: null, system_sha256: null, reason: 'not_recorded' }), 'Not recorded'],
    ...['unverified_session', 'missing_session', 'system_modified', 'unverified_event'].map((reason) =>
      [provenancePayload({ status: 'unknown', version_id: null, base_sha256: null, reason }), 'Cannot confirm']),
    [null, 'Cannot confirm'], [{}, 'Cannot confirm'],
    ...[
      { provenance_version: undefined }, { provenance_version: '1' }, { provenance_version: 2 },
      { version_id: null }, { version_id: promptVersion.toUpperCase() }, { version_id: '<img src=x>' },
      { version_id: `${promptVersion}\n` }, { version_id: 'baseline\n' },
      { base_sha256: null }, { base_sha256: 'a'.repeat(63) }, { base_sha256: 'A'.repeat(64) },
      { base_sha256: `${'a'.repeat(64)}\n` },
      { system_sha256: null }, { system_sha256: 'b'.repeat(65) }, { system_sha256: 'B'.repeat(64) },
      { llm_call_id: undefined }, { llm_call_id: null }, { llm_call_id: '' }, { llm_call_id: 'two calls' },
      { llm_call_id: 'control\u0000' }, { llm_call_id: 'x'.repeat(513) },
      { reason: undefined }, { reason: null }, { reason: 'recorded' }, { reason: 'new-session' },
      ...['unverified_session', 'missing_session', 'system_modified', 'unverified_event'].map((reason) => ({ reason })),
      { launch_version_id: undefined }, { launch_version_id: 1 }, { launch_version_id: '' },
      { launch_version_id: promptVersion.toUpperCase() },
      { model: undefined }, { model: 1 }, { model: '' }, { model: 'm'.repeat(513) },
      { extra: 'unexpected field' },
      { status: 'running' },
    ].map((overrides) => [provenancePayload(overrides), 'Cannot confirm']),
  ];
  for (const [payload, note] of cases) {
    assert.deepEqual(describeExecutionEvent({ kind: 'prompt_provenance', payload, duration_ms: 0 }),
      { label: 'Base prompt', failed: false, note });
  }
});

test('timeline approval labels describe the record without claiming authorization', () => {
  const item = describeExecutionEvent({ kind: 'studio_approval',
    payload: { approval_id: 'request-1', action: 'decision', name: 'calculate', status: 'executing' } });
  assert.deepEqual(item, { label: 'Approval record · calculate', failed: false, note: 'decision · executing' });
  assert.doesNotMatch(item.label, /authorized|approved/i);
  assert.equal(describeExecutionEvent({ kind: 'studio_approval', payload: { status: 'rejected' } }).failed, true);
});

test('history API uses the shared GET wrapper and encodes opaque identifiers', async () => {
  const calls = [];
  const previous = globalThis.fetch;
  globalThis.fetch = async (...args) => {
    calls.push(args);
    return { ok: true, status: 200, json: async () => ({ marker: calls.length }) };
  };
  try {
    assert.deepEqual(await api.listSessions('alpha / beta', { offset: 40, limit: 20 }), { marker: 1 });
    const [listPath, listOptions] = calls[0];
    const listURL = new URL(listPath, 'http://localhost');
    assert.equal(listURL.pathname, '/api/agents/alpha%20%2F%20beta/sessions');
    assert.equal(listURL.searchParams.get('offset'), '40');
    assert.equal(listURL.searchParams.get('limit'), '20');
    assert.equal(listOptions.method, 'GET');
    assert.equal(listOptions.body, undefined);

    await api.getSession('alpha', 'chat/中文?#%', {
      turn_offset: 20, turn_limit: 10, event_offset: 50, event_limit: 25, turn_no: 2,
    });
    const [detailPath, detailOptions] = calls[1];
    const detailURL = new URL(detailPath, 'http://localhost');
    assert.equal(detailURL.pathname, '/api/agents/alpha/sessions/chat%2F%E4%B8%AD%E6%96%87%3F%23%25');
    assert.deepEqual(Object.fromEntries(detailURL.searchParams), {
      turn_offset: '20', turn_limit: '10', event_offset: '50', event_limit: '25', turn_no: '2',
    });
    assert.equal(detailOptions.method, 'GET');
  } finally {
    globalThis.fetch = previous;
  }
});

test('history API preserves zero offsets and omits an absent turn filter', async () => {
  const previous = globalThis.fetch;
  let path;
  globalThis.fetch = async (value) => {
    path = value;
    return { ok: true, status: 200, json: async () => ({}) };
  };
  try {
    await api.getSession('alpha', 'session', { turn_offset: 0, event_offset: 0 });
    const params = new URL(path, 'http://localhost').searchParams;
    assert.equal(params.get('turn_offset'), '0');
    assert.equal(params.get('event_offset'), '0');
    assert.equal(params.has('turn_no'), false);
    assert.equal(params.has('undefined'), false);
  } finally {
    globalThis.fetch = previous;
  }
});

test('history API surfaces backend errors as the existing ApiError', async () => {
  const previous = globalThis.fetch;
  globalThis.fetch = async () => ({
    ok: false, status: 404, statusText: 'Not Found',
    text: async () => JSON.stringify({ detail: 'Session not found' }),
  });
  try {
    await assert.rejects(api.getSession('alpha', 'gone'), (error) =>
      error instanceof api.ApiError && error.status === 404 && error.message === 'Session not found');
    globalThis.fetch = async () => { throw new Error('network down'); };
    await assert.rejects(api.listSessions('alpha'), (error) =>
      error instanceof api.ApiError && error.status === 0);
  } finally {
    globalThis.fetch = previous;
  }
});

test('archive API encodes opaque IDs, preserves paging/filter values and only issues GETs', async () => {
  const calls = [];
  const previous = globalThis.fetch;
  globalThis.fetch = async (...args) => {
    calls.push(args);
    return { ok: true, status: 200, json: async () => ({ marker: calls.length }) };
  };
  try {
    assert.deepEqual(await api.getHistoryStorage(), { marker: 1 });
    assert.equal(calls[0][0], '/api/history/storage');
    assert.deepEqual(await api.listArchives('alpha / 中文?#%', { offset: 7, limit: 4 }), { marker: 2 });
    const listURL = new URL(calls[1][0], 'http://localhost');
    assert.equal(listURL.pathname, '/api/agents/alpha%20%2F%20%E4%B8%AD%E6%96%87%3F%23%25/archives');
    assert.deepEqual(Object.fromEntries(listURL.searchParams), { offset: '7', limit: '4' });

    await api.getArchive('alpha / 中文?#%', 'opaque/capture 中文?#%', {
      turn_offset: 0, turn_limit: 10, event_offset: 13, event_limit: 25, turn_no: 3,
    });
    const detailURL = new URL(calls[2][0], 'http://localhost');
    assert.equal(detailURL.pathname, '/api/agents/alpha%20%2F%20%E4%B8%AD%E6%96%87%3F%23%25/archives/opaque%2Fcapture%20%E4%B8%AD%E6%96%87%3F%23%25');
    assert.deepEqual(Object.fromEntries(detailURL.searchParams), {
      turn_offset: '0', turn_limit: '10', event_offset: '13', event_limit: '25', turn_no: '3',
    });
    await api.getArchive('alpha', 'capture-1');
    const defaultParams = new URL(calls[3][0], 'http://localhost').searchParams;
    assert.equal(defaultParams.get('turn_offset'), '0');
    assert.equal(defaultParams.get('event_offset'), '0');
    assert.equal(defaultParams.has('turn_no'), false);
    assert.equal(defaultParams.has('undefined'), false);
    for (const [, options] of calls) {
      assert.equal(options.method, 'GET');
      assert.equal(options.body, undefined, 'reading History must never create a capture or start an Agent');
    }
  } finally {
    globalThis.fetch = previous;
  }
});

test('archive API preserves sanitized 503 errors instead of falling back to file requests', async () => {
  const calls = [];
  const previous = globalThis.fetch;
  globalThis.fetch = async (path) => {
    calls.push(path);
    return {
      ok: false, status: 503, statusText: 'Service Unavailable',
      text: async () => JSON.stringify({ detail: 'History archive unavailable' }),
    };
  };
  try {
    for (const request of [
      () => api.getHistoryStorage(), () => api.listArchives('alpha'), () => api.getArchive('alpha', 'capture-1'),
    ]) {
      await assert.rejects(request, (error) => error instanceof api.ApiError
        && error.status === 503 && error.message === 'History archive unavailable');
    }
    assert.equal(calls.length, 3);
    assert.equal(calls.some((path) => path.includes('/sessions')), false);
  } finally {
    globalThis.fetch = previous;
  }
});

test('History controller DOM hooks are present exactly once in the real shell', async () => {
  const source = await readFile(sourceURL('history.js'), 'utf8');
  const html = await readFile(new URL('../../co_studio/frontend/index.html', import.meta.url), 'utf8');
  const ids = new Set([...source.matchAll(/\$\('([^']+)'\)/g)].map((match) => `history-${match[1]}`));
  for (const id of ids) {
    assert.equal([...html.matchAll(new RegExp(`id="${id}"`, 'g'))].length, 1, `${id} must appear exactly once`);
  }
});

class ClassList {
  constructor(element) { this.element = element; }
  values() { return new Set(this.element.className.split(/\s+/).filter(Boolean)); }
  contains(name) { return this.values().has(name); }
  add(...names) { this.element.className = [...new Set([...this.values(), ...names])].join(' '); }
  remove(...names) { this.element.className = [...this.values()].filter((name) => !names.includes(name)).join(' '); }
  toggle(name, force) {
    const add = force ?? !this.contains(name);
    add ? this.add(name) : this.remove(name);
    return add;
  }
}

class Element {
  constructor(tagName = 'div', ownerDocument) {
    this.tagName = tagName.toUpperCase();
    this.ownerDocument = ownerDocument;
    this.children = [];
    this.parentNode = null;
    this.listeners = new Map();
    this.attributes = new Map();
    this.dataset = {};
    this.style = {};
    this.className = '';
    this.classList = new ClassList(this);
    this.hidden = false;
    this.disabled = false;
    this.value = '';
    this.id = '';
    this.scrollTop = 0;
    this._text = '';
  }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(''); }
  set textContent(value) { this._text = String(value ?? ''); this.children = []; }
  set innerHTML(_) { throw new Error('History must render untrusted content with textContent, not innerHTML'); }
  get childNodes() { return this.children; }
  get firstChild() { return this.children[0] ?? null; }
  get options() { return this.children.filter((child) => child.tagName === 'OPTION'); }
  append(...nodes) {
    for (let child of nodes) {
      if (typeof child === 'string') { const text = new Element('#text', this.ownerDocument); text.textContent = child; child = text; }
      if (child.parentNode) child.parentNode.children = child.parentNode.children.filter((item) => item !== child);
      child.parentNode = this;
      this.children.push(child);
    }
  }
  appendChild(child) { this.append(child); return child; }
  replaceChildren(...nodes) { this._text = ''; this.children = []; this.append(...nodes); }
  remove() {
    if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((child) => child !== this);
    this.parentNode = null;
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  removeAttribute(name) { this.attributes.delete(name); }
  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }
  removeEventListener(type, handler) {
    this.listeners.set(type, (this.listeners.get(type) ?? []).filter((value) => value !== handler));
  }
  async dispatchEvent(event) {
    await Promise.all((this.listeners.get(event.type) ?? []).map((handler) => handler({ ...event, target: event.target ?? this })));
    return true;
  }
  async click() { if (!this.disabled) await this.dispatchEvent({ type: 'click' }); }
  matches(selector) {
    if (selector.startsWith('#')) return this.id === selector.slice(1);
    if (selector.startsWith('.')) return this.classList.contains(selector.slice(1));
    return this.tagName.toLowerCase() === selector.toLowerCase();
  }
  querySelectorAll(selector) {
    return this.children.flatMap((child) => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null; }
  focus() { this.ownerDocument.activeElement = this; }
  scrollIntoView() {}
}

function makeDOM() {
  const document = {
    createElement: (tag) => new Element(tag, document),
    createDocumentFragment: () => new Element('fragment', document),
  };
  const root = new Element('div', document);
  root.id = 'history-view';
  const ids = [
    'history-agent', 'history-list-label', 'history-sync-message',
    'history-refresh', 'history-sessions', 'history-list-message',
    'history-more-sessions', 'history-detail', 'history-empty', 'history-session-id',
    'history-session-status', 'history-session-meta', 'history-warning', 'history-turns',
    'history-more-turns', 'history-event-filter', 'history-events', 'history-more-events',
    'history-detail-message', 'history-event-message', 'history-count',
    'history-turn-count', 'history-event-count', 'history-event-section',
    'history-advanced', 'history-capture-meta', 'history-versions-section',
    'history-versions-toggle', 'history-versions-panel', 'history-versions',
    'history-more-versions', 'history-version-message',
    'history-recovery', 'history-connection-error', 'history-update-notice', 'history-latest',
  ];
  const buttons = new Set(['history-refresh', 'history-more-sessions', 'history-more-turns', 'history-more-events', 'history-versions-toggle', 'history-more-versions']);
  for (const id of ids) {
    const tag = ['history-agent', 'history-event-filter'].includes(id) ? 'select' : buttons.has(id) ? 'button' : 'div';
    const element = document.createElement(tag);
    element.id = id;
    root.append(element);
  }
  const recovery = root.querySelector('#history-recovery');
  recovery.hidden = true;
  for (const id of ['history-connection-error', 'history-refresh']) {
    const element = root.querySelector(`#${id}`);
    element.remove();
    recovery.append(element);
  }
  root.querySelector('#history-connection-error').hidden = true;
  root.querySelector('#history-refresh').textContent = 'Retry';
  root.querySelector('#history-refresh').hidden = true;
  const scroll = document.createElement('div');
  scroll.className = 'history-scroll';
  root.querySelector('#history-detail').append(scroll);
  document.querySelector = (selector) => selector === '#history-view' ? root : root.querySelector(selector);
  return { root, document, get: (id) => root.querySelector(`#${id}`) };
}

const page = (items, next_offset = null, offset = 0, total = items.length) => ({ items, total, offset, limit: 20, next_offset });
const session = (id, overrides = {}) => ({
  schema_version: 1, agent_slug: 'alpha', session_id: id, source_status: 'done',
  source_revision: 3, created_at: 100, updated_at: 200, ...overrides,
});
const listing = (items = [], overrides = {}) => ({
  agent_slug: 'alpha', source: 'file', sync: { state: 'disabled' },
  sessions: page(items), source_exists: true, issue_counts: {}, ...overrides,
});
const turn = (number = 1, overrides = {}) => ({
  schema_version: 1, agent_slug: 'alpha', session_id: 'session-1', turn_no: number,
  prompt: `Question ${number}`, result: `Answer ${number}`, duration_ms: 12, finish_reason: 'natural', ...overrides,
});
const event = (number = 0, overrides = {}) => ({
  schema_version: 1, agent_slug: 'alpha', session_id: 'session-1', event_id: `event-${number}`,
  sequence: number, kind: 'tool_result', turn_no: 1, source_id: null, call_id: null,
  occurred_at: null, duration_ms: null, payload: { name: 'lookup', result: 'safe' }, ...overrides,
});
const detail = (id = 'session-1', overrides = {}) => ({
  session: session(id), turns: page([turn()]), events: page([event()]), issue_counts: {}, ...overrides,
});
const capture = (captureId = 'capture-1', overrides = {}) => ({
  ...session('session-1'), capture_id: captureId, archived_at: '2026-10-27T00:00:00Z', version_count: 1, ...overrides,
});
const archiveListing = (items = [capture()], overrides = {}) => listing(items, {
  source: 'archive', sync: { state: 'ok' }, ...overrides,
});
const archiveDetail = (captureId = 'capture-1', overrides = {}) => detail('session-1', {
  source: 'archive', capture_id: captureId, archived_at: '2026-10-27T00:00:00Z', ...overrides,
});
const version = (captureId = 'capture-1', overrides = {}) => ({
  capture_id: captureId, archived_at: '2026-10-27T00:00:00Z', session: session('session-1'), ...overrides,
});
const versionListing = (items = [version()], overrides = {}) => ({
  agent_slug: 'alpha', session_id: 'session-1', versions: page(items), ...overrides,
});
const agents = [{ slug: 'alpha', name: 'Alpha', state: 'stopped' }, { slug: 'beta', name: 'Beta', state: 'running' }];
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const flush = async () => { for (let index = 0; index < 32; index++) await Promise.resolve(); };

function fakeTimers() {
  const tasks = new Map(); let sequence = 0;
  return {
    tasks,
    setTimeout(callback, delay) { const id = ++sequence; tasks.set(id, { callback, delay }); return id; },
    clearTimeout(id) { tasks.delete(id); },
    async tick(delay = 200) {
      const ready = [...tasks.entries()].filter(([, task]) => task.delay === delay);
      for (const [id, task] of ready) { tasks.delete(id); task.callback(); }
      await flush();
    },
  };
}

async function mount(t, handlers = {}) {
  const dom = makeDOM();
  const previousDocument = globalThis.document;
  globalThis.document = dom.document;
  t.after(() => { globalThis.document = previousDocument; });
  const calls = { list: [], detail: [], archive: [], versions: [], unexpected: [] };
  const streams = [];
  const timers = fakeTimers();
  const client = {
    subscribeHistory(slug, handlers) {
      const stream = {
        slug, closed: 0,
        close() { this.closed++; },
        emit(type = 'history.changed', data = {}) { handlers.onEvent({ type, id: 'test-cursor', data: { agent_slug: slug, ...data } }); },
        status(value) { handlers.onStatus(value); },
      };
      streams.push(stream);
      stream.status('live');
      return stream;
    },
    listHistoryVersions(slug, id, options) {
      calls.versions.push({ slug, id, options });
      return (handlers.versions ?? (() => Promise.resolve(versionListing())))(slug, id, options);
    },
    getArchive(slug, id, options) {
      calls.archive.push({ slug, id, options });
      return (handlers.archive ?? ((_, selected) => Promise.resolve(archiveDetail(selected))))(slug, id, options);
    },
    listHistory(slug, options) {
      calls.list.push({ slug, options });
      return (handlers.list ?? (() => Promise.resolve(listing([session('session-1')]))))(slug, options);
    },
    getSession(slug, id, options) {
      calls.detail.push({ slug, id, options });
      return (handlers.detail ?? ((_, selected) => Promise.resolve(detail(selected))))(slug, id, options);
    },
  };
  const { createHistoryView } = await import(sourceURL('history.js'));
  const readOnlyClient = new Proxy(client, {
    get(target, key) {
      if (key in target) return target[key];
      calls.unexpected.push(String(key));
      throw new Error(`History may only use unified read-only API methods, not ${String(key)}`);
    },
  });
  const controller = createHistoryView({ root: dom.root, api: readOnlyClient, timers });
  t.after(() => controller.close());
  return { ...dom, controller, calls, streams, timers };
}

async function open(view, entries = agents) {
  view.controller.open(entries);
  await flush();
}
async function selectSession(view, index = 0) {
  const buttons = view.get('history-sessions').querySelectorAll('button');
  assert.ok(buttons[index], `expected a selectable session at index ${index}`);
  await buttons[index].click();
  await flush();
}
async function selectAgent(view, slug) {
  view.get('history-agent').value = slug;
  await view.get('history-agent').dispatchEvent({ type: 'change' });
  await flush();
}
function assertRecovery(view, visible) {
  assert.equal(view.get('history-recovery').hidden, !visible);
  assert.equal(view.get('history-refresh').hidden, !visible);
  assert.equal(view.get('history-refresh').parentNode, view.get('history-recovery'));
}

test('History header has no live label or refresh controls and Retry starts hidden in the sidebar', async (t) => {
  const html = await readFile(new URL('../../co_studio/frontend/index.html', import.meta.url), 'utf8');
  const header = html.match(/<div id="history-view"[^>]*>\s*<header[^>]*>([\s\S]*?)<\/header>/)?.[1];
  assert.ok(header, 'the History header must exist');
  assert.doesNotMatch(header, /<button\b|history-live|history-refresh|history-connection-error|head-actions/);
  assert.doesNotMatch(html, /id="history-live"/);
  const sidebar = html.match(/<aside class="[^"]*history-master[^"]*">([\s\S]*?)<\/aside>/)?.[1];
  assert.ok(sidebar, 'the History sidebar must exist');
  assert.match(sidebar, /<[^>]+id="history-recovery"[^>]*\bhidden\b[^>]*>/);
  assert.match(sidebar, /id="history-connection-error"[^>]*\bhidden\b/);
  assert.match(sidebar, /<button\b[^>]*id="history-refresh"[^>]*>Retry<\/button>/);

  const view = await mount(t, { list: async () => archiveListing() });
  await open(view);
  assert.equal(view.get('history-live'), null);
  assert.equal(view.get('history-connection-error').hidden, true);
  assert.equal(view.get('history-connection-error').textContent, '');
  assertRecovery(view, false);
  view.streams[0].status('connecting');
  assert.equal(view.get('history-connection-error').hidden, true);
  assert.equal(view.get('history-connection-error').textContent, '');
  assertRecovery(view, false);
});

test('unified History API encodes query session IDs and uses GET only', async () => {
  const calls = [];
  const previous = globalThis.fetch;
  globalThis.fetch = async (...args) => {
    calls.push(args);
    return { ok: true, status: 200, json: async () => ({}) };
  };
  try {
    await api.listHistory('alpha / 中文', { offset: 7, limit: 4 });
    await api.listHistoryVersions('alpha / 中文', 'session/中文?#%', { offset: 9, limit: 3 });
    const listURL = new URL(calls[0][0], 'http://localhost');
    assert.equal(listURL.pathname, '/api/agents/alpha%20%2F%20%E4%B8%AD%E6%96%87/history');
    assert.deepEqual(Object.fromEntries(listURL.searchParams), { offset: '7', limit: '4' });
    const versionURL = new URL(calls[1][0], 'http://localhost');
    assert.equal(versionURL.pathname, '/api/agents/alpha%20%2F%20%E4%B8%AD%E6%96%87/history/versions');
    assert.deepEqual(Object.fromEntries(versionURL.searchParams), { session_id: 'session/中文?#%', offset: '9', limit: '3' });
    assert.ok(calls.every(([, options]) => options.method === 'GET' && options.body === undefined));
  } finally { globalThis.fetch = previous; }
});

test('History has only Agent and Sessions in the main view and never discovers storage', async (t) => {
  const view = await mount(t, { list: async () => archiveListing() });
  await open(view);
  assert.equal(view.calls.list.length, 1);
  assert.equal(view.calls.archive.length + view.calls.detail.length, 0);
  assert.equal(view.get('history-source'), null);
  assert.equal(view.get('history-source-note'), null);
  assert.deepEqual(view.calls.unexpected, []);
  const html = await readFile(new URL('../../co_studio/frontend/index.html', import.meta.url), 'utf8');
  assert.doesNotMatch(html, /id="history-source"|id="history-source-note"/);
  assert.match(html, /id="history-list-label">Sessions</);
});

test('History uses files normally when the server has no archive configured', async (t) => {
  const view = await mount(t);
  await open(view);
  await selectSession(view);
  assert.equal(view.calls.detail.length, 1);
  assert.equal(view.calls.archive.length, 0);
  assert.equal(view.get('history-sync-message').hidden, true);
  assert.equal(view.get('history-versions-section').hidden, true);
  assert.deepEqual(view.calls.unexpected, []);
});

test('History initial database 503 exposes Retry for the same unified endpoint', async (t) => {
  let attempts = 0;
  const view = await mount(t, { list: async () => {
    if (++attempts === 1) throw new api.ApiError('History archive unavailable', 503);
    return archiveListing([]);
  } });
  await open(view);
  assert.match(view.get('history-list-message').textContent, /archive unavailable/);
  assert.equal(view.get('history-list-message').classList.contains('is-error'), true);
  assert.equal(view.get('history-list-message').classList.contains('logs-empty'), false);
  assert.equal(view.calls.detail.length, 0);
  assertRecovery(view, true);
  assert.equal(view.get('history-refresh').disabled, false);
  await view.get('history-refresh').click();
  await flush();
  assert.equal(attempts, 2);
  assert.equal(view.get('history-list-message').textContent, 'No saved sessions.');
  assert.equal(view.get('history-list-message').classList.contains('logs-empty'), true);
  assertRecovery(view, false);
  assert.deepEqual(view.calls.unexpected, []);
});

for (const state of ['pending', 'syncing', 'warning', 'error', 'stopped']) {
  test(`History shows ${state} reliably without claiming an empty history`, async (t) => {
    const view = await mount(t, { list: async () => archiveListing([], { sync: { state, message: 'Server context' } }) });
    await open(view);
    assert.match(view.get('history-list-message').textContent, /sync/i);
    assert.doesNotMatch(view.get('history-list-message').textContent, /no.*session|no.*histor/i);
    assert.equal(view.get('history-list-message').hidden, false);
    assertRecovery(view, state === 'error' || state === 'warning');
  });
}

test('History keeps sync notices compact and hides successful sync', async (t) => {
  let state = 'pending';
  const view = await mount(t, { list: async () => archiveListing([capture()], { sync: { state } }) });
  await open(view);
  assert.match(view.get('history-sync-message').textContent, /pending/i);
  assertRecovery(view, false);
  state = 'ok';
  await view.get('history-refresh').click();
  await flush();
  assert.equal(view.get('history-sync-message').hidden, true);
  assertRecovery(view, false);
});

test('History lists each session once while advanced versions page by capture ID', async (t) => {
  const view = await mount(t, {
    list: async (_, options) => options.offset === 7
      ? archiveListing([capture('capture-2', { version_count: 3 })], { sessions: page([capture('capture-2', { version_count: 3 })], null, 7, 1) })
      : archiveListing([capture('capture-1', { version_count: 3 })], { sessions: page([capture('capture-1', { version_count: 3 })], 7, 0, 1) }),
    versions: async (_, id, options) => options.offset === 8
      ? versionListing([version('capture-1'), version('older/capture 中文?#%')], { versions: page([version('capture-1'), version('older/capture 中文?#%')], null, 8, 3) })
      : versionListing([version('capture-2')], { versions: page([version('capture-2')], 8, 0, 3) }),
  });
  await open(view);
  await view.get('history-more-sessions').click();
  await flush();
  assert.equal(view.get('history-sessions').querySelectorAll('button').length, 1);
  assert.equal(view.get('history-count').textContent, '(1)');
  await selectSession(view);
  assert.equal(view.calls.archive[0].id, 'capture-2');
  assert.equal(view.get('history-advanced').open, false);
  assert.equal(view.get('history-versions-panel').hidden, true);
  await view.get('history-versions-toggle').click();
  await flush();
  assert.equal(view.calls.versions[0].id, 'session-1');
  await view.get('history-more-versions').click();
  await flush();
  assert.equal(view.calls.versions[1].options.offset, 8);
  await view.get('history-versions').querySelectorAll('button')[2].click();
  await flush();
  assert.equal(view.calls.archive.at(-1).id, 'older/capture 中文?#%');
  assert.equal(view.get('history-versions').querySelectorAll('button').length, 3);
  assert.equal(view.get('history-versions-panel').hidden, false);
  assert.match(view.get('history-capture-meta').textContent, /older\/capture/);
  assert.deepEqual(view.calls.unexpected, []);
});

test('History archive turn/event paging and filters stay pinned to the selected capture', async (t) => {
  const captureId = 'fixed-capture/中文?#%';
  const view = await mount(t, {
    list: async () => archiveListing([capture(captureId)]),
    archive: async (_, id, options = {}) => archiveDetail(id, {
      turns: options.turn_offset === 8 ? page([turn(9)], null, 8, 9) : page([turn(1)], 8, 0, 9),
      events: options.turn_no === 1 ? page([event(1)])
        : options.event_offset === 13 ? page([event(13)], null, 13, 14) : page([event(0)], 13, 0, 14),
    }),
  });
  await open(view);
  await selectSession(view);
  await view.get('history-more-turns').click();
  await flush();
  await view.get('history-more-events').click();
  await flush();
  view.get('history-event-filter').value = '1';
  await view.get('history-event-filter').dispatchEvent({ type: 'change' });
  await flush();
  assert.equal(view.calls.archive.length, 4);
  assert.ok(view.calls.archive.every((call) => call.id === captureId));
  assert.equal(view.calls.archive[1].options.turn_offset, 8);
  assert.equal(view.calls.archive[2].options.event_offset, 13);
  assert.equal(view.calls.archive[3].options.turn_no, 1);
  assert.equal(view.calls.detail.length, 0);
});

// These direct callback regressions do not imply Retry is visible in normal UI.
test('History refresh callback upgrades a selected session to the server-selected latest capture', async (t) => {
  let generation = 0;
  const view = await mount(t, { list: async () => archiveListing([capture(`capture-${++generation}`)]) });
  await open(view);
  await selectSession(view);
  await view.get('history-refresh').click();
  await flush();
  assert.deepEqual(view.calls.archive.map((call) => call.id), ['capture-1', 'capture-2']);
  assert.match(view.get('history-capture-meta').textContent, /capture-2/);
});

test('History preserves a page-2 session capture when the refresh callback cannot resolve it on the first page', async (t) => {
  const view = await mount(t, { list: async (_, options) => options.offset === 1
    ? archiveListing([capture('older-capture', { session_id: 'older-session' })], { sessions: page([capture('older-capture', { session_id: 'older-session' })], null, 1, 2) })
    : archiveListing([capture()], { sessions: page([capture()], 1, 0, 2) }),
    archive: async (_, id) => archiveDetail(id, { session: session('older-session') }),
  });
  await open(view);
  await view.get('history-more-sessions').click();
  await flush();
  await selectSession(view, 1);
  await view.get('history-refresh').click();
  await flush();
  assert.deepEqual(view.calls.archive.map((call) => call.id), ['older-capture', 'older-capture']);
  assert.match(view.get('history-list-message').textContent, /outside this page/);
});

for (const action of ['agent change', 'close', 'refresh', 'version selection']) {
  test(`History ignores pending versions after ${action}`, async (t) => {
    const oldVersions = deferred();
    const view = await mount(t, {
      list: async () => archiveListing([capture('capture-1', { version_count: 2 })]),
      versions: (_, id, options) => options.offset === 1 ? oldVersions.promise
        : Promise.resolve(versionListing([version('capture-1'), version('capture-2')], { versions: page([version('capture-1'), version('capture-2')], 1, 0, 3) })),
    });
    await open(view);
    await selectSession(view);
    await view.get('history-versions-toggle').click();
    await flush();
    void view.get('history-more-versions').click();
    await flush();
    if (action === 'agent change') await selectAgent(view, 'beta');
    if (action === 'close') view.controller.close();
    if (action === 'refresh') { await view.get('history-refresh').click(); await flush(); }
    if (action === 'version selection') { await view.get('history-versions').querySelectorAll('button')[1].click(); await flush(); }
    oldVersions.resolve(versionListing([version('stale-version')], { versions: page([version('stale-version')], null, 1, 3) }));
    await flush();
    assert.ok(view.get('history-versions').querySelectorAll('button').every((button) => !button.title.includes('stale-version')));
    assert.doesNotMatch(view.get('history-version-message').textContent, /Loading/);
  });
}

test('History old capture detail and pages cannot replace a new version or refreshed capture', async (t) => {
  const oldDetail = deferred();
  const oldPage = deferred();
  let loads = 0;
  const view = await mount(t, {
    list: async () => archiveListing([capture(++loads === 1 ? 'capture-1' : 'capture-2')]),
    archive: (_, id, options = {}) => id === 'capture-1' ? oldDetail.promise
      : options.turn_offset ? oldPage.promise : Promise.resolve(archiveDetail(id, { turns: page([turn(1)], 1, 0, 2) })),
    versions: async () => versionListing([version('capture-2'), version('capture-3')]),
  });
  await open(view);
  void view.get('history-sessions').querySelector('button').click();
  await flush();
  await view.get('history-refresh').click();
  await flush();
  oldDetail.resolve(archiveDetail('capture-1', { turns: page([turn(1, { prompt: 'stale detail' })]) }));
  await flush();
  assert.doesNotMatch(view.get('history-turns').textContent, /stale detail/);
  void view.get('history-more-turns').click();
  await flush();
  await view.get('history-versions-toggle').click();
  await flush();
  await view.get('history-versions').querySelectorAll('button')[1].click();
  await flush();
  oldPage.resolve(archiveDetail('capture-2', { turns: page([turn(2, { prompt: 'stale page' })]) }));
  await flush();
  assert.doesNotMatch(view.get('history-turns').textContent, /stale page/);
  assert.match(view.get('history-capture-meta').textContent, /capture-3/);
});

for (const malformed of ['wrong source', 'wrong capture ID', 'wrong session ID']) {
  test(`History rejects archive detail with ${malformed}`, async (t) => {
    const view = await mount(t, {
      list: async () => archiveListing(),
      archive: async () => archiveDetail('capture-1', {
        ...(malformed === 'wrong source' ? { source: 'file' } : malformed === 'wrong capture ID' ? { capture_id: 'other' } : { session: session('other') }),
        turns: page([turn(1, { prompt: 'Wrong content' })]),
      }),
    });
    await open(view);
    await selectSession(view);
    assert.equal(view.get('history-detail').hidden, true);
    assert.doesNotMatch(view.get('history-turns').textContent, /Wrong content/);
    assert.match(view.get('history-empty').textContent, /did not match/i);
  });
}

test('History rejects mismatched capture pages while retaining current turns and events', async (t) => {
  const view = await mount(t, {
    list: async () => archiveListing(),
    archive: async (_, id, options = {}) => options.turn_offset || options.event_offset
      ? archiveDetail('other', { turns: page([turn(2, { prompt: 'Wrong turn' })]), events: page([event(1, { payload: { result: 'Wrong event' } })]) })
      : archiveDetail(id, { turns: page([turn(1)], 1, 0, 2), events: page([event(0)], 1, 0, 2) }),
  });
  await open(view);
  await selectSession(view);
  await view.get('history-more-turns').click();
  await flush();
  await view.get('history-more-events').click();
  await flush();
  assert.match(view.get('history-turns').textContent, /Question 1/);
  assert.doesNotMatch(view.get('history-turns').textContent, /Wrong turn/);
  assert.doesNotMatch(view.get('history-events').textContent, /Wrong event/);
  assert.match(view.get('history-detail-message').textContent, /did not match/);
  assert.match(view.get('history-event-message').textContent, /did not match/);
});

test('History renders archive metadata, versions and selection warnings only as plain text', async (t) => {
  const attack = '<img src=x onerror="attack()"><script>attack()</script>';
  const view = await mount(t, {
    list: async () => archiveListing([capture('capture-1', { session_id: attack, selection_warning: attack })]),
    archive: async (_, id) => archiveDetail(id, { session: session(attack), turns: page([turn(1, { prompt: attack, result: attack })]) }),
    versions: async () => versionListing([version(attack, { session: session(attack) })], { session_id: attack }),
  });
  await open(view);
  await selectSession(view);
  await view.get('history-versions-toggle').click();
  await flush();
  assert.ok(view.get('history-turns').textContent.includes(attack));
  assert.ok(view.get('history-detail-message').textContent.includes(attack));
  assert.equal(view.root.querySelectorAll('script').length + view.root.querySelectorAll('img').length, 0);
  assert.deepEqual(view.calls.unexpected, []);
});

test('History keeps a selected capture fixed when a stale filtered event request finishes', async (t) => {
  const oldEvents = deferred();
  const view = await mount(t, {
    list: async () => archiveListing(),
    archive: (_, id, options = {}) => options.turn_no === 1 ? oldEvents.promise
      : Promise.resolve(archiveDetail(id, { events: page([event(0, { payload: { result: id } })]) })),
    versions: async () => versionListing([version('capture-1'), version('capture-2')]),
  });
  await open(view);
  await selectSession(view);
  view.get('history-event-filter').value = '1';
  void view.get('history-event-filter').dispatchEvent({ type: 'change' });
  await flush();
  await view.get('history-versions-toggle').click();
  await flush();
  await view.get('history-versions').querySelectorAll('button')[1].click();
  await flush();
  oldEvents.resolve(archiveDetail('capture-1', { events: page([event(1, { payload: { result: 'stale filtered event' } })]) }));
  await flush();
  assert.match(view.get('history-events').textContent, /capture-2/);
  assert.doesNotMatch(view.get('history-events').textContent, /stale filtered event/);
  assert.equal(view.get('history-event-message').textContent, '');
});

test('History version pages retry the same offset and prevent duplicate requests', async (t) => {
  const pending = deferred();
  let attempts = 0;
  const view = await mount(t, {
    list: async () => archiveListing(),
    versions: async (_, id, options) => {
      if (!options.offset) return versionListing([version()], { versions: page([version()], 4, 0, 2) });
      if (++attempts === 1) return pending.promise;
      return versionListing([version('older')], { versions: page([version('older')], null, 4, 2) });
    },
  });
  await open(view);
  await selectSession(view);
  await view.get('history-versions-toggle').click();
  await flush();
  void view.get('history-more-versions').click();
  await flush();
  assert.equal(view.get('history-more-versions').disabled, true);
  await view.get('history-more-versions').click();
  assert.equal(attempts, 1);
  pending.reject(new Error('Version page unavailable'));
  await flush();
  assert.equal(view.get('history-versions').querySelectorAll('button').length, 1);
  assert.match(view.get('history-version-message').textContent, /Load more versions/);
  assertRecovery(view, true);
  await view.get('history-more-versions').click();
  await flush();
  assert.deepEqual(view.calls.versions.map((call) => call.options.offset), [0, 4, 4]);
  assert.equal(view.get('history-versions').querySelectorAll('button').length, 2);
  assertRecovery(view, false);
});

test('History validates archive list and version identities before rendering', async (t) => {
  let valid = false;
  const view = await mount(t, {
    list: async () => valid ? archiveListing() : archiveListing([session('missing-capture')]),
    versions: async () => versionListing([version('other', { session: session('wrong-session') })]),
  });
  await open(view);
  assert.match(view.get('history-list-message').textContent, /snapshot identities/);
  assert.equal(view.get('history-sessions').querySelectorAll('button').length, 0);
  valid = true;
  await view.get('history-refresh').click();
  await flush();
  await selectSession(view);
  await view.get('history-versions-toggle').click();
  await flush();
  assert.match(view.get('history-version-message').textContent, /did not match/);
  assert.equal(view.get('history-versions').querySelectorAll('button').length, 0);
});

test('History cannot merge a changed server source while paging sessions', async (t) => {
  const view = await mount(t, { list: async (_, options) => options.offset
    ? listing([session('file-row')])
    : archiveListing([capture()], { sessions: page([capture()], 5, 0, 2) }),
  });
  await open(view);
  await selectSession(view);
  await view.get('history-more-sessions').click();
  await flush();
  assert.match(view.get('history-list-message').textContent, /source changed/);
  assert.doesNotMatch(view.get('history-sessions').textContent, /file-row/);
  assert.equal(view.calls.detail.length, 0);
  assert.equal(view.get('history-detail').hidden, false);
});

test('History does not restore the old Agent selection after a pending refresh callback', async (t) => {
  const refresh = deferred();
  let attempts = 0;
  const view = await mount(t, { list: (slug) => slug === 'alpha' && ++attempts === 2
    ? refresh.promise : Promise.resolve(archiveListing([capture(slug, { session_id: `${slug}-session` })])),
    archive: async (_, id) => archiveDetail(id, { session: session(`${id}-session`) }),
  });
  await open(view);
  await selectSession(view);
  void view.get('history-refresh').click();
  await flush();
  assert.equal(view.get('history-refresh').disabled, true);
  await view.get('history-refresh').click();
  assert.equal(attempts, 2);
  await selectAgent(view, 'beta');
  refresh.reject(new Error('Stale refresh failure'));
  await flush();
  assert.match(view.get('history-sessions').textContent, /beta-session/);
  assert.doesNotMatch(view.get('history-list-message').textContent, /Stale refresh failure/);
  assert.equal(view.get('history-detail').hidden, true);
  assert.equal(view.calls.archive.length, 1);
});

test('History includes stopped agents and waits for a session selection', async (t) => {
  const view = await mount(t);
  await open(view);
  assert.equal(view.get('history-agent').options.length, 2);
  assert.deepEqual(view.get('history-agent').options.map((option) => option.value), ['alpha', 'beta']);
  assert.equal(view.get('history-agent').options[0].textContent, 'Alpha');
  assert.equal(view.get('history-count').textContent, '(1)');
  assert.equal(view.get('history-empty').textContent, 'Select a session to view its history.');
  assert.equal(view.get('history-empty').hidden, false);
  assert.equal(view.calls.list[0].slug, 'alpha');
  assert.equal(view.calls.detail.length, 0);
  await selectSession(view);
  assert.equal(view.calls.detail[0].id, 'session-1');
  assert.equal(view.get('history-empty').hidden, true);
  assert.ok(view.get('history-turns').textContent.includes('Question 1'));
  assert.ok(view.get('history-turns').textContent.includes('Answer 1'));
});

test('History renders absent metadata without inventing success or zero duration', async (t) => {
  const view = await mount(t, {
    list: async () => listing([session('session-1', { source_status: null, updated_at: null, created_at: null })]),
    detail: async () => detail('session-1', {
      turns: page([turn(1, { prompt: null, result: null, duration_ms: null, finish_reason: null }), turn(2, { duration_ms: 0 })]),
      events: page([event(0, { turn_no: null, duration_ms: null, kind: 'unknown' })]),
    }),
  });
  await open(view);
  assert.match(view.get('history-sessions').textContent, /unknown|not recorded/i);
  await selectSession(view);
  assert.match(view.get('history-turns').textContent, /not recorded/i);
  assert.match(view.get('history-turns').textContent, /0\s*ms/);
  assert.doesNotMatch(view.get('history-turns').textContent, /undefined|null/);
  assert.match(view.get('history-events').textContent, /unassigned|unknown|not recorded/i);
  assert.doesNotMatch(view.get('history-session-status').textContent, /succeed|success/i);
});

test('turn metadata shows only recorded base prompt provenance without looking up current prompts', async (t) => {
  const cases = [
    [undefined, 'Not recorded'], [null, 'Not recorded'],
    [promptBinding(), '01234567'], [promptBinding({ version_id: 'baseline' }), 'Original'],
    [promptBinding({ status: 'unknown', version_id: null, base_sha256: null, reason: 'not_recorded' }), 'Not recorded'],
    ...['incomplete', 'invalid', 'unbound', 'conflict'].map((reason) =>
      [promptBinding({ status: 'unknown', version_id: null, base_sha256: null, reason }), 'Cannot confirm']),
    [{}, 'Cannot confirm'], ['baseline', 'Cannot confirm'],
    ...[
      { version_id: null }, { version_id: 'a'.repeat(31) }, { version_id: promptVersion.toUpperCase() },
      { version_id: '<img src=x>' }, { base_sha256: null }, { base_sha256: 'A'.repeat(64) },
      { status: 'running' }, { status: 'unknown', reason: '<script>' },
    ].map((overrides) => [promptBinding(overrides), 'Cannot confirm']),
  ];
  const view = await mount(t, { detail: async () => detail('session-1', {
    turns: page(cases.map(([prompt_provenance], index) => turn(index + 1, { prompt_provenance }))),
  }) });
  await open(view); await selectSession(view);
  const cards = view.get('history-turns').querySelectorAll('article');
  assert.equal(cards.length, cases.length);
  cards.forEach((card, index) => {
    assert.equal(card.querySelector('.history-note').textContent,
      `Duration: 12 ms · Finish reason: natural · Base prompt: ${cases[index][1]}`);
    assert.ok(card.textContent.includes(`Question ${index + 1}`));
    assert.ok(card.textContent.includes(`Answer ${index + 1}`));
    assert.equal(card.querySelectorAll('button').length, 1, 'provenance is read-only metadata');
    assert.equal(card.querySelectorAll('a').length, 0);
  });
  assert.equal(view.get('history-turns').querySelectorAll('img').length, 0);
  assert.equal(view.get('history-turns').querySelectorAll('script').length, 0);
  assert.deepEqual(view.calls.unexpected, [], 'History must not access getPrompts or another current-prompt API');
  assert.equal(view.calls.versions.length, 0);
});

test('History empty file has no recovery control; reopening reads again and a failure exposes Retry', async (t) => {
  let attempt = 0;
  const view = await mount(t, {
    list: async () => {
      attempt++;
      if (attempt === 1) return listing([], { source_exists: false });
      if (attempt === 2) throw new Error('Cannot read saved history');
      return listing([session('recovered')]);
    },
  });
  await open(view);
  assert.equal(view.get('history-sessions').querySelectorAll('button').length, 0);
  assert.equal(view.get('history-list-message').textContent, 'No saved sessions.');
  assert.equal(view.get('history-count').textContent, '(0)');
  assert.equal(view.get('history-list-message').classList.contains('logs-empty'), true);
  assert.equal(view.get('history-list-message').classList.contains('history-empty-message'), true);
  assertRecovery(view, false);
  view.controller.close();
  await open(view);
  assert.match(view.get('history-list-message').textContent, /Cannot read saved history/);
  assert.equal(view.get('history-list-message').hidden, false);
  assert.equal(view.get('history-list-message').classList.contains('is-error'), true);
  assert.equal(view.get('history-list-message').classList.contains('logs-empty'), false);
  assert.equal(view.get('history-list-message').classList.contains('history-empty-message'), false);
  assertRecovery(view, true);
  await view.get('history-refresh').click();
  await flush();
  assert.match(view.get('history-sessions').textContent, /recovered/);
  assert.equal(view.get('history-list-message').hidden, true);
  assert.equal(view.get('history-list-message').classList.contains('logs-empty'), false);
  assert.equal(view.get('history-more-sessions').hidden || view.get('history-more-sessions').disabled, true);
  assertRecovery(view, false);
});

test('History uses concise shared empty-state styling for a present but empty file', async (t) => {
  const view = await mount(t, { list: async () => listing([], { source_exists: true }) });
  await open(view);
  const empty = view.get('history-list-message');
  assert.equal(empty.textContent, 'No readable sessions.');
  assert.equal(empty.hidden, false);
  assert.equal(empty.classList.contains('logs-empty'), true);
  assert.equal(empty.classList.contains('history-empty-message'), true);
  assert.equal(empty.classList.contains('is-error'), false);
  assert.equal(view.get('history-empty').textContent, 'Select a session to view its history.');
  assert.equal(view.get('history-count').textContent, '(0)');
});

test('History shows loaded/total while paging and a compact total after the last page', async (t) => {
  const view = await mount(t, {
    list: async (_, options = {}) => options.offset === 1
      ? listing([session('second')], { sessions: page([session('second')], null, 1, 2) })
      : listing([session('first')], { sessions: page([session('first')], 1, 0, 2) }),
  });
  await open(view);
  assert.equal(view.get('history-count').textContent, '(1/2)');
  await view.get('history-more-sessions').click();
  await flush();
  assert.equal(view.get('history-count').textContent, '(2)');
});

test('History ignores a stale session list after switching agents', async (t) => {
  const oldRequest = deferred();
  const view = await mount(t, {
    list: (slug) => slug === 'alpha' ? oldRequest.promise : Promise.resolve(listing([session('beta-session', { agent_slug: 'beta' })])),
  });
  await open(view);
  await selectAgent(view, 'beta');
  assert.match(view.get('history-sessions').textContent, /beta-session/);
  oldRequest.resolve(listing([session('stale-alpha-session')]));
  await flush();
  assert.match(view.get('history-sessions').textContent, /beta-session/);
  assert.doesNotMatch(view.get('history-sessions').textContent, /stale-alpha-session/);
});

test('History ignores an old session response after a new selection', async (t) => {
  const oldRequest = deferred();
  const view = await mount(t, {
    list: async () => listing([session('first'), session('second')]),
    detail: (_, id) => id === 'first' ? oldRequest.promise : Promise.resolve(detail(id, { turns: page([turn(1, { prompt: 'Second session question' })]) })),
  });
  await open(view);
  // A click handler intentionally does not await the request itself.
  void view.get('history-sessions').querySelectorAll('button')[0].click();
  await flush();
  await selectSession(view, 1);
  oldRequest.resolve(detail('first', { turns: page([turn(1, { prompt: 'Stale question' })]) }));
  await flush();
  assert.match(view.get('history-session-id').textContent, /second/);
  assert.match(view.get('history-turns').textContent, /Second session question/);
  assert.doesNotMatch(view.get('history-turns').textContent, /Stale question/);
});

test('History invalidates in-flight work when closed', async (t) => {
  const oldRequest = deferred();
  let first = true;
  const view = await mount(t, {
    list: () => { if (first) { first = false; return oldRequest.promise; } return Promise.resolve(listing([session('fresh')])); },
  });
  await open(view);
  view.controller.close();
  await open(view);
  oldRequest.resolve(listing([session('closed-request')]));
  await flush();
  assert.match(view.get('history-sessions').textContent, /fresh/);
  assert.doesNotMatch(view.get('history-sessions').textContent, /closed-request/);
});

test('History uses next_offset for session pages and keeps existing rows', async (t) => {
  const view = await mount(t, {
    list: async (_, options) => (options?.offset ?? 0) === 0
      ? listing([session('first-page')], { sessions: page([session('first-page')], 7, 0, 8) })
      : listing([session('last-page')], { sessions: page([session('last-page')], null, 7, 8) }),
  });
  await open(view);
  await view.get('history-more-sessions').click();
  await flush();
  assert.equal(view.calls.list[1].options.offset, 7);
  assert.match(view.get('history-sessions').textContent, /first-page/);
  assert.match(view.get('history-sessions').textContent, /last-page/);
  assert.equal(view.get('history-more-sessions').hidden || view.get('history-more-sessions').disabled, true);
});

test('History independently pages turns and events without discarding either collection', async (t) => {
  const view = await mount(t, {
    detail: async (_, id, options = {}) => detail(id, {
      turns: options.turn_offset === 8 ? page([turn(9)], null, 8, 9) : page([turn(1)], 8, 0, 9),
      events: options.event_offset === 13 ? page([event(13)], null, 13, 14) : page([event(0)], 13, 0, 14),
    }),
  });
  await open(view);
  await selectSession(view);
  await view.get('history-more-turns').click();
  await flush();
  assert.equal(view.calls.detail.at(-1).options.turn_offset, 8);
  assert.match(view.get('history-turns').textContent, /Question 1/);
  assert.match(view.get('history-turns').textContent, /Question 9/);
  await view.get('history-more-events').click();
  await flush();
  assert.equal(view.calls.detail.at(-1).options.event_offset, 13);
  assert.match(view.get('history-turns').textContent, /Question 9/);
  assert.ok(view.get('history-events').querySelectorAll('details').length >= 2);
});

test('History refuses to merge pages from different source revisions', async (t) => {
  const view = await mount(t, {
    detail: async (_, id, options = {}) => options.turn_offset
      ? detail(id, { session: session(id, { source_revision: 4 }), turns: page([turn(2, { prompt: 'Changed snapshot question' })]) })
      : detail(id, { turns: page([turn(1)], 1, 0, 2) }),
  });
  await open(view);
  await selectSession(view);
  await view.get('history-more-turns').click();
  await flush();
  assert.doesNotMatch(view.get('history-turns').textContent, /Changed snapshot question/);
  assert.match(view.get('history-warning').textContent + view.get('history-detail-message').textContent, /refresh|changed/i);
  assertRecovery(view, true);
});

test('History renders prompts, results, errors and event payloads as literal text', async (t) => {
  const attack = '<img src=x onerror="throw new Error(\'xss\')"><script>attack()</script>';
  const view = await mount(t, {
    detail: async () => detail('session-1', {
      turns: page([turn(1, { prompt: attack, result: attack })]),
      events: page([event(0, { payload: { result: attack, error: attack } })]),
      issue_counts: { invalid_record: 1 },
    }),
  });
  await open(view);
  await selectSession(view);
  assert.ok(view.get('history-turns').textContent.includes(attack));
  assert.ok(view.get('history-events').textContent.includes('<img'));
  assert.equal(view.root.querySelectorAll('img').length, 0);
  assert.equal(view.root.querySelectorAll('script').length, 0);
  assert.equal(view.get('history-warning').hidden, false);
  assert.match(view.get('history-warning').textContent, /partial|skip|incomplete|record/i);
});

test('History keeps unassigned events accessible through the all-events filter', async (t) => {
  const view = await mount(t, {
    detail: async (_, id, options = {}) => detail(id, {
      events: page(options.turn_no === 1 ? [event(1)] : [event(0, { turn_no: null, payload: { result: 'unassigned payload' } }), event(1)]),
    }),
  });
  await open(view);
  await selectSession(view);
  assert.match(view.get('history-events').textContent, /unassigned payload/);
  view.get('history-event-filter').value = '1';
  await view.get('history-event-filter').dispatchEvent({ type: 'change' });
  await flush();
  assert.equal(view.calls.detail.at(-1).options.turn_no, 1);
  assert.doesNotMatch(view.get('history-events').textContent, /unassigned payload/);
  const allOption = view.get('history-event-filter').options.find((option) => /all/i.test(option.textContent));
  assert.ok(allOption);
  view.get('history-event-filter').value = allOption.value;
  await view.get('history-event-filter').dispatchEvent({ type: 'change' });
  await flush();
  assert.equal(view.calls.detail.at(-1).options.turn_no, undefined);
  assert.match(view.get('history-events').textContent, /unassigned payload/);
});

test('History handles no agents and removal of the selected agent during a request', async (t) => {
  const oldRequest = deferred();
  const view = await mount(t, { list: () => oldRequest.promise });
  await open(view, []);
  assert.equal(view.calls.list.length, 0);
  assert.equal(view.get('history-agent').disabled, true);
  assert.match(view.get('history-list-message').textContent, /No registered Agents/i);
  view.controller.setAgents(agents);
  await flush();
  assert.equal(view.calls.list.length, 1);
  view.controller.setAgents([]);
  oldRequest.resolve(listing([session('removed-agent-session')]));
  await flush();
  assert.doesNotMatch(view.get('history-sessions').textContent, /removed-agent-session/);
  assert.equal(view.get('history-refresh').disabled, false, 'a stale request must not leave the Retry callback disabled');
  assertRecovery(view, false);
});

test('History allows retry after a failed session detail request', async (t) => {
  let attempt = 0;
  const view = await mount(t, {
    detail: async (_, id) => {
      if (++attempt === 1) throw new Error('Record temporarily unavailable');
      return detail(id);
    },
  });
  await open(view);
  await selectSession(view);
  assert.equal(view.get('history-detail').hidden, true);
  assert.match(view.get('history-empty').textContent, /Record temporarily unavailable/);
  assertRecovery(view, true);
  await view.get('history-refresh').click();
  await flush();
  assert.equal(view.get('history-detail').hidden, false);
  assert.match(view.get('history-turns').textContent, /Question 1/);
  assert.deepEqual(view.calls.detail.map((call) => call.id), ['session-1', 'session-1']);
  assertRecovery(view, false);
});

test('History event failure exposes Retry and clears recovery after the selected session reloads', async (t) => {
  let fail = true;
  const view = await mount(t, {
    detail: async (_, id, options = {}) => {
      if (fail && options.turn_no === 1) throw new Error('Event page temporarily unavailable');
      return detail(id);
    },
  });
  await open(view); await selectSession(view);
  view.get('history-event-filter').value = '1';
  await view.get('history-event-filter').dispatchEvent({ type: 'change' }); await flush();
  assert.match(view.get('history-event-message').textContent, /Event page temporarily unavailable/);
  assert.equal(view.get('history-event-message').classList.contains('is-error'), true);
  assert.equal(view.get('history-connection-error').hidden, true);
  assertRecovery(view, true);
  fail = false;
  await view.get('history-refresh').click(); await flush();
  assert.equal(view.calls.list.length, 2);
  assert.equal(view.calls.detail.at(-1).id, 'session-1');
  assert.equal(view.get('history-event-message').hidden, true);
  assert.equal(view.get('history-events').querySelectorAll('details').length, 1);
  assertRecovery(view, false);
});

test('History turn page failure exposes recovery until the page successfully retries', async (t) => {
  let fail = true;
  const view = await mount(t, {
    detail: async (_, id, options = {}) => {
      if (options.turn_offset) {
        if (fail) throw new Error('Turn page temporarily unavailable');
        return detail(id, { turns: page([turn(2)], null, 1, 2) });
      }
      return detail(id, { turns: page([turn(1)], 1, 0, 2) });
    },
  });
  await open(view); await selectSession(view);
  await view.get('history-more-turns').click(); await flush();
  assert.match(view.get('history-detail-message').textContent, /Turn page temporarily unavailable/);
  assertRecovery(view, true);
  fail = false;
  await view.get('history-more-turns').click(); await flush();
  assert.match(view.get('history-turns').textContent, /Question 2/);
  assertRecovery(view, false);
});

test('History file mode reads newly saved data when the view is closed and reopened', async (t) => {
  let current = 'first-file-session';
  const view = await mount(t, { list: async () => listing([session(current)]) });
  await open(view);
  assert.match(view.get('history-sessions').textContent, /first-file-session/);
  assertRecovery(view, false);
  assert.equal(view.streams.length, 0);
  current = 'new-file-session';
  view.controller.close();
  await open(view);
  assert.equal(view.calls.list.length, 2);
  assert.match(view.get('history-sessions').textContent, /new-file-session/);
  assert.doesNotMatch(view.get('history-sessions').textContent, /first-file-session/);
  assert.equal(view.streams.length, 0);
  assert.equal(view.timers.tasks.size, 0);
  assertRecovery(view, false);
});

test('History ignores errors from the previous agent instead of hiding the current list', async (t) => {
  const oldRequest = deferred();
  const view = await mount(t, {
    list: (slug) => slug === 'alpha' ? oldRequest.promise : Promise.resolve(listing([session('beta-session')])),
  });
  await open(view);
  await selectAgent(view, 'beta');
  oldRequest.reject(new Error('Old request failure'));
  await flush();
  assert.match(view.get('history-sessions').textContent, /beta-session/);
  assert.doesNotMatch(view.get('history-list-message').textContent, /Old request failure/);
});

test('History prevents an old event filter response from replacing a newer selection', async (t) => {
  const filteredRequest = deferred();
  const view = await mount(t, {
    detail: (_, id, options = {}) => options.turn_no === 1
      ? filteredRequest.promise
      : Promise.resolve(detail(id, { events: page([event(0, { turn_no: null, payload: { result: 'all events view' } })]) })),
  });
  await open(view);
  await selectSession(view);
  view.get('history-event-filter').value = '1';
  void view.get('history-event-filter').dispatchEvent({ type: 'change' });
  await flush();
  view.get('history-event-filter').value = 'all';
  await view.get('history-event-filter').dispatchEvent({ type: 'change' });
  await flush();
  filteredRequest.resolve(detail('session-1', { events: page([event(1, { payload: { result: 'stale filtered result' } })]) }));
  await flush();
  assert.match(view.get('history-events').textContent, /all events view/);
  assert.doesNotMatch(view.get('history-events').textContent, /stale filtered result/);
});

test('History prevents duplicate load-more requests while a turn page is pending', async (t) => {
  const pendingPage = deferred();
  const view = await mount(t, {
    detail: (_, id, options = {}) => options.turn_offset
      ? pendingPage.promise
      : Promise.resolve(detail(id, { turns: page([turn(1)], 1, 0, 2) })),
  });
  await open(view);
  await selectSession(view);
  void view.get('history-more-turns').click();
  await flush();
  assert.equal(view.get('history-more-turns').disabled, true);
  await view.get('history-more-turns').click();
  assert.equal(view.calls.detail.length, 2);
  pendingPage.resolve(detail('session-1', { turns: page([turn(2)], null, 1, 2) }));
  await flush();
  assert.match(view.get('history-turns').textContent, /Question 2/);
});

test('History preserves loaded turns and retries a failed page using the same offset', async (t) => {
  let pageAttempts = 0;
  const view = await mount(t, {
    detail: async (_, id, options = {}) => {
      if (!options.turn_offset) return detail(id, { turns: page([turn(1)], 1, 0, 2) });
      if (++pageAttempts === 1) throw new Error('Page read failed');
      return detail(id, { turns: page([turn(2)], null, 1, 2) });
    },
  });
  await open(view);
  await selectSession(view);
  await view.get('history-more-turns').click();
  await flush();
  assert.match(view.get('history-turns').textContent, /Question 1/);
  assert.match(view.get('history-detail-message').textContent, /Page read failed/);
  assert.equal(view.get('history-more-turns').disabled, false);
  await view.get('history-more-turns').click();
  await flush();
  assert.equal(view.calls.detail[1].options.turn_offset, 1);
  assert.equal(view.calls.detail[2].options.turn_offset, 1);
  assert.match(view.get('history-turns').textContent, /Question 2/);
  assert.equal(view.get('history-detail-message').hidden, true);
});

test('History refresh callback preserves a selected session outside the refreshed first page', async (t) => {
  let firstPageLoads = 0;
  let selectedLoads = 0;
  const view = await mount(t, {
    list: async (_, options = {}) => {
      if (options.offset === 1) return listing([session('older-session')], {
        sessions: page([session('older-session')], null, 1, 2),
      });
      firstPageLoads++;
      const newest = session(firstPageLoads === 1 ? 'first-page' : 'new-first-page');
      return listing([newest], { sessions: page([newest], 1, 0, 2) });
    },
    detail: async (_, id) => {
      selectedLoads++;
      return detail(id, {
        session: session(id, { source_revision: selectedLoads }),
        turns: page([turn(1, { prompt: `Older session load ${selectedLoads}` })]),
      });
    },
  });
  await open(view);
  await view.get('history-more-sessions').click();
  await flush();
  await selectSession(view, 1);
  assert.match(view.get('history-session-id').textContent, /older-session/);
  await view.get('history-refresh').click();
  await flush();
  assert.equal(view.calls.list.at(-1).options.offset, 0);
  assert.equal(view.calls.detail.at(-1).id, 'older-session');
  assert.equal(selectedLoads, 2);
  assert.equal(view.get('history-detail').hidden, false);
  assert.match(view.get('history-session-id').textContent, /older-session/);
  assert.match(view.get('history-turns').textContent, /Older session load 2/);
  assert.match(view.get('history-sessions').textContent, /new-first-page/);
  assert.doesNotMatch(view.get('history-sessions').textContent, /older-session/);
  assert.match(view.get('history-list-message').textContent, /outside this page/i);
});

for (const changedResponse of ['turns', 'events']) {
  test(`History clears concurrent loading messages when ${changedResponse} detects a changed snapshot`, async (t) => {
    const pendingTurns = deferred();
    const pendingEvents = deferred();
    const view = await mount(t, {
      detail: (_, id, options = {}) => {
        if (options.turn_offset === 1) return pendingTurns.promise;
        if (options.event_offset === 1) return pendingEvents.promise;
        return Promise.resolve(detail(id, {
          turns: page([turn(1)], 1, 0, 2),
          events: page([event(0)], 1, 0, 2),
        }));
      },
    });
    await open(view);
    await selectSession(view);
    void view.get('history-more-turns').click();
    void view.get('history-more-events').click();
    await flush();
    assert.match(view.get('history-detail-message').textContent, /Loading turns/);
    assert.match(view.get('history-event-message').textContent, /Loading events/);
    const newerSnapshot = detail('session-1', {
      session: session('session-1', { source_revision: 4 }),
      turns: page([turn(2, { prompt: 'Unmerged newer turn' })], null, 1, 2),
      events: page([event(1, { payload: { result: 'Unmerged newer event' } })], null, 1, 2),
    });
    const first = changedResponse === 'turns' ? pendingTurns : pendingEvents;
    const second = changedResponse === 'turns' ? pendingEvents : pendingTurns;
    first.resolve(newerSnapshot);
    await flush();
    assert.match(view.get('history-detail-message').textContent, /changed on disk.*Retry/i);
    assertRecovery(view, true);
    assert.equal(view.get('history-event-message').hidden, true);
    assert.doesNotMatch(view.get('history-event-message').textContent, /Loading/);
    second.resolve(newerSnapshot);
    await flush();
    assert.match(view.get('history-detail-message').textContent, /changed on disk.*Retry/i);
    assertRecovery(view, true);
    assert.equal(view.get('history-event-message').hidden, true);
    assert.doesNotMatch(view.get('history-turns').textContent, /Unmerged newer turn/);
    assert.doesNotMatch(view.get('history-events').textContent, /Unmerged newer event/);
    assert.equal(view.get('history-more-turns').hidden, true);
    assert.equal(view.get('history-more-events').hidden, true);
    assert.equal(view.get('history-event-filter').disabled, true);
  });
}

const personalisationStart = '<<<CONNECTONION_PERSONALISATION_V2>>>';
const personalisationEnd = '<<<CONNECTONION_END_PERSONALISATION_V2>>>';
const userRequestStart = '<<<CONNECTONION_USER_REQUEST_V2>>>';
const wrappedPrompt = (request, context = 'Preference: concise\nCredential: [REDACTED]') =>
  `${personalisationStart}\n${context}\n${personalisationEnd}\n\n${userRequestStart}\n${request}`;

test('History displays the iOS request by default and preserves the API prompt in a closed disclosure', async (t) => {
  const prompt = wrappedPrompt('请查询订单状态。');
  const record = Object.freeze(turn(1, { prompt }));
  const view = await mount(t, { detail: async () => detail('session-1', { turns: page([record]) }) });
  await open(view);
  await selectSession(view);
  const card = view.get('history-turns').querySelector('article');
  assert.equal(card.querySelector('.history-text').textContent, '请查询订单状态。');
  const disclosure = card.querySelector('.history-full-input');
  assert.ok(disclosure);
  assert.equal(disclosure.tagName, 'DETAILS');
  assert.equal(disclosure.getAttribute('open'), null);
  assert.notEqual(disclosure.open, true);
  assert.equal(disclosure.querySelector('summary').textContent, 'Original user input');
  assert.match(disclosure.querySelector('summary').title, /Not the full provider request or compressed context/);
  assert.match(disclosure.querySelector('summary').title, /API.*best-effort.*not full anonymisation/i);
  assert.equal(disclosure.querySelector('.history-text').textContent, prompt);
  assert.equal(record.prompt, prompt, 'display projection must never replace the stored/API prompt');
});

test('History preserves multiline request whitespace after the one iOS separator newline', async (t) => {
  const request = '\n  第一段\nSecond paragraph\n\n  last line  \n';
  const view = await mount(t, {
    detail: async () => detail('session-1', { turns: page([turn(1, { prompt: wrappedPrompt(request) })]) }),
  });
  await open(view);
  await selectSession(view);
  assert.equal(view.get('history-turns').querySelector('.history-text').textContent, request);
});

test('History recognises CRLF envelope separators without trimming the request body', async (t) => {
  const request = '  first line\r\nsecond line  ';
  const prompt = `${personalisationStart}\r\n[REDACTED]\r\n${personalisationEnd}\r\n\r\n${userRequestStart}\r\n${request}`;
  const view = await mount(t, {
    detail: async () => detail('session-1', { turns: page([turn(1, { prompt })]) }),
  });
  await open(view);
  await selectSession(view);
  assert.equal(view.get('history-turns').querySelector('.history-text').textContent, request);
});

test('History leaves ordinary, incomplete, nested and malformed envelopes fully visible', async (t) => {
  const prompts = [
    'An ordinary user question.',
    `Explain this quoted envelope: ${wrappedPrompt('quoted body')}`,
    ` ${wrappedPrompt('leading whitespace is not a known envelope')}`,
    `${personalisationStart}\ncontext\n${userRequestStart}\nmissing end`,
    `${personalisationStart}\ncontext\n${personalisationEnd}\nmissing request marker`,
    wrappedPrompt('wrong start version').replace(personalisationStart, '<<<CONNECTONION_PERSONALISATION_V1>>>'),
    wrappedPrompt('wrong end version').replace(personalisationEnd, '<<<CONNECTONION_END_PERSONALISATION_V1>>>'),
    wrappedPrompt('wrong request version').replace(userRequestStart, '<<<CONNECTONION_USER_REQUEST_V1>>>'),
    `${personalisationStart}\ncontext\n${personalisationEnd}\nordinary content\n${userRequestStart}\nbody`,
    `${personalisationStart}\ncontext\n${personalisationEnd}${userRequestStart}\nno required separator`,
    wrappedPrompt('nested header', `${personalisationStart}\nnested context`),
    wrappedPrompt('out-of-order header', `${userRequestStart}\nnot a context`),
    wrappedPrompt('wrong marker in header', '<<<CONNECTONION_END_PERSONALISATION_V1>>>'),
    `${personalisationStart}\ncontext\n${personalisationEnd}\n${personalisationEnd}\n${userRequestStart}\nbody`,
    wrappedPrompt(''),
    wrappedPrompt('\n \t'),
  ];
  const view = await mount(t, {
    detail: async () => detail('session-1', { turns: page(prompts.map((prompt, index) => turn(index + 1, { prompt }))) }),
  });
  await open(view);
  await selectSession(view);
  const cards = view.get('history-turns').querySelectorAll('article');
  assert.equal(cards.length, prompts.length);
  for (const [index, card] of cards.entries()) {
    assert.equal(card.querySelector('.history-text').textContent, prompts[index], `input ${index} must remain intact`);
    assert.equal(card.querySelector('.history-full-input'), null, `input ${index} must not get an unnecessary disclosure`);
  }
});

test('History never rescans or truncates markers inside the extracted user request', async (t) => {
  const request = `Please explain these markers literally:\n${personalisationEnd}\n${userRequestStart}\n${wrappedPrompt('this nested example belongs to my message')}\nKeep the final line.`;
  const view = await mount(t, {
    detail: async () => detail('session-1', { turns: page([turn(1, { prompt: wrappedPrompt(request) })]) }),
  });
  await open(view);
  await selectSession(view);
  const card = view.get('history-turns').querySelector('article');
  assert.equal(card.querySelector('.history-text').textContent, request);
  assert.equal(card.querySelectorAll('.history-full-input').length, 1);
});

test('History renders both wrapped and extracted HTML-like input as literal text', async (t) => {
  const attack = '<img src=x onerror="attack()"><script>attack()</script>';
  const prompt = wrappedPrompt(attack, `Context ${attack}\nCredential: [REDACTED]`);
  const view = await mount(t, {
    detail: async () => detail('session-1', { turns: page([turn(1, { prompt })]) }),
  });
  await open(view);
  await selectSession(view);
  const card = view.get('history-turns').querySelector('article');
  assert.equal(card.querySelector('.history-text').textContent, attack);
  assert.equal(card.querySelector('.history-full-input').querySelector('.history-text').textContent, prompt);
  assert.equal(view.root.querySelectorAll('img').length, 0);
  assert.equal(view.root.querySelectorAll('script').length, 0);
});

test('SSE subscribes only for archive mode and releases old subscriptions and timers', async (t) => {
  const fileView = await mount(t);
  await open(fileView);
  assert.equal(fileView.streams.length, 0);
  const view = await mount(t, { list: async (slug) => archiveListing([capture('c1', { agent_slug: slug })]) });
  await open(view);
  assert.equal(view.streams.length, 1);
  assert.equal(view.get('history-connection-error').hidden, true);
  assertRecovery(view, false);
  const first = view.streams[0];
  first.emit();
  await selectAgent(view, 'beta');
  assert.equal(first.closed, 1);
  assert.equal(view.streams.length, 2);
  first.emit(); first.status('unavailable');
  assert.equal(view.get('history-connection-error').hidden, true);
  assertRecovery(view, false);
  assert.equal(view.timers.tasks.size, 0);
  view.streams[1].emit();
  view.controller.close();
  assert.equal(view.streams[1].closed, 1);
  assert.equal(view.timers.tasks.size, 0);
  await view.timers.tick();
  assert.equal(view.calls.list.length, 2);
});

test('SSE connection failures show an exception-only recovery entry and recovery hides it', async (t) => {
  const view = await mount(t, { list: async () => archiveListing() });
  await open(view);
  const stream = view.streams[0];
  for (const status of ['reconnecting', 'unavailable']) {
    stream.status(status);
    assert.equal(view.get('history-connection-error').textContent, 'Automatic updates interrupted.');
    assert.equal(view.get('history-connection-error').hidden, false);
    assertRecovery(view, true);
    stream.status('live');
    assert.equal(view.get('history-connection-error').textContent, '');
    assert.equal(view.get('history-connection-error').hidden, true);
    assertRecovery(view, false);
  }
  assert.equal(view.calls.list.length, 1, 'connection status alone does not issue a GET');
});

test('SSE coalesces bursts and reloads every reset without rebuilding unchanged detail', async (t) => {
  const view = await mount(t, { list: async () => archiveListing() });
  await open(view); await selectSession(view);
  const row = view.get('history-sessions').querySelector('button');
  for (let i = 0; i < 20; i++) view.streams[0].emit();
  assert.equal(view.timers.tasks.size, 1);
  await view.timers.tick();
  assert.equal(view.calls.list.length, 2);
  assert.equal(view.calls.archive.length, 1);
  assert.equal(view.get('history-sessions').querySelector('button'), row);
  for (let i = 0; i < 2; i++) {
    view.streams[0].emit('history.reset', { reason: 'reconcile' });
    await view.timers.tick();
  }
  assert.equal(view.calls.list.length, 4);
});

test('SSE updates a current capture atomically while preserving scroll, disclosure, focus and filter', async (t) => {
  let current = 'c1';
  const view = await mount(t, {
    list: async () => archiveListing([capture(current)]),
    archive: async (_, id, options = {}) => archiveDetail(id, {
      turns: page([turn(1, { prompt: wrappedPrompt('Question'), result: `Answer ${id}` })]),
      events: page([event(0, { payload: { result: `Result ${id}` } })]),
    }),
  });
  await open(view); await selectSession(view);
  view.get('history-event-filter').value = '1';
  await view.get('history-event-filter').dispatchEvent({ type: 'change' }); await flush();
  view.get('history-turns').querySelector('.history-full-input').open = true;
  view.get('history-events').querySelector('.history-event').open = true;
  view.get('history-events').querySelector('summary').focus();
  view.get('history-advanced').open = true;
  const scroll = view.get('history-detail').querySelector('.history-scroll');
  scroll.scrollTop = 143; view.get('history-sessions').scrollTop = 29;
  current = 'c2'; view.streams[0].emit(); await view.timers.tick();
  assert.match(view.get('history-turns').textContent, /Answer c2/);
  assert.match(view.get('history-events').textContent, /Result c2/);
  assert.equal(scroll.scrollTop, 143);
  assert.equal(view.get('history-sessions').scrollTop, 29);
  assert.equal(view.get('history-turns').querySelector('.history-full-input').open, true);
  assert.equal(view.get('history-events').querySelector('.history-event').open, true);
  assert.equal(view.get('history-advanced').open, true);
  assert.equal(view.get('history-event-filter').value, '1');
  assert.equal(view.calls.archive.at(-1).options.turn_no, 1);
  assert.equal(view.document.activeElement, view.get('history-events').querySelector('summary'));
  assert.equal(view.get('history-update-notice').hidden, true);
});

test('SSE keeps an explicitly chosen version pinned until View latest is clicked', async (t) => {
  let current = 'c1';
  const view = await mount(t, {
    list: async () => archiveListing([capture(current, { version_count: 3 })]),
    versions: async () => versionListing([version(current), version('old')]),
    archive: async (_, id) => archiveDetail(id, { turns: page([turn(1, { result: id })]) }),
  });
  await open(view); await selectSession(view);
  await view.get('history-versions-toggle').click(); await flush();
  await view.get('history-versions').querySelectorAll('button')[1].click(); await flush();
  assert.match(view.get('history-turns').textContent, /old/);
  const count = view.calls.archive.length;
  current = 'c2'; view.streams[0].emit(); await view.timers.tick();
  assert.equal(view.calls.archive.length, count);
  assert.match(view.get('history-turns').textContent, /old/);
  assert.equal(view.get('history-update-notice').hidden, false);
  assert.equal(view.get('history-versions-panel').hidden, false);
  assert.match(view.get('history-versions').textContent, /Revision/);
  await view.get('history-latest').click(); await flush();
  assert.match(view.get('history-turns').textContent, /c2/);
  assert.equal(view.get('history-update-notice').hidden, true);
});

test('SSE retains prior content on GET failure, then recovers without file fallback', async (t) => {
  let outage = false;
  const view = await mount(t, { list: async () => { if (outage) throw new Error('DB unavailable'); return archiveListing(); } });
  await open(view); await selectSession(view);
  outage = true; view.streams[0].emit(); await view.timers.tick();
  assert.equal(view.get('history-connection-error').textContent, 'Could not update history.');
  assertRecovery(view, true);
  assert.equal(view.get('history-detail').hidden, false);
  assert.match(view.get('history-turns').textContent, /Answer 1/);
  assert.equal(view.calls.detail.length, 0);
  assert.equal(view.timers.tasks.size, 0, 'no aggressive failed GET polling');
  outage = false; view.streams[0].emit('history.reset', { reason: 'reconcile' }); await view.timers.tick();
  assert.equal(view.get('history-connection-error').hidden, true);
  assert.equal(view.get('history-connection-error').textContent, '');
  assertRecovery(view, false);
});

test('SSE does not publish half a new capture if its event request fails', async (t) => {
  let current = 'c1';
  const view = await mount(t, {
    list: async () => archiveListing([capture(current)]),
    archive: async (_, id, options = {}) => {
      if (id === 'c2' && options.turn_limit === 1) throw new Error('events failed');
      return archiveDetail(id, { turns: page([turn(1, { result: id })]) });
    },
  });
  await open(view); await selectSession(view);
  current = 'c2'; view.streams[0].emit(); await view.timers.tick();
  assert.match(view.get('history-turns').textContent, /c1/);
  assert.match(view.get('history-capture-meta').textContent, /c1/);
  assert.equal(view.get('history-connection-error').textContent, 'Could not update history.');
  assertRecovery(view, true);
});

test('SSE merges another notification after the current quiet request settles', async (t) => {
  const pending = deferred(); let count = 0;
  const view = await mount(t, { list: async () => ++count === 2 ? pending.promise : archiveListing() });
  await open(view);
  view.streams[0].emit(); await view.timers.tick();
  view.streams[0].emit(); view.streams[0].emit();
  assert.equal(view.calls.list.length, 2);
  pending.resolve(archiveListing()); await flush();
  await view.timers.tick();
  assert.equal(view.calls.list.length, 3);
});

test('SSE queues invalidation during user detail loading without resetting it', async (t) => {
  const pending = deferred();
  const view = await mount(t, { list: async () => archiveListing(), archive: () => pending.promise });
  await open(view);
  void view.get('history-sessions').querySelector('button').click(); await flush();
  view.streams[0].emit(); await view.timers.tick();
  assert.equal(view.calls.list.length, 1);
  pending.resolve(archiveDetail()); await flush(); await view.timers.tick();
  assert.equal(view.calls.list.length, 2);
  assert.equal(view.calls.archive.length, 1);
  assert.match(view.get('history-turns').textContent, /Answer 1/);
});

test('SSE aborts quiet reads and ignores stale responses when closing or switching Agent', async (t) => {
  const pending = deferred(); let alphaLoads = 0;
  const view = await mount(t, {
    list: async (slug) => slug === 'alpha' && ++alphaLoads === 2 ? pending.promise
      : archiveListing([capture('c1', { agent_slug: slug, session_id: `${slug}-session` })]),
  });
  await open(view); view.streams[0].emit(); await view.timers.tick();
  const signal = view.calls.list.at(-1).options.signal;
  assert.equal(signal.aborted, false);
  await selectAgent(view, 'beta');
  assert.equal(signal.aborted, true);
  pending.resolve(archiveListing([capture('stale', { session_id: 'stale-session' })])); await flush();
  assert.match(view.get('history-sessions').textContent, /beta-session/);
  assert.doesNotMatch(view.get('history-sessions').textContent, /stale-session/);
  view.controller.close();
  assert.equal(view.timers.tasks.size, 0);
});

test('SSE rejects a stale quiet response after the user selects another session', async (t) => {
  const pending = deferred(); let loads = 0;
  const rows = [capture('c1'), capture('c2', { session_id: 'session-2' })];
  const view = await mount(t, {
    list: async () => ++loads === 2 ? pending.promise : archiveListing(rows),
    archive: async (_, id) => archiveDetail(id, { session: session(id === 'c2' ? 'session-2' : 'session-1') }),
  });
  await open(view); await selectSession(view);
  view.streams[0].emit(); await view.timers.tick();
  await selectSession(view, 1);
  pending.resolve(archiveListing([capture('new')])); await flush();
  assert.equal(view.get('history-session-id').textContent, 'session-2');
  assert.match(view.get('history-capture-meta').textContent, /c2/);
  assert.equal(view.timers.tasks.size, 1, 'retry the invalidation after user action');
});

test('SSE retains the loaded session window and a selection pushed outside it', async (t) => {
  let inserted = false;
  const rows = Array.from({ length: 45 }, (_, i) => capture(`c${i}`, { session_id: `s${i}` }));
  const view = await mount(t, {
    list: async (_, { offset = 0, limit = 20 } = {}) => {
      const all = inserted ? [capture('new', { session_id: 'new' }), ...rows] : rows;
      return archiveListing([], { sessions: page(all.slice(offset, offset + limit),
        offset + limit < all.length ? offset + limit : null, offset, all.length) });
    },
    archive: async (_, id) => archiveDetail(id, { session: session(`s${id.slice(1)}`) }),
  });
  await open(view);
  await view.get('history-more-sessions').click(); await flush();
  await view.get('history-more-sessions').click(); await flush();
  await selectSession(view, 44);
  inserted = true; view.streams[0].emit(); await view.timers.tick();
  assert.equal(view.get('history-sessions').querySelectorAll('button').length, 45);
  assert.equal(view.get('history-session-id').textContent, 's44');
  assert.match(view.get('history-list-message').textContent, /outside this page/);
  assert.equal(view.get('history-more-sessions').hidden, false);
  await view.get('history-more-sessions').click(); await flush();
  assert.equal(view.get('history-sessions').querySelectorAll('button').length, 46);
});

test('SSE preserves loaded child pages and reads new captures from offset zero', async (t) => {
  let current = 'c1';
  const view = await mount(t, {
    list: async () => archiveListing([capture(current)]),
    archive: async (_, id, options = {}) => {
      const allTurns = Array.from({ length: 30 }, (_, i) => turn(i + 1, { result: `${id}-${i}` }));
      const allEvents = Array.from({ length: 65 }, (_, i) => event(i));
      const to = options.turn_offset || 0, tl = options.turn_limit || 20;
      const eo = options.event_offset || 0, el = options.event_limit || 50;
      return archiveDetail(id, {
        turns: page(allTurns.slice(to, to + tl), to + tl < 30 ? to + tl : null, to, 30),
        events: page(allEvents.slice(eo, eo + el), eo + el < 65 ? eo + el : null, eo, 65),
      });
    },
  });
  await open(view); await selectSession(view);
  await view.get('history-more-turns').click(); await flush();
  await view.get('history-more-events').click(); await flush();
  current = 'c2'; view.streams[0].emit(); await view.timers.tick();
  assert.equal(view.get('history-turns').querySelectorAll('article').length, 30);
  assert.equal(view.get('history-events').querySelectorAll('details').length, 65);
  assert.equal(view.get('history-more-turns').hidden, true);
  assert.equal(view.get('history-more-events').hidden, true);
  assert.match(view.get('history-turns').textContent, /c2-29/);
  assert.ok(view.calls.archive.filter((call) => call.id === 'c2').every((call) =>
    (call.options.turn_offset ?? 0) === 0 && (call.options.event_offset ?? 0) === 0));
});

test('SSE mode change follows server selection and closes the stream', async (t) => {
  let mode = 'archive';
  const view = await mount(t, { list: async () => mode === 'archive' ? archiveListing() : listing([session('file-session')]) });
  await open(view); await selectSession(view);
  mode = 'file'; view.streams[0].emit(); await view.timers.tick();
  assert.equal(view.streams[0].closed, 1);
  assert.match(view.get('history-sessions').textContent, /file-session/);
  assert.equal(view.get('history-detail').hidden, true);
  assert.equal(view.get('history-connection-error').hidden, true);
  assertRecovery(view, false);
});

test('quiet History requests forward AbortSignal without serializing it in query strings', async () => {
  const original = globalThis.fetch; const controller = new AbortController(); const calls = [];
  globalThis.fetch = async (path, options) => { calls.push([path, options]); return { ok: true, status: 200, json: async () => ({}) }; };
  try {
    await api.listHistory('alpha', { signal: controller.signal });
    await api.listHistoryVersions('alpha', 's1', { signal: controller.signal });
    await api.getArchive('alpha', 'c1', { signal: controller.signal });
    assert.ok(calls.every(([path, options]) => !path.includes('signal') && options.signal === controller.signal));
  } finally { globalThis.fetch = original; }
});

test('successful SSE windows clear prior event and version failures', async (t) => {
  let current = 'c1'; let fail = false;
  const view = await mount(t, {
    list: async () => archiveListing([capture(current)]),
    archive: async (_, id) => { if (fail) throw new Error('events temporarily unavailable'); return archiveDetail(id); },
    versions: async () => { if (fail) throw new Error('versions temporarily unavailable'); return versionListing([version(current)]); },
  });
  await open(view); await selectSession(view);
  fail = true;
  view.get('history-event-filter').value = '1';
  await view.get('history-event-filter').dispatchEvent({ type: 'change' }); await flush();
  await view.get('history-versions-toggle').click(); await flush();
  assert.equal(view.get('history-event-message').hidden, false);
  assert.equal(view.get('history-version-message').hidden, false);
  assertRecovery(view, true);
  fail = false; current = 'c2'; view.streams[0].emit(); await view.timers.tick();
  assert.equal(view.get('history-event-message').hidden, true);
  assert.equal(view.get('history-version-message').hidden, true);
  assert.equal(view.get('history-events').querySelectorAll('details').length, 1);
  assertRecovery(view, false);
});

test('a filtered turn omitted by compaction remains selectable after further turn paging', async (t) => {
  let current = 'c1';
  const view = await mount(t, {
    list: async () => archiveListing([capture(current)]),
    archive: async (_, id, options = {}) => {
      const all = id === 'c1' ? [turn(1)] : Array.from({ length: 40 }, (_, i) => turn(101 + i));
      const offset = options.turn_offset || 0, limit = options.turn_limit || 20;
      return archiveDetail(id, {
        turns: page(all.slice(offset, offset + limit), offset + limit < all.length ? offset + limit : null, offset, all.length),
        events: page(id === 'c2' ? [] : [event()]),
      });
    },
  });
  await open(view); await selectSession(view);
  view.get('history-event-filter').value = '1';
  await view.get('history-event-filter').dispatchEvent({ type: 'change' }); await flush();
  current = 'c2'; view.streams[0].emit(); await view.timers.tick();
  assert.ok(view.get('history-event-filter').options.some((item) => item.value === '1'));
  await view.get('history-more-turns').click(); await flush();
  assert.ok(view.get('history-event-filter').options.some((item) => item.value === '1'));
  assert.equal(view.get('history-event-filter').value, '1');
});

test('quiet GET timeout aborts work, preserves saved content, and allows a later retry', async (t) => {
  let loads = 0;
  const view = await mount(t, {
    list: async (_, options) => {
      if (++loads !== 2) return archiveListing();
      return new Promise((_, reject) => options.signal.addEventListener('abort', () => reject(new Error('aborted'))));
    },
  });
  await open(view); await selectSession(view);
  view.streams[0].emit(); await view.timers.tick();
  await view.timers.tick(15000);
  assert.equal(view.calls.list.at(-1).options.signal.aborted, true);
  assert.equal(view.get('history-connection-error').textContent, 'Could not update history.');
  assertRecovery(view, true);
  assert.match(view.get('history-turns').textContent, /Answer 1/);
  view.streams[0].emit(); await view.timers.tick();
  assert.equal(view.get('history-connection-error').hidden, true);
  assertRecovery(view, false);
  assert.equal(view.timers.tasks.size, 0);
});

const timelineButton = (view, index = 0) => view.get('history-turns').children[index].querySelector('button');
const timelineBody = (view, index = 0) => view.get('history-turns').children[index].querySelector('.history-turn-timeline');
const contextPayload = (overrides = {}) => ({
  report_version: 1, status: 'prepared', llm_call_id: 'llm-1', policy_version: 'studio-extractive-v1',
  estimator: 'utf8-json-bytes-div4-v1', input_budget: 4096, full_estimated_tokens: 6000,
  estimated_tokens: 3500, source_messages: 30, retained_messages: 8, input_messages: 9,
  task_fields: 3, tool_references: 2, omitted_tool_references: 1, compacted: true,
  summary_kind: 'deterministic_extract_not_semantic', ...overrides,
});
const contextEvent = (overrides = {}) => event(0, { kind: 'context_report', call_id: 'llm-1', payload: contextPayload(), ...overrides });

test('context timeline shows readable recorded estimates with optional closed raw JSON', async (t) => {
  const payload = contextPayload();
  const view = await mount(t, { list: async () => archiveListing(),
    archive: async (_, id) => archiveDetail(id, { events: page([contextEvent({ payload })]) }) });
  await open(view); await selectSession(view); await timelineButton(view).click(); await flush();
  const detail = timelineBody(view).querySelector('details');
  assert.equal(detail.querySelector('.execution-label').textContent, 'Context preparation');
  assert.equal(detail.querySelector('.history-note').textContent, 'Prepared · Compressed');
  const report = detail.querySelector('.context-report');
  assert.match(report.textContent, /Input before \(estimated tokens\)/);
  assert.match(report.textContent, /Original messages retained8/);
  assert.match(report.textContent, /Prepared input messages9/);
  assert.match(report.textContent, /not provider billing/);
  assert.match(report.textContent, /does not confirm.*sent or succeeded/);
  assert.equal(detail.querySelector('.context-report-raw').open, false);
  assert.equal(detail.querySelector('.context-report-raw').querySelector('summary').textContent, 'Raw record');
  assert.deepEqual(JSON.parse(detail.querySelector('pre').textContent).payload, payload);
  assert.match(view.get('history-events').querySelector('summary').textContent, /Context preparation.*Prepared/);
});

test('old history has no fabricated context metrics; unknown and blocked reports stay honest and inert', async (t) => {
  const cases = [event(0), contextEvent({ payload: contextPayload({ status: 'unknown', task_fields: 999999 }) }),
    contextEvent({ payload: contextPayload({ status: 'invalid_context', compacted: null, estimated_tokens: null }) }),
    contextEvent({ payload: contextPayload({ status: 'budget_exceeded', compacted: null, estimated_tokens: null,
      policy_version: '<img src=x onerror=alert(1)>', estimator: '<script>unsafe</script>' }) })];
  for (const item of cases) {
    const view = await mount(t, { list: async () => archiveListing(),
      archive: async (_, id) => archiveDetail(id, { events: page([item]) }) });
    await open(view); await selectSession(view); await timelineButton(view).click(); await flush();
    const body = timelineBody(view); const report = body.querySelector('.context-report');
    if (item.kind !== 'context_report') assert.equal(report, null);
    else if (item.payload.status === 'unknown') {
      assert.match(report.textContent, /Cannot confirm/);
      assert.doesNotMatch(report.textContent, /999999|999,999|Not compressed/);
      assert.equal(report.querySelector('.context-report-metrics'), null);
      assert.doesNotMatch(report.textContent, /provider billing/);
      assert.equal(body.querySelector('.context-report-raw').querySelector('summary').textContent, 'Raw record');
    } else {
      assert.match(report.textContent, /Not sent to model/);
      assert.match(report.textContent, /Prepared input \(estimated tokens\)Not recorded/);
      assert.ok(report.querySelector('.context-report-metrics'));
    }
    assert.equal(body.querySelectorAll('img').length, 0);
    assert.equal(body.querySelectorAll('script').length, 0);
  }
});

test('context metric and raw disclosures remain open across fixed-capture pagination', async (t) => {
  const view = await mount(t, { list: async () => archiveListing(),
    archive: async (_, id, options = {}) => archiveDetail(id, {
      events: options.turn_no ? page(options.event_offset ? [event(50)] :
        [contextEvent(), ...Array.from({ length: 49 }, (_, index) => event(index + 1))],
        options.event_offset ? null : 50, options.event_offset || 0, 51) : page([]),
    }) });
  await open(view); await selectSession(view); await timelineButton(view).click(); await flush();
  const original = timelineBody(view).querySelector('details'); original.open = true;
  original.querySelector('.context-report-raw').open = true;
  await timelineBody(view).querySelector('button').click(); await flush();
  const detail = timelineBody(view).querySelector('details');
  assert.equal(detail.open, true); assert.equal(detail.querySelector('.context-report-raw').open, true);
  assert.equal(view.calls.archive.at(-1).id, 'capture-1');
  assert.equal(view.calls.archive.at(-1).options.event_offset, 50);
});

test('context raw disclosure focus and expansion survive an SSE capture upgrade without stale metrics', async (t) => {
  let current = 'c1';
  const view = await mount(t, { list: async () => archiveListing([capture(current)]),
    archive: async (_, id) => archiveDetail(id, { events: page([contextEvent({
      payload: contextPayload({ estimated_tokens: id === 'c1' ? 3500 : 3400 }),
    })]) }) });
  await open(view); await selectSession(view); await timelineButton(view).click(); await flush();
  timelineBody(view).querySelector('details').open = true;
  const raw = timelineBody(view).querySelector('.context-report-raw'); raw.open = true; raw.querySelector('summary').focus();
  view.get('history-events').querySelector('details').open = true;
  view.get('history-events').querySelector('.context-report-raw').open = true;
  current = 'c2'; view.streams[0].emit(); await view.timers.tick(); await flush();
  const replacement = timelineBody(view).querySelector('.context-report-raw');
  assert.equal(replacement.open, true); assert.equal(timelineBody(view).querySelector('details').open, true);
  assert.equal(view.document.activeElement, replacement.querySelector('summary'));
  assert.equal(view.get('history-events').querySelector('.context-report-raw').open, true);
  assert.match(timelineBody(view).querySelector('.context-report').textContent, new RegExp((3400).toLocaleString()));
  assert.doesNotMatch(timelineBody(view).querySelector('.context-report').textContent, new RegExp((3500).toLocaleString()));
});

test('per-turn provenance remains plain text and preserves raw detail and ordinary event labels', async (t) => {
  const payloads = [
    provenancePayload(), provenancePayload({ version_id: 'baseline' }),
    provenancePayload({ status: 'unknown', version_id: null, base_sha256: null, system_sha256: null, reason: 'not_recorded' }),
    provenancePayload({ status: 'unknown', version_id: null, base_sha256: null, system_sha256: null, reason: 'unverified_event' }),
    provenancePayload({ system_sha256: null, model: '<img src=x onerror=alert(1)>', reason: '<script>unsafe</script>' }),
  ];
  const view = await mount(t, {
    list: async () => archiveListing(),
    archive: async (_, id, options = {}) => archiveDetail(id, {
      turns: page([turn(1, { prompt_provenance: promptBinding() })]),
      events: page(options.turn_no ? [
        ...payloads.map((payload, index) => event(index, { kind: 'prompt_provenance', payload })),
        event(5, { kind: 'llm_call', payload: { status: 'running' } }),
        event(6, { kind: 'tool_result', duration_ms: 0, payload: { name: 'lookup', error: 'interrupted' } }),
      ] : []),
    }),
  });
  await open(view); await selectSession(view); await timelineButton(view).click(); await flush();
  const body = timelineBody(view);
  const details = body.querySelectorAll('details');
  ['01234567', 'Original', 'Not recorded', 'Cannot confirm', 'Cannot confirm'].forEach((note, index) => {
    assert.equal(details[index].querySelector('.execution-label').textContent, 'Base prompt');
    assert.equal(details[index].querySelector('.history-note').textContent, note);
    assert.deepEqual(JSON.parse(details[index].querySelector('pre').textContent).payload, payloads[index]);
    assert.equal(details[index].parentNode.classList.contains('is-error'), false);
  });
  assert.equal(details[5].querySelector('.execution-label').textContent, 'Model request');
  assert.equal(details[5].querySelector('.history-note').textContent, '');
  assert.equal(details[6].querySelector('.execution-label').textContent, 'Tool result · lookup');
  assert.equal(details[6].querySelector('.history-note').textContent, 'Error / interrupted · 0 ms');
  assert.equal(details[6].parentNode.classList.contains('is-error'), true);
  assert.equal(body.querySelectorAll('img').length, 0);
  assert.equal(body.querySelectorAll('script').length, 0);
  assert.equal(body.querySelectorAll('a').length, 0);
  assert.deepEqual(view.calls.unexpected, [], 'recorded provenance never calls getPrompts');
});

test('per-turn execution is lazy, ordered, independently filtered and plain text', async (t) => {
  const view = await mount(t, { list: async () => archiveListing(), archive: async (_, id, options = {}) => archiveDetail(id, {
    turns: page([turn(1), turn(2)]), events: page(options.turn_no ? [
      event(2, { turn_no: options.turn_no, kind: 'tool_result', payload: { name: '<img>', result: '<script>' } }),
      event(1, { turn_no: options.turn_no, kind: 'tool_call', payload: { name: '<img>' } }),
    ] : []),
  }) });
  await open(view); await selectSession(view);
  const before = view.calls.archive.length;
  assert.equal(timelineBody(view).hidden, true);
  await timelineButton(view, 1).click(); await flush();
  assert.equal(view.calls.archive.length, before + 1);
  assert.equal(view.calls.archive.at(-1).options.turn_no, 2);
  assert.equal(timelineBody(view).hidden, true);
  assert.match(timelineBody(view, 1).textContent, /Tool request[\s\S]*Tool result/);
  assert.equal(timelineBody(view, 1).querySelectorAll('img').length, 0);
  await timelineButton(view, 1).click(); await timelineButton(view, 1).click(); await flush();
  assert.equal(view.calls.archive.length, before + 1, 'reopen uses same immutable capture');
});

test('timeline errors retry locally and reject records from other turns', async (t) => {
  let wrong = true;
  const view = await mount(t, { list: async () => archiveListing(), archive: async (_, id, options = {}) => archiveDetail(id, {
    events: page([event(0, { turn_no: options.turn_no && wrong ? 2 : 1 })]),
  }) });
  await open(view); await selectSession(view); await timelineButton(view).click(); await flush();
  assert.match(timelineBody(view).textContent, /Could not load/);
  assert.equal(timelineBody(view).querySelectorAll('details').length, 0);
  wrong = false;
  await timelineBody(view).querySelector('button').click(); await flush();
  assert.doesNotMatch(timelineBody(view).textContent, /Could not load/);
  assert.equal(timelineBody(view).querySelectorAll('details').length, 1);
});

test('timeline pages remain on the same capture and preserve expanded raw details', async (t) => {
  const view = await mount(t, { list: async () => archiveListing(), archive: async (_, id, options = {}) => archiveDetail(id, {
    events: options.turn_no ? page(options.event_offset ? [event(50)] : Array.from({ length: 50 }, (_, n) => event(n)),
      options.event_offset ? null : 50, options.event_offset || 0, 51) : page([]),
  }) });
  await open(view); await selectSession(view); await timelineButton(view).click(); await flush();
  timelineBody(view).querySelector('details').open = true;
  await timelineBody(view).querySelector('button').click(); await flush();
  assert.equal(view.calls.archive.at(-1).id, 'capture-1');
  assert.equal(view.calls.archive.at(-1).options.event_offset, 50);
  assert.equal(timelineBody(view).querySelectorAll('details').length, 51);
  assert.equal(timelineBody(view).querySelector('details').open, true);
});

test('timeline late responses cannot cross session lifecycle', async (t) => {
  const late = deferred(); let signal;
  const view = await mount(t, { list: async () => archiveListing(), archive: async (_, id, options = {}) => {
    if (options.turn_no) { signal = options.signal; return late.promise; }
    return archiveDetail(id);
  } });
  await open(view); await selectSession(view); await timelineButton(view).click(); await flush();
  view.controller.close();
  assert.equal(signal.aborted, true);
  late.resolve(archiveDetail('capture-1')); await flush();
  assert.equal(view.get('history-turns').children.length, 0);
  assert.equal(view.timers.tasks.size, 0);
});

test('SSE refresh keeps an open timeline but reloads all children from the new capture', async (t) => {
  let id = 'c1';
  const view = await mount(t, { list: async () => archiveListing([capture(id)]),
    archive: async (_, selected, options = {}) => archiveDetail(selected, { events: page([event(0, { payload: { result: selected } })]) }),
  });
  await open(view); await selectSession(view); await timelineButton(view).click(); await flush();
  timelineBody(view).querySelector('details').open = true;
  id = 'c2'; view.streams[0].emit(); await view.timers.tick(); await flush();
  assert.equal(timelineBody(view).hidden, false);
  assert.match(timelineBody(view).textContent, /c2/);
  assert.doesNotMatch(timelineBody(view).textContent, /c1/);
  assert.equal(timelineBody(view).querySelector('details').open, true);
});

test('SSE removal of a turn aborts its pending execution request', async (t) => {
  const pending = deferred();
  let current = 'c1'; let timelineSignal;
  const view = await mount(t, {
    list: async () => archiveListing([capture(current)]),
    archive: async (_, id, options = {}) => {
      if (id === 'c1' && options.turn_no === 1) {
        timelineSignal = options.signal;
        return pending.promise;
      }
      const number = id === 'c1' ? 1 : 2;
      return archiveDetail(id, { turns: page([turn(number)]), events: page([event(0, { turn_no: number })]) });
    },
  });
  await open(view); await selectSession(view); await timelineButton(view).click(); await flush();
  assert.equal(timelineSignal.aborted, false);
  current = 'c2'; view.streams[0].emit(); await view.timers.tick(); await flush();
  try {
    assert.equal(timelineSignal.aborted, true, 'removed turn must release its pending request');
    assert.match(view.get('history-turns').textContent, /Turn 2/);
  } finally {
    pending.resolve(archiveDetail('c1', { events: page([event(0, { payload: { result: 'removed stale execution' } })]) }));
    await flush();
  }
  assert.doesNotMatch(view.get('history-turns').textContent, /removed stale execution/);
});

test('SSE restores focus to an expanded execution record after its replacement loads', async (t) => {
  let current = 'c1';
  const view = await mount(t, {
    list: async () => archiveListing([capture(current)]),
    archive: async (_, id) => archiveDetail(id, { events: page([event(0, { payload: { result: id } })]) }),
  });
  await open(view); await selectSession(view); await timelineButton(view).click(); await flush();
  const oldDetail = timelineBody(view).querySelector('details');
  oldDetail.open = true;
  oldDetail.querySelector('summary').focus();
  current = 'c2'; view.streams[0].emit(); await view.timers.tick(); await flush();
  const replacement = timelineBody(view).querySelector('details');
  assert.notEqual(replacement, oldDetail);
  assert.equal(replacement.open, true);
  assert.equal(view.document.activeElement === replacement.querySelector('summary'), true,
    'focus must move from the detached summary to its replacement');
});
