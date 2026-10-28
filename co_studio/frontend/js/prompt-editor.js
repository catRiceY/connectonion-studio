// Studio-owned base prompts. Never restart an Agent or claim historical binding.
export function promptDiff(before, after) {
  const old = before.split('\n'); const next = after.split('\n');
  let prefix = 0; let suffix = 0;
  while (prefix < old.length && prefix < next.length && old[prefix] === next[prefix]) prefix++;
  if (prefix === old.length && prefix === next.length) return 'No changes.';
  while (suffix < old.length - prefix && suffix < next.length - prefix
    && old[old.length - 1 - suffix] === next[next.length - 1 - suffix]) suffix++;
  return [prefix ? `… ${prefix} unchanged lines` : '',
    ...old.slice(prefix, old.length - suffix).map((line) => `− ${line}`),
    ...next.slice(prefix, next.length - suffix).map((line) => `+ ${line}`),
    suffix ? `… ${suffix} unchanged lines` : ''].filter(Boolean).join('\n');
}

export function createPromptEditor({ root, api }) {
  const $ = (id) => root.querySelector(`#prompt-${id}`);
  const doc = root.ownerDocument;
  const drafts = new Map();
  let slug = null; let generation = 0; let comparisonGeneration = 0;
  let data = null; let base = null; let baselineText = ''; let comparison = null;
  let busy = false; let request = null; let compareRequest = null; let restoreConfirm = false; let comparisonError = false;
  const notice = (text = '', error = false) => {
    comparisonError = false;
    $('message').textContent = text; $('message').hidden = !text;
    $('message').classList.toggle('is-error', error);
  };
  const dirty = () => base !== null && $('text').value !== baselineText;
  function remember() {
    if (!slug || base === null) return;
    if (dirty()) {
      const previous = drafts.get(slug);
      if (!previous || previous.text !== $('text').value || previous.base !== base || previous.baselineText !== baselineText)
        drafts.set(slug, { text: $('text').value, base, baselineText });
    }
    else drafts.delete(slug);
  }
  function controls() {
    const editable = Boolean(data?.supported && base);
    $('save').disabled = busy || !editable || !dirty() || !$('text').value.trim();
    $('text').disabled = busy || !editable;
    $('versions').disabled = busy || !editable;
    $('restore').disabled = busy || !editable || !comparison || comparison.id === data.current.id;
    $('reload').disabled = busy;
    $('restore').textContent = restoreConfirm ? 'Confirm restore as new version' : 'Restore selected';
    $('base').textContent = base ? `Editing: ${base === 'baseline' ? 'Original prompt' : base.slice(0, 12)}` : '';
    $('diff').textContent = comparison ? promptDiff(comparison.text, $('text').value) : '';
  }
  async function compare() {
    restoreConfirm = false;
    const id = $('versions').value;
    const view = generation; const action = ++comparisonGeneration; const owner = slug;
    compareRequest?.abort(); comparison = null; controls();
    if (!id || !data?.supported) return;
    const abort = new AbortController(); compareRequest = abort;
    try {
      const version = id === data.current.id ? data.current : await api.getPromptVersion(owner, id, { signal: abort.signal });
      if (view !== generation || action !== comparisonGeneration || owner !== slug) return;
      comparison = version;
      if (comparisonError) notice();
      controls();
    } catch {
      if (view === generation && action === comparisonGeneration) {
        notice('Could not load the comparison version. Select it again to retry.', true); comparisonError = true;
      }
    }
  }
  function install(result, keepDraft) {
    comparisonGeneration++; compareRequest?.abort(); restoreConfirm = false;
    data = result;
    $('form').hidden = !result.supported;
    if (!result.supported) { base = null; notice(result.reason || 'Prompt editing is not supported for this Agent.'); controls(); return; }
    const draft = keepDraft ? drafts.get(slug) : null;
    base = draft?.base ?? result.current.id;
    baselineText = draft?.baselineText ?? result.current.text;
    $('text').value = draft?.text ?? result.current.text;
    const versions = new Map([[result.current.id, result.current], ...result.versions.map((version) => [version.id, version]),
      ['baseline', { id: 'baseline', created_at: null }]]);
    $('versions').replaceChildren();
    for (const version of versions.values()) {
      const option = doc.createElement('option'); option.value = version.id;
      const date = new Date(version.created_at);
      const shortDate = version.created_at && Number.isFinite(date.getTime())
        ? date.toLocaleString(undefined, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }) : '';
      option.textContent = version.id === 'baseline' ? 'Original prompt' : `${version.id.slice(0, 8)}${shortDate ? ` · ${shortDate}` : ''}`;
      option.title = version.id === 'baseline' ? 'Original prompt' : `${version.id} · ${version.created_at || 'Time not recorded'}`;
      $('versions').append(option);
    }
    $('versions').value = result.current.id;
    comparison = result.current;
    if (draft && draft.base !== result.current.id) notice('The saved version changed. Your draft is retained; reload to use the latest base.', true);
    else notice();
    controls();
  }
  async function reload(discard = false) {
    const owner = slug; const view = generation;
    if (!owner || busy) return;
    comparisonGeneration++; compareRequest?.abort(); restoreConfirm = false;
    if (!discard) remember();
    request?.abort(); const abort = new AbortController(); request = abort;
    busy = true; controls(); notice('Loading prompt…');
    try {
      const result = await api.getPrompts(owner, { signal: abort.signal });
      if (view !== generation || owner !== slug) return;
      if (discard) drafts.delete(owner);
      install(result, !discard);
    } catch {
      if (view === generation) notice('Could not load prompts. Use Reload to retry.', true);
    } finally {
      if (view === generation) { busy = false; controls(); }
    }
  }
  async function write(restore = false) {
    if (busy || !data?.supported || !base) return;
    if (restore && (!comparison || comparison.id === data.current.id)) return;
    if (restore && !restoreConfirm) { restoreConfirm = true; controls(); return; }
    if (!restore && (!dirty() || !$('text').value.trim())) return;
    const owner = slug; const view = generation;
    remember(); const submittedDraft = drafts.get(owner);
    comparisonGeneration++; compareRequest?.abort();
    busy = true; controls(); notice('Saving new version…');
    try {
      const result = restore
        ? await api.restorePrompt(owner, { base_version: base, version_id: comparison.id })
        : await api.savePrompt(owner, { base_version: base, text: $('text').value });
      // A write cannot be undone by closing its view. Never paint another Agent.
      if (drafts.get(owner) === submittedDraft) drafts.delete(owner);
      if (view !== generation || owner !== slug) return;
      install(result, false);
      notice('Saved for next launch. Start or restart explicitly, then create a new conversation.');
    } catch (error) {
      if (view === generation) notice(error.status === 409
        ? 'Another version was saved. Your draft is retained; reload the latest base before saving.'
        : `Could not save. ${error.message || 'Please retry.'}`, true);
    } finally {
      if (view === generation) { busy = false; restoreConfirm = false; controls(); }
    }
  }
  $('text').addEventListener('input', () => { restoreConfirm = false; remember(); controls(); });
  $('versions').addEventListener('change', () => { void compare(); });
  $('save').addEventListener('click', () => write());
  $('restore').addEventListener('click', () => write(true));
  $('reload').addEventListener('click', () => reload(true));
  return {
    open(owner) {
      this.close(); slug = owner; data = null; base = null; baselineText = ''; comparison = null;
      root.open = false; $('text').value = ''; $('versions').replaceChildren(); $('form').hidden = true;
      notice(); controls(); return reload();
    },
    close() {
      remember(); generation++; comparisonGeneration++;
      request?.abort(); compareRequest?.abort(); slug = null; busy = false; base = null;
      restoreConfirm = false;
    },
  };
}
