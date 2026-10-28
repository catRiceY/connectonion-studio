// Read-only History controller. Navigation, Agent discovery and design primitives
// stay in the existing app; only this view's selection/paging state lives here.
// No Agent startup, WebSocket, file path, HTML interpolation or localStorage.
import { describeExecutionEvent, describePromptProvenance } from './execution-timeline.js';
import { describeContextReport } from './context-report.js';

const recorded = (value) => value ?? 'Not recorded';
const dateText = (seconds) => seconds == null ? 'Not recorded' : new Date(seconds * 1000).toLocaleString();
const archiveDateText = (value) => value == null ? 'Not recorded' : new Date(value).toLocaleString();
const durationText = (ms) => ms == null ? 'Not recorded' : `${ms.toLocaleString()} ms`;
const sourceKey = (session) => JSON.stringify([
  session.source_revision, session.updated_at, session.created_at, session.source_status,
]);

const PERSONALISATION_START = '<<<CONNECTONION_PERSONALISATION_V2>>>';
const PERSONALISATION_END = '<<<CONNECTONION_END_PERSONALISATION_V2>>>';
const USER_REQUEST_START = '<<<CONNECTONION_USER_REQUEST_V2>>>';

function personalisedUserRequest(prompt) {
  // Display-only recognition of the iOS V2 envelope. Do not search inside plain
  // user text or scan the extracted request again: it may quote these markers.
  if (typeof prompt !== 'string' || !prompt.startsWith(PERSONALISATION_START)) return null;
  const end = prompt.indexOf(PERSONALISATION_END, PERSONALISATION_START.length);
  if (end < 0) return null;
  const personalisation = prompt.slice(PERSONALISATION_START.length, end);
  // Nested, out-of-order or misspelled envelope markers are ambiguous. Leave
  // the original input visible rather than accidentally hiding user content.
  if (personalisation.includes('<<<CONNECTONION_')) return null;
  const remainder = prompt.slice(end + PERSONALISATION_END.length);
  const whitespace = /^\s+/.exec(remainder)?.[0];
  if (!whitespace || !remainder.slice(whitespace.length).startsWith(USER_REQUEST_START)) return null;
  let request = remainder.slice(whitespace.length + USER_REQUEST_START.length);
  // iOS separates the request marker and message with one newline. Keep the
  // message's own leading indentation, further newlines and trailing whitespace.
  if (request.startsWith('\r\n')) request = request.slice(2);
  else if (request.startsWith('\n')) request = request.slice(1);
  return request.trim() ? request : null;
}

export function createHistoryView({ root, api, timers = globalThis, onKnowledge = null }) {
  const $ = (id) => root.querySelector(`#history-${id}`);
  const doc = root.ownerDocument;
  const node = (tag, className, text) => {
    const element = doc.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = String(text);
    return element;
  };
  const message = (id, text = '', error = false) => {
    const element = $(id);
    element.textContent = text;
    element.hidden = !text;
    element.classList.toggle('is-error', error);
    if (id === 'list-message') {
      // Use Logs' actual empty-state component, not a small paragraph above the list.
      const empty = sessions.length === 0 && !error;
      element.classList.toggle('logs-empty', empty);
      element.classList.toggle('history-empty-message', empty);
    }
    renderRecovery();
  };
  const more = (id, page, busy = false) => {
    $(id).hidden = page?.next_offset == null;
    $(id).disabled = busy;
  };

  let active = false;
  let agents = [];
  let agentSignature = '';
  let slug = null;
  let source = null;
  let sync = null;
  let archivedAt = null;
  let selected = null;
  let selectedCapture = null;
  let selectionWarning = null;
  let versions = [];
  let versionPage = null;
  let versionGeneration = 0;
  let versionBusy = false;
  let sessions = [];
  let sessionPage = null;
  let session = null;
  let turns = [];
  let turnPage = null;
  let events = [];
  let eventPage = null;
  let filter = 'all';
  let changed = false;
  // Invalidating generations also prevents an old failure/finally from clearing
  // the current selection or its loading flag. Requests need not mutate runtime.
  let listGeneration = 0;
  let detailGeneration = 0;
  let eventGeneration = 0;
  let listBusy = false;
  let turnBusy = false;
  let eventBusy = false;
  let detailBusy = false;
  let detailFailed = false;
  let followLatest = true;
  let latestCapture = null;
  let liveConnection = null;
  let liveGeneration = 0;
  let liveStatus = '';
  let liveError = '';
  let liveTimer = null;
  let liveBusy = false;
  let liveDirty = false;
  let liveRequest = null;
  let liveDeadline = null;
  let interaction = 0;
  const timelines = new Map();

  function renderRecovery() {
    // Healthy auto-updates need no permanent toolbar or connection badge.
    const connectionError = source === 'archive' ? liveError
      || (['reconnecting', 'unavailable'].includes(liveStatus) ? 'Automatic updates interrupted.' : '') : '';
    const readError = ['list-message', 'detail-message', 'event-message', 'version-message']
      .some((id) => !$(id).hidden && $(id).classList.contains('is-error'));
    const needed = Boolean(active && slug && (connectionError || readError || detailFailed || changed
      || ['error', 'warning'].includes(sync?.state)));
    $('connection-error').textContent = connectionError;
    $('connection-error').hidden = !connectionError;
    $('recovery').hidden = !needed;
    $('refresh').hidden = !needed;
    $('refresh').disabled = listBusy;
  }

  function stopLive() {
    liveGeneration++;
    liveConnection?.close(); liveConnection = null;
    if (liveTimer !== null) timers.clearTimeout(liveTimer);
    if (liveDeadline !== null) timers.clearTimeout(liveDeadline);
    liveRequest?.abort(); liveRequest = null; liveDeadline = null;
    liveTimer = null; liveBusy = false; liveDirty = false;
    liveStatus = ''; liveError = '';
    renderRecovery();
  }

  function queueLiveRefresh() {
    if (!active || source !== 'archive' || !liveConnection || !liveDirty || liveTimer !== null || liveBusy) return;
    liveTimer = timers.setTimeout(() => {
      liveTimer = null;
      void refreshLive();
    }, 200);
  }

  function startLive() {
    if (liveConnection || !active || source !== 'archive') return;
    const generation = liveGeneration;
    const agent = slug;
    liveConnection = api.subscribeHistory(agent, {
      onStatus(status) {
        if (!active || generation !== liveGeneration || slug !== agent) return;
        liveStatus = status; renderRecovery();
      },
      onEvent(notification) {
        if (!active || generation !== liveGeneration || slug !== agent
            || notification.data.agent_slug !== agent) return;
        liveDirty = true;
        queueLiveRefresh();
      },
    });
    // Also works with a synchronous initial-reset notification in a test client.
    queueLiveRefresh();
  }

  function showUpdateNotice() {
    $('update-notice').hidden = !latestCapture || latestCapture === selectedCapture;
  }

  // Fetch as many pages as the user already loaded, into temporary arrays.
  // Never mix children from different captures or partly replace the display.
  async function readWindow(fetchPage, key, target, options = {}, valid = () => true) {
    let offset = 0; let last; const items = [];
    do {
      const data = await fetchPage({ ...options, offset, limit: Math.min(100, Math.max(20, target - items.length)) });
      if (!valid()) return null;
      last = data;
      const page = data[key];
      items.push(...page.items);
      if (page.next_offset == null || items.length >= target) break;
      if (page.next_offset <= offset) throw new Error('History pagination did not advance.');
      offset = page.next_offset;
    } while (valid());
    return { data: last, page: { ...last[key], items }, items };
  }

  async function refreshLive() {
    if (!active || source !== 'archive' || !liveDirty || liveBusy) return;
    // User navigation/pagination owns the screen until its request settles.
    if (listBusy || detailBusy || turnBusy || eventBusy || versionBusy) return;
    const generation = liveGeneration;
    const userAction = interaction;
    const agent = slug;
    const id = selected;
    const currentCapture = selectedCapture;
    const current = () => active && generation === liveGeneration && interaction === userAction
      && slug === agent && source === 'archive';
    liveBusy = true; liveDirty = false;
    const request = new AbortController();
    liveRequest = request;
    liveDeadline = timers.setTimeout(() => request.abort(), 15000);
    try {
      const list = await readWindow((options) => api.listHistory(agent, { ...options, signal: request.signal }), 'sessions',
        Math.max(20, sessions.length), {}, current);
      if (!list || !current()) return;
      if (list.data.source !== 'archive') {
        // A real server-side mode change, never a fallback after a DB failure.
        chooseAgent(agent);
        return;
      }
      if (list.items.some((item) => !item.capture_id || item.agent_slug !== agent)) {
        throw new Error('History response did not match this Agent.');
      }
      const unique = [...new Map(list.items.map((item) => [item.session_id, item])).values()];
      const chosen = unique.find((item) => item.session_id === id);
      const upgrade = session && followLatest && chosen && chosen.capture_id !== currentCapture;
      let updated = null; let turnWindow; let eventWindow; let versionWindow;
      if (upgrade) {
        const fetchDetail = async (options) => {
          const data = await api.getArchive(agent, chosen.capture_id, { ...options, signal: request.signal });
          validateDetail(data, 'archive', chosen.capture_id);
          if (data.session.session_id !== id || data.session.agent_slug !== agent) throw new Error('History session mismatch.');
          updated = data;
          return data;
        };
        turnWindow = await readWindow(({ offset, limit }) => fetchDetail({
          turn_offset: offset, turn_limit: limit, event_limit: 1,
        }), 'turns', Math.max(20, turns.length), {}, current);
        if (!turnWindow || !current()) return;
        eventWindow = await readWindow(({ offset, limit }) => fetchDetail({
          event_offset: offset, event_limit: limit, turn_limit: 1,
          ...(filter === 'all' ? {} : { turn_no: Number(filter) }),
        }), 'events', Math.max(50, events.length), {}, current);
        if (!eventWindow || !current()) return;
      }
      if (session && !$('versions-panel').hidden) {
        versionWindow = await readWindow((options) => api.listHistoryVersions(agent, id, { ...options, signal: request.signal }),
          'versions', Math.max(20, versions.length), {}, current);
        if (!versionWindow || !current()) return;
        if (versionWindow.data.session_id !== id || versionWindow.items.some((item) =>
          !item.capture_id || item.session.session_id !== id || item.session.agent_slug !== agent)) {
          throw new Error('History versions did not match this session.');
        }
      }
      if (!current()) return;
      // Read UI state at commit time: scrolling/expanding while GETs run is safe.
      const scroll = $('detail').querySelector('.history-scroll');
      const top = scroll.scrollTop;
      const focusKey = doc.activeElement?.dataset.historyFocus;
      const openTurns = new Set([...$('turns').querySelectorAll('.history-full-input')]
        .filter((item) => item.open).map((item) => item.dataset.turn));
      const openEvents = new Set([...$('events').querySelectorAll('details')]
        .filter((item) => item.open).map((item) => item.dataset.event));
      sessions = unique; sessionPage = list.page; sync = list.data.sync;
      renderSessions(); renderSync();
      message('list-message', sessions.length ? '' : syncText() || 'No saved sessions.', !sessions.length && sync?.state === 'error');
      if (session && !chosen) message('list-message', 'Selected session is outside this page.');
      if (upgrade) {
        detailGeneration++; eventGeneration++; versionGeneration++;
        selectedCapture = chosen.capture_id; session = updated.session; archivedAt = updated.archived_at;
        selectionWarning = chosen.selection_warning || null;
        turns = turnWindow.items; turnPage = turnWindow.page;
        events = eventWindow.items; eventPage = eventWindow.page;
        renderTurns(turns, true);
        renderEvents(events, true);
        message('event-message');
        for (const item of $('turns').querySelectorAll('.history-full-input')) item.open = openTurns.has(item.dataset.turn);
        for (const item of $('events').querySelectorAll('details')) item.open = openEvents.has(item.dataset.event);
        warnings(updated.issue_counts);
        message('detail-message', selectionWarning || '');
      }
      latestCapture = chosen?.capture_id || null;
      showUpdateNotice();
      if (session) renderSession();
      if (versionWindow) {
        versions = versionWindow.items; versionPage = versionWindow.page; renderVersions();
        message('version-message', versions.length ? '' : 'No saved versions.');
      } else if ($('versions-panel').hidden) {
        versions = []; versionPage = null; // Fetch fresh versions when reopened.
      }
      scroll.scrollTop = top;
      if (focusKey) [...root.querySelectorAll('.history-focus-target')]
        .find((item) => item.dataset.historyFocus === focusKey)?.focus({ preventScroll: true });
      liveError = ''; renderRecovery();
    } catch {
      if (current()) { liveError = 'Could not update history.'; renderRecovery(); }
      // Keep existing data. A subsequent notification/reconcile or manual
      // Retry reloads; never create an aggressive failed-GET polling loop.
    } finally {
      if (generation === liveGeneration) {
        timers.clearTimeout(liveDeadline); liveDeadline = null; liveRequest = null;
        if (interaction !== userAction && active) liveDirty = true;
        liveBusy = false;
        queueLiveRefresh();
      }
    }
  }

  function syncText() {
    return ({
      pending: 'History sync is pending.',
      syncing: 'Syncing history…',
      warning: 'History sync is incomplete. Use Retry to check again.',
      error: 'History sync failed. Use Retry to check again.',
      stopped: 'History sync is stopped. Showing saved history when available.',
    })[sync?.state] || '';
  }

  function renderSync() {
    message('sync-message', sessions.length ? syncText() : '', sync?.state === 'error');
    $('sync-message').title = sync?.message || '';
  }

  function requestDetail(selectedSource, agent, id, options) {
    return selectedSource === 'archive' ? api.getArchive(agent, id, options) : api.getSession(agent, id, options);
  }

  function validateDetail(data, selectedSource, id) {
    if (selectedSource === 'archive' && (data.source !== 'archive' || data.capture_id !== id)) {
      throw new Error('Archive response did not match the selected snapshot.');
    }
    if (selectedSource === 'file' && data.source === 'archive') {
      throw new Error('History response did not match the selected source.');
    }
  }

  function warnings(counts) {
    const entries = Object.entries(counts || {}).filter(([, count]) => count > 0);
    message('warning', entries.length
      ? `Some history may be incomplete. ${source === 'archive' ? 'Snapshot' : 'Agent-file'} notices: ${entries.map(([code, count]) => `${code}: ${count}`).join('; ')}.` : '');
  }

  function clearDetail(preserveVersions = false) {
    for (const state of timelines.values()) {
      state.request?.abort(); timers.clearTimeout(state.deadline);
    }
    timelines.clear();
    detailGeneration++;
    eventGeneration++;
    versionGeneration++;
    selected = null;
    selectedCapture = null;
    latestCapture = null; followLatest = true;
    selectionWarning = null;
    session = null;
    archivedAt = null;
    turns = []; events = [];
    turnPage = eventPage = null;
    filter = 'all'; changed = false;
    turnBusy = eventBusy = versionBusy = detailBusy = detailFailed = false;
    showUpdateNotice();
    $('detail').hidden = true;
    $('empty').hidden = false;
    $('empty').textContent = 'Select a session to view its history.';
    $('turns').replaceChildren();
    $('events').replaceChildren();
    message('detail-message'); message('event-message'); message('version-message');
    more('more-turns', null); more('more-events', null);
    if (!preserveVersions) {
      versions = []; versionPage = null;
      $('advanced').open = false;
      $('versions').replaceChildren();
      $('versions-panel').hidden = true;
      $('versions-toggle').setAttribute('aria-expanded', 'false');
    }
    $('versions-toggle').disabled = false;
    more('more-versions', versionPage);
  }

  function renderSessions() {
    const list = $('sessions');
    const top = list.scrollTop;
    const focused = doc.activeElement;
    const old = new Map([...list.querySelectorAll('button')].map((item) => [item.dataset.session, item]));
    const buttons = [];
    for (const item of sessions) {
      const id = item.session_id;
      const button = old.get(id) || node('button', 'logs-item history-session-item');
      if (!old.has(id)) {
        button.type = 'button'; button.dataset.session = id;
        button.addEventListener('click', () => selectSession(id));
      }
      button.classList.toggle('is-selected', id === selected);
      button.setAttribute('aria-pressed', String(id === selected));
      button.title = source === 'archive'
        ? `${item.session_id}\nSaved: ${archiveDateText(item.archived_at)}\nVersions: ${item.version_count}`
        : `${item.session_id}\nSource status: ${recorded(item.source_status)}\nUpdated: ${dateText(item.updated_at)}`;
      const body = node('span', 'logs-item-body');
      body.append(
        node('span', 'logs-item-name', item.session_id),
        node('span', 'logs-item-meta', `${recorded(item.source_status)} · ${dateText(item.updated_at)}`),
      );
      button.replaceChildren(body);
      buttons.push(button);
    }
    const previous = [...list.children];
    if (previous.length !== buttons.length || previous.some((item, index) => item !== buttons[index])) {
      list.replaceChildren(...buttons);
      if (buttons.includes(focused)) focused.focus({ preventScroll: true });
    }
    list.scrollTop = top;
    $('count').textContent = sessionPage
      ? sessions.length === sessionPage.total ? `(${sessionPage.total})` : `(${sessions.length}/${sessionPage.total})`
      : '(0)';
    more('more-sessions', sessionPage, listBusy);
  }

  async function loadSessions(append = false, restore = null) {
    if (!active || !slug || (append && (listBusy || sessionPage?.next_offset == null))) return;
    interaction++;
    const generation = ++listGeneration;
    const agent = slug;
    listBusy = true;
    $('refresh').disabled = true;
    more('more-sessions', sessionPage, true);
    message('list-message', 'Loading sessions…');
    try {
      const options = { offset: append ? sessionPage.next_offset : 0, limit: 20 };
      const data = await api.listHistory(agent, options);
      if (!active || generation !== listGeneration || slug !== agent) return;
      if (!['archive', 'file'].includes(data.source)) throw new Error('Invalid history source response.');
      if (data.source === 'archive'
          && data.sessions.items.some((item) => typeof item.capture_id !== 'string' || !item.capture_id)) {
        throw new Error('Archive response did not include snapshot identities.');
      }
      if (append && data.source !== source) throw new Error('History source changed. Use Retry to reload.');
      source = data.source;
      sync = data.sync;
      sessionPage = data.sessions;
      const merged = new Map((append ? sessions : []).map((item) => [item.session_id, item]));
      data.sessions.items.forEach((item) => merged.set(item.session_id, item));
      sessions = [...merged.values()];
      warnings(data.issue_counts);
      renderSync();
      message('list-message', sessions.length ? '' : syncText() || (source === 'archive'
        ? 'No saved sessions.' : data.source_exists ? 'No readable sessions.' : 'No saved sessions.'),
      !sessions.length && sync?.state === 'error');
      renderSessions();
      if (source === 'archive') startLive();
      if (restore && restore.source === source) {
        const current = sessions.find((item) => item.session_id === restore.id);
        if (!current) {
          message('list-message', 'Showing the latest sessions. The selected session is outside this page.');
        }
        // A visible session upgrades to the server-selected version on Retry.
        // Keep a page-2 selection pinned until its latest row becomes available.
        selectSession(restore.id, current?.capture_id || restore.captureId);
      }
    } catch (error) {
      if (active && generation === listGeneration) {
        message('list-message', `Could not load sessions. ${error.message}`, true);
      }
    } finally {
      if (active && generation === listGeneration) {
        listBusy = false;
        $('refresh').disabled = false;
        renderRecovery();
        more('more-sessions', sessionPage);
        queueLiveRefresh();
      }
    }
  }

  function chooseAgent(nextSlug, restore = null) {
    interaction++;
    stopLive();
    listGeneration++;
    listBusy = false;
    $('refresh').disabled = false;
    slug = nextSlug;
    source = null; sync = null;
    $('agent').value = nextSlug || '';
    sessions = []; sessionPage = null;
    clearDetail(); renderSessions(); warnings({}); renderSync();
    message('list-message', slug ? '' : 'No registered Agents.');
    if (slug) return loadSessions(false, restore);
  }

  function setAgents(list) {
    agents = list;
    if (!active) return;
    // Polls update the shared Agent list; don't rebuild the select on every tick.
    const signature = JSON.stringify(list.map(({ slug, name, state }) => [slug, name, state]));
    if (signature !== agentSignature) {
      agentSignature = signature;
      $('agent').replaceChildren();
      if (!list.length) {
        const option = node('option', '', 'No registered Agents');
        option.value = '';
        $('agent').append(option);
      }
      for (const agent of list) {
        const option = node('option', '', agent.name || agent.slug);
        option.value = agent.slug;
        $('agent').append(option);
      }
      $('agent').disabled = !list.length;
    }
    if (!list.some((agent) => agent.slug === slug)) chooseAgent(list[0]?.slug || null);
    else $('agent').value = slug;
  }

  function renderSession() {
    $('session-id').textContent = session.session_id;
    $('session-id').title = session.session_id;
    $('session-status').textContent = `${source === 'archive' ? 'Archive' : 'Files'} · Source status: ${recorded(session.source_status)} · Source revision: ${recorded(session.source_revision)}`;
    $('session-meta').textContent = `Created: ${dateText(session.created_at)} · Updated: ${dateText(session.updated_at)}`;
    $('capture-meta').hidden = source !== 'archive';
    $('capture-meta').textContent = source === 'archive'
      ? `Archived: ${archiveDateText(archivedAt)} · Capture: ${selectedCapture}` : '';
    $('versions-section').hidden = source !== 'archive';
    const count = sessions.find((item) => item.session_id === selected)?.version_count;
    $('versions-toggle').textContent = `History versions${count ? ` (${count})` : ''}`;
  }

  function renderVersions() {
    $('versions').replaceChildren();
    for (const version of versions) {
      const button = node('button', 'btn btn-outline history-version-item',
        `${archiveDateText(version.archived_at)} · Revision ${recorded(version.session.source_revision)}`);
      button.type = 'button';
      button.title = `Capture: ${version.capture_id}`;
      button.classList.add('history-focus-target');
      button.dataset.historyFocus = `version:${version.capture_id}`;
      button.classList.toggle('is-selected', version.capture_id === selectedCapture);
      button.setAttribute('aria-pressed', String(version.capture_id === selectedCapture));
      button.addEventListener('click', () => selectSession(selected, version.capture_id, true));
      $('versions').append(button);
    }
    more('more-versions', versionPage, versionBusy);
  }

  async function loadVersions(append = false) {
    if (!active || !selected || source !== 'archive' || versionBusy
        || (append && versionPage?.next_offset == null)) return;
    interaction++;
    const generation = ++versionGeneration;
    const detailRequest = detailGeneration;
    const agent = slug;
    const id = selected;
    versionBusy = true;
    more('more-versions', versionPage, true);
    message('version-message', 'Loading versions…');
    try {
      const data = await api.listHistoryVersions(agent, id, { offset: append ? versionPage.next_offset : 0, limit: 20 });
      if (!active || generation !== versionGeneration || detailRequest !== detailGeneration || slug !== agent || selected !== id) return;
      if (data.session_id !== id || data.versions.items.some((item) =>
        typeof item.capture_id !== 'string' || !item.capture_id || item.session.session_id !== id)) {
        throw new Error('History versions did not match the selected session.');
      }
      const merged = new Map((append ? versions : []).map((item) => [item.capture_id, item]));
      data.versions.items.forEach((item) => merged.set(item.capture_id, item));
      versions = [...merged.values()];
      versionPage = data.versions;
      renderVersions();
      message('version-message', versions.length ? '' : 'No saved versions.');
    } catch (error) {
      if (active && generation === versionGeneration && detailRequest === detailGeneration) {
        message('version-message', `Could not load versions. ${error.message} ${append ? 'Retry with Load more versions.' : 'Reopen History versions to retry.'}`, true);
      }
    } finally {
      if (active && generation === versionGeneration && detailRequest === detailGeneration) {
        versionBusy = false;
        more('more-versions', versionPage);
        queueLiveRefresh();
      }
    }
  }

  function renderFilter() {
    const all = node('option', '', 'All turns + unassigned');
    all.value = 'all';
    $('event-filter').replaceChildren(all);
    for (const turn of turns) {
      const option = node('option', '', `Turn ${turn.turn_no}`);
      option.value = String(turn.turn_no);
      $('event-filter').append(option);
    }
    // Pagination and compacted snapshots can omit a previously selected turn.
    // Keep the filter explicit instead of letting <select> silently become blank.
    if (filter !== 'all' && !turns.some((turn) => String(turn.turn_no) === filter)) {
      const option = node('option', '', `Turn ${filter}`);
      option.value = filter;
      $('event-filter').append(option);
    }
    $('event-filter').value = filter;
  }

  function appendContextReport(detail, event, focusKey, expanded = new Set()) {
    const presentation = describeContextReport(event);
    const report = node('div', 'context-report');
    report.append(node('p', 'history-note', presentation.explanation));
    if (presentation.showMetrics) {
      const metrics = node('dl', 'context-report-metrics');
      for (const [label, value] of presentation.rows) {
        metrics.append(node('dt', '', label), node('dd', '', value));
      }
      report.append(metrics, node('p', 'history-note', presentation.disclaimer));
    }
    const raw = node('details', 'context-report-raw');
    raw.dataset.event = `${event.event_id}:raw`;
    raw.open = expanded.has(raw.dataset.event);
    const summary = node('summary', 'history-focus-target', 'Raw record');
    summary.dataset.historyFocus = `${focusKey}:raw`;
    raw.append(summary, node('pre', '', JSON.stringify({
      sequence: event.sequence, occurred_at: event.occurred_at,
      event_id: event.event_id, call_id: event.call_id, source_id: event.source_id,
      payload: event.payload,
    }, null, 2)));
    detail.append(report, raw);
  }

  function renderTimeline(state) {
    const expanded = new Set([...state.body.querySelectorAll('details')]
      .filter((item) => item.open).map((item) => item.dataset.event));
    if (state.body.querySelectorAll('details').length) state.expanded = expanded;
    state.body.replaceChildren();
    state.body.hidden = !state.open;
    state.button.setAttribute('aria-expanded', String(state.open));
    state.button.textContent = state.open ? 'Hide execution' : 'View execution';
    if (!state.open) return;
    const list = node('ol', 'execution-timeline');
    for (const event of state.items) {
      const presentation = describeExecutionEvent(event);
      const item = node('li', `execution-step${presentation.failed ? ' is-error' : ''}`);
      const detail = node('details');
      detail.dataset.event = event.event_id;
      detail.open = state.expanded.has(event.event_id);
      const summary = node('summary', 'history-focus-target');
      summary.dataset.historyFocus = `timeline:${state.turn}:${event.event_id}`;
      summary.append(node('span', 'execution-label', presentation.label),
        node('span', 'history-note', presentation.note));
      detail.append(summary);
      if (event.kind === 'context_report') appendContextReport(detail, event, summary.dataset.historyFocus, state.expanded);
      else detail.append(node('pre', '', JSON.stringify({
          sequence: event.sequence, occurred_at: event.occurred_at,
          event_id: event.event_id, call_id: event.call_id, source_id: event.source_id,
          payload: event.payload,
        }, null, 2)));
      item.append(detail); list.append(item);
    }
    state.body.append(list);
    const note = state.error || (state.busy ? 'Loading execution…'
      : state.page ? `${state.items.length}/${state.page.total} recorded events` : 'No events loaded.');
    state.body.append(node('p', `history-note${state.error ? ' is-error' : ''}`, note));
    if (state.error || state.page?.next_offset != null) {
      const button = node('button', 'btn btn-outline', state.error ? 'Retry' : 'Load more events');
      button.type = 'button'; button.disabled = state.busy || changed;
      button.addEventListener('click', () => loadTimeline(state, Boolean(state.page)));
      state.body.append(button);
    }
    if (state.focusKey && !state.busy && (doc.activeElement === state.previousFocus || doc.activeElement === doc.body)) {
      const target = [...state.body.querySelectorAll('summary')].find((item) => item.dataset.historyFocus === state.focusKey);
      if (target) { target.focus({ preventScroll: true }); state.focusKey = null; }
    }
  }

  async function loadTimeline(state, append = false) {
    if (!active || state.busy || changed || !state.open) return;
    const generation = detailGeneration;
    const agent = slug; const id = selectedCapture || selected; const selectedSource = source;
    const valid = () => active && generation === detailGeneration && timelines.get(state.turn) === state
      && state.capture === id && !state.request?.signal.aborted;
    const request = new AbortController(); state.request = request;
    state.busy = true; state.error = ''; renderTimeline(state);
    const deadline = timers.setTimeout(() => request.abort(), 15000);
    state.deadline = deadline;
    try {
      const fetchPage = async ({ offset, limit }) => {
        const data = await requestDetail(selectedSource, agent, id, {
          turn_limit: 1, turn_no: state.turn, event_offset: offset, event_limit: limit, signal: request.signal,
        });
        validateDetail(data, selectedSource, id);
        if (data.session.session_id !== selected || data.session.agent_slug !== agent
          || data.events.items.some((event) => event.turn_no !== state.turn
            || event.agent_slug !== agent || event.session_id !== selected)) throw new Error('Unexpected execution records.');
        return data;
      };
      const window = append ? { data: await fetchPage({ offset: state.page.next_offset, limit: 50 }) }
        : await readWindow(fetchPage, 'events', state.target, {}, valid);
      if (!window || !valid()) return;
      if (!sameSnapshot(window.data)) { state.error = 'Saved files changed. Use Retry in the session list.'; return; }
      const page = append ? window.data.events : window.page;
      state.items = [...new Map([...(append ? state.items : []), ...page.items].map((event) => [event.event_id, event])).values()]
        .sort((a, b) => a.sequence - b.sequence);
      state.page = page;
    } catch {
      if (active && generation === detailGeneration && timelines.get(state.turn) === state)
        state.error = 'Could not load execution.';
    } finally {
      timers.clearTimeout(deadline);
      if (generation === detailGeneration && timelines.get(state.turn) === state) {
        state.busy = false; state.request = null; renderTimeline(state);
      }
    }
  }

  function attachTimeline(turn, card, button) {
    const id = selectedCapture || selected;
    const previous = timelines.get(turn);
    previous?.request?.abort();
    timers.clearTimeout(previous?.deadline);
    const state = {
      turn, capture: id, open: previous?.open || false, expanded: previous?.expanded || new Set(),
      items: previous?.capture === id ? previous.items : [],
      page: previous?.capture === id ? previous.page : null,
      target: Math.max(50, previous?.items.length || 0), busy: false, error: '', request: null,
      body: node('div', 'history-turn-timeline'), button,
      focusKey: doc.activeElement?.dataset.historyFocus?.startsWith(`timeline:${turn}:`)
        ? doc.activeElement.dataset.historyFocus : null,
      previousFocus: doc.activeElement,
    };
    if (previous?.body) state.expanded = new Set([...previous.body.querySelectorAll('details')]
      .filter((item) => item.open).map((item) => item.dataset.event));
    timelines.set(turn, state);
    button.addEventListener('click', () => {
      state.open = !state.open; renderTimeline(state);
      if (state.open && !state.page) void loadTimeline(state);
    });
    card.append(state.body); renderTimeline(state);
    if (state.open && !state.page) void loadTimeline(state);
  }

  function renderTurns(added, reset = false) {
    if (reset) {
      const retained = new Set(added.map((turn) => turn.turn_no));
      for (const [number, state] of timelines) {
        if (!retained.has(number)) {
          state.request?.abort(); timers.clearTimeout(state.deadline); timelines.delete(number);
        }
      }
      $('turns').replaceChildren();
    }
    for (const turn of added) {
      const card = node('article', 'history-turn');
      const header = node('div', 'history-turn-head');
      const button = node('button', 'btn btn-outline', 'View execution');
      button.type = 'button';
      button.classList.add('history-focus-target');
      button.dataset.historyFocus = `turn:${turn.turn_no}:events`;
      header.append(node('h4', '', `Turn ${turn.turn_no}`), button);
      const userRequest = personalisedUserRequest(turn.prompt);
      if (onKnowledge) {
        const evidence = node('button', 'btn btn-outline', 'Find evidence'); evidence.type = 'button';
        evidence.title = 'Draft a knowledge query from this turn; review before searching.';
        evidence.addEventListener('click', () => onKnowledge({ query: String(userRequest ?? turn.prompt ?? '').slice(0, 2000), turn: turn.turn_no, agentSlug: slug }));
        header.append(evidence);
      }
      card.append(header,
        node('p', 'history-note', `Duration: ${durationText(turn.duration_ms)} · Finish reason: ${recorded(turn.finish_reason)} · Base prompt: ${describePromptProvenance(turn.prompt_provenance)}`),
        node('p', 'history-label', 'User input'), node('p', 'history-text', userRequest ?? recorded(turn.prompt)),
      );
      if (userRequest !== null) {
        const fullInput = node('details', 'history-full-input');
        fullInput.dataset.turn = String(turn.turn_no);
        const summary = node('summary', '', 'Original user input');
        summary.classList.add('history-focus-target');
        summary.dataset.historyFocus = `turn:${turn.turn_no}:input`;
        summary.title = 'Original user input returned by the API, including client-added personalisation. Not the full provider request or compressed context. Credential masking is best-effort, not full anonymisation.';
        fullInput.append(summary, node('p', 'history-text', turn.prompt));
        card.append(fullInput);
      }
      card.append(
        node('p', 'history-label', 'Answer'), node('p', 'history-text', recorded(turn.result)),
      );
      attachTimeline(turn.turn_no, card, button);
      $('turns').append(card);
    }
    if (!turns.length) $('turns').append(node('p', 'history-note', 'No recorded turns. Events may still be available below.'));
    $('turn-count').textContent = `(${turns.length}/${turnPage.total})`;
    more('more-turns', turnPage, turnBusy);
    renderFilter();
  }

  function renderEvents(added, reset = false) {
    if (reset) $('events').replaceChildren();
    for (const event of added) {
      const detail = node('details', 'history-event');
      detail.dataset.event = event.event_id;
      const turnLabel = event.turn_no == null ? 'Unassigned turn' : `Turn ${event.turn_no}`;
      const name = typeof event.payload?.name === 'string' ? ` · ${event.payload.name}` : '';
      const presentation = event.kind === 'context_report' ? describeExecutionEvent(event) : null;
      const summary = node('summary', 'history-focus-target', presentation
        ? `#${event.sequence} ${presentation.label} · ${turnLabel} · ${presentation.note}`
        : `#${event.sequence} ${event.kind}${name} · ${turnLabel} · ${durationText(event.duration_ms)}`);
      summary.dataset.historyFocus = `event:${event.event_id}`;
      detail.append(summary);
      detail.append(node('p', 'history-note', `Recorded: ${dateText(event.occurred_at)}`));
      // IDs, errors, usage and payloads are plain text, never injected as HTML.
      if (event.kind === 'context_report') appendContextReport(detail, event, summary.dataset.historyFocus);
      else detail.append(node('pre', '', JSON.stringify({
          event_id: event.event_id, source_id: event.source_id, call_id: event.call_id,
          payload: event.payload,
        }, null, 2)));
      $('events').append(detail);
    }
    if (!events.length) $('events').append(node('p', 'history-note', 'No recorded events for this selection.'));
    $('event-count').textContent = `(${events.length}/${eventPage.total})`;
    more('more-events', eventPage, eventBusy);
  }

  function sameSnapshot(data) {
    // Archive pagination is pinned to one immutable capture, never the newest
    // capture for a session. validateDetail() checks the returned capture ID.
    if (source === 'archive') return true;
    // The file is not a frozen archive. Do not silently append a newer snapshot
    // to old turns/events if the source changed while the user was paging.
    if (sourceKey(data.session) === sourceKey(session)) return true;
    changed = true;
    message('detail-message', 'This session changed on disk. Showing the previously loaded snapshot; use Retry before continuing.');
    message('event-message');
    $('event-filter').disabled = true;
    more('more-turns', null); more('more-events', null);
    return false;
  }

  async function selectSession(id, captureId = null, preserveVersions = false) {
    if (!active || !source || !slug) return;
    const item = sessions.find((entry) => entry.session_id === id);
    const fixedCapture = source === 'archive' ? captureId || item?.capture_id : null;
    if (source === 'archive' && !fixedCapture) return;
    interaction++;
    clearDetail(preserveVersions);
    detailBusy = true;
    followLatest = !preserveVersions;
    selected = id;
    selectedCapture = fixedCapture;
    latestCapture = item?.capture_id || null;
    showUpdateNotice();
    // The default-selection warning describes the server's chosen version,
    // not a different version the user explicitly opens from the disclosure.
    selectionWarning = source === 'archive' && fixedCapture === item?.capture_id
      ? item?.selection_warning || null : null;
    const generation = detailGeneration;
    const agent = slug;
    const selectedSource = source;
    renderSessions();
    $('empty').textContent = 'Loading session…';
    try {
      const data = await requestDetail(selectedSource, agent, fixedCapture || id);
      if (!active || generation !== detailGeneration || selected !== id || slug !== agent || source !== selectedSource) return;
      validateDetail(data, selectedSource, fixedCapture || id);
      if (data.session.session_id !== id) throw new Error('History response did not match the selected session.');
      session = data.session;
      archivedAt = selectedSource === 'archive' ? data.archived_at : null;
      turns = data.turns.items; turnPage = data.turns;
      events = data.events.items; eventPage = data.events;
      warnings(data.issue_counts);
      $('empty').hidden = true;
      $('detail').hidden = false;
      $('event-filter').disabled = false;
      renderSession(); renderTurns(turns, true); renderEvents(events, true); renderVersions();
      message('detail-message', selectionWarning || '');
      $('detail').querySelector('.history-scroll').scrollTop = 0;
    } catch (error) {
      if (active && generation === detailGeneration) {
        detailFailed = true;
        $('empty').textContent = `Could not load session. ${error.message} Select it again or use Retry.`;
        renderRecovery();
      }
    } finally {
      if (active && generation === detailGeneration) {
        detailBusy = false;
        queueLiveRefresh();
      }
    }
  }

  async function loadTurns() {
    if (!active || !session || changed || turnBusy || turnPage.next_offset == null) return;
    interaction++;
    const generation = detailGeneration;
    const selectedSource = source;
    const id = selectedCapture || selected;
    turnBusy = true;
    more('more-turns', turnPage, true);
    message('detail-message', 'Loading turns…');
    try {
      const data = await requestDetail(selectedSource, slug, id, {
        turn_offset: turnPage.next_offset, turn_limit: 20, event_limit: 1,
      });
      if (!active || generation !== detailGeneration || changed) return;
      validateDetail(data, selectedSource, id);
      if (!sameSnapshot(data)) return;
      turns.push(...data.turns.items); turnPage = data.turns;
      renderTurns(data.turns.items);
      warnings(data.issue_counts); message('detail-message', selectionWarning || '');
    } catch (error) {
      if (active && generation === detailGeneration && !changed) message('detail-message', `Could not load more turns. ${error.message} Retry with Load more turns.`, true);
    } finally {
      if (active && generation === detailGeneration) {
        turnBusy = false;
        more('more-turns', changed ? null : turnPage);
        queueLiveRefresh();
      }
    }
  }

  async function loadEvents(append = false) {
    if (!active || !session || changed || (append && (eventBusy || eventPage?.next_offset == null))) return;
    interaction++;
    const generation = detailGeneration;
    const selectedSource = source;
    const id = selectedCapture || selected;
    const eventRequest = ++eventGeneration;
    const turnFilter = filter;
    eventBusy = true;
    if (!append) {
      events = []; eventPage = null;
      $('events').replaceChildren(); $('event-count').textContent = '';
    }
    more('more-events', eventPage, true);
    message('event-message', 'Loading events…');
    try {
      const data = await requestDetail(selectedSource, slug, id, {
        turn_limit: 1, event_offset: append ? eventPage.next_offset : 0, event_limit: 50,
        ...(turnFilter === 'all' ? {} : { turn_no: Number(turnFilter) }),
      });
      if (!active || generation !== detailGeneration || eventRequest !== eventGeneration || changed) return;
      validateDetail(data, selectedSource, id);
      if (!sameSnapshot(data)) { message('event-message'); return; }
      events.push(...data.events.items); eventPage = data.events;
      renderEvents(data.events.items, !append);
      warnings(data.issue_counts); message('event-message');
    } catch (error) {
      if (active && generation === detailGeneration && eventRequest === eventGeneration && !changed) {
        message('event-message', `Could not load events. ${error.message} ${append ? 'Retry with Load more events.' : 'Use Retry to reload.'}`, true);
      }
    } finally {
      if (active && generation === detailGeneration && eventRequest === eventGeneration) {
        eventBusy = false;
        more('more-events', changed ? null : eventPage);
        queueLiveRefresh();
      }
    }
  }

  $('agent').addEventListener('change', () => chooseAgent($('agent').value));
  $('refresh').addEventListener('click', () => chooseAgent(slug,
    selected ? { id: selected, captureId: selectedCapture, source } : null));
  $('more-sessions').addEventListener('click', () => loadSessions(true));
  $('latest').addEventListener('click', () => {
    if (latestCapture) selectSession(selected, latestCapture);
  });
  $('versions-toggle').addEventListener('click', () => {
    interaction++;
    const show = $('versions-panel').hidden;
    $('versions-panel').hidden = !show;
    $('versions-toggle').setAttribute('aria-expanded', String(show));
    if (show && !versionPage) loadVersions();
    else if (!show) {
      versionGeneration++;
      versionBusy = false;
      message('version-message');
      more('more-versions', versionPage);
    }
    queueLiveRefresh();
  });
  $('more-versions').addEventListener('click', () => loadVersions(true));
  $('more-turns').addEventListener('click', loadTurns);
  $('more-events').addEventListener('click', () => loadEvents(true));
  $('event-filter').addEventListener('change', () => {
    filter = $('event-filter').value;
    loadEvents(false);
  });

  return {
    open(list = agents) {
      stopLive();
      active = true;
      agentSignature = ''; // recreate options after a close, never retain payloads
      slug = null;
      source = null;
      setAgents(list);
    },
    close() {
      active = false;
      interaction++;
      stopLive();
      source = null;
      sync = null;
      listGeneration++;
      listBusy = false;
      clearDetail();
      sessions = []; sessionPage = null;
      renderSessions(); renderSync(); warnings({}); message('list-message');
      $('session-id').textContent = '';
      $('session-status').textContent = '';
      $('session-meta').textContent = '';
      $('session-meta').title = '';
      $('capture-meta').textContent = '';
    },
    setAgents,
  };
}
