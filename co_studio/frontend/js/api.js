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

async function request(method, path, body) {
  let res;
  try {
    res = await fetch(path, {
      method,
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
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
