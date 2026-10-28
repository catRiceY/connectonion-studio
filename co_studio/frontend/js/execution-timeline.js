// Presentation of observed facts only. A request without a result is not proof
// that an operation is still running. Keep separate events, even with shared IDs.
import { describeContextReport } from './context-report.js';
const hex = (value, length) => typeof value === 'string' && value.length === length && /^[a-f0-9]+$/.test(value);
const versionId = (value) => value === 'baseline' || hex(value, 32);
const sha256 = (value) => hex(value, 64);

function validBoundEvent(payload) {
  return Object.keys(payload).length === 9 && payload.provenance_version === 1
    && versionId(payload.version_id) && sha256(payload.base_sha256) && sha256(payload.system_sha256)
    && ['new_session', 'restored_session'].includes(payload.reason)
    && typeof payload.llm_call_id === 'string' && payload.llm_call_id.length > 0
    && payload.llm_call_id.length <= 512 && !/[\s\x00-\x1f]/.test(payload.llm_call_id)
    && (payload.launch_version_id === null || versionId(payload.launch_version_id))
    && (payload.model === null || (typeof payload.model === 'string' && payload.model.length > 0 && payload.model.length <= 512));
}

export function describePromptProvenance(provenance) {
  if (provenance == null) return 'Not recorded';
  if (provenance.status === 'bound' && versionId(provenance.version_id) && sha256(provenance.base_sha256)) {
    return provenance.version_id === 'baseline' ? 'Original' : provenance.version_id.slice(0, 8);
  }
  return provenance.status === 'unknown' && provenance.reason === 'not_recorded' ? 'Not recorded' : 'Cannot confirm';
}

export function describeExecutionEvent(event) {
  const payload = event.payload || {};
  if (event.kind === 'context_report') {
    const { label, note, failed } = describeContextReport(event);
    return { label, note, failed };
  }
  if (event.kind === 'prompt_provenance') {
    return {
      label: 'Base prompt', failed: false,
      note: payload.status === 'bound' && !validBoundEvent(payload)
        ? 'Cannot confirm' : describePromptProvenance(payload),
    };
  }
  if (event.kind === 'studio_approval') {
    // This is an observed trace record, not the authoritative approval store.
    return {
      label: `Approval record${typeof payload.name === 'string' ? ` · ${payload.name}` : ''}`,
      failed: ['failed', 'cancelled', 'expired', 'rejected'].includes(payload.status),
      note: [payload.action, payload.status].filter((value) => typeof value === 'string' && value).join(' · '),
    };
  }
  if (event.kind === 'studio_mcp_attempt') {
    // An observation, not an authorization or a proof that a scheduled retry
    // ran. Keep each attempt distinct from the final tool result.
    const statuses = {
      success: 'Recorded success', transport_error: 'Recorded transport error',
      timeout: 'Recorded timeout', rejected: 'Recorded rejection',
      tool_error: 'Recorded tool error', cancelled: 'Recorded cancellation', error: 'Recorded error',
    };
    const status = typeof payload.status === 'string' && Object.hasOwn(statuses, payload.status)
      ? statuses[payload.status] : 'Outcome not recorded';
    const attempt = payload.max_attempts === 2 && [1, 2].includes(payload.attempt)
      ? ` ${payload.attempt}/${payload.max_attempts}` : '';
    const retry = payload.retry_scheduled === true
      ? 'Retry scheduled, not completed' : null;
    return {
      label: `MCP attempt record${attempt}${typeof payload.name === 'string' && payload.name ? ` · ${payload.name}` : ''}`,
      failed: ['transport_error', 'timeout', 'rejected', 'tool_error', 'cancelled', 'error'].includes(payload.status),
      note: [status, retry, Number.isFinite(event.duration_ms) && event.duration_ms >= 0
        ? `${event.duration_ms.toLocaleString()} ms` : null].filter(Boolean).join(' · '),
    };
  }
  const names = {
    user_input: 'Input', llm_call: 'Model request', llm_result: 'Model response',
    tool_call: 'Tool request', tool_result: 'Tool result', turn_result: 'Round ended',
    plan: 'Plan', files_received: 'Files received',
  };
  const failed = Boolean(payload.error || payload.error_type || payload.declined
    || ['error', 'failed', 'timeout', 'cancelled', 'declined'].includes(payload.status));
  const name = event.kind.startsWith('tool_') && typeof payload.name === 'string' ? payload.name : null;
  return {
    label: `${names[event.kind] || event.kind}${name ? ` · ${name}` : ''}`,
    failed,
    note: [failed ? 'Error / interrupted' : null,
      typeof payload.status === 'string' && !event.kind.endsWith('_call') ? payload.status : null,
      event.duration_ms == null ? null : `${event.duration_ms.toLocaleString()} ms`].filter(Boolean).join(' · '),
  };
}
