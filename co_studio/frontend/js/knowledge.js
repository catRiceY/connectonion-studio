import { message, node } from './workspace-ui.js';
import { isReadOnlyVerificationStep } from './verification-policy.js';

export function createKnowledgeView({ root, api }) {
  const $ = (id) => root.querySelector(`#knowledge-${id}`); const doc = root.ownerDocument;
  let active = false; let generation = 0; let queryGeneration = 0; let request = null; let searchRequest = null;
  let ready = false; let busy = false; let loading = false; let documents = []; let selected = null; let nextOffset = null;
  let collection = 'public'; let scopes = []; let requestedAgent = null;
  let deleteConfirm = false;
  let generating = false;
  function controls() {
    $('add').hidden = collection !== 'public'; $('add').disabled = !ready || busy;
    $('import-save').disabled = !ready || busy || collection !== 'public';
    $('delete').hidden = collection !== 'public'; $('delete').disabled = !ready || busy || !selected || collection !== 'public';
    $('search').disabled = !ready || !collection || busy || !$('query').value.trim();
    $('diagnose').disabled = $('search').disabled;
    $('generate').disabled = $('search').disabled;
    $('generation-start').disabled = $('search').disabled || !$('generation-consent').checked
      || !/^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$/.test($('generation-model').value.trim())
      || !['2048', '4096'].includes($('generation-budget').value);
    $('generation-model').disabled = busy; $('generation-budget').disabled = busy; $('generation-consent').disabled = busy;
    $('generation-dismiss').disabled = busy;
    $('generation-cancel').hidden = !generating;
    $('more').disabled = loading || busy; $('retry').disabled = loading;
    $('status-retry').disabled = loading;
    $('collection').disabled = !ready || busy;
    for (const id of ['query', 'mode', 'title', 'source', 'version', 'license', 'text']) $(id).disabled = busy;
    $('import-cancel').disabled = busy;
    $('delete').textContent = deleteConfirm ? 'Confirm removal' : 'Remove from index';
  }
  function showSelection(item) {
    selected = item; deleteConfirm = false; $('selection').hidden = !item;
    $('selected-title').textContent = item?.title || '';
    $('selected-source').textContent = item ? `${item.source_uri} · Version ${item.version}` : '';
    for (const button of $('list').children) {
      const selected = button.dataset.docId === item?.doc_id;
      button.classList.toggle('is-selected', selected); button.setAttribute('aria-pressed', String(selected));
    }
    controls();
  }
  function renderDocuments(total) {
    $('list').replaceChildren(); $('count').textContent = String(total);
    $('empty').hidden = Boolean(documents.length); $('more').hidden = nextOffset === null;
    for (const item of documents) {
      const button = node(doc, 'button', 'logs-item'); button.type = 'button'; button.dataset.docId = item.doc_id;
      const body = node(doc, 'span', 'logs-item-body');
      body.append(node(doc, 'span', 'logs-item-name', item.title),
        node(doc, 'span', 'logs-item-meta', `v${item.version} · ${item.chunk_count} chunks`));
      button.append(body); button.addEventListener('click', () => showSelection(item)); $('list').append(button);
    }
    showSelection(documents.find((item) => item.doc_id === selected?.doc_id) || null);
  }
  async function list(more = false) {
    if (!active || !ready || !collection || loading) return;
    const view = generation; loading = true; controls();
    try {
      const result = await api.listKnowledge({ collection, offset: more ? nextOffset : 0, limit: 20 });
      if (!active || view !== generation) return;
      documents = more ? [...documents, ...result.items] : result.items;
      nextOffset = result.next_offset ?? null; renderDocuments(result.total); message($('message')); $('retry').hidden = true;
    } catch {
      if (view === generation) { message($('message'), 'Could not load documents.', true); $('retry').hidden = false; }
    } finally { if (view === generation) { loading = false; controls(); } }
  }
  async function load() {
    if (!active || loading) return;
    const view = generation;
    request?.abort(); request = new AbortController(); loading = true; ready = false; controls();
    $('body').hidden = true; $('unavailable').hidden = false; $('unavailable-text').textContent = 'Loading knowledge index…';
    $('status-retry').hidden = true;
    try {
      const status = await api.getKnowledgeStatus({ signal: request.signal });
      if (!active || view !== generation) return;
      ready = Boolean(status.configured && status.ready);
      $('unavailable').hidden = ready; $('body').hidden = !ready;
      if (!ready) {
        $('unavailable-text').textContent = status.configured ? 'Knowledge index is unavailable.' : 'Knowledge storage is not configured.';
        $('status-retry').hidden = false; return;
      }
      $('model').textContent = status.embedding_model ? `Local index · ${status.embedding_model}` : 'Local index';
      scopes = (status.agent_collections || []).filter((scope) => typeof scope.agent_slug === 'string'
        && /^agent-[a-f0-9]{24}$/.test(scope.collection));
      $('collection').replaceChildren();
      const options = [{ collection: 'public', agent_name: 'Public documents' }, ...scopes];
      for (const scope of options) {
        const option = node(doc, 'option', '', scope.agent_name || scope.agent_slug);
        option.value = scope.collection; $('collection').append(option);
      }
      if (requestedAgent) {
        collection = scopes.find((scope) => scope.agent_slug === requestedAgent)?.collection || null;
        if (!collection) {
          const unavailable = node(doc, 'option', '', 'Agent scope unavailable'); unavailable.value = ''; unavailable.disabled = true;
          $('collection').append(unavailable);
          message($('query-message'), 'This Agent has no available knowledge scope. Choose Public documents explicitly to search those sources.', true);
        }
      } else if (!options.some((scope) => scope.collection === collection)) collection = 'public';
      $('collection').value = collection || '';
      $('scope-note').textContent = collection === 'public' ? 'Explicitly imported documents.' : 'Only this Agent’s synced records. Read only.';
      documents = []; nextOffset = null; selected = null; renderDocuments(0);
      loading = false; await list();
    } catch {
      if (view === generation) { $('unavailable-text').textContent = 'Could not connect to the knowledge index.'; $('status-retry').hidden = false; }
    } finally { if (view === generation) { loading = false; controls(); } }
  }
  function evidenceCard(hit) {
    const card = node(doc, 'article', 'history-turn knowledge-evidence');
    card.append(node(doc, 'h3', '', hit.title),
      node(doc, 'p', 'history-note', `Version ${hit.version} · Indexed lines ${hit.line_start}–${hit.line_end}`),
      node(doc, 'p', 'history-note', hit.source_uri),
      node(doc, 'p', 'history-text', hit.content),
      node(doc, 'code', 'knowledge-citation', hit.chunk_id));
    return card;
  }
  function renderResult(result, diagnostic) {
    $('results').replaceChildren();
    if (diagnostic) {
      const heading = node(doc, 'h3', 'history-section-title', result.status === 'no_evidence' ? 'No supporting evidence' : 'Evidence-based diagnosis');
      $('results').append(heading, node(doc, 'p', 'history-note', 'Extractive evidence summary — no LLM-generated reasoning.'));
      const ids = new Set((result.evidence || []).map((hit) => hit.chunk_id));
      for (const finding of result.findings || []) {
        const grounded = Array.isArray(finding.citations) && finding.citations.length && finding.citations.every((id) => ids.has(id));
        if (!grounded) continue;
        const item = node(doc, 'article', 'knowledge-finding');
        item.append(node(doc, 'p', 'history-text', finding.text), node(doc, 'p', 'knowledge-citation', finding.citations.join(' · ')));
        $('results').append(item);
      }
      if (result.next_steps?.length) {
        const steps = node(doc, 'ul', 'knowledge-next-steps');
        for (const step of result.next_steps) steps.append(node(doc, 'li', '', step));
        $('results').append(steps);
      }
    } else if (!result.hits?.length) $('results').append(node(doc, 'p', 'workspace-result-empty', 'No matching evidence. Try another query.'));
    const hits = diagnostic ? result.evidence || [] : result.hits || [];
    for (const hit of hits) $('results').append(evidenceCard(hit));
  }
  function renderGenerated(result) {
    $('results').replaceChildren();
    const titles = { answered: '资料回答', hypotheses: '诊断假设 · 待核实', needs_clarification: '需要补充信息',
      insufficient_evidence: '证据不足', conflicting_evidence: '证据存在冲突', refused: '无法执行此请求' };
    $('results').append(node(doc, 'h3', 'history-section-title', titles[result.status] || '未获得有效诊断'));
    $('results').append(node(doc, 'p', 'history-note', result.model_called
      ? `模型：${result.model} · Token / 费用未返回，请以服务商账单为准。` : '未调用模型。'));
    $('results').append(node(doc, 'p', 'history-note', '引用检查仅核对来源版本与原文，不证明诊断正确；验证步骤由你人工判断，不会自动执行。'));
    if (result.protocol_version === 'diagnostic-v2') {
      $('results').append(node(doc, 'p', 'history-note', `回答协议：diagnostic-v2 · 本次输出上限：${result.max_output_tokens} Token`));
      renderGeneratedV2(result); return;
    }
    const evidence = result.evidence || []; const byId = new Map(evidence.map((hit) => [hit.chunk_id, hit]));
    for (const hypothesis of result.status === 'hypotheses' ? result.hypotheses || [] : []) {
      const grounded = hypothesis.evidence?.length && hypothesis.evidence.every((quote) =>
        typeof quote.quote === 'string' && quote.quote.trim() && byId.get(quote.chunk_id)?.content.includes(quote.quote));
      if (!grounded) { $('results').append(node(doc, 'p', 'history-note', '有诊断未通过引用检查，已隐藏。')); continue; }
      const card = node(doc, 'article', 'history-turn knowledge-hypothesis');
      card.append(node(doc, 'h3', '', hypothesis.possible_cause));
      for (const quote of hypothesis.evidence) {
        const source = byId.get(quote.chunk_id);
        card.append(node(doc, 'blockquote', 'history-text', quote.quote),
          node(doc, 'p', 'history-note', `${source.title || source.source_uri} · ${source.version} · ${quote.chunk_id.slice(0, 12)}`));
      }
      card.append(node(doc, 'p', 'history-text', `旧版模型建议（未核验只读，未执行）：${hypothesis.verification_step}`)); $('results').append(card);
    }
    $('results').append(node(doc, 'p', 'history-note', result.limitation || '请人工核对原始资料。'));
    for (const hit of evidence) $('results').append(evidenceCard(hit));
  }
  function renderGeneratedV2(result) {
    const evidence = Array.isArray(result.evidence) ? result.evidence : [];
    const units = new Map((result.evidence_units || []).map((unit) => [unit.evidence_id, unit]));
    const grounded = (item) => Array.isArray(item.evidence_ids) && item.evidence_ids.length > 0
      && Array.isArray(item.evidence) && item.evidence.length === item.evidence_ids.length
      && new Set(item.evidence_ids).size === item.evidence_ids.length
      && item.evidence.every((quote, index) => {
        const unit = units.get(item.evidence_ids[index]);
        return unit && quote.evidence_id === item.evidence_ids[index] && quote.chunk_id === unit.chunk_id
          && typeof quote.quote === 'string' && quote.quote.trim() && quote.quote === unit.quote
          && quote.source_uri === unit.source_uri && quote.version === unit.version
          && quote.content_hash === unit.content_hash && quote.chunk_hash === unit.chunk_hash
          && quote.line_start === unit.line_start && quote.line_end === unit.line_end;
      });
    function citedCard(item, label, text) {
      if (!grounded(item)) {
        $('results').append(node(doc, 'p', 'history-note', '有内容未通过引用检查，已隐藏。')); return;
      }
      const card = node(doc, 'article', 'history-turn knowledge-hypothesis');
      if (label) card.append(node(doc, 'h3', '', label));
      card.append(node(doc, 'p', 'history-text', text));
      const sources = node(doc, 'details', 'knowledge-claim-sources');
      sources.append(node(doc, 'summary', '', `查看依据（${item.evidence.length}）`));
      for (const quote of item.evidence) {
        sources.append(node(doc, 'blockquote', 'history-text', quote.quote), node(doc, 'p', 'history-note',
          `${quote.evidence_id} · ${quote.source_uri} · ${quote.version} · ${quote.line_start}–${quote.line_end}`));
      }
      card.append(sources); $('results').append(card);
    }
    if (result.message) $('results').append(node(doc, 'p', 'history-text', result.message));
    if (['answered', 'hypotheses', 'conflicting_evidence'].includes(result.status)) {
      for (const item of result.claims || []) citedCard(item, result.status === 'hypotheses' ? '可能原因（待核实）' : '', item.text);
    }
    for (const item of result.limitations || []) citedCard(item, '适用条件与限制', item.text);
    if (['answered', 'hypotheses', 'conflicting_evidence'].includes(result.status)) {
      for (const item of result.verification_steps || []) {
        if (!isReadOnlyVerificationStep(result.verification_policy, item)) {
          $('results').append(node(doc, 'p', 'history-note', result.verification_policy
            ? '核查建议未通过只读策略校验，已隐藏。'
            : '旧版核查建议未经过只读策略校验，已隐藏；请重新生成。'));
          continue;
        }
        citedCard(item, '只读核查（未执行）', item.action);
      }
    }
    if (result.status === 'needs_clarification' && result.questions?.length) {
      const questions = node(doc, 'ul', 'knowledge-next-steps');
      for (const question of result.questions) questions.append(node(doc, 'li', '', question));
      $('results').append(questions);
    }
    if (evidence.length) {
      const sources = node(doc, 'details', 'knowledge-claim-sources');
      sources.append(node(doc, 'summary', '', `全部检索来源（${evidence.length}）`));
      for (const hit of evidence) sources.append(evidenceCard(hit));
      $('results').append(sources);
    }
  }
  function resetConsent() { $('generation-consent').checked = false; $('generation-confirm').hidden = true; }
  async function generate() {
    if ($('generation-start').disabled || !active || busy) return;
    const view = generation; const action = ++queryGeneration;
    searchRequest?.abort(); searchRequest = new AbortController(); const signal = searchRequest.signal;
    const body = { query: $('query').value.trim(), collection, mode: $('mode').value,
      model: $('generation-model').value.trim(), max_output_tokens: Number($('generation-budget').value), allow_model_call: true };
    if (body.query.length > 2000) { message($('query-message'), '问题请限制在 2,000 字以内。', true); return; }
    generating = busy = true; resetConsent(); controls(); $('results').replaceChildren();
    message($('query-message'), '正在检索并生成诊断，最多等待 90 秒…');
    try {
      const result = await api.generateKnowledgeDiagnosis(body, { signal });
      if (!active || view !== generation || action !== queryGeneration) return;
      if (signal.aborted) throw new Error('Cancelled');
      renderGenerated(result); message($('query-message'));
    } catch (error) {
      if (active && view === generation && action === queryGeneration) message($('query-message'), signal.aborted
        ? '已请求取消；已发送给服务商的请求仍可能计费。'
        : `诊断未完成，未展示未经核验的回答。${error.message || '请稍后重试。'}`, !signal.aborted);
    } finally {
      if (view === generation && action === queryGeneration) { generating = busy = false; controls(); }
    }
  }
  async function search(diagnostic = false) {
    if (!ready || !collection || busy || !$('query').value.trim()) return;
    resetConsent(); const view = generation; const action = ++queryGeneration;
    searchRequest?.abort(); searchRequest = new AbortController();
    busy = true; controls(); message($('query-message'), diagnostic ? 'Checking evidence…' : 'Searching…');
    $('results').replaceChildren();
    const body = { query: $('query').value.trim(), mode: $('mode').value, k: 5, collection };
    if (body.query.length > 2000) { busy = false; controls(); message($('query-message'), 'Use a query of at most 2,000 characters.', true); return; }
    try {
      const result = await (diagnostic ? api.diagnoseKnowledge : api.searchKnowledge)(body, { signal: searchRequest.signal });
      if (!active || view !== generation || action !== queryGeneration) return;
      renderResult(result, diagnostic); message($('query-message'));
    } catch (error) {
      if (view === generation && action === queryGeneration) message($('query-message'), `Could not retrieve evidence. ${error.message || 'Please retry.'}`, true);
    } finally { if (view === generation && action === queryGeneration) { busy = false; controls(); } }
  }
  function showImport(show) {
    $('import').hidden = !show; $('explore').hidden = show; message($('import-message'));
    if (show) $('title').focus();
  }
  async function add() {
    if (!ready || busy || collection !== 'public') return;
    const body = { title: $('title').value.trim(), source_uri: $('source').value.trim(),
      text: $('text').value, version: $('version').value.trim(), license: $('license').value.trim(), collection: 'public', kind: 'document' };
    if (![body.title, body.source_uri, body.text.trim(), body.version, body.license].every(Boolean)) {
      message($('import-message'), 'Complete all fields, including source and license.', true); return;
    }
    const view = generation; busy = true; controls(); message($('import-message'), 'Indexing document…');
    try {
      const result = await api.addKnowledge(body);
      if (!active || view !== generation) return;
      for (const id of ['title', 'source', 'text', 'license']) $(id).value = '';
      $('version').value = '1'; showImport(false); message($('query-message'), result.duplicate
        ? 'Matching content is already indexed; no duplicate was added.' : 'Document indexed. You can now search its content.');
      await list();
    } catch (error) {
      if (view === generation) message($('import-message'), `Could not index document. ${error.message || 'Please retry.'}`, true);
    } finally { if (view === generation) { busy = false; controls(); } }
  }
  async function remove() {
    if (!ready || busy || !selected || collection !== 'public') return;
    if (!deleteConfirm) { deleteConfirm = true; controls(); return; }
    const view = generation; const id = selected.doc_id; busy = true; controls();
    try {
      await api.deleteKnowledge(id);
      if (!active || view !== generation) return;
      showSelection(null); $('results').replaceChildren();
      message($('query-message'), 'Removed from the index. The source file was not deleted.'); await list();
    } catch (error) {
      if (view === generation) message($('query-message'), `Could not remove document. ${error.message || 'Please retry.'}`, true);
    } finally { if (view === generation) { busy = false; deleteConfirm = false; controls(); } }
  }
  $('query').addEventListener('input', () => { $('generation-consent').checked = false; controls(); }); $('search').addEventListener('click', () => search());
  $('mode').addEventListener('change', () => { $('generation-consent').checked = false; controls(); });
  $('generate').addEventListener('click', () => { if (busy) return; $('generation-confirm').hidden = false; $('generation-consent').checked = false; controls(); $('generation-model').focus(); });
  $('generation-model').addEventListener('input', () => { $('generation-consent').checked = false; controls(); });
  $('generation-budget').addEventListener('change', () => { $('generation-consent').checked = false; controls(); });
  $('generation-consent').addEventListener('change', controls);
  $('generation-start').addEventListener('click', generate);
  $('generation-dismiss').addEventListener('click', () => { resetConsent(); controls(); });
  $('generation-cancel').addEventListener('click', () => { if (generating) { searchRequest?.abort(); message($('query-message'), '已请求取消；已发送给服务商的请求仍可能计费。'); } });
  $('diagnose').addEventListener('click', () => search(true)); $('add').addEventListener('click', () => showImport(true));
  $('import-cancel').addEventListener('click', () => { if (!busy) showImport(false); });
  $('import-save').addEventListener('click', add); $('delete').addEventListener('click', remove);
  $('retry').addEventListener('click', () => list()); $('status-retry').addEventListener('click', load);
  $('more').addEventListener('click', () => { if (nextOffset !== null) void list(true); });
  $('collection').addEventListener('change', () => {
    if (busy) return;
    const value = $('collection').value;
    if (value !== 'public' && !scopes.some((scope) => scope.collection === value)) return;
    resetConsent(); generation++; queryGeneration++; searchRequest?.abort(); requestedAgent = null; collection = value;
    loading = false; documents = []; nextOffset = null; selected = null; renderDocuments(0);
    $('results').replaceChildren(); message($('query-message')); showImport(false);
    $('scope-note').textContent = value === 'public' ? 'Explicitly imported documents.' : 'Only this Agent’s synced records. Read only.';
    controls(); void list();
  });
  return {
    open() {
      this.close(); active = true; requestedAgent = null; showImport(false);
      $('results').replaceChildren(); message($('query-message')); return load();
    },
    prefill({ query, turn, agentSlug }) {
      if (!active) return;
      requestedAgent = agentSlug || null;
      $('query').value = query; $('results').replaceChildren();
      message($('query-message'), `Draft from History turn ${turn}. Review before searching.`); controls();
    },
    close() { active = false; generation++; queryGeneration++; request?.abort(); searchRequest?.abort(); resetConsent(); generating = false; ready = false; busy = false; loading = false; },
  };
}
