import { message, node } from './workspace-ui.js';

export function createToolConfig({ root, api }) {
  const $ = (id) => root.querySelector(`#tools-${id}`);
  let slug = null; let generation = 0; let data = null; let busy = false; let read = null;
  const dirty = () => data && ($('mcp').checked !== data.mcp_enabled
    || $('approvals').checked !== data.approvals_enabled || Number($('timeout').value) !== data.approval_timeout_seconds
    || ($('context-budget').value.trim() === '' ? null : Number($('context-budget').value)) !== (data.context_budget ?? null));
  function controls() {
    for (const id of ['mcp', 'approvals', 'timeout', 'context-budget']) $(id).disabled = busy || !data;
    $('save').disabled = busy || !dirty(); $('reload').disabled = busy;
  }
  function install(result) {
    data = result; $('form').hidden = false;
    $('mcp').checked = result.mcp_enabled; $('approvals').checked = result.approvals_enabled;
    $('timeout').value = String(result.approval_timeout_seconds);
    $('context-budget').value = result.context_budget == null ? '' : String(result.context_budget);
    $('catalog').replaceChildren();
    for (const tool of result.available_tools || []) {
      const item = node(root.ownerDocument, 'li');
      item.append(node(root.ownerDocument, 'strong', '', tool.name),
        node(root.ownerDocument, 'span', '', ` — ${tool.description}`));
      $('catalog').append(item);
    }
  }
  async function reload() {
    if (!slug || busy) return;
    const owner = slug; const view = generation;
    read?.abort(); read = new AbortController(); busy = true; data = null;
    $('form').hidden = true; controls(); message($('message'), 'Loading tool policy…');
    try {
      const result = await api.getToolConfig(owner, { signal: read.signal });
      if (view !== generation || slug !== owner) return;
      install(result); message($('message'));
    } catch (error) {
      if (view === generation) message($('message'), error.status === 422
        ? 'Studio tool policy is available for generated custom Agents.' : 'Could not load tool policy. Retry below.', true);
    } finally { if (view === generation) { busy = false; controls(); } }
  }
  async function save() {
    if (!slug || busy || !dirty()) return;
    const timeout = Number($('timeout').value);
    if (!Number.isInteger(timeout) || timeout < 30 || timeout > 300) {
      message($('message'), 'Approval timeout must be 30–300 seconds.', true); return;
    }
    const budget = $('context-budget').value.trim() === '' ? null : Number($('context-budget').value);
    if (budget !== null && (!Number.isInteger(budget) || budget < 128 || budget > 2000000)) {
      message($('message'), 'Context budget must be 128–2,000,000 estimated tokens, or empty to disable.', true); return;
    }
    const owner = slug; const view = generation;
    const body = { expected_revision: data.revision, mcp_enabled: $('mcp').checked,
      approvals_enabled: $('approvals').checked, approval_timeout_seconds: timeout, context_budget: budget };
    busy = true; controls(); message($('message'), 'Saving tool policy…');
    try {
      const result = await api.saveToolConfig(owner, body);
      if (view !== generation || owner !== slug) return;
      install(result); message($('message'), 'Saved for next start. Restart the Agent explicitly to apply.');
    } catch (error) {
      if (view === generation) message($('message'), error.status === 409
        ? 'Policy changed elsewhere. Reload before saving again.' : `Could not save. ${error.message || 'Please retry.'}`, true);
    } finally { if (view === generation) { busy = false; controls(); } }
  }
  for (const id of ['mcp', 'approvals', 'timeout', 'context-budget']) $(id).addEventListener('input', controls);
  $('save').addEventListener('click', save); $('reload').addEventListener('click', reload);
  return {
    open(owner) { this.close(); slug = owner; root.open = false; return reload(); },
    close() { generation++; read?.abort(); slug = null; data = null; busy = false; $('form').hidden = true; message($('message')); controls(); },
  };
}
