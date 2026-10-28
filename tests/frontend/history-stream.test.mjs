// Deterministic browser transport doubles: no network, polling or timers escape.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { subscribeHistory } from '../../co_studio/frontend/js/history-stream.js';
import { subscribeHistory as apiSubscribeHistory } from '../../co_studio/frontend/js/api.js';

function harness({ failures = 0 } = {}) {
  const sources = [];
  const attempts = [];
  const pending = new Map();
  const delays = [];
  const events = [];
  const statuses = [];
  let nextTimer = 0;
  class Source {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSED = 2;
    readyState = Source.CONNECTING;
    listeners = new Map();
    closeCalls = 0;
    constructor(...args) {
      attempts.push(args);
      if (failures-- > 0) throw new Error('Constructor unavailable');
      assert.equal(sources.filter((source) => source.readyState !== Source.CLOSED).length, 0);
      this.url = args[0];
      sources.push(this);
    }
    addEventListener(type, listener) {
      const callbacks = this.listeners.get(type) || new Set();
      callbacks.add(listener);
      this.listeners.set(type, callbacks);
    }
    removeEventListener(type, listener) { this.listeners.get(type)?.delete(listener); }
    emit(type, event = {}) {
      for (const listener of [...(this.listeners.get(type) || [])]) listener(event);
    }
    open() { this.readyState = Source.OPEN; this.emit('open'); }
    fail(state = Source.CONNECTING) { this.readyState = state; this.emit('error'); }
    close() { this.closeCalls++; this.readyState = Source.CLOSED; }
  }
  const dependencies = {
    EventSource: Source,
    setTimeout(callback, delay) {
      assert.equal(pending.size, 0, 'at most one retry timer');
      const id = nextTimer++;
      pending.set(id, callback);
      delays.push(delay);
      return id;
    },
    clearTimeout(id) { pending.delete(id); },
  };
  return {
    sources, attempts, pending, delays, events, statuses, dependencies,
    subscribe(slug = 'alpha') {
      return subscribeHistory(slug, { onEvent: (event) => events.push(event), onStatus: (status) => statuses.push(status) }, dependencies);
    },
    tick() {
      assert.equal(pending.size, 1);
      const [id, callback] = pending.entries().next().value;
      pending.delete(id);
      callback();
    },
  };
}

const changed = (agent_slug = 'alpha', extra = {}) => ({
  agent_slug, session_id: 'opaque/session:中文?#%', capture_id: 'opaque/capture:中文?#%', ...extra,
});
const send = (source, type, id, data) => source.emit(type, { lastEventId: id, data: JSON.stringify(data) });

test('API re-exports a same-origin EventSource subscription with an encoded slug', () => {
  assert.equal(apiSubscribeHistory, subscribeHistory);
  const h = harness();
  const slug = 'alpha / 中文?#%';
  const subscription = h.subscribe(slug);
  assert.deepEqual(h.attempts, [[`/api/agents/${encodeURIComponent(slug)}/history/stream`]]);
  assert.deepEqual(h.statuses, ['connecting']);
  h.sources[0].open();
  h.sources[0].open();
  assert.deepEqual(h.statuses, ['connecting', 'live']);
  send(h.sources[0], 'history.changed', 'opaque cursor / 中文?%', changed(slug));
  assert.deepEqual(h.events, [{ type: 'history.changed', id: 'opaque cursor / 中文?%', data: changed(slug) }]);
  subscription.close();
});

test('native CONNECTING reconnects reuse one source and its browser-owned cursor', () => {
  const h = harness();
  const subscription = h.subscribe();
  const source = h.sources[0];
  source.open();
  send(source, 'history.changed', 'id:1', changed());
  source.fail();
  source.fail();
  assert.equal(h.sources.length, 1);
  assert.equal(h.pending.size, 0);
  assert.equal(source.closeCalls, 0);
  source.open();
  send(source, 'history.changed', 'id:1', changed());
  send(source, 'history.changed', 'id:2', changed());
  assert.equal(h.events.length, 2);
  assert.deepEqual(h.statuses, ['connecting', 'live', 'reconnecting', 'live']);
  subscription.close();
});

test('CLOSED failures retry with one timer and bounded backoff, reset after open', () => {
  const h = harness();
  const subscription = h.subscribe();
  for (let index = 0; index < 7; index++) {
    const source = h.sources.at(-1);
    source.fail(2);
    source.fail(2);
    assert.equal(source.closeCalls, 1);
    assert.equal(h.statuses.at(-1), 'unavailable');
    assert.equal(h.pending.size, 1);
    h.tick();
    assert.equal(h.statuses.at(-1), 'reconnecting');
  }
  assert.deepEqual(h.delays, [2000, 4000, 8000, 16000, 30000, 30000, 30000]);
  assert.equal(h.sources.length, 8);
  assert.ok(h.attempts.every((args) => args.length === 1 && args[0] === '/api/agents/alpha/history/stream'));
  h.sources.at(-1).open();
  h.sources.at(-1).fail(2);
  assert.equal(h.delays.at(-1), 2000);
  subscription.close();
  assert.equal(h.pending.size, 0);
});

test('constructor failures follow the same capped retry policy and recover', () => {
  const h = harness({ failures: 7 });
  const subscription = h.subscribe();
  for (let index = 0; index < 7; index++) h.tick();
  assert.deepEqual(h.delays, [2000, 4000, 8000, 16000, 30000, 30000, 30000]);
  assert.equal(h.sources.length, 1);
  assert.equal(h.attempts.length, 8);
  assert.equal(h.pending.size, 0);
  h.sources[0].open();
  assert.equal(h.statuses.at(-1), 'live');
  subscription.close();
});

test('missing EventSource is unavailable without attempts or a retry loop', () => {
  const h = harness();
  const subscription = subscribeHistory('alpha', { onStatus: (status) => h.statuses.push(status) }, {
    ...h.dependencies, EventSource: undefined,
  });
  assert.deepEqual(h.statuses, ['unavailable']);
  assert.equal(h.pending.size, 0);
  assert.equal(h.attempts.length, 0);
  subscription.close();
  subscription.close();
});

test('close is idempotent, detaches listeners, cancels retry and rejects queued callbacks', () => {
  const h = harness();
  const subscription = h.subscribe();
  const source = h.sources[0];
  const queuedEvent = [...source.listeners.get('history.changed')][0];
  const queuedOpen = [...source.listeners.get('open')][0];
  source.fail(2);
  const queuedRetry = [...h.pending.values()][0];
  const statuses = [...h.statuses];
  subscription.close();
  subscription.close();
  queuedEvent({ lastEventId: 'late', data: JSON.stringify(changed()) });
  queuedOpen();
  queuedRetry();
  assert.equal(h.events.length, 0);
  assert.deepEqual(h.statuses, statuses);
  assert.equal(h.sources.length, 1);
  assert.equal(h.pending.size, 0);
  assert.equal(source.closeCalls, 1);
  assert.ok([...source.listeners.values()].every((listeners) => listeners.size === 0));
});

test('close releases a live source exactly once and never delivers later events', () => {
  const h = harness();
  const subscription = h.subscribe();
  const source = h.sources[0];
  source.open();
  subscription.close();
  subscription.close();
  source.fail();
  send(source, 'history.changed', 'late', changed());
  assert.equal(source.closeCalls, 1);
  assert.equal(h.events.length, 0);
  assert.deepEqual(h.statuses, ['connecting', 'live']);
});

test('replaced sources cannot report stale events or status or create extra retries', () => {
  const h = harness();
  const subscription = h.subscribe();
  const previous = h.sources[0];
  const queued = Object.fromEntries([...previous.listeners].map(([type, callbacks]) => [type, [...callbacks][0]]));
  previous.fail(2);
  h.tick();
  h.sources[1].open();
  queued.open();
  queued.error();
  queued['history.changed']({ lastEventId: 'old', data: JSON.stringify(changed()) });
  assert.equal(h.events.length, 0);
  assert.equal(h.pending.size, 0);
  assert.equal(h.sources.length, 2);
  assert.deepEqual(h.statuses, ['connecting', 'unavailable', 'reconnecting', 'live']);
  subscription.close();
});

test('reset events always deliver even when their ID repeats a changed or sync cursor', () => {
  const h = harness();
  const subscription = h.subscribe();
  const source = h.sources[0];
  send(source, 'history.changed', 'same', changed());
  send(source, 'history.sync', 'sync', { agent_slug: 'alpha', state: 'warning' });
  for (const reason of ['initial', 'cursor_expired', 'cursor_invalid', 'reconcile', 'reconcile']) {
    send(source, 'history.reset', 'same', { agent_slug: 'alpha', reason });
    send(source, 'history.reset', 'sync', { agent_slug: 'alpha', reason });
  }
  send(source, 'history.changed', 'same', changed());
  send(source, 'history.sync', 'sync', { agent_slug: 'alpha', state: 'warning' });
  assert.equal(h.events.length, 12);
  assert.equal(h.events.filter((event) => event.type === 'history.reset').length, 10);
  subscription.close();
});

test('changed/sync dedup is shared and bounded to the latest 256 opaque IDs', () => {
  const h = harness();
  const subscription = h.subscribe();
  const source = h.sources[0];
  for (let index = 0; index < 257; index++) send(source, 'history.changed', `id:${index}`, changed());
  send(source, 'history.sync', 'id:256', { agent_slug: 'alpha', state: 'ok' });
  send(source, 'history.changed', 'id:1', changed());
  assert.equal(h.events.length, 257);
  send(source, 'history.changed', 'id:0', changed());
  assert.equal(h.events.length, 258, 'the oldest ID was evicted');
  send(source, 'history.changed', 'id:1', changed());
  assert.equal(h.events.length, 259, 'inserting the evicted ID also maintains the bound');
  subscription.close();
});

test('malformed, foreign, untyped and unexpected payload fields are ignored', () => {
  const h = harness();
  const subscription = h.subscribe();
  const source = h.sources[0];
  for (const raw of ['', '{', 'null', '[]', '"<script>bad()</script>"', '<h1>HTTP error</h1>', '42']) {
    source.emit('history.changed', { lastEventId: 'valid', data: raw });
  }
  for (const data of [changed('other'), changed('alpha', { session_id: '' }), changed('alpha', { capture_id: 12 }),
    changed('alpha', { capture_id: '\ninvalid' }), changed('alpha', { body: 'private content' }),
    { agent_slug: 'alpha', session_id: 'session' }, { agent_slug: 'alpha', capture_id: 'capture' }]) {
    send(source, 'history.changed', 'valid', data);
  }
  for (const id of ['', null, 12, 'bad\nline', 'x'.repeat(2049)]) send(source, 'history.changed', id, changed());
  source.emit('history.changed', { lastEventId: 'valid', data: changed() });
  send(source, 'history.sync', 'valid', { agent_slug: 'alpha', state: 'arbitrary' });
  send(source, 'history.sync', 'valid', { agent_slug: 'alpha', state: 'ok', error: 'private details' });
  send(source, 'history.reset', 'valid', { agent_slug: 'alpha', reason: 'arbitrary' });
  send(source, 'message', 'valid', changed());
  send(source, 'heartbeat', 'valid', changed());
  assert.equal(h.events.length, 0);
  send(source, 'history.changed', 'valid', changed());
  assert.equal(h.events.length, 1, 'invalid events must not consume dedup IDs');
  subscription.close();
});

test('each supported sync state validates without interpreting its contents', () => {
  const h = harness();
  const subscription = h.subscribe();
  const states = ['pending', 'syncing', 'ok', 'warning', 'error', 'stopped', 'disabled'];
  for (const state of states) send(h.sources[0], 'history.sync', state, { agent_slug: 'alpha', state });
  assert.deepEqual(h.events.map((event) => event.data.state), states);
  subscription.close();
});
