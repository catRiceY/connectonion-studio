// Presentation-only observations. No networking, model or user data.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { describeExecutionEvent } from '../../co_studio/frontend/js/execution-timeline.js';

const observation = (payload = {}, duration_ms = null) => ({
  kind: 'studio_mcp_attempt', duration_ms,
  payload: { name: 'studio_read_doc', attempt: 1, max_attempts: 2, ...payload },
});

test('an unsuccessful attempt may schedule a retry but never confirms a second outcome', () => {
  const item = describeExecutionEvent(observation({
    status: 'transport_error', retry_scheduled: true, error_type: 'ConnectionError', elapsed_ms: 500,
  }, 12.5));
  assert.deepEqual(item, {
    label: 'MCP attempt record 1/2 · studio_read_doc', failed: true,
    note: 'Recorded transport error · Retry scheduled, not completed · 12.5 ms',
  });
  assert.doesNotMatch(item.label + item.note, /authorized|retry succeeded|retry completed|running/i);
  assert.doesNotMatch(item.note, /500/); // Total elapsed is not attempt duration.
});

test('a recorded second success is separate from the failed first attempt', () => {
  const first = describeExecutionEvent(observation({ status: 'transport_error', retry_scheduled: true }));
  const second = describeExecutionEvent(observation({ attempt: 2, status: 'success', retry_scheduled: false }, 0));
  assert.equal(first.failed, true);
  assert.deepEqual(second, {
    label: 'MCP attempt record 2/2 · studio_read_doc', failed: false,
    note: 'Recorded success · 0 ms',
  });
  assert.doesNotMatch(second.note, /task|authorized|approved|workflow/i);
});

for (const [status, title] of [
  ['transport_error', 'transport error'], ['timeout', 'timeout'], ['rejected', 'rejection'],
  ['tool_error', 'tool error'], ['cancelled', 'cancellation'], ['error', 'error'],
]) {
  test(`${status} is shown as a recorded unsuccessful attempt`, () => {
    assert.deepEqual(describeExecutionEvent(observation({ status, retry_scheduled: false })), {
      label: 'MCP attempt record 1/2 · studio_read_doc', failed: true, note: `Recorded ${title}`,
    });
  });
}

test('missing or malformed attempt details do not invent retry, success, counts or timings', () => {
  for (const overrides of [{}, { status: 'toString' }, { status: 'future' }, { status: {} }, { status: ['success'] }]) {
    const item = describeExecutionEvent({ kind: 'studio_mcp_attempt', payload: overrides });
    assert.deepEqual(item, { label: 'MCP attempt record', note: 'Outcome not recorded', failed: false });
  }
  for (const attempt of [null, 0, 3, '1', true]) {
    const item = describeExecutionEvent(observation({ attempt, retry_scheduled: 'true' }, -1));
    assert.equal(item.label, 'MCP attempt record · studio_read_doc');
    assert.equal(item.note, 'Outcome not recorded');
  }
});

test('tool names remain text for the existing safe renderer', () => {
  const item = describeExecutionEvent(observation({ name: '<img src=x onerror=evil()>' }));
  assert.equal(item.label, 'MCP attempt record 1/2 · <img src=x onerror=evil()>');
});
