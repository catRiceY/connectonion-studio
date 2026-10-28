// Presentation of recorded preparation facts, never provider usage or outcomes.
const statuses = new Set(['prepared', 'budget_exceeded', 'invalid_context', 'unknown']);
const fields = [
  ['full_estimated_tokens', 'Input before (estimated tokens)'],
  ['estimated_tokens', 'Prepared input (estimated tokens)'],
  ['input_budget', 'Input budget (estimated tokens)'],
  ['source_messages', 'Original messages'],
  ['retained_messages', 'Original messages retained'],
  ['input_messages', 'Prepared input messages'],
  ['task_fields', 'Task fields detected'],
  ['tool_references', 'Tool references retained'],
  ['omitted_tool_references', 'Tool references omitted'],
];
const missing = (value) => value === undefined || value === null;
const count = (value) => Number.isSafeInteger(value) && value >= 0;
const identifier = (value) => typeof value === 'string' && value.length > 0
  && value.length <= 512 && !/[\s\x00-\x1f\x7f]/.test(value);
const tag = (value) => typeof value === 'string' && value.length <= 128 && /^[a-zA-Z0-9_.-]+$/.test(value);
const text = (value, valid = count) => missing(value) ? 'Not recorded'
  : valid(value) ? typeof value === 'number' ? value.toLocaleString() : value : 'Cannot confirm';
function contradictory(payload) {
  const pair = (left, right, invalid) => count(payload[left]) && count(payload[right]) && invalid(payload[left], payload[right]);
  return pair('retained_messages', 'source_messages', (kept, original) => kept > original)
    || pair('input_messages', 'retained_messages', (input, kept) => input < kept)
    || (payload.status === 'prepared' && pair('estimated_tokens', 'input_budget', (input, budget) => input > budget))
    || (payload.compacted === false && (
      pair('full_estimated_tokens', 'estimated_tokens', (before, after) => before !== after)
      || pair('source_messages', 'retained_messages', (before, after) => before !== after)
      || pair('retained_messages', 'input_messages', (before, after) => before !== after)
      || payload.summary_kind === 'deterministic_extract_not_semantic'))
    || (payload.compacted === true && (payload.summary_kind === 'none'
      || pair('full_estimated_tokens', 'estimated_tokens', (before, after) => after >= before)));
}

export function describeContextReport(event) {
  const original = event?.payload;
  const payload = original && typeof original === 'object' && !Array.isArray(original) ? original : {};
  const validEnvelope = payload.report_version === 1 && statuses.has(payload.status)
    && identifier(payload.llm_call_id) && event.call_id === payload.llm_call_id;
  const unknown = !validEnvelope || payload.status === 'unknown' || contradictory(payload);
  const status = unknown ? 'unknown' : payload.status;
  const compression = unknown ? 'Cannot confirm' : missing(payload.compacted) ? 'Not recorded'
    : payload.compacted === true ? 'Compressed' : payload.compacted === false ? 'Not compressed' : 'Cannot confirm';
  const summaryKinds = { none: 'None', deterministic_extract_not_semantic: 'Deterministic extraction — not a semantic summary' };
  const rows = [['Compression', compression], ...fields.map(([key, label]) => [label,
    unknown ? 'Cannot confirm' : text(payload[key], key === 'input_budget'
      ? (value) => count(value) && value >= 128 && value <= 2_000_000 : count)]),
  ['Summary method', unknown ? 'Cannot confirm' : missing(payload.summary_kind) ? 'Not recorded'
    : typeof payload.summary_kind === 'string' && Object.hasOwn(summaryKinds, payload.summary_kind)
      ? summaryKinds[payload.summary_kind] : 'Cannot confirm'],
  ['Policy', unknown ? 'Cannot confirm' : text(payload.policy_version, tag)],
  ['Estimator', unknown ? 'Cannot confirm' : payload.estimator === 'utf8-json-bytes-div4-v1'
    ? 'UTF-8 JSON bytes ÷ 4 (approximate)' : text(payload.estimator, tag)]];
  const explanation = {
    prepared: 'Input preparation completed. This does not confirm that a model request was sent or succeeded.',
    budget_exceeded: 'Not sent to model: the estimated input budget was exceeded.',
    invalid_context: 'Not sent to model: context validation failed.',
    unknown: 'Cannot confirm context preparation. Metrics from this record are not used.',
  }[status];
  const note = status === 'unknown' ? 'Cannot confirm' : status === 'prepared'
    ? `Prepared · ${compression}` : status === 'budget_exceeded' ? 'Not sent · Budget exceeded' : 'Not sent · Invalid context';
  return { label: 'Context preparation', note, failed: ['budget_exceeded', 'invalid_context'].includes(status),
    explanation, showMetrics: !unknown, rows, disclaimer: 'Token values are local estimates, not provider billing or actual token usage. Prepared message counts do not prove the input was sent.' };
}
