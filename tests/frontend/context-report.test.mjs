import assert from 'node:assert/strict';
import { test } from 'node:test';
import { describeContextReport } from '../../co_studio/frontend/js/context-report.js';
import { describeExecutionEvent } from '../../co_studio/frontend/js/execution-timeline.js';

const report = (overrides = {}) => ({ kind: 'context_report', call_id: 'llm-1', payload: {
  report_version: 1, status: 'prepared', llm_call_id: 'llm-1', policy_version: 'studio-extractive-v1',
  estimator: 'utf8-json-bytes-div4-v1', input_budget: 4096, full_estimated_tokens: 6000,
  estimated_tokens: 3500, source_messages: 30, retained_messages: 8, input_messages: 9,
  task_fields: 3, tool_references: 2, omitted_tool_references: 1,
  compacted: true, summary_kind: 'deterministic_extract_not_semantic', ...overrides,
} });
const values = (value) => Object.fromEntries(describeContextReport(value).rows);

test('authenticated v1, v2 and v3 reports share the existing presentation contract', () => {
  for (const policy_version of ['studio-extractive-v1', 'studio-extractive-v2', 'studio-extractive-v3']) {
    const item = describeContextReport(report({ policy_version }));
    assert.equal(item.showMetrics, true);
    assert.equal(item.note, 'Prepared · Compressed');
    assert.equal(Object.fromEntries(item.rows)['Policy'], policy_version);
    assert.equal(Object.fromEntries(item.rows)['Task fields detected'], '3');
  }
});

test('context preparation displays estimates and counts without implying model success or billing', () => {
  const item = describeContextReport(report());
  assert.equal(item.note, 'Prepared · Compressed');
  assert.equal(item.failed, false);
  assert.match(item.explanation, /does not confirm.*sent or succeeded/);
  assert.match(item.disclaimer, /not provider billing or actual token usage/);
  const rows = Object.fromEntries(item.rows);
  assert.equal(rows['Input before (estimated tokens)'], (6000).toLocaleString());
  assert.equal(rows['Prepared input (estimated tokens)'], (3500).toLocaleString());
  assert.equal(rows['Original messages retained'], '8');
  assert.equal(rows['Prepared input messages'], '9');
  assert.equal(rows['Task fields detected'], '3');
  assert.equal(rows['Tool references retained'], '2');
  assert.equal(rows['Tool references omitted'], '1');
  assert.match(rows['Summary method'], /not a semantic summary/);
  assert.deepEqual(describeExecutionEvent(report()), { label: item.label, note: item.note, failed: false });
});

test('uncompressed preparation preserves real zero counts, not absent values', () => {
  const item = report({ compacted: false, summary_kind: 'none', full_estimated_tokens: 80,
    estimated_tokens: 80, source_messages: 2, retained_messages: 2, input_messages: 2,
    task_fields: 0, tool_references: 0, omitted_tool_references: 0 });
  assert.equal(describeContextReport(item).note, 'Prepared · Not compressed');
  assert.equal(values(item)['Task fields detected'], '0');
  assert.equal(values(report({ task_fields: null }))['Task fields detected'], 'Not recorded');
  assert.equal(values(report({ tool_references: undefined }))['Tool references retained'], 'Not recorded');
});

test('blocking statuses explicitly report no model send, without fabricated estimates', () => {
  for (const status of ['budget_exceeded', 'invalid_context']) {
    const item = describeContextReport(report({ status, compacted: null, summary_kind: null,
      full_estimated_tokens: null, estimated_tokens: null, input_messages: null }));
    assert.equal(item.failed, true);
    assert.match(item.note, /Not sent/);
    assert.match(item.explanation, /Not sent to model/);
    assert.equal(Object.fromEntries(item.rows)['Prepared input (estimated tokens)'], 'Not recorded');
  }
});

test('unknown, unsupported and mismatched reports ignore all numeric claims', () => {
  const invalid = [report({ status: 'unknown' }), report({ status: 'success' }), report({ report_version: 2 }),
    report({ report_version: '1' }), report({ report_version: null }), report({ llm_call_id: 'wrong-call' }),
    report({ llm_call_id: 'line\nbreak' }), { ...report(), call_id: null },
    { kind: 'context_report', payload: null }, { kind: 'context_report', payload: [] }, {}];
  for (const event of invalid) {
    const item = describeContextReport(event);
    assert.equal(item.note, 'Cannot confirm');
    assert.equal(item.failed, false);
    assert.ok(item.rows.every(([, value]) => value === 'Cannot confirm'));
  }
});

test('malformed counts never become NaN, negative, fractional or coerced metrics', () => {
  for (const invalid of [NaN, Infinity, -1, 0.5, '123', true, [], {}, Number.MAX_SAFE_INTEGER + 1]) {
    const rows = values(report({ task_fields: invalid }));
    assert.equal(rows['Task fields detected'], 'Cannot confirm');
  }
  for (const invalid of [0, 127, 2_000_001, '4096']) {
    const item = describeContextReport(report({ input_budget: invalid, estimated_tokens: null }));
    assert.equal(Object.fromEntries(item.rows)['Input budget (estimated tokens)'], 'Cannot confirm');
  }
});

test('contradictory counts and compression claims are not presented as valid metrics', () => {
  for (const invalid of [{ retained_messages: 31 }, { input_messages: 7 }, { estimated_tokens: 4097 },
    { compacted: false }, { compacted: true, summary_kind: 'none' },
    { full_estimated_tokens: 3500, estimated_tokens: 3500 }]) {
    assert.equal(describeContextReport(report(invalid)).note, 'Cannot confirm');
  }
});

test('malformed optional metadata stays inert and does not throw during coercion', () => {
  const item = report({ compacted: 'false', summary_kind: { toString: null, valueOf: null },
    policy_version: '<img src=x>', estimator: '<script>oops</script>' });
  const rows = values(item);
  for (const key of ['Compression', 'Summary method', 'Policy', 'Estimator']) assert.equal(rows[key], 'Cannot confirm');
});
