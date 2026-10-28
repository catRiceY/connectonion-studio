// Isolated DOM doubles: no browser, global document, network or Agent runtime.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import { createPromptEditor, promptDiff } from '../../co_studio/frontend/js/prompt-editor.js';
import * as api from '../../co_studio/frontend/js/api.js';

class Element {
  constructor(tag, document) {
    this.tagName = tag.toUpperCase(); this.ownerDocument = document;
    this.children = []; this.id = ''; this.value = ''; this.hidden = false;
    this.disabled = false; this.open = false; this._text = ''; this.listeners = new Map();
    const classes = new Set();
    this.classList = {
      toggle(name, force) { const enabled = force ?? !classes.has(name); enabled ? classes.add(name) : classes.delete(name); },
      contains(name) { return classes.has(name); },
    };
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(''); }
  set innerHTML(_) { throw new Error('Prompt content must be rendered as literal text'); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; this._text = ''; }
  querySelectorAll(selector) {
    const matches = (element) => selector.startsWith('#') ? element.id === selector.slice(1)
      : element.tagName.toLowerCase() === selector.toLowerCase();
    return this.children.flatMap((child) => [...(matches(child) ? [child] : []), ...child.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null; }
  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener); this.listeners.set(type, listeners);
  }
  async dispatchEvent(event) {
    await Promise.all((this.listeners.get(event.type) || []).map((listener) => listener({ ...event, target: this })));
  }
  async click() { if (!this.disabled) await this.dispatchEvent({ type: 'click' }); }
}

const version = (id = 'v1', text = 'Current base prompt') => ({ id, text, created_at: '2026-10-28T00:00:00Z' });
const envelope = (current = version(), extra = {}) => ({
  supported: true, current,
  versions: [{ id: current.id, created_at: current.created_at }, { id: 'older', created_at: '2026-10-27T00:00:00Z' }],
  ...extra,
});
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const flush = async () => { for (let index = 0; index < 24; index++) await Promise.resolve(); };

function mount(t, handlers = {}) {
  const document = { createElement: (tag) => new Element(tag, document) };
  const root = new Element('details', document); root.id = 'prompt-editor';
  const tags = { message: 'p', form: 'div', base: 'p', text: 'textarea', versions: 'select', diff: 'pre', save: 'button', restore: 'button', reload: 'button' };
  for (const [name, tag] of Object.entries(tags)) {
    const item = document.createElement(tag); item.id = `prompt-${name}`; root.append(item);
  }
  const calls = [];
  const defaults = {
    getPrompts: async () => envelope(),
    getPromptVersion: async (_, id) => version(id, `Saved ${id} prompt`),
    savePrompt: async (_, body) => envelope(version('saved-version', body.text)),
    restorePrompt: async () => envelope(version('restored-version', 'Restored base prompt')),
  };
  const client = new Proxy(Object.fromEntries(Object.keys(defaults).map((name) => [name, (...args) => {
    calls.push({ method: name, args });
    return (handlers[name] ?? defaults[name])(...args);
  }])), { get(target, key) {
    assert.ok(key in target, `Prompt editor must not invoke ${String(key)}, including Agent start/restart`);
    return target[key];
  } });
  const controller = createPromptEditor({ root, api: client });
  t.after(() => controller.close());
  const get = (name) => root.querySelector(`#prompt-${name}`);
  return {
    root, get, calls, controller,
    async edit(text) { get('text').value = text; await get('text').dispatchEvent({ type: 'input' }); await flush(); },
    async compare(id) { get('versions').value = id; await get('versions').dispatchEvent({ type: 'change' }); await flush(); },
    writes() { return calls.filter((call) => ['savePrompt', 'restorePrompt'].includes(call.method)); },
  };
}

test('Prompt API encodes opaque identifiers, forwards read aborts and writes only explicit version payloads', async () => {
  const previous = globalThis.fetch; const calls = []; const abort = new AbortController();
  globalThis.fetch = async (...args) => { calls.push(args); return { ok: true, status: 200, json: async () => envelope() }; };
  try {
    const slug = 'alpha / 中文?#%'; const id = 'opaque/version 中文?#%';
    await api.getPrompts(slug, { signal: abort.signal });
    await api.getPromptVersion(slug, id, { signal: abort.signal });
    const draft = { base_version: 'old-id', text: 'Keep\n  exact whitespace\n' };
    await api.savePrompt(slug, draft);
    await api.restorePrompt(slug, { base_version: 'old-id', version_id: id });
    const base = `/api/agents/${encodeURIComponent(slug)}/prompts`;
    assert.deepEqual(calls.map(([path]) => path), [base, `${base}/${encodeURIComponent(id)}`, base, `${base}/restore`]);
    assert.deepEqual(calls.map(([, options]) => options.method), ['GET', 'GET', 'POST', 'POST']);
    for (const [, options] of calls.slice(0, 2)) {
      assert.equal(options.signal, abort.signal); assert.equal(options.body, undefined);
    }
    assert.deepEqual(JSON.parse(calls[2][1].body), draft);
    assert.deepEqual(JSON.parse(calls[3][1].body), { base_version: 'old-id', version_id: id });
  } finally { globalThis.fetch = previous; }
});

test('Prompt API preserves conflict status for the optimistic concurrency UI', async () => {
  const previous = globalThis.fetch;
  globalThis.fetch = async () => ({ ok: false, status: 409, statusText: 'Conflict', text: async () => JSON.stringify({ detail: 'Base version changed' }) });
  try {
    await assert.rejects(api.savePrompt('alpha', { base_version: 'v1', text: 'draft' }), (error) =>
      error instanceof api.ApiError && error.status === 409 && error.message === 'Base version changed');
  } finally { globalThis.fetch = previous; }
});

test('Prompt editor hooks exist once and UI explains next-launch and historical-binding limits', async () => {
  const source = await readFile(new URL('../../co_studio/frontend/js/prompt-editor.js', import.meta.url), 'utf8');
  const html = await readFile(new URL('../../co_studio/frontend/index.html', import.meta.url), 'utf8');
  for (const name of new Set([...source.matchAll(/\$\('([^']+)'\)/g)].map((match) => match[1]))) {
    assert.equal([...html.matchAll(new RegExp(`id="prompt-${name}"`, 'g'))].length, 1, name);
  }
  assert.match(html, /Base prompt for new conversations after the next Studio launch/);
  assert.match(html, /historical version binding is not yet recorded/);
  assert.match(html, /Reload \/ discard draft/);
});

test('promptDiff preserves literal changed lines and omits only shared prefix and suffix', () => {
  assert.equal(promptDiff('same\n  unchanged\n', 'same\n  unchanged\n'), 'No changes.');
  assert.equal(promptDiff('header\nold\nfooter', 'header\nnew\nfooter'), '… 1 unchanged lines\n− old\n+ new\n… 1 unchanged lines');
  assert.equal(promptDiff('a', 'a\nb'), '… 1 unchanged lines\n+ b');
  assert.equal(promptDiff('a\nb', 'a'), '… 1 unchanged lines\n− b');
  assert.equal(promptDiff('  original', '<script>literal()</script>'), '−   original\n+ <script>literal()</script>');
});

test('opening shows the saved prompt in a collapsed editor without writing or starting an Agent', async (t) => {
  const view = mount(t);
  await view.controller.open('alpha');
  assert.equal(view.root.open, false);
  assert.equal(view.get('form').hidden, false);
  assert.equal(view.get('text').value, 'Current base prompt');
  assert.equal(view.get('diff').textContent, 'No changes.');
  assert.equal(view.get('message').hidden, true);
  assert.equal(view.get('save').disabled, true);
  assert.equal(view.get('restore').disabled, true);
  assert.deepEqual(view.calls.map((call) => call.method), ['getPrompts']);
});

test('comparison labels lead with a short version ID while titles retain the full ID and timestamp', async (t) => {
  const current = {
    ...version('abcdef1234567890fedcba0987654321'),
    created_at: '2026-10-28T23:41:12.345Z',
  };
  const view = mount(t, { getPrompts: async () => envelope(current) });
  await view.controller.open('alpha');
  const option = view.get('versions').querySelectorAll('option').find((item) => item.value === current.id);
  assert.ok(option);
  assert.match(option.textContent, /^abcdef12 · \d{2}\/\d{2},?\s+\d{2}:\d{2}$/);
  assert.doesNotMatch(option.textContent, /\.345|T23:41|2026-/);
  assert.ok(option.title.includes(current.id));
  assert.ok(option.title.includes(current.created_at));
});

test('unsupported co_ai prompts stay read-only even if a click handler is invoked directly', async (t) => {
  const view = mount(t, { getPrompts: async () => ({ supported: false, reason: 'co_ai owns its runtime prompt.' }) });
  await view.controller.open('co-ai');
  assert.equal(view.get('form').hidden, true);
  assert.equal(view.get('text').disabled, true);
  assert.equal(view.get('save').disabled, true);
  assert.equal(view.get('restore').disabled, true);
  assert.match(view.get('message').textContent, /co_ai owns/);
  await view.get('save').dispatchEvent({ type: 'click' });
  await view.get('restore').dispatchEvent({ type: 'click' });
  assert.equal(view.writes().length, 0);
});

test('Save requires a changed nonblank draft and preserves all submitted whitespace', async (t) => {
  const view = mount(t);
  await view.controller.open('alpha');
  await view.edit(' \n\t'); assert.equal(view.get('save').disabled, true);
  await view.edit('Current base prompt'); assert.equal(view.get('save').disabled, true);
  const text = '  Edited base\n\nKeep these lines.\n';
  await view.edit(text); assert.equal(view.get('save').disabled, false);
  await view.get('save').click();
  assert.deepEqual(view.writes(), [{ method: 'savePrompt', args: ['alpha', { base_version: 'v1', text }] }]);
  assert.equal(view.get('text').value, text);
  assert.equal(view.get('save').disabled, true);
  assert.match(view.get('message').textContent, /Saved for next launch/);
  assert.match(view.get('message').textContent, /Start or restart explicitly/);
});

test('pending save disables duplicate writes and success installs a fresh version', async (t) => {
  const pending = deferred();
  const view = mount(t, { savePrompt: () => pending.promise });
  await view.controller.open('alpha'); await view.edit('draft');
  const saving = view.get('save').click(); await flush();
  assert.equal(view.get('save').disabled, true); assert.equal(view.get('reload').disabled, true);
  await view.get('save').click();
  assert.equal(view.writes().length, 1);
  pending.resolve(envelope(version('new-immutable-id', 'draft'))); await saving;
  assert.equal(view.get('versions').value, 'new-immutable-id');
  assert.equal(view.get('text').value, 'draft');
  assert.equal(view.get('reload').disabled, false);
});

test('unsaved drafts remain isolated per Agent and survive closing and reopening', async (t) => {
  const view = mount(t, { getPrompts: async (slug) => envelope(version(`${slug}-base`, `${slug} saved prompt`)) });
  await view.controller.open('alpha'); await view.edit('alpha draft');
  await view.controller.open('beta');
  assert.equal(view.get('text').value, 'beta saved prompt');
  await view.edit('beta draft');
  view.controller.close();
  await view.controller.open('alpha');
  assert.equal(view.get('text').value, 'alpha draft');
  assert.match(view.get('base').textContent, /alpha-base/);
  await view.controller.open('beta');
  assert.equal(view.get('text').value, 'beta draft');
  assert.equal(view.writes().length, 0);
});

test('a changed saved base retains the old draft and warns until explicit discard reload', async (t) => {
  let current = version();
  const view = mount(t, { getPrompts: async () => envelope(current) });
  await view.controller.open('alpha'); await view.edit('unsaved draft');
  view.controller.close(); current = version('v2', 'new remote prompt');
  await view.controller.open('alpha');
  assert.equal(view.get('text').value, 'unsaved draft');
  assert.match(view.get('message').textContent, /saved version changed.*draft is retained/i);
  assert.equal(view.get('message').classList.contains('is-error'), true);
  assert.match(view.get('base').textContent, /v1/);
  await view.get('reload').click();
  assert.equal(view.get('text').value, 'new remote prompt');
  assert.match(view.get('base').textContent, /v2/);
  assert.equal(view.get('save').disabled, true);
});

test('409 keeps the user draft and its original base without automatic overwrite or restart', async (t) => {
  const view = mount(t, { savePrompt: async () => { throw new api.ApiError('Base changed', 409); } });
  await view.controller.open('alpha'); await view.edit('keep this local draft');
  await view.get('save').click();
  assert.equal(view.get('text').value, 'keep this local draft');
  assert.match(view.get('base').textContent, /v1/);
  assert.match(view.get('message').textContent, /Another version was saved.*draft is retained/);
  assert.equal(view.get('save').disabled, false);
  assert.equal(view.writes().length, 1);
  assert.deepEqual(view.calls.map((call) => call.method), ['getPrompts', 'savePrompt']);
  view.controller.close(); await view.controller.open('alpha');
  assert.equal(view.get('text').value, 'keep this local draft');
});

test('initial loading failure exposes Reload and then recovers normally', async (t) => {
  let fail = true;
  const view = mount(t, { getPrompts: async () => { if (fail) throw new Error('offline'); return envelope(); } });
  await view.controller.open('alpha');
  assert.match(view.get('message').textContent, /Could not load prompts/);
  assert.equal(view.get('reload').disabled, false); assert.equal(view.get('save').disabled, true);
  fail = false; await view.get('reload').click();
  assert.equal(view.get('message').hidden, true);
  assert.equal(view.get('form').hidden, false);
});

for (const outcome of ['resolve', 'reject']) {
  test(`late prompt GET ${outcome} cannot replace another Agent or show its errors`, async (t) => {
    const pending = deferred(); let oldSignal;
    const view = mount(t, { getPrompts: (slug, options) => {
      if (slug === 'alpha') { oldSignal = options.signal; return pending.promise; }
      return Promise.resolve(envelope(version('beta-current', 'beta prompt')));
    } });
    const first = view.controller.open('alpha'); await flush();
    await view.controller.open('beta');
    assert.equal(oldSignal.aborted, true);
    if (outcome === 'resolve') pending.resolve(envelope(version('old-alpha', 'stale alpha prompt')));
    else pending.reject(new Error('stale alpha error'));
    await first;
    assert.equal(view.get('text').value, 'beta prompt');
    assert.equal(view.get('message').hidden, true);
  });
}

test('comparison requests abort prior selection and ignore out-of-order results', async (t) => {
  const pending = deferred(); let oldSignal;
  const view = mount(t, { getPromptVersion: (_, id, options) => {
    if (id === 'older') { oldSignal = options.signal; return pending.promise; }
    return Promise.resolve(version(id, 'Original baseline text'));
  } });
  await view.controller.open('alpha');
  await view.compare('older'); await view.compare('baseline');
  assert.equal(oldSignal.aborted, true);
  assert.match(view.get('diff').textContent, /Original baseline text/);
  pending.resolve(version('older', 'stale comparison')); await flush();
  assert.doesNotMatch(view.get('diff').textContent, /stale comparison/);
  assert.equal(view.get('versions').value, 'baseline');
});

test('comparison and API error text stay literal and cannot introduce markup', async (t) => {
  const attack = '<img src=x onerror="attack()"><script>attack()</script>';
  const view = mount(t, {
    getPromptVersion: async (_, id) => version(id, attack),
    savePrompt: async () => { throw new Error(attack); },
  });
  await view.controller.open('alpha'); await view.compare('older');
  assert.ok(view.get('diff').textContent.includes(attack));
  await view.edit('draft'); await view.get('save').click();
  assert.ok(view.get('message').textContent.includes(attack));
  assert.equal(view.root.querySelectorAll('img').length + view.root.querySelectorAll('script').length, 0);
});

for (const selected of ['older', 'baseline']) {
  test(`restore ${selected} requires confirmation and creates a new version without restarting`, async (t) => {
    const view = mount(t);
    await view.controller.open('alpha'); await view.compare(selected);
    await view.get('restore').click();
    assert.equal(view.writes().length, 0);
    assert.equal(view.get('restore').textContent, 'Confirm restore as new version');
    await view.get('restore').click();
    assert.deepEqual(view.writes(), [{ method: 'restorePrompt', args: ['alpha', { base_version: 'v1', version_id: selected }] }]);
    assert.equal(view.get('versions').value, 'restored-version');
    assert.equal(view.get('text').value, 'Restored base prompt');
    assert.equal(view.get('restore').disabled, true);
    assert.equal(view.get('restore').textContent, 'Restore selected');
  });
}

test('editing the draft cancels an outstanding restore confirmation', async (t) => {
  const view = mount(t);
  await view.controller.open('alpha'); await view.compare('older');
  await view.get('restore').click(); await view.edit('new draft');
  assert.equal(view.get('restore').textContent, 'Restore selected');
  await view.get('restore').click();
  assert.equal(view.writes().length, 0, 'first click must confirm the new editing context again');
});

test('a stale completed write cannot paint another Agent', async (t) => {
  const pending = deferred();
  const view = mount(t, {
    getPrompts: async (slug) => envelope(version(`${slug}-current`, `${slug} prompt`)),
    savePrompt: () => pending.promise,
  });
  await view.controller.open('alpha'); await view.edit('alpha submitted draft');
  const saving = view.get('save').click(); await flush();
  await view.controller.open('beta'); await view.edit('beta unsaved draft');
  pending.resolve(envelope(version('alpha-saved', 'alpha submitted draft'))); await saving;
  assert.equal(view.get('text').value, 'beta unsaved draft');
  assert.match(view.get('base').textContent, /beta-current/);
  assert.equal(view.get('save').disabled, false);
  assert.doesNotMatch(view.get('message').textContent, /Saved for next launch/);
});

test('a stale completed write cannot delete a newer cached draft for the same Agent', async (t) => {
  const pending = deferred();
  const view = mount(t, { savePrompt: () => pending.promise });
  await view.controller.open('alpha'); await view.edit('submitted first draft');
  const saving = view.get('save').click(); await flush();
  await view.controller.open('beta'); await view.controller.open('alpha');
  await view.edit('newer unsaved draft'); await view.controller.open('beta');
  pending.resolve(envelope(version('saved-first', 'submitted first draft'))); await saving;
  await view.controller.open('alpha');
  assert.equal(view.get('text').value, 'newer unsaved draft');
});

test('a completed background save clears its unchanged submitted draft on reopening', async (t) => {
  const pending = deferred(); let current = version();
  const view = mount(t, {
    getPrompts: async () => envelope(current),
    savePrompt: () => pending.promise,
  });
  await view.controller.open('alpha'); await view.edit('submitted draft');
  const saving = view.get('save').click(); await flush();
  await view.controller.open('beta');
  current = version('saved-current', 'submitted draft');
  pending.resolve(envelope(current)); await saving;
  await view.controller.open('alpha');
  assert.equal(view.get('text').value, 'submitted draft');
  assert.equal(view.get('save').disabled, true, 'the persisted draft is clean against its new saved base');
  assert.equal(view.get('message').hidden, true);
  assert.match(view.get('base').textContent, /saved-curren/);
});

test('Reload invalidates a pending comparison before installing the new current selection', async (t) => {
  const pending = deferred(); let signal; let current = version();
  const view = mount(t, {
    getPrompts: async () => envelope(current),
    getPromptVersion: (_, __, options) => { signal = options.signal; return pending.promise; },
  });
  await view.controller.open('alpha'); await view.compare('older');
  current = version('v2', 'new saved prompt'); await view.get('reload').click();
  assert.equal(signal.aborted, true);
  pending.resolve(version('older', 'stale comparison text')); await flush();
  assert.equal(view.get('versions').value, 'v2');
  assert.equal(view.get('diff').textContent, 'No changes.');
  assert.equal(view.get('restore').disabled, true);
});

test('Save invalidates a pending comparison before installing the saved version', async (t) => {
  const pending = deferred(); let signal;
  const view = mount(t, { getPromptVersion: (_, __, options) => { signal = options.signal; return pending.promise; } });
  await view.controller.open('alpha'); await view.compare('older'); await view.edit('saved draft');
  await view.get('save').click();
  assert.equal(signal.aborted, true);
  pending.resolve(version('older', 'stale comparison text')); await flush();
  assert.equal(view.get('versions').value, 'saved-version');
  assert.equal(view.get('diff').textContent, 'No changes.');
});

test('successful comparison retry clears its previous load error', async (t) => {
  let fail = true;
  const view = mount(t, { getPromptVersion: async (_, id) => {
    if (fail) throw new Error('temporary comparison failure');
    return version(id, 'Recovered comparison');
  } });
  await view.controller.open('alpha'); await view.compare('older');
  assert.match(view.get('message').textContent, /Could not load the comparison version/);
  fail = false; await view.compare('older');
  assert.match(view.get('diff').textContent, /Recovered comparison/);
  assert.equal(view.get('message').hidden, true);
});
