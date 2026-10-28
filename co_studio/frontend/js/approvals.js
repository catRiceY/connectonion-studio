import { epochMillis, message, node, populateAgents, timestamp } from './workspace-ui.js';

export function createApprovalsView({ root, api, pollMs = 4000, schedule = setTimeout, unschedule = clearTimeout }) {
  const $ = (id) => root.querySelector(`#approval-${id}`); const doc = root.ownerDocument;
  let active = false; let agents = []; let slug = null; let selected = null; let record = null;
  let generation = 0; let detailGeneration = 0; let read = null; let detailRead = null;
  let busy = false; let loading = false; let timer = null;
  const clearTimer = () => { if (timer !== null) unschedule(timer); timer = null; };
  function later() {
    clearTimer();
    if (active && slug) timer = schedule(() => { timer = null; void refresh(true); }, pollMs);
  }
  function controls() {
    const actionable = Boolean(record && record.agent_slug === slug && record.id === selected
      && record.status === 'pending' && typeof record.binding === 'string' && /^[a-f0-9]{64}$/.test(record.binding)
      && epochMillis(record.expires_at) > Date.now());
    $('approve').disabled = busy || !actionable; $('reject').disabled = busy || !actionable;
    $('retry').disabled = loading || busy;
  }
  function renderDetail(item) {
    record = item; $('detail').hidden = !item; $('detail-empty').hidden = Boolean(item);
    if (!item) { controls(); return; }
    $('tool').textContent = item.tool_name; $('status').textContent = item.status;
    $('context').textContent = `Session ${item.session_id || 'unknown'} · Turn ${item.turn_no ?? 'unknown'} · ${timestamp(item.created_at)}`;
    $('arguments').textContent = JSON.stringify(item.arguments ?? {}, null, 2);
    $('expiry').textContent = `Expires ${timestamp(item.expires_at)}`;
    $('identity').textContent = `Call ${item.call_id || 'unknown'} · Request ${item.id}`;
    message($('reason'), item.reason || ''); controls();
  }
  function renderList(items, total) {
    $('list').replaceChildren(); $('count').textContent = items.length < total ? `${items.length}/${total}` : String(total);
    $('empty').hidden = Boolean(items.length);
    $('empty').textContent = slug ? 'No requests in this view.' : 'No agents yet.';
    for (const item of items) {
      const button = node(doc, 'button', 'logs-item'); button.type = 'button';
      button.dataset.requestId = item.id;
      button.classList.toggle('is-selected', item.id === selected);
      button.setAttribute('aria-pressed', String(item.id === selected));
      const body = node(doc, 'span', 'logs-item-body');
      body.append(node(doc, 'span', 'logs-item-name', item.tool_name),
        node(doc, 'span', 'logs-item-meta', `${item.status} · Turn ${item.turn_no ?? '—'}`));
      button.append(body); button.addEventListener('click', () => select(item.id)); $('list').append(button);
    }
  }
  async function select(id) {
    selected = id; const owner = slug; const view = generation; const action = ++detailGeneration;
    message($('decision-message'));
    detailRead?.abort(); detailRead = new AbortController(); renderDetail(null);
    $('detail-empty').textContent = 'Loading request…';
    for (const button of $('list').children) {
      const selected = button.dataset.requestId === id;
      button.classList.toggle('is-selected', selected); button.setAttribute('aria-pressed', String(selected));
    }
    try {
      const item = await api.getApproval(owner, id, { signal: detailRead.signal });
      if (!active || view !== generation || action !== detailGeneration || owner !== slug || selected !== id) return;
      if (item.agent_slug !== owner || item.id !== id) throw new Error('Request identity mismatch');
      renderDetail(item);
    } catch {
      if (view === generation && action === detailGeneration) $('detail-empty').textContent = 'Could not load this request. Select it again to retry.';
    }
  }
  async function refresh(quiet = false) {
    if (!active || !slug || loading || busy) { later(); return; }
    const owner = slug; const view = generation; const filter = $('filter').value;
    loading = true; read?.abort(); read = new AbortController(); controls();
    if (!quiet) { message($('message'), 'Loading requests…'); $('empty').hidden = true; }
    try {
      const result = await api.listApprovals(owner, { status: filter, limit: 50, signal: read.signal });
      if (!active || view !== generation || owner !== slug) return;
      if (result.items.some((item) => item.agent_slug !== owner)) throw new Error('Request identity mismatch');
      renderList(result.items, result.total); message($('message')); $('retry').hidden = true;
      const update = result.items.find((item) => item.id === selected);
      if (update && record && !busy) renderDetail(update);
      if (!update && record && selected) {
        const id = selected; const action = detailGeneration;
        const current = await api.getApproval(owner, id, { signal: read.signal });
        if (active && view === generation && action === detailGeneration && owner === slug && selected === id && !busy) {
          if (current.agent_slug !== owner || current.id !== id) throw new Error('Request identity mismatch');
          renderDetail(current);
        }
      }
      if (!selected && result.items.length) void select(result.items[0].id);
    } catch {
      if (view === generation) {
        message($('message'), 'Could not load approvals.', true); $('retry').hidden = false;
        if (!quiet) $('empty').hidden = true;
      }
    } finally { if (view === generation) { loading = false; controls(); later(); } }
  }
  function chooseAgent(owner) {
    generation++; detailGeneration++; clearTimer(); read?.abort(); detailRead?.abort();
    slug = owner || null; selected = null; record = null; busy = false; loading = false;
    $('agent').value = slug || ''; message($('message')); $('retry').hidden = true;
    message($('decision-message'));
    renderList([], 0); renderDetail(null); $('detail-empty').textContent = 'Select a request to review.';
    if (slug && active) void refresh();
  }
  async function decide(decision) {
    controls(); if (busy || $(decision === 'approve' ? 'approve' : 'reject').disabled) return;
    // A pre-decision poll must never restore a stale pending state afterward.
    generation++; detailGeneration++; read?.abort(); detailRead?.abort(); loading = false;
    const owner = slug; const id = selected; const binding = record.binding; const view = generation;
    busy = true; clearTimer(); controls(); message($('decision-message'), 'Saving decision…');
    try {
      const item = await api.decideApproval(owner, id, { decision, binding });
      if (!active || view !== generation || owner !== slug || selected !== id) return;
      if (item.agent_slug !== owner || item.id !== id) throw new Error('Request identity mismatch');
      renderDetail(item); message($('decision-message'), decision === 'approve'
        ? 'Approved for this call only. Execution outcome appears in History.' : 'This call was rejected.');
    } catch (error) {
      if (view === generation) message($('decision-message'), error.status === 409
        ? 'This request changed or expired. Select it again to review.' : 'Could not save the decision. Review the request before retrying.', true);
    } finally { if (view === generation) { busy = false; controls(); void refresh(true); } }
  }
  $('agent').addEventListener('change', () => chooseAgent($('agent').value));
  $('filter').addEventListener('change', () => chooseAgent(slug));
  $('retry').addEventListener('click', () => refresh());
  $('approve').addEventListener('click', () => decide('approve'));
  $('reject').addEventListener('click', () => decide('reject'));
  return {
    open(list) { active = true; agents = list; chooseAgent(populateAgents($('agent'), agents, slug)); },
    setAgents(list) {
      agents = list; if (!active) return;
      const next = populateAgents($('agent'), agents, slug); if (next !== slug) chooseAgent(next);
    },
    close() { active = false; generation++; detailGeneration++; clearTimer(); read?.abort(); detailRead?.abort(); busy = false; loading = false; },
  };
}
