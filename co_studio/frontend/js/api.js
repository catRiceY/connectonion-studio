// ============================================================
// api.js — REST client for the co-studio manager (127.0.0.1:9900)
// Same-origin: the manager serves this frontend at /.
// ============================================================

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

async function request(method, path, body, signal) {
  let res;
  try {
    res = await fetch(path, {
      method,
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      ...(signal ? { signal } : {}),
    });
  } catch (err) {
    throw new ApiError('Cannot reach co-studio backend', 0);
  }
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const text = await res.text();
      if (text) {
        try {
          const data = JSON.parse(text);
          detail = data.detail || data.message || text;
        } catch {
          detail = text;
        }
      }
    } catch { /* keep default detail */ }
    throw new ApiError(String(detail).slice(0, 300), res.status);
  }
  if (res.status === 204) return null;
  return res.json();
}

const slugPath = (slug) => `/api/agents/${encodeURIComponent(slug)}`;

// -- agents ---------------------------------------------------
export const listAgents   = () => request('GET', '/api/agents').then((d) => d.agents || []);
export const createAgent  = (payload) => request('POST', '/api/agents', payload);
export const getAgent     = (slug) => request('GET', slugPath(slug));
export const startAgent   = (slug) => request('POST', `${slugPath(slug)}/start`);
export const stopAgent    = (slug) => request('POST', `${slugPath(slug)}/stop`);
export const restartAgent = (slug) => request('POST', `${slugPath(slug)}/restart`);
export const renameAgent  = (slug, name) => request('POST', `${slugPath(slug)}/rename`, { name });
export const deleteAgent  = (slug) => request('DELETE', slugPath(slug));
export const revealLogs   = (slug) => request('POST', `${slugPath(slug)}/reveal-logs`);

// -- immutable base-prompt revisions (saving never starts/restarts an Agent) --
export const getPrompts = (slug, { signal } = {}) => request('GET', `${slugPath(slug)}/prompts`, undefined, signal);
export const getPromptVersion = (slug, id, { signal } = {}) => request('GET', `${slugPath(slug)}/prompts/${encodeURIComponent(id)}`, undefined, signal);
export const savePrompt = (slug, body) => request('POST', `${slugPath(slug)}/prompts`, body);
export const restorePrompt = (slug, body) => request('POST', `${slugPath(slug)}/prompts/restore`, body);

// -- Studio tool policy and per-call approvals (never start an Agent) --
export const getToolConfig = (slug, { signal } = {}) => request('GET', `${slugPath(slug)}/tools/config`, undefined, signal);
export const saveToolConfig = (slug, body) => request('PUT', `${slugPath(slug)}/tools/config`, body);
export const listApprovals = (slug, { status = 'pending', limit = 50, signal } = {}) =>
  request('GET', `${slugPath(slug)}/approvals?${new URLSearchParams({ status, limit })}`, undefined, signal);
export const getApproval = (slug, id, { signal } = {}) => request('GET', `${slugPath(slug)}/approvals/${encodeURIComponent(id)}`, undefined, signal);
export const decideApproval = (slug, id, body) => request('POST', `${slugPath(slug)}/approvals/${encodeURIComponent(id)}/decision`, body);

// -- Explicit, local knowledge management; URLs are labels, never fetched here. --
export const getKnowledgeStatus = ({ signal } = {}) => request('GET', '/api/knowledge/status', undefined, signal);
export const listKnowledge = ({ collection = 'public', offset = 0, limit = 20, signal } = {}) =>
  request('GET', `/api/knowledge/documents?${new URLSearchParams({ collection, offset, limit })}`, undefined, signal);
export const addKnowledge = (body) => request('POST', '/api/knowledge/documents', body);
export const deleteKnowledge = (id) => request('DELETE', `/api/knowledge/documents/${encodeURIComponent(id)}`);
export const searchKnowledge = (body, { signal } = {}) => request('POST', '/api/knowledge/search', body, signal);
export const diagnoseKnowledge = (body, { signal } = {}) => request('POST', '/api/knowledge/diagnose', body, signal);
export const generateKnowledgeDiagnosis = (body, { signal } = {}) => request('POST', '/api/knowledge/generate', body, signal);

// -- saved execution history (read-only; uses the same error handling) --
export { subscribeHistory } from './history-stream.js';
export const listHistory = (slug, { offset = 0, limit = 20, signal } = {}) =>
  request('GET', `${slugPath(slug)}/history?${new URLSearchParams({ offset, limit })}`, undefined, signal);
export const listHistoryVersions = (slug, sessionId, { offset = 0, limit = 20, signal } = {}) =>
  request('GET', `${slugPath(slug)}/history/versions?${new URLSearchParams({ session_id: sessionId, offset, limit })}`, undefined, signal);
export const getHistoryStorage = () => request('GET', '/api/history/storage');
export const listSessions = (slug, { offset = 0, limit = 20 } = {}) =>
  request('GET', `${slugPath(slug)}/sessions?${new URLSearchParams({ offset, limit })}`);
export const listArchives = (slug, { offset = 0, limit = 20 } = {}) =>
  request('GET', `${slugPath(slug)}/archives?${new URLSearchParams({ offset, limit })}`);
export const getSession = (slug, sessionId, {
  turn_offset = 0, turn_limit = 20, event_offset = 0, event_limit = 50, turn_no, signal,
} = {}) => {
  const query = new URLSearchParams({ turn_offset, turn_limit, event_offset, event_limit });
  if (turn_no !== undefined) query.set('turn_no', turn_no);
  return request('GET', `${slugPath(slug)}/sessions/${encodeURIComponent(sessionId)}?${query}`, undefined, signal);
};
export const getArchive = (slug, captureId, {
  turn_offset = 0, turn_limit = 20, event_offset = 0, event_limit = 50, turn_no, signal,
} = {}) => {
  const query = new URLSearchParams({ turn_offset, turn_limit, event_offset, event_limit });
  if (turn_no !== undefined) query.set('turn_no', turn_no);
  return request('GET', `${slugPath(slug)}/archives/${encodeURIComponent(captureId)}?${query}`, undefined, signal);
};

// -- skills store (co-ai agents) ------------------------------
const skillPath = (slug) => `/api/skills/agent/${encodeURIComponent(slug)}`;
export const getAgentSkills = (slug) => request('GET', skillPath(slug));
export const installSkill   = (slug, name) => request('POST', `${skillPath(slug)}/install`, { name });
export const uninstallSkill = (slug, name) => request('POST', `${skillPath(slug)}/uninstall`, { name });
export const getMarket      = () => request('GET', '/api/skills/market');
export const installBulk    = (name, slugs) => request('POST', '/api/skills/install-bulk', { name, slugs });

// -- setup / settings -----------------------------------------
export const setupStatus = () => request('GET', '/api/setup/status');
export const updateStatus = () => request('GET', '/api/setup/update');
export const setStorage  = (path) => request('POST', '/api/settings/storage', { path });
export const pickFolder  = () => request('POST', '/api/settings/pick-folder');
export const pickWorkspace = () => request('POST', '/api/settings/pick-workspace');
export const setAppearance = (appearance) => request('POST', '/api/settings/appearance', { appearance });

export async function fetchDiagnostics(slug) {
  const res = await fetch(`${slugPath(slug)}/diagnostics`);
  if (!res.ok) throw new ApiError(`Diagnostics fetch failed (${res.status})`, res.status);
  return res.text();
}

export const qrUrl = (slug) => `${slugPath(slug)}/qr.svg`;
