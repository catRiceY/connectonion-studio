import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import { createApprovalsView } from '../../co_studio/frontend/js/approvals.js';
import { createToolConfig } from '../../co_studio/frontend/js/tool-config.js';
import { createKnowledgeView } from '../../co_studio/frontend/js/knowledge.js';
import { READONLY_VERIFICATION_ACTIONS, VERIFICATION_POLICY, isReadOnlyVerificationStep } from '../../co_studio/frontend/js/verification-policy.js';
import * as api from '../../co_studio/frontend/js/api.js';

class Element {
  constructor(tag, document) {
    this.tagName = tag; this.ownerDocument = document; this.children = []; this.dataset = {};
    this.listeners = new Map(); this.value = ''; this.hidden = false; this.disabled = false; this.checked = false; this._text = ''; this.attributes = {};
    const classes = new Set();
    this.classList = { toggle(name, force) { (force ?? !classes.has(name)) ? classes.add(name) : classes.delete(name); },
      contains(name) { return classes.has(name); }, remove(...names) { names.forEach((name) => classes.delete(name)); } };
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((item) => item.textContent).join(''); }
  set innerHTML(_) { throw new Error('All server-provided content must remain literal text'); }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this._text = ''; this.children = items; }
  querySelector(selector) { return this.children.find((item) => `#${item.id}` === selector) || null; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(type, listener) { this.listeners.set(type, [...(this.listeners.get(type) || []), listener]); }
  async dispatch(type) { await Promise.all((this.listeners.get(type) || []).map((listener) => listener({ target: this }))); await flush(); }
  async click() { if (!this.disabled) await this.dispatch('click'); }
  focus() { this.focused = true; }
}
const flush = async () => { for (let i = 0; i < 24; i++) await Promise.resolve(); };
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
function dom(prefix, names) {
  const doc = { createElement: (tag) => new Element(tag, doc) }; const root = new Element('section', doc);
  for (const name of names.split(' ')) { const item = doc.createElement('div'); item.id = `${prefix}-${name}`; root.append(item); }
  return { root, get: (id) => root.querySelector(`#${prefix}-${id}`) };
}
function client(defaults, overrides = {}) {
  const calls = [];
  const api = new Proxy(Object.fromEntries(Object.keys(defaults).map((name) => [name, (...args) => {
    calls.push({ name, args }); return (overrides[name] || defaults[name])(...args);
  }])), { get(target, key) { assert.ok(key in target, `Unexpected API: ${String(key)}`); return target[key]; } });
  return { api, calls };
}
const agents = [{ slug: 'alpha', name: 'Alpha' }, { slug: 'beta', name: 'Beta' }];

test('Application shell and off-canvas drawer are clip boundaries, never focus scroll containers', () => {
  const css = readFileSync(new URL('../../co_studio/frontend/css/app.css', import.meta.url), 'utf8');
  const shell = css.match(/(?:^|\n)#app\s*\{([^}]+)\}/)?.[1];
  const drawer = css.match(/(?:^|\n)#detail-view\s*\{([^}]+)\}/)?.[1];
  assert.match(shell, /overflow:\s*clip\s*;/);
  assert.match(drawer, /overflow:\s*clip\s*;/);
  assert.match(css, /\.workspace-detail\s*\{[^}]*overflow-y:\s*auto/,
    'only the inner detail pane should retain vertical scrolling');
});
const approval = (overrides = {}) => ({ id: 'call-a', agent_slug: 'alpha', session_id: 'session-a', turn_no: 1,
  call_id: 'tool-1', tool_name: 'write_note', arguments: { content: '<script>example</script>' }, binding: 'a'.repeat(64),
  status: 'pending', created_at: '2026-10-28T00:00:00Z', expires_at: '2099-01-01T00:00:00Z', reason: null, ...overrides });
function approvals(t, overrides) {
  const view = dom('approval', 'agent filter message retry list count empty detail detail-empty tool status context arguments expiry identity reason approve reject decision-message');
  view.get('filter').value = 'pending';
  const network = client({ listApprovals: async (slug) => ({ items: slug === 'alpha' ? [approval()] : [], total: slug === 'alpha' ? 1 : 0 }),
    getApproval: async (slug, id) => approval({ agent_slug: slug, id }),
    decideApproval: async (slug, id, body) => approval({ agent_slug: slug, id, status: body.decision === 'approve' ? 'approved' : 'rejected' }) }, overrides);
  const timers = new Map(); let sequence = 0;
  const controller = createApprovalsView({ root: view.root, api: network.api,
    schedule: (fn) => { timers.set(++sequence, fn); return sequence; }, unschedule: (id) => timers.delete(id) });
  t.after(() => controller.close()); return { ...view, ...network, controller, timers };
}

test('Workspace API uses opaque paths, exact read/write methods and explicit payloads', async () => {
  const previous = globalThis.fetch; const calls = [];
  globalThis.fetch = async (...args) => { calls.push(args); return { ok: true, status: 200, json: async () => ({}) }; };
  try {
    const slug = 'alpha /?#'; const id = 'opaque /?#'; const signal = new AbortController().signal;
    await api.getToolConfig(slug, { signal }); await api.saveToolConfig(slug, { expected_revision: 3 });
    await api.listApprovals(slug, { status: 'all', signal }); await api.getApproval(slug, id, { signal });
    await api.decideApproval(slug, id, { decision: 'approve', binding: 'x' });
    await api.getKnowledgeStatus({ signal }); await api.listKnowledge({ offset: 20, signal });
    await api.addKnowledge({ text: 'explicit paste' }); await api.deleteKnowledge(id);
    await api.searchKnowledge({ query: 'test' }, { signal }); await api.diagnoseKnowledge({ query: 'test' }, { signal });
    assert.deepEqual(calls.map(([, options]) => options.method), ['GET', 'PUT', 'GET', 'GET', 'POST', 'GET', 'GET', 'POST', 'DELETE', 'POST', 'POST']);
    assert.equal(calls[3][0], `/api/agents/${encodeURIComponent(slug)}/approvals/${encodeURIComponent(id)}`);
    assert.equal(calls[8][0], `/api/knowledge/documents/${encodeURIComponent(id)}`);
    assert.equal(calls[0][1].signal, signal); assert.equal(calls[10][1].signal, signal);
    assert.equal(calls[7][1].body, '{"text":"explicit paste"}');
  } finally { globalThis.fetch = previous; }
});
test('Approvals open read-only, render literal arguments and approve only the selected binding', async (t) => {
  const h = approvals(t); h.controller.open(agents); await flush();
  assert.equal(h.get('tool').textContent, 'write_note'); assert.match(h.get('arguments').textContent, /<script>/);
  assert.equal(h.calls.filter((item) => item.name === 'decideApproval').length, 0);
  await h.get('approve').click();
  const write = h.calls.find((item) => item.name === 'decideApproval');
  assert.deepEqual(write.args, ['alpha', 'call-a', { decision: 'approve', binding: 'a'.repeat(64) }]);
  assert.match(h.get('decision-message').textContent, /this call only/);
});
test('Approvals never enable expired or malformed bindings', async (t) => {
  for (const bad of [{ expires_at: '2000-01-01' }, { binding: 'invalid' }, { status: 'succeeded' }]) {
    const h = approvals(t, { getApproval: async () => approval(bad) }); h.controller.open(agents); await flush();
    assert.equal(h.get('approve').disabled, true); await h.get('approve').click();
    assert.equal(h.calls.some((item) => item.name === 'decideApproval'), false);
  }
});
test('Approvals support the real API Unix-second timestamps', async (t) => {
  const h = approvals(t, { getApproval: async () => approval({ created_at: 1793145600, expires_at: Date.now() / 1000 + 300 }) });
  h.controller.open(agents); await flush(); assert.equal(h.get('approve').disabled, false);
  assert.match(h.get('context').textContent, /2026/);
});
test('Approvals discard delayed responses after Agent switch and close', async (t) => {
  const pending = deferred(); const h = approvals(t, { listApprovals: async (slug) => slug === 'alpha' ? pending.promise : { items: [], total: 0 } });
  h.controller.open(agents); h.get('agent').value = 'beta'; await h.get('agent').dispatch('change');
  pending.resolve({ items: [approval()], total: 1 }); await flush();
  assert.equal(h.get('count').textContent, '0'); assert.equal(h.get('detail').hidden, true);
  h.controller.close(); assert.equal(h.timers.size, 0);
});
test('Approvals reject mismatched detail identity', async (t) => {
  const h = approvals(t, { getApproval: async () => approval({ agent_slug: 'beta' }) }); h.controller.open(agents); await flush();
  assert.equal(h.get('detail').hidden, true); assert.match(h.get('detail-empty').textContent, /Could not load/);
});
test('Approvals error is not an empty inbox and has Retry', async (t) => {
  const h = approvals(t, { listApprovals: async () => { throw new Error('offline'); } }); h.controller.open(agents); await flush();
  assert.equal(h.get('retry').hidden, false); assert.equal(h.get('empty').hidden, true); assert.match(h.get('message').textContent, /Could not load/);
});
test('Approvals empty Agent list schedules no poll', async (t) => {
  const h = approvals(t); h.controller.open([]); await flush(); assert.equal(h.calls.length, 0); assert.equal(h.timers.size, 0);
});
test('Approvals conflict never reports success', async (t) => {
  const h = approvals(t, { decideApproval: async () => { throw { status: 409 }; } }); h.controller.open(agents); await flush();
  await h.get('reject').click(); assert.match(h.get('decision-message').textContent, /changed or expired/);
});
test('A pre-decision poll cannot overwrite the completed decision', async (t) => {
  const oldPoll = deferred(); let lists = 0;
  const h = approvals(t, { listApprovals: async () => {
    lists++;
    if (lists === 2) return oldPoll.promise;
    return { items: [approval({ status: lists > 2 ? 'approved' : 'pending' })], total: 1 };
  } });
  h.controller.open(agents); await flush();
  [...h.timers.values()][0](); await flush();
  await h.get('approve').click();
  oldPoll.resolve({ items: [approval()], total: 1 }); await flush();
  assert.equal(h.get('status').textContent, 'approved');
  assert.equal(h.get('approve').disabled, true);
  assert.equal(h.get('reject').disabled, true);
});

const config = (overrides = {}) => ({ revision: 1, mcp_enabled: false, approvals_enabled: false,
  approval_timeout_seconds: 120, applies_on_next_start: true, available_tools: [], ...overrides });
function policy(t, overrides) {
  const view = dom('tools', 'message form mcp approvals timeout context-budget save reload catalog');
  const network = client({ getToolConfig: async () => config(), saveToolConfig: async (_, body) => config({ ...body, revision: 2 }) }, overrides);
  const controller = createToolConfig({ root: view.root, api: network.api }); t.after(() => controller.close());
  return { ...view, ...network, controller };
}
test('Tool policy saves revision guard and never auto-starts', async (t) => {
  const h = policy(t); await h.controller.open('alpha'); h.get('mcp').checked = true; await h.get('mcp').dispatch('input');
  await h.get('save').click();
  assert.deepEqual(h.calls.find((call) => call.name === 'saveToolConfig').args, ['alpha', {
    expected_revision: 1, mcp_enabled: true, approvals_enabled: false, approval_timeout_seconds: 120, context_budget: null }]);
  assert.match(h.get('message').textContent, /next start/);
});
test('Tool policy retains draft after CAS conflict', async (t) => {
  const h = policy(t, { saveToolConfig: async () => { throw { status: 409 }; } }); await h.controller.open('alpha');
  h.get('mcp').checked = true; await h.get('mcp').dispatch('input'); await h.get('save').click();
  assert.equal(h.get('mcp').checked, true); assert.match(h.get('message').textContent, /Reload/);
});
test('Tool policy unsupported Agents are not editable', async (t) => {
  const h = policy(t, { getToolConfig: async () => { throw { status: 422 }; } }); await h.controller.open('alpha');
  assert.equal(h.get('form').hidden, true); assert.equal(h.get('save').disabled, true);
});
test('Tool policy rejects invalid timeout locally and ignores stale Agent load', async (t) => {
  const h = policy(t); await h.controller.open('alpha'); h.get('timeout').value = '5'; await h.get('timeout').dispatch('input');
  await h.get('save').click(); assert.equal(h.calls.some((call) => call.name === 'saveToolConfig'), false);
  const pending = deferred(); const stale = policy(t, { getToolConfig: async (slug) => slug === 'alpha' ? pending.promise : config({ revision: 9 }) });
  const first = stale.controller.open('alpha'); await stale.controller.open('beta'); pending.resolve(config({ mcp_enabled: true })); await first;
  assert.equal(stale.get('mcp').checked, false);
});

const documentRecord = { doc_id: 'a'.repeat(64), title: 'Timeout guide', source_uri: 'local:guide', version: '1', kind: 'document', chunk_count: 2 };
const hit = { ...documentRecord, chunk_id: 'chunk-1', line_start: 1, line_end: 4, content: '<b>Read the timeout log</b>', score: 0.5 };
function knowledge(t, overrides) {
  const view = dom('knowledge', 'add import-save delete search diagnose generate generation-confirm generation-model generation-budget generation-consent generation-start generation-dismiss generation-cancel query mode collection scope-note more retry status-retry title source version license text import-cancel body unavailable unavailable-text model list count empty message selection selected-title selected-source results query-message import explore import-message');
  view.get('mode').value = 'hybrid'; view.get('version').value = '1'; view.get('generation-budget').value = '4096';
  const network = client({ getKnowledgeStatus: async () => ({ configured: true, ready: true, embedding_model: 'local-model' }),
    listKnowledge: async () => ({ items: [documentRecord], total: 1, next_offset: null }),
    addKnowledge: async () => ({}), deleteKnowledge: async () => ({}), generateKnowledgeDiagnosis: async () => ({ status: 'insufficient_evidence', model_called: false, hypotheses: [], evidence: [] }),
    searchKnowledge: async () => ({ hits: [hit] }), diagnoseKnowledge: async () => ({ status: 'evidence', method: 'extractive',
      findings: [{ text: 'Timeout was reported.', citations: ['chunk-1'] }], evidence: [hit], next_steps: [] }) }, overrides);
  const controller = createKnowledgeView({ root: view.root, api: network.api }); t.after(() => controller.close());
  return { ...view, ...network, controller };
}
test('Knowledge open only reads, renders citation source as text and supports mode', async (t) => {
  const h = knowledge(t); await h.controller.open(); assert.deepEqual(h.calls.map((call) => call.name), ['getKnowledgeStatus', 'listKnowledge']);
  h.get('query').value = 'timeout'; h.get('mode').value = 'bm25'; await h.get('query').dispatch('input'); await h.get('search').click();
  assert.deepEqual(h.calls.find((call) => call.name === 'searchKnowledge').args[0], { query: 'timeout', mode: 'bm25', k: 5, collection: 'public' });
  assert.match(h.get('results').textContent, /<b>Read/); assert.match(h.get('results').textContent, /local:guide/); assert.match(h.get('results').textContent, /chunk-1/);
});
test('Knowledge unconfigured does not search, import, or silently create storage', async (t) => {
  const h = knowledge(t, { getKnowledgeStatus: async () => ({ configured: false, ready: false }) }); await h.controller.open();
  assert.equal(h.get('body').hidden, true); assert.equal(h.get('add').disabled, true); assert.match(h.get('unavailable-text').textContent, /not configured/);
  assert.equal(h.calls.length, 1);
});
test('Knowledge failure is distinguished from an empty document list', async (t) => {
  const h = knowledge(t, { getKnowledgeStatus: async () => { throw new Error('offline'); } }); await h.controller.open();
  assert.match(h.get('unavailable-text').textContent, /Could not connect/); assert.equal(h.get('status-retry').hidden, false);
});
test('Knowledge import requires explicit complete input and never fetches source URL', async (t) => {
  const h = knowledge(t); await h.controller.open(); await h.get('add').click(); await h.get('import-save').click();
  assert.equal(h.calls.some((call) => call.name === 'addKnowledge'), false);
  for (const [id, value] of Object.entries({ title: 'Doc', source: 'https://example.invalid/doc', text: 'Pasted text', license: 'CC0' })) h.get(id).value = value;
  await h.get('import-save').click();
  assert.deepEqual(h.calls.find((call) => call.name === 'addKnowledge').args[0], { title: 'Doc', source_uri: 'https://example.invalid/doc', text: 'Pasted text', version: '1', license: 'CC0', collection: 'public', kind: 'document' });
  assert.match(h.get('query-message').textContent, /indexed/);
});
test('Knowledge deletion requires two explicit clicks and identifies index-only removal', async (t) => {
  const h = knowledge(t); await h.controller.open(); await h.get('list').children[0].click();
  await h.get('delete').click(); assert.equal(h.calls.some((call) => call.name === 'deleteKnowledge'), false);
  await h.get('delete').click(); assert.deepEqual(h.calls.find((call) => call.name === 'deleteKnowledge').args, [documentRecord.doc_id]);
  assert.match(h.get('query-message').textContent, /source file was not deleted/);
});
test('Knowledge diagnosis omits findings with nonexistent citations and labels extractive scope', async (t) => {
  const h = knowledge(t, { diagnoseKnowledge: async () => ({ status: 'evidence', evidence: [hit], findings: [
    { text: 'Supported', citations: ['chunk-1'] }, { text: 'Unsupported claim', citations: ['missing'] }], next_steps: [] }) });
  await h.controller.open(); h.get('query').value = 'timeout'; await h.get('query').dispatch('input'); await h.get('diagnose').click();
  assert.match(h.get('results').textContent, /Supported/); assert.doesNotMatch(h.get('results').textContent, /Unsupported claim/);
  assert.match(h.get('results').textContent, /no LLM-generated/);
});
test('Knowledge closed-page search results never overwrite the next open', async (t) => {
  const pending = deferred(); const h = knowledge(t, { searchKnowledge: () => pending.promise }); await h.controller.open();
  h.get('query').value = 'timeout'; await h.get('query').dispatch('input'); const click = h.get('search').click(); await flush();
  h.controller.close(); await h.controller.open(); pending.resolve({ hits: [hit] }); await click;
  assert.equal(h.get('results').textContent, '');
});
test('History query prefers exactly the mapped Agent scope and never auto-searches', async (t) => {
  const scope = 'agent-' + 'a'.repeat(24);
  const h = knowledge(t, { getKnowledgeStatus: async () => ({ configured: true, ready: true,
    agent_collections: [{ collection: scope, agent_slug: 'alpha', agent_name: 'Alpha' }] }) });
  const opening = h.controller.open(); h.controller.prefill({ query: 'timeout', turn: 2, agentSlug: 'alpha' }); await opening;
  assert.equal(h.get('collection').value, scope); assert.equal(h.get('add').hidden, true);
  assert.equal(h.calls.find((call) => call.name === 'listKnowledge').args[0].collection, scope);
  assert.equal(h.calls.some((call) => call.name === 'searchKnowledge'), false);
  await h.get('search').click(); assert.equal(h.calls.find((call) => call.name === 'searchKnowledge').args[0].collection, scope);
});
test('Missing History Agent scope never silently falls back to public', async (t) => {
  const h = knowledge(t); const opening = h.controller.open(); h.controller.prefill({ query: 'timeout', turn: 1, agentSlug: 'missing' }); await opening;
  assert.equal(h.calls.some((call) => call.name === 'listKnowledge'), false); assert.equal(h.get('search').disabled, true);
  assert.match(h.get('query-message').textContent, /explicitly/);
  h.get('collection').value = 'public'; await h.get('collection').dispatch('change');
  assert.equal(h.calls.find((call) => call.name === 'listKnowledge').args[0].collection, 'public');
});
test('Changing collection discards stale list responses and clears prior evidence', async (t) => {
  const pending = deferred(); const scope = 'agent-' + 'a'.repeat(24);
  const h = knowledge(t, { getKnowledgeStatus: async () => ({ configured: true, ready: true,
    agent_collections: [{ collection: scope, agent_slug: 'alpha', agent_name: 'Alpha' }] }),
    listKnowledge: ({ collection }) => collection === 'public' ? pending.promise : Promise.resolve({ items: [], total: 0 }) });
  const opening = h.controller.open(); await flush(); h.get('collection').value = scope; await h.get('collection').dispatch('change');
  pending.resolve({ items: [documentRecord], total: 1 }); await opening;
  assert.equal(h.get('list').children.length, 0); assert.equal(h.get('count').textContent, '0');
});
test('Tool policy context estimate is explicit, range-checked, and nullable', async (t) => {
  const h = policy(t); await h.controller.open('alpha'); h.get('context-budget').value = '127'; await h.get('context-budget').dispatch('input');
  await h.get('save').click(); assert.equal(h.calls.some((call) => call.name === 'saveToolConfig'), false);
  h.get('context-budget').value = '4096'; await h.get('context-budget').dispatch('input'); await h.get('save').click();
  assert.equal(h.calls.find((call) => call.name === 'saveToolConfig').args[1].context_budget, 4096);
});

async function consent(h) {
  h.get('query').value = '为什么无法连接？'; await h.get('query').dispatch('input'); await h.get('generate').click();
  h.get('generation-model').value = 'co/test-model'; await h.get('generation-model').dispatch('input');
  h.get('generation-consent').checked = true; await h.get('generation-consent').dispatch('change');
}
test('Generated diagnosis requires separate explicit consent and exact selected model/scope', async (t) => {
  const h = knowledge(t); await h.controller.open();
  h.get('query').value = 'why'; await h.get('query').dispatch('input'); await h.get('generate').click();
  assert.equal(h.calls.some((c) => c.name === 'generateKnowledgeDiagnosis'), false);
  assert.equal(h.get('generation-start').disabled, true);
  await consent(h); await h.get('generation-start').click();
  assert.deepEqual(h.calls.find((c) => c.name === 'generateKnowledgeDiagnosis').args[0], {
    query: '为什么无法连接？', model: 'co/test-model', mode: 'hybrid', collection: 'public', max_output_tokens: 4096, allow_model_call: true });
  assert.equal(h.get('generation-consent').checked, false); assert.match(h.get('results').textContent, /未调用模型/);
});
test('Changing query, model or retrieval mode clears generation consent', async (t) => {
  const h = knowledge(t); await h.controller.open();
  for (const [id, event] of [['query', 'input'], ['generation-model', 'input'], ['mode', 'change'], ['generation-budget', 'change']]) {
    await consent(h); assert.equal(h.get('generation-start').disabled, false);
    await h.get(id).dispatch(event); assert.equal(h.get('generation-start').disabled, true);
  }
});
test('Generated output remains literal, separates hypotheses from proof and hides invented quotes', async (t) => {
  const h = knowledge(t, { generateKnowledgeDiagnosis: async () => ({ status: 'hypotheses', model_called: true, model: 'co/test-model', evidence: [hit],
    hypotheses: [ { possible_cause: '<script>hypothesis</script>', evidence: [{ chunk_id: hit.chunk_id, quote: 'Read the timeout log' }], verification_step: '检查端口' },
      { possible_cause: 'Invented claim', evidence: [{ chunk_id: hit.chunk_id, quote: 'Not in the source' }], verification_step: 'ignored' } ], limitation: '仅供核查' }) });
  await h.controller.open(); await consent(h); await h.get('generation-start').click();
  assert.match(h.get('results').textContent, /<script>hypothesis<\/script>/);
  assert.doesNotMatch(h.get('results').textContent, /Invented claim/);
  assert.match(h.get('results').textContent, /不证明诊断正确/); assert.match(h.get('results').textContent, /未执行/);
});
test('Conflicting evidence never displays speculative hypotheses', async (t) => {
  const h = knowledge(t, { generateKnowledgeDiagnosis: async () => ({ status: 'conflicting_evidence', model_called: true, model: 'm', hypotheses: [{ possible_cause: 'hidden' }], evidence: [hit], limitation: '需核对冲突版本' }) });
  await h.controller.open(); await consent(h); await h.get('generation-start').click();
  assert.match(h.get('results').textContent, /证据存在冲突/); assert.doesNotMatch(h.get('results').textContent, /hidden/);
});
test('Cancel aborts generation and late success cannot render on a reopened page', async (t) => {
  const pending = deferred(); const h = knowledge(t, { generateKnowledgeDiagnosis: () => pending.promise });
  await h.controller.open(); await consent(h); const click = h.get('generation-start').click(); await flush();
  const call = h.calls.find((c) => c.name === 'generateKnowledgeDiagnosis');
  assert.equal(h.get('generation-cancel').hidden, false); await h.get('generation-cancel').click();
  assert.equal(call.args[1].signal.aborted, true); assert.match(h.get('query-message').textContent, /仍可能计费/);
  h.controller.close(); await h.controller.open(); pending.resolve({ status: 'hypotheses', hypotheses: [], evidence: [] }); await click;
  assert.equal(h.get('results').textContent, '');
});
test('Generation failure restores controls without returning a fabricated diagnosis', async (t) => {
  const h = knowledge(t, { generateKnowledgeDiagnosis: async () => { throw new Error('Diagnosis timed out'); } });
  await h.controller.open(); await consent(h); await h.get('generation-start').click();
  assert.match(h.get('query-message').textContent, /timed out/); assert.equal(h.get('generate').disabled, false);
  assert.equal(h.get('results').textContent, ''); assert.equal(h.get('generation-cancel').hidden, true);
});

const evidenceUnit = { evidence_id: 'ev_' + 'c'.repeat(32), chunk_id: hit.chunk_id,
  quote: hit.content, source_uri: hit.source_uri, version: hit.version,
  content_hash: 'd'.repeat(64), chunk_hash: 'e'.repeat(64), line_start: 1, line_end: 4 };
const cited = (text) => ({ text, evidence_ids: [evidenceUnit.evidence_id], evidence: [evidenceUnit] });
const v2 = (overrides = {}) => ({ protocol_version: 'diagnostic-v2', status: 'answered', model_called: true,
  verification_policy: VERIFICATION_POLICY,
  model: 'co/test-model', max_output_tokens: 4096, claims: [cited('文档支持的回答')], limitations: [],
  verification_steps: [], questions: [], message: '依据所选资料回答。', evidence: [hit], evidence_units: [evidenceUnit], ...overrides });

test('V2 factual answers use server quotes and expandable sources rather than hypotheses', async (t) => {
  const h = knowledge(t, { generateKnowledgeDiagnosis: async () => v2({
    claims: [cited('<script>按原文回答</script>')], limitations: [cited('仅适用当前版本')],
    verification_steps: [{ kind: 'inspect_client_version', action: READONLY_VERIFICATION_ACTIONS.inspect_client_version,
      evidence_ids: [evidenceUnit.evidence_id], evidence: [evidenceUnit] }],
  }) });
  await h.controller.open(); await consent(h); await h.get('generation-start').click();
  const text = h.get('results').textContent;
  assert.match(text, /资料回答/); assert.match(text, /<script>按原文回答<\/script>/);
  assert.match(text, /适用条件与限制/); assert.match(text, /只读核查（未执行）/);
  assert.match(text, /diagnostic-v2.*4096 Token/); assert.match(text, /查看依据（1）/);
  assert.match(text, /全部检索来源（1）/); assert.doesNotMatch(text, /可能原因/);
  assert.ok(h.get('results').children.some((el) => el.tagName === 'details'));
});

test('V2 answer cards hide invented, wrong-version and mismatched-ID quotations', async (t) => {
  for (const change of [{ quote: 'Invented' }, { version: 'stale' }, { chunk_id: 'other' },
    { evidence_id: 'ev_other' }, { source_uri: 'private:other' }, { content_hash: 'changed' }, { line_start: 999 }]) {
    const h = knowledge(t, { generateKnowledgeDiagnosis: async () => v2({
      claims: [{ ...cited('UNSUPPORTED'), evidence: [{ ...evidenceUnit, ...change }] }],
    }) });
    await h.controller.open(); await consent(h); await h.get('generation-start').click();
    assert.doesNotMatch(h.get('results').textContent, /UNSUPPORTED/);
    assert.match(h.get('results').textContent, /内容未通过引用检查/);
  }
});

test('V2 states distinguish clarification, refusal, missing evidence and conflict', async (t) => {
  for (const [status, title] of [['needs_clarification', '需要补充信息'], ['refused', '无法执行此请求'],
    ['insufficient_evidence', '证据不足'], ['conflicting_evidence', '证据存在冲突'], ['hypotheses', '诊断假设 · 待核实']]) {
    const h = knowledge(t, { generateKnowledgeDiagnosis: async () => v2({ status,
      questions: ['请提供客户端、版本和具体错误信息'], message: '服务器固定边界说明',
      claims: [cited('受引用支持的主体')],
    }) });
    await h.controller.open(); await consent(h); await h.get('generation-start').click();
    const text = h.get('results').textContent;
    assert.match(text, new RegExp(title)); assert.match(text, /服务器固定边界说明/);
    if (status === 'needs_clarification') assert.match(text, /请提供客户端、版本和具体错误信息/);
    else assert.doesNotMatch(text, /请提供客户端、版本和具体错误信息/);
    if (['needs_clarification', 'refused', 'insufficient_evidence'].includes(status)) assert.doesNotMatch(text, /受引用支持的主体/);
  }
});

test('Output budget is explicit, bounded and requires fresh consent after change', async (t) => {
  const h = knowledge(t); await h.controller.open(); await consent(h);
  h.get('generation-budget').value = '2048'; await h.get('generation-budget').dispatch('change');
  assert.equal(h.get('generation-start').disabled, true);
  h.get('generation-consent').checked = true; await h.get('generation-consent').dispatch('change');
  await h.get('generation-start').click();
  assert.equal(h.calls.find((c) => c.name === 'generateKnowledgeDiagnosis').args[0].max_output_tokens, 2048);
  await consent(h); h.get('generation-budget').value = '99999'; await h.get('generation-budget').dispatch('change');
  h.get('generation-consent').checked = true; await h.get('generation-consent').dispatch('change');
  await h.get('generation-start').click();
  assert.equal(h.calls.filter((c) => c.name === 'generateKnowledgeDiagnosis').length, 1);
});

test('Read-only verification map is identical to the fixed server contract', () => {
  const source = readFileSync(new URL('../../co_studio/diagnostic.py', import.meta.url), 'utf8');
  assert.ok(source.includes(`VERIFICATION_POLICY = "${VERIFICATION_POLICY}"`));
  const block = source.match(/READONLY_VERIFICATION_ACTIONS\s*=\s*\{([\s\S]*?)\n\}/)?.[1];
  assert.ok(block, 'server policy map must exist');
  const pairs = [...block.matchAll(/"([a-z_]+)":\s*"([^"\n]*)"/g)];
  assert.deepEqual(Object.fromEntries(pairs.map(([, kind, action]) => [kind, action])), READONLY_VERIFICATION_ACTIONS);
});

test('Read-only verification requires exact policy, enumerated kind and server wording', () => {
  for (const [kind, action] of Object.entries(READONLY_VERIFICATION_ACTIONS)) {
    assert.equal(isReadOnlyVerificationStep(VERIFICATION_POLICY, { kind, action }), true);
    assert.equal(isReadOnlyVerificationStep(VERIFICATION_POLICY, { kind, action: `${action} co init ./` }), false);
    assert.equal(isReadOnlyVerificationStep('unknown', { kind, action }), false);
  }
  for (const bad of [null, {}, { kind: 'execute_shell', action: 'co init ./ --template co-ai' },
    { kind: 'toString', action: Object.prototype.toString }, { kind: {}, action: 'hidden' }]) {
    assert.equal(isReadOnlyVerificationStep(VERIFICATION_POLICY, bad), false);
  }
});

test('V2 hides legacy free-form, unknown-kind and tampered verification text', async (t) => {
  for (const change of [
    { policy: undefined, step: { action: 'co init ./ --template co-ai' } },
    { policy: 'unknown', step: { kind: 'inspect_config', action: READONLY_VERIFICATION_ACTIONS.inspect_config } },
    { policy: VERIFICATION_POLICY, step: { kind: 'write_config', action: 'co init ./ --template co-ai' } },
    { policy: VERIFICATION_POLICY, step: { kind: 'inspect_config', action: 'co init ./ --template co-ai' } },
  ]) {
    const h = knowledge(t, { generateKnowledgeDiagnosis: async () => v2({ verification_policy: change.policy,
      verification_steps: [{ ...change.step, evidence_ids: [evidenceUnit.evidence_id], evidence: [evidenceUnit] }],
    }) });
    await h.controller.open(); await consent(h); await h.get('generation-start').click();
    assert.doesNotMatch(h.get('results').textContent, /co init|只读核查（未执行）/);
    assert.match(h.get('results').textContent, /只读策略校验.*已隐藏/);
  }
});
