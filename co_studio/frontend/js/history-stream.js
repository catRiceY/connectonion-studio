// History events invalidate authoritative GET results; they contain no history
// bodies. EventSource owns reconnect cursors while its native connection lives.
const EVENT_FIELDS = {
  'history.changed': ['agent_slug', 'session_id', 'capture_id'],
  'history.sync': ['agent_slug', 'state'],
  'history.reset': ['agent_slug', 'reason'],
};
const SYNC_STATES = new Set(['pending', 'syncing', 'ok', 'warning', 'error', 'stopped', 'disabled']);
const RESET_REASONS = new Set(['initial', 'cursor_expired', 'cursor_invalid', 'reconcile']);
const MAX_SEEN_IDS = 256;
const FIRST_RETRY_MS = 2000;
const MAX_RETRY_MS = 30000;

const isIdentifier = (value) => typeof value === 'string'
  && value.length > 0 && value.length <= 2048 && !/[\u0000-\u001f\u007f-\u009f]/u.test(value);

function parseEvent(type, event, slug) {
  if (!isIdentifier(event.lastEventId) || typeof event.data !== 'string') return null;
  let data;
  try {
    data = JSON.parse(event.data);
  } catch {
    return null;
  }
  if (!data || typeof data !== 'object' || Array.isArray(data) || data.agent_slug !== slug) return null;
  const fields = EVENT_FIELDS[type];
  const keys = Object.keys(data);
  if (keys.length !== fields.length || !fields.every((key) => Object.hasOwn(data, key))) return null;
  if (type === 'history.changed' && (!isIdentifier(data.session_id) || !isIdentifier(data.capture_id))) return null;
  if (type === 'history.sync' && !SYNC_STATES.has(data.state)) return null;
  if (type === 'history.reset' && !RESET_REASONS.has(data.reason)) return null;
  return { type, id: event.lastEventId, data };
}

/**
 * Subscribe to same-origin history metadata. Status is one of connecting,
 * live, reconnecting, unavailable. The optional third argument supplies browser
 * primitives for tests; callers normally provide only slug and callbacks.
 */
export function subscribeHistory(slug, { onEvent, onStatus } = {}, dependencies = {}) {
  const EventSourceImpl = Object.hasOwn(dependencies, 'EventSource')
    ? dependencies.EventSource : globalThis.EventSource;
  const setTimer = dependencies.setTimeout ?? globalThis.setTimeout.bind(globalThis);
  const clearTimer = dependencies.clearTimeout ?? globalThis.clearTimeout.bind(globalThis);
  const seen = new Set();
  let connection = null;
  let retryTimer = null;
  let retryDelay = FIRST_RETRY_MS;
  let attempted = false;
  let closed = false;
  let status = null;

  function report(next) {
    if (closed || status === next) return;
    status = next;
    if (typeof onStatus === 'function') onStatus(next);
  }

  function disconnect() {
    const previous = connection;
    connection = null;
    if (!previous) return;
    for (const [type, listener] of previous.listeners) {
      previous.source.removeEventListener(type, listener);
    }
    previous.source.close();
  }

  function scheduleRetry() {
    if (closed || retryTimer !== null) return;
    retryTimer = setTimer(() => {
      retryTimer = null;
      connect();
    }, retryDelay);
    retryDelay = Math.min(retryDelay * 2, MAX_RETRY_MS);
  }

  function connect() {
    if (closed || connection || retryTimer !== null) return;
    report(attempted ? 'reconnecting' : 'connecting');
    if (closed) return;
    attempted = true;
    let source;
    try {
      // A fresh source deliberately has no explicit cursor. The server sends an
      // initial reset, so a gap cannot be hidden by a guessed replay position.
      source = new EventSourceImpl(`/api/agents/${encodeURIComponent(slug)}/history/stream`);
    } catch {
      report('unavailable');
      scheduleRetry();
      return;
    }
    const current = { source, listeners: new Map() };
    connection = current;
    const isCurrent = () => !closed && connection === current;
    const listen = (type, listener) => {
      current.listeners.set(type, listener);
      source.addEventListener(type, listener);
    };
    listen('open', () => {
      if (!isCurrent()) return;
      retryDelay = FIRST_RETRY_MS;
      report('live');
    });
    listen('error', () => {
      if (!isCurrent()) return;
      if (source.readyState === (EventSourceImpl.CLOSED ?? 2)) {
        // HTTP failures can permanently close EventSource. Only this case
        // replaces it; CONNECTING keeps the browser's Last-Event-ID handling.
        disconnect();
        report('unavailable');
        scheduleRetry();
      } else {
        report('reconnecting');
      }
    });
    for (const type of Object.keys(EVENT_FIELDS)) {
      listen(type, (event) => {
        if (!isCurrent()) return;
        const parsed = parseEvent(type, event, slug);
        if (!parsed) return;
        if (type !== 'history.reset') {
          if (seen.has(parsed.id)) return;
          seen.add(parsed.id);
          if (seen.size > MAX_SEEN_IDS) seen.delete(seen.values().next().value);
        }
        // Periodic reset events intentionally reuse the latest cursor.
        if (typeof onEvent === 'function') onEvent(parsed);
      });
    }
  }

  const subscription = {
    close() {
      if (closed) return;
      closed = true;
      if (retryTimer !== null) clearTimer(retryTimer);
      retryTimer = null;
      disconnect();
      seen.clear();
      onEvent = null;
      onStatus = null;
    },
  };
  if (typeof EventSourceImpl !== 'function') report('unavailable');
  else connect();
  return subscription;
}
