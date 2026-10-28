// ============================================================
// app.js — ConnectOnion Studio SPA orchestrator
// Views: splash → agent list ⇄ create modal ⇄ detail drawer,
// full-screen onboarding when `co auth` is missing.
// Zero build: vanilla ES modules only.
// ============================================================

import * as api from './api.js';
import { statusSocket, logSocket } from './ws.js';
import { createOnion, playSplash } from './onion.js';
import { copyForClaude, copyText, copyWithFeedback } from './diagnostics.js';
import { createHistoryView } from './history.js';
import { createPromptEditor } from './prompt-editor.js';
import { createToolConfig } from './tool-config.js';
import { createApprovalsView } from './approvals.js';
import { createKnowledgeView } from './knowledge.js';

const $ = (sel, root = document) => root.querySelector(sel);
let historyView;
let promptEditor;
let toolConfig;
let approvalsView;
let knowledgeView;

// ---- state ----------------------------------------------------------
const state = {
  agents: [],
  cards: new Map(),          // slug → card element
  logs: { selected: null, socket: null, following: true, errorsOnly: false, lines: 0 },   // master–detail logs view
  busy: new Set(),           // slugs with an action in flight
  drawerSlug: null,
  drawerDetail: null,
  drawerSkills: null,        // { slug, is_co_ai, installed[], catalog[], running } for the open co-ai drawer
  market: null,              // { catalog[], agents[] } for the Skills Market view
  statusSock: null,
  onboardingTimer: null,
  onboardingActive: false,
  setup: null,
  search: '',
};

// ---- helpers --------------------------------------------------------
const shortAddr = (addr) =>
  addr && addr.length > 14 ? `${addr.slice(0, 7)}…${addr.slice(-5)}` : (addr || '');
const capitalize = (s) => (s ? s[0].toUpperCase() + s.slice(1) : '');

function toast(message, kind = '') {
  const host = $('#toasts');
  const el = document.createElement('div');
  el.className = `toast glass${kind ? ` ${kind}` : ''}`;
  el.textContent = message;
  host.appendChild(el);
  setTimeout(() => {
    el.classList.add('leaving');
    el.addEventListener('animationend', () => el.remove(), { once: true });
    setTimeout(() => el.remove(), 500); // fallback removal
  }, 3600);
}

// ---- theme -----------------------------------------------------------
// Light-only for now (dark isn't dialed in yet). `data-theme="light"` on <html>
// pins it so the system's dark preference can't leak through.
function initTheme() {
  document.documentElement.dataset.theme = 'light';
}

// ---- appearance skin (Warm default / Lavender) -----------------------
// Opt-in second skin from css/appearance.css, toggled in Settings. The saved
// choice is applied PRE-PAINT by the inline script in index.html; this only
// syncs the Settings radios and re-applies live when the user switches.
const APPEARANCE_KEY = 'co-studio-appearance';
function paintAppearance(name) {
  if (name === 'lavender') document.documentElement.dataset.appearance = 'lavender';
  else delete document.documentElement.dataset.appearance;   // Warm = no attribute (theme.css default)
}
function persistAppearance(name) {
  const appearance = name === 'lavender' ? 'lavender' : 'warm';
  try { localStorage.setItem(APPEARANCE_KEY, appearance); } catch { /* backend remains authoritative */ }
  window.__coStudio?.setAppearance?.(appearance);   // native cover updates immediately
  api.setAppearance(appearance).catch(() => {
    toast('Theme changed for this session, but could not be saved.', 'danger');
  });
}
function applyAppearance(name) {
  paintAppearance(name);
  persistAppearance(name);
}
function initAppearance() {
  const saved = window.__coStudioInitialAppearance === 'lavender' ? 'lavender' : 'warm';
  paintAppearance(saved);   // make the runtime state agree with the pre-paint state
  document.querySelectorAll('input[name="appearance"]').forEach((radio) => {
    radio.checked = radio.value === saved;
    radio.addEventListener('change', () => { if (radio.checked) applyAppearance(radio.value); });
  });
  if (window.__coStudioAppearanceNeedsMigration) persistAppearance(saved);
}

// ---- agent list -----------------------------------------------------
// row/action icons (Lucide-style, stroke 1.8 to match the rest of the UI)
const ICON = {
  port: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect width="20" height="8" x="2" y="2" rx="2"/><rect width="20" height="8" x="2" y="14" rx="2"/><line x1="6" x2="6.01" y1="6" y2="6"/><line x1="6" x2="6.01" y1="18" y2="18"/></svg>`,
  toolkits: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect width="7" height="7" x="3" y="3" rx="1"/><rect width="7" height="7" x="14" y="3" rx="1"/><rect width="7" height="7" x="14" y="14" rx="1"/><rect width="7" height="7" x="3" y="14" rx="1"/></svg>`,
  id: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 10a2 2 0 0 0-2 2c0 1.02-.1 2.51-.26 4"/><path d="M14 13.12c0 2.38 0 6.38-1 8.88"/><path d="M17.29 21.02c.12-.6.43-2.3.5-3.02"/><path d="M2 12a10 10 0 0 1 18-6"/><path d="M2 16h.01"/><path d="M21.8 16c.2-2 .131-5.354 0-6"/><path d="M5 19.5C5.5 18 6 15 6 12a6 6 0 0 1 .34-2"/><path d="M8.65 22c.21-.66.45-1.32.57-2"/><path d="M9 6.8a6 6 0 0 1 9 5.2c0 .47 0 1.17-.02 2"/></svg>`,
  model: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/></svg>`,
  copy: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect width="14" height="14" x="8" y="8" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>`,
  qr: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect width="5" height="5" x="3" y="3" rx="1"/><rect width="5" height="5" x="16" y="3" rx="1"/><rect width="5" height="5" x="3" y="16" rx="1"/><path d="M21 16h-3a2 2 0 0 0-2 2v3"/><path d="M21 21v.01"/><path d="M12 7v3a2 2 0 0 1-2 2H7"/><path d="M3 12h.01"/><path d="M12 3h.01"/><path d="M12 16v.01"/><path d="M16 12h1"/><path d="M21 12v.01"/><path d="M12 21v-1"/></svg>`,
  play: `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M7 4.5v15a1 1 0 0 0 1.5.87l12-7.5a1 1 0 0 0 0-1.74l-12-7.5A1 1 0 0 0 7 4.5Z"/></svg>`,
  stop: `<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2.5"/></svg>`,
  access: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/></svg>`,
  dots: `<svg viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="5" r="1.7"/><circle cx="12" cy="12" r="1.7"/><circle cx="12" cy="19" r="1.7"/></svg>`,
  rename: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>`,
  pin: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 17v5"/><path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V5a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z"/></svg>`,
  close: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg>`,
  back: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 19-7-7 7-7"/><path d="M19 12H5"/></svg>`,
};

function agentCard(agent) {
  const card = document.createElement('article');
  card.className = 'agent-card';
  card.dataset.slug = agent.slug;
  card.tabIndex = 0;
  card.setAttribute('role', 'button');
  card.innerHTML = `
    <div class="card-inner">
      <div class="card-front">
        <div class="card-head">
          <span class="pin-flag" aria-hidden="true">${ICON.pin}</span>
          <h3 class="card-name"></h3>
          <div class="card-menu">
            <button class="card-menu-btn" title="More" aria-label="More actions" aria-haspopup="true">${ICON.dots}</button>
            <div class="card-menu-pop" hidden role="menu">
              <button class="menu-item" data-act="rename" role="menuitem">${ICON.rename}<span>Rename</span></button>
              <button class="menu-item" data-act="pin" role="menuitem">${ICON.pin}<span class="pin-label">Pin</span></button>
            </div>
          </div>
        </div>
        <div class="card-sub">
          <span class="card-state">
            <span class="status-dot"></span>
            <span class="status-onion"></span>
            <span class="state-word"></span>
          </span>
          <span class="sub-sep" aria-hidden="true"></span>
          <span class="toolkits-chips"></span>
        </div>
        <div class="card-rows">
          <div class="card-row">
            <span class="row-ico">${ICON.port}</span>
            <span class="row-label">Port</span>
            <span class="row-value mono port-val"></span>
          </div>
          <div class="card-row">
            <span class="row-ico">${ICON.access}</span>
            <span class="row-label">Access</span>
            <span class="row-value access-val"></span>
          </div>
          <div class="card-row">
            <span class="row-ico">${ICON.id}</span>
            <span class="row-label">ID</span>
            <span class="row-value id-val">
              <code class="addr-short"></code>
              <button class="row-copy btn-copy-addr" title="Copy ID" aria-label="Copy ID">${ICON.copy}</button>
            </span>
          </div>
          <div class="card-row">
            <span class="row-ico">${ICON.model}</span>
            <span class="row-label">Model</span>
            <span class="row-value mono model-val"></span>
          </div>
        </div>
        <div class="card-actions">
          <button class="btn btn-outline icon-square btn-qr" title="Show QR" aria-label="Show QR code">${ICON.qr}</button>
          <button class="btn btn-outline btn-detail">Detail</button>
          <button class="btn btn-toggle"></button>
        </div>
      </div>
      <div class="card-back">
        <button class="card-back-close" title="Back" aria-label="Back to details">${ICON.back}</button>
        <img class="card-qr-img" alt="Agent address QR code" draggable="false">
        <div class="card-qr-name"></div>
        <div class="card-qr-addr mono"></div>
      </div>
    </div>`;
  $('.status-onion', card).appendChild(
    createOnion({ size: 16, mode: 'thinking', label: 'starting' }),   // ≤ the row's text/chip height → no row-height jump on Start
  );

  $('.btn-copy-addr', card).addEventListener('click', (e) => {
    e.stopPropagation();
    copyWithFeedback(e.currentTarget, card._agent.address);
  });
  $('.btn-qr', card).addEventListener('click', (e) => {
    e.stopPropagation();
    const img = $('.card-qr-img', card);
    if (!img.getAttribute('src')) img.src = api.qrUrl(card._agent.slug);   // lazy-load the QR
    $('.card-qr-name', card).textContent = card._agent.name;
    $('.card-qr-addr', card).textContent = shortAddr(card._agent.address);
    closeAllCardMenus();
    card.classList.add('flipped');                                          // flip to the QR back
  });
  $('.card-back-close', card).addEventListener('click', (e) => {
    e.stopPropagation();
    card.classList.remove('flipped');
  });
  $('.btn-detail', card).addEventListener('click', (e) => {
    e.stopPropagation();
    openDrawer(card._agent.slug);
  });
  $('.btn-toggle', card).addEventListener('click', (e) => {
    e.stopPropagation();
    const a = card._agent;
    doAction(a.slug, a.state === 'online' ? 'stop' : 'start');
  });

  // three-dots menu (Rename / Pin)
  const menuPop = $('.card-menu-pop', card);
  $('.card-menu-btn', card).addEventListener('click', (e) => {
    e.stopPropagation();
    const willOpen = menuPop.hidden;
    closeAllCardMenus();
    menuPop.hidden = !willOpen;
    card.classList.toggle('menu-open', willOpen);
  });
  menuPop.addEventListener('click', (e) => e.stopPropagation());
  $('.menu-item[data-act="rename"]', card).addEventListener('click', (e) => {
    e.stopPropagation();
    closeAllCardMenus();
    startRename(card);
  });
  $('.menu-item[data-act="pin"]', card).addEventListener('click', (e) => {
    e.stopPropagation();
    closeAllCardMenus();
    togglePin(card._agent.slug);
  });

  card.addEventListener('click', (e) => {
    if (card.classList.contains('flipped')) {              // showing the QR → any click flips back
      card.classList.remove('flipped');
      return;
    }
    if (e.target.closest('button')) return;
    if ($('.card-name', card).isContentEditable) return;   // don't open the drawer mid-rename
    openDrawer(card._agent.slug);
  });
  card.addEventListener('keydown', (e) => {
    if ((e.key === 'Enter' || e.key === ' ') && !e.target.closest('button')
        && !$('.card-name', card).isContentEditable) {
      e.preventDefault();
      openDrawer(card._agent.slug);
    }
  });

  if (chipFitObserver) chipFitObserver.observe($('.toolkits-chips', card));
  updateCard(card, agent);
  return card;
}

function updateCard(card, agent) {
  card._agent = agent;
  card.dataset.state = agent.state;
  const nameEl = $('.card-name', card);
  if (!nameEl.isContentEditable) nameEl.textContent = agent.name;   // don't clobber an in-progress rename
  nameEl.title = agent.name;
  $('.state-word', card).textContent = capitalize(agent.state);
  $('.port-val', card).textContent = agent.port;
  $('.access-val', card).textContent = accessLabel(agent);
  $('.addr-short', card).textContent = shortAddr(agent.address);
  const modelEl = $('.model-val', card);
  modelEl.textContent = agent.model;
  modelEl.title = agent.model;

  // Capabilities → violet chips. Only (re)build when they actually change, so the
  // periodic /ws/status refresh doesn't rebuild the DOM and flicker the card.
  const capabilities = agent.capabilities || agent.toolkits || [];
  const tkKey = `${agent.preset || 'custom'}:${capabilities.join('')}`;
  if (card._tkKey !== tkKey) {
    card._tkKey = tkKey;
    fitChips(card);
  }

  // Start / Stop toggle — likewise, only rewrite when the visual state changes.
  const busy = state.busy.has(agent.slug);
  const toggleKey = `${agent.state}:${busy}`;
  if (card._toggleKey !== toggleKey) {
    card._toggleKey = toggleKey;
    const toggle = $('.btn-toggle', card);
    toggle.classList.remove('btn-primary', 'btn-danger-solid');
    if (agent.state === 'starting' || agent.state === 'creating') {
      toggle.innerHTML = '<span>Starting…</span>';
      toggle.disabled = true;
    } else if (agent.state === 'online') {
      toggle.innerHTML = `${ICON.stop}<span>Stop</span>`;
      toggle.classList.add('btn-danger-solid');
      toggle.disabled = busy;
    } else {
      toggle.innerHTML = `${ICON.play}<span>Start</span>`;
      toggle.classList.add('btn-primary');
      toggle.disabled = busy;
    }
  }
}

// Render capability chips into the sub-row. `more` appends a trailing "…" chip.
function renderChips(wrap, list, more, fullTitle) {
  wrap.textContent = '';
  for (const name of list) {
    const chip = document.createElement('span');
    chip.className = 'tk-chip';
    chip.textContent = CAPABILITY_LABEL[name] || name;
    wrap.appendChild(chip);
  }
  if (more) {
    const m = document.createElement('span');
    m.className = 'tk-chip tk-chip-more';
    m.textContent = '…';
    m.title = fullTitle;
    wrap.appendChild(m);
  }
}
// Priority: user-added capabilities first, the default "utility" last. Show up to 3;
// 4+ → three chips + a "…". If even that overflows the row, drop chips (keep "…").
function fitChips(card) {
  const wrap = $('.toolkits-chips', card);
  if (!wrap) return;
  const agent = card._agent;
  const full = agent?.preset === 'co-ai' ? ['co ai'] : (agent?.capabilities || agent?.toolkits || []);
  const ordered = [...full.filter((t) => t !== 'utility'), ...full.filter((t) => t === 'utility')];
  const title = full.map((name) => CAPABILITY_LABEL[name] || name).join(', ');
  let n = Math.min(3, ordered.length);
  renderChips(wrap, ordered.slice(0, n), ordered.length > n, title);
  if (!card.isConnected) return;                          // no layout yet — observer re-fits
  while (n > 0 && wrap.scrollWidth > wrap.clientWidth + 1) {
    n -= 1;
    renderChips(wrap, ordered.slice(0, n), true, title);
  }
}
const chipFitObserver = typeof ResizeObserver !== 'undefined'
  ? new ResizeObserver((entries) => {
      for (const e of entries) {
        const card = e.target.closest('.agent-card');
        if (card) fitChips(card);
      }
    })
  : null;
// belt-and-suspenders re-fit (reveal + window resize) — infrequent, so no flicker
function refitChips() {
  for (const [, card] of state.cards) fitChips(card);
}

// ---- pin (frontend-only, localStorage) + card menus + inline rename --
const PINNED_KEY = 'co-studio-pinned';
function getPinned() {
  try { return new Set(JSON.parse(localStorage.getItem(PINNED_KEY) || '[]')); }
  catch { return new Set(); }
}
function togglePin(slug) {
  const pinned = getPinned();
  pinned.has(slug) ? pinned.delete(slug) : pinned.add(slug);
  try { localStorage.setItem(PINNED_KEY, JSON.stringify([...pinned])); } catch { /* ignore */ }
  renderAgents();
}
function closeAllCardMenus() {
  document.querySelectorAll('.card-menu-pop:not([hidden])').forEach((p) => { p.hidden = true; });
  document.querySelectorAll('.agent-card.menu-open').forEach((c) => c.classList.remove('menu-open'));
}
async function commitRename(card, raw) {
  const agent = card._agent;
  const nameEl = $('.card-name', card);
  nameEl.contentEditable = 'false';
  nameEl.classList.remove('editing');
  const name = (raw || '').trim();
  if (!name || name === agent.name) { nameEl.textContent = agent.name; return; }
  const validation = nameStatus(name, agent.slug);
  if (validation.kind === 'bad') {
    nameEl.textContent = agent.name;
    toast(validation.msg, 'danger');
    return;
  }
  try {
    const updated = await api.renameAgent(agent.slug, name);
    card._agent = { ...agent, ...updated };
    updateCard(card, card._agent);
    toast(`Renamed to “${name}”`);
  } catch (err) {
    nameEl.textContent = agent.name;
    toast(err.status === 409 ? err.message : `Rename failed: ${err.message}`, 'danger');
  }
}
function startRename(card) {
  const nameEl = $('.card-name', card);
  if (nameEl.isContentEditable) return;
  nameEl.contentEditable = 'true';
  nameEl.classList.add('editing');
  nameEl.focus();
  const range = document.createRange();
  range.selectNodeContents(nameEl);
  const sel = window.getSelection();
  sel.removeAllRanges(); sel.addRange(range);

  let done = false;
  const finish = (commit) => {
    if (done) return;
    done = true;
    nameEl.removeEventListener('keydown', onKey);
    nameEl.removeEventListener('blur', onBlur);
    if (commit) {
      commitRename(card, nameEl.textContent);        // sets contentEditable='false', persists
    } else {
      nameEl.contentEditable = 'false';
      nameEl.classList.remove('editing');
      nameEl.textContent = card._agent.name;         // cancel → restore
    }
  };
  const onKey = (e) => {
    e.stopPropagation();
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  };
  const onBlur = () => finish(true);                 // click away = commit
  nameEl.addEventListener('keydown', onKey);
  nameEl.addEventListener('blur', onBlur);
}

let firstrunBuilt = false;
function renderAgents() {
  const grid = $('#agent-grid');

  // No agents → the first-run screen covers the shell; adding the first agent
  // reveals the shell, deleting the last one crossfades back here.
  if (!state.agents.length) {
    grid.textContent = '';
    state.cards.clear();
    if (!firstrunBuilt) {
      $('#firstrun-onion').appendChild(
        createOnion({ size: 148, mode: 'hero', label: 'ConnectOnion' }),
      );
      firstrunBuilt = true;
    }
    $('#app').classList.add('is-firstrun');
    return;
  }
  $('#app').classList.remove('is-firstrun');

  const pinned = getPinned();
  // pinned first; stable sort keeps created order within each group
  const ordered = [...state.agents].sort(
    (a, b) => (pinned.has(a.slug) ? 0 : 1) - (pinned.has(b.slug) ? 0 : 1),
  );

  const seen = new Set();
  for (const agent of ordered) {
    seen.add(agent.slug);
    let card = state.cards.get(agent.slug);
    if (!card) {
      card = agentCard(agent);
      state.cards.set(agent.slug, card);
    } else {
      updateCard(card, agent);
    }
    const isPinned = pinned.has(agent.slug);
    card.classList.toggle('is-pinned', isPinned);
    const pinLabel = $('.pin-label', card);
    if (pinLabel) pinLabel.textContent = isPinned ? 'Unpin' : 'Pin';
  }
  for (const [slug, card] of state.cards) {
    if (!seen.has(slug)) {
      if (chipFitObserver) chipFitObserver.unobserve($('.toolkits-chips', card));
      card.remove();
      state.cards.delete(slug);
    }
  }

  // Reconcile DOM order to the sorted list, but ONLY when it actually changed —
  // re-appending every card on each /ws/status tick is what made the grid jitter.
  const desired = ordered.map((a) => state.cards.get(a.slug)).filter(Boolean);
  const current = [...grid.children];
  const orderChanged = desired.length !== current.length
    || desired.some((c, i) => c !== current[i]);
  if (orderChanged) for (const card of desired) grid.appendChild(card);

  applySearch();
}

function applySearch() {
  const q = (state.search || '').toLowerCase();
  for (const [, card] of state.cards) {
    const a = card._agent || {};
    const match = !q
      || (a.name || '').toLowerCase().includes(q)
      || (a.address || '').toLowerCase().includes(q);
    card.hidden = !match;
  }
}

function initSearch() {
  const input = $('#agent-search');
  if (!input) return;
  input.addEventListener('input', (e) => {
    state.search = e.target.value.trim();
    applySearch();
  });
}

function updateFleetHeader() {
  const total = state.agents.length;
  const online = state.agents.filter((a) => a.state === 'online').length;
  const cnt = $('#nav-agents-count');
  if (cnt) {                                    // sidebar badge = ONLINE count, in green (hidden at 0)
    cnt.textContent = online || '';
    cnt.classList.add('nav-count-online');
  }
  const fleet = $('#fleet');
  if (fleet) fleet.textContent = total ? `${online} online · ${total} total` : '';
  // Warn that quitting leaves agents alive only when at least one has a live process (survives quit).
  const note = $('#persist-note');
  if (note) note.hidden = !state.agents.some((a) => ['online', 'starting', 'offline'].includes(a.state));
}

function renderSideHealth(setup) {
  const fwEl = $('#side-fw');
  const dot = $('#side-dot');
  if (!fwEl) return;
  const fw = (setup?.doctor || []).find((c) => c.check === 'import connectonion');
  if (setup?.framework_ok && fw?.ok) {
    fwEl.textContent = `connectonion ${String(fw.detail).replace('version ', '')}`;
    dot?.classList.remove('bad');
  } else {
    fwEl.textContent = 'framework needs setup';
    dot?.classList.add('bad');
  }
}

// ---- settings sheet (rises from the bottom of the current card) ----
function setNav(view) {   // mark exactly one sidebar item active
  document.querySelectorAll('.sidebar-nav .nav-item').forEach((el) => {
    const on = el.dataset.view === view;
    el.classList.toggle('is-active', on);
    el.setAttribute('aria-current', on ? 'page' : 'false');
  });
}
function setSettingsNav(on) { setNav(on ? 'settings' : 'agents'); }
function openSettingsModal({ inset = false } = {}) {
  closeWorkspaceViews();
  if ($('#app').classList.contains('is-history')) closeHistoryView();
  renderSettings(state.setup);
  const app = $('#app');
  app.classList.toggle('is-settings-inset', inset);   // main-interface: content-pane only
  app.classList.add('is-settings');
  if (inset) setSettingsNav(true);                     // reflect it in the sidebar
}
function closeSettingsModal() {
  const app = $('#app');
  const wasInset = app.classList.contains('is-settings-inset');
  app.classList.remove('is-settings');
  if (wasInset) {
    setSettingsNav(false);                             // Agents becomes active again
    // Keep the inset flag through the 0.3s fade so the pane stays in the content column (left:244) and
    // never expands full-width over the sidebar mid-fade (that "whole panel flashes" bug). Drop it once
    // the pane is fully hidden — and only if Settings wasn't re-opened in the meantime.
    setTimeout(() => { if (!app.classList.contains('is-settings')) app.classList.remove('is-settings-inset'); }, 380);
  } else {
    app.classList.remove('is-settings-inset');         // gate mode is full-screen anyway
  }
  if (app.classList.contains('is-firstrun')) {   // bring "No agents yet" back to life
    const fr = $('#firstrun');
    fr.classList.remove('is-returning');
    void fr.offsetWidth;
    fr.classList.add('is-returning');
  }
}

function initNav() {
  const app = $('#app');
  app.appendChild($('#settings-view'));         // relocate the sheet inside the card
  $('#settings-view').hidden = false;           // CSS visibility/transform drives it now
  app.appendChild($('#logs-view'));             // same for the logs view (fills the content pane)
  $('#logs-view').hidden = false;
  app.appendChild($('#history-view'));          // saved history uses the same shell and Agent list
  $('#history-view').hidden = false;
  historyView = createHistoryView({ root: $('#history-view'), api, onKnowledge: (draft) => {
    openWorkspaceView('knowledge'); knowledgeView.prefill(draft);
  } });
  for (const name of ['knowledge', 'approvals']) {
    app.appendChild($(`#${name}-view`)); $(`#${name}-view`).hidden = false;
    const toggle = () => app.classList.contains(`is-${name}`) ? closeWorkspaceViews() : openWorkspaceView(name);
    $(`#open-${name}`).addEventListener('click', toggle);
    $(`#open-${name}`).addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
    });
  }
  approvalsView = createApprovalsView({ root: $('#approvals-view'), api });
  knowledgeView = createKnowledgeView({ root: $('#knowledge-view'), api });
  app.appendChild($('#market-view'));           // and the Skills Market view (fills the content pane)
  $('#market-view').hidden = false;

  // sidebar → Settings: toggle the content-pane settings (sidebar stays put); leaving Logs first
  const toggleInset = () => {
    if (app.classList.contains('is-settings')) closeSettingsModal();
    else {
      if (app.classList.contains('is-logs')) closeLogsView();
      if (app.classList.contains('is-market')) closeMarketView();
      openSettingsModal({ inset: true });
    }
  };
  $('#open-settings').addEventListener('click', toggleInset);
  $('#open-settings').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleInset(); }
  });

  // sidebar → Logs: toggle the content-pane log consoles
  const toggleLogs = () => {
    if (app.classList.contains('is-logs')) closeLogsView();
    else openLogsView();
  };
  $('#open-logs').addEventListener('click', toggleLogs);
  $('#open-logs').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleLogs(); }
  });

  const toggleHistory = () => {
    if (app.classList.contains('is-history')) closeHistoryView();
    else openHistoryView();
  };
  $('#open-history').addEventListener('click', toggleHistory);
  $('#open-history').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleHistory(); }
  });

  // sidebar → Skills: toggle the content-pane market
  const toggleMarket = () => {
    if (app.classList.contains('is-market')) closeMarketView();
    else openMarketView();
  };
  $('#open-market').addEventListener('click', toggleMarket);
  $('#open-market').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleMarket(); }
  });
  $('#logs-search').addEventListener('input', applyLogsFilter);   // filter the master list by name
  $('#logs-reveal').addEventListener('click', revealLogsFolder);
  $('#ld-follow').addEventListener('change', (e) => {
    state.logs.following = e.target.checked;
    if (e.target.checked) { const c = $('#ld-console'); c.scrollTop = c.scrollHeight; }
  });
  $('#ld-errors').addEventListener('click', (e) => {
    state.logs.errorsOnly = !state.logs.errorsOnly;
    e.currentTarget.classList.toggle('is-active', state.logs.errorsOnly);
    e.currentTarget.setAttribute('aria-pressed', state.logs.errorsOnly ? 'true' : 'false');
    applyErrorsFilter();
  });
  $('#ld-copy').addEventListener('click', (e) => {
    if (state.logs.selected) copyForClaude(state.logs.selected, e.currentTarget);
  });
  $('#ld-pin').addEventListener('click', () => {
    if (!state.logs.selected) return;
    togglePin(state.logs.selected);          // updates localStorage + re-renders the Agents grid
    renderLogs();                            // reflect the pin in the master list (outline + reorder)
    const a = state.agents.find((x) => x.slug === state.logs.selected);
    if (a) fillDetailHead(a);
  });

  // sidebar → Agents: step back out of whichever content-pane view is open
  const agentsNav = $('.nav-item[data-view="agents"]');
  if (agentsNav) agentsNav.addEventListener('click', () => {
    closeWorkspaceViews();
    if (app.classList.contains('is-settings')) closeSettingsModal();
    if (app.classList.contains('is-logs')) closeLogsView();
    if (app.classList.contains('is-market')) closeMarketView();
    if (app.classList.contains('is-history')) closeHistoryView();
  });

  // first-run gear → full-card settings (keeps its own top-right close gear)
  $('#firstrun-settings').addEventListener('click', () => openSettingsModal({ inset: false }));
  $('#settings-close').addEventListener('click', closeSettingsModal);
}

function closeWorkspaceViews() {
  const app = $('#app');
  if (!app.classList.contains('is-knowledge') && !app.classList.contains('is-approvals')) return;
  app.classList.remove('is-knowledge', 'is-approvals');
  approvalsView?.close(); knowledgeView?.close(); setNav('agents');
}

function openWorkspaceView(name) {
  closeWorkspaceViews();
  const app = $('#app');
  if (app.classList.contains('is-detail')) closeDrawer();
  if (app.classList.contains('is-history')) closeHistoryView();
  if (app.classList.contains('is-logs')) closeLogsView();
  if (app.classList.contains('is-market')) closeMarketView();
  if (app.classList.contains('is-settings')) closeSettingsModal();
  app.classList.add(`is-${name}`); setNav(name);
  if (name === 'approvals') approvalsView.open(state.agents);
  else void knowledgeView.open();
}

// History shares navigation/Agent discovery with the existing views. It never
// opens a runtime socket or starts an Agent just to inspect its saved records.
function openHistoryView() {
  closeWorkspaceViews();
  const app = $('#app');
  if (app.classList.contains('is-detail')) closeDrawer();
  if (app.classList.contains('is-logs')) closeLogsView();
  if (app.classList.contains('is-market')) closeMarketView();
  if (app.classList.contains('is-settings')) closeSettingsModal();
  app.classList.add('is-history');
  setNav('history');
  historyView.open(state.agents);
}

function closeHistoryView() {
  $('#app').classList.remove('is-history');
  historyView?.close();
  setNav('agents');
}

// ---- logs view (master–detail: running-agent list + one live detail) ----
const LOG_ERROR_RE = /\b(error|exception|traceback|critical|fatal|failed)\b|\[ERROR\]/i;
const LOG_LINE_CAP = 2000;   // trim the console DOM so a chatty agent can't grow it without bound
const shortModel = (m) => (m || '').replace(/^co\//, '');

/** Started agents (anything not cleanly stopped — includes crashed, so logs stay), pinned floated up. */
function runningAgents() {
  const pinned = getPinned();
  return state.agents
    .filter((a) => a.state !== 'stopped')
    .sort((a, b) => (pinned.has(b.slug) ? 1 : 0) - (pinned.has(a.slug) ? 1 : 0));
}

function relTime(epoch) {
  if (!epoch) return '';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - epoch));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

/** Filter the master list by the search box (matches agent name). */
function applyLogsFilter() {
  const q = ($('#logs-search')?.value || '').trim().toLowerCase();
  document.querySelectorAll('.logs-item').forEach((el) => {
    el.hidden = !!q && !$('.logs-item-name', el).textContent.toLowerCase().includes(q);
  });
}

/** Rebuild the master list from the running agents; reconcile the selection. Called on open + status push. */
function renderLogs() {
  const running = runningAgents();
  const pinned = getPinned();
  const list = $('#logs-list');

  $('#logs-subtitle').textContent = `${running.length} streaming · ${state.agents.length} total`;
  $('#logs-count').textContent = `(${running.length})`;
  $('#logs-empty').hidden = running.length > 0;

  list.replaceChildren();
  for (const a of running) {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'logs-item';
    item.dataset.slug = a.slug;
    item.setAttribute('role', 'option');
    item.classList.toggle('is-selected', a.slug === state.logs.selected);
    item.classList.toggle('is-pinned', pinned.has(a.slug));
    item.innerHTML = `
      <span class="logs-item-dot" data-state="${a.state}"></span>
      <span class="logs-item-body">
        <span class="logs-item-top"><span class="logs-item-name"></span><span class="logs-item-time"></span></span>
        <span class="logs-item-meta"></span>
      </span>`;
    $('.logs-item-name', item).textContent = a.name;
    $('.logs-item-time', item).textContent = relTime(a.started_at);
    $('.logs-item-meta', item).textContent = shortAddr(a.address);
    item.addEventListener('click', () => selectLogAgent(a.slug));
    list.appendChild(item);
  }
  applyLogsFilter();

  // selection: keep the current one if still running (don't churn its socket); else pick the first
  if (state.logs.selected && running.some((a) => a.slug === state.logs.selected)) {
    const summary = state.agents.find((a) => a.slug === state.logs.selected);
    if (summary) fillDetailHead(summary);   // refresh the state badge while streaming continues
  } else {
    selectLogAgent(running[0]?.slug || null);
  }
}

/** Select an agent: swap the live socket + console and populate the detail pane. */
function selectLogAgent(slug) {
  if (state.logs.socket) { state.logs.socket.close(); state.logs.socket = null; }
  clearInterval(state.logs.tilePoll); state.logs.tilePoll = null;   // stop any prior stat-tile poll
  state.logs.selected = slug;
  state.logs.lines = 0;
  $('#ld-console').replaceChildren();
  $('#ld-lines').textContent = '0 lines';
  document.querySelectorAll('.logs-item').forEach((el) => el.classList.toggle('is-selected', el.dataset.slug === slug));

  const detail = $('#logs-detail');
  const empty = $('#logs-detail-empty');
  if (!slug) { detail.hidden = true; empty.hidden = false; return; }
  detail.hidden = false; empty.hidden = true;

  const summary = state.agents.find((a) => a.slug === slug);
  if (summary) fillDetailHead(summary);
  fetchLogTiles(slug);
  // A just-Started agent is still booting, so tools/balance/endpoints may be empty on the first
  // fetch (and nothing re-triggers it). Poll a few times so they fill in on their own — no need to
  // leave the Logs view and come back.
  let tileTries = 0;
  state.logs.tilePoll = setInterval(() => {
    if (state.logs.selected !== slug || ++tileTries >= 8) { clearInterval(state.logs.tilePoll); state.logs.tilePoll = null; return; }
    fetchLogTiles(slug);
  }, 2000);

  // one live socket at a time — the backend streams ONLY this run, from its first line
  state.logs.socket = logSocket(slug, appendLogLine, (connected) => {
    $('#logs-detail').classList.toggle('is-disconnected', !connected);
  });
}

function fillDetailHead(a) {
  $('#ld-name').textContent = a.name;
  const badge = $('#ld-state');
  badge.dataset.state = a.state;
  badge.textContent = capitalize(a.state);
  const pinned = getPinned().has(a.slug);
  const pin = $('#ld-pin');
  pin.classList.toggle('is-pinned', pinned);
  pin.setAttribute('aria-pressed', pinned ? 'true' : 'false');
}

/** Fetch the detail endpoint, fill the 4 stat tiles + the subtitle. */
async function fetchLogTiles(slug) {
  let d;
  try { d = await api.getAgent(slug); } catch { return; }
  if (state.logs.selected !== slug) return;   // selection changed mid-fetch
  $('#tile-tools').textContent = d.tools_count ?? '—';
  $('#tile-balance').textContent = d.balance || '—';
  $('#tile-endpoints').textContent = d.endpoints_announced ?? '—';
  $('#ld-sub').textContent = shortModel(d.model) || '—';
}

function appendLogLine({ line }) {
  const el = $('#ld-console');
  if (!el) return;
  const isError = LOG_ERROR_RE.test(line);
  const div = document.createElement('div');
  div.className = 'ld-line' + (isError ? ' is-error' : '');
  if (state.logs.errorsOnly && !isError) div.hidden = true;
  div.textContent = line || ' ';
  el.appendChild(div);
  state.logs.lines += 1;
  while (el.childElementCount > LOG_LINE_CAP) { el.firstElementChild.remove(); state.logs.lines -= 1; }
  $('#ld-lines').textContent = `${state.logs.lines} lines`;
  if (state.logs.following) el.scrollTop = el.scrollHeight;   // Follow logs = stick to newest
}

function applyErrorsFilter() {
  const on = state.logs.errorsOnly;
  document.querySelectorAll('#ld-console .ld-line').forEach((el) => {
    el.hidden = on && !el.classList.contains('is-error');
  });
}

async function revealLogsFolder() {
  const slug = state.logs.selected;
  if (!slug) { toast('Select an agent first'); return; }
  try { await api.revealLogs(slug); }
  catch (e) { toast(e.message || 'Could not open the folder', 'danger'); }
}

function openLogsView() {
  closeWorkspaceViews();
  const app = $('#app');
  if (app.classList.contains('is-history')) closeHistoryView();
  const fromSettings = app.classList.contains('is-settings');
  if (app.classList.contains('is-detail')) closeDrawer();
  if (app.classList.contains('is-market')) closeMarketView();
  renderLogs();
  app.classList.add('is-logs');
  setNav('logs');
  // From Settings: let the opaque Logs pane fade in ON TOP first (one fade over a solid base, like the
  // Agents/Logs switch — no grid flash). Then fade Settings out BEHIND it, keeping the inset flag so
  // the pane never expands over the sidebar mid-fade; clear the flag only once it's fully hidden.
  if (fromSettings) setTimeout(() => {
    app.classList.remove('is-settings');
    setTimeout(() => { if (!app.classList.contains('is-settings')) app.classList.remove('is-settings-inset'); }, 380);
  }, 300);
}

function closeLogsView() {
  const app = $('#app');
  app.classList.remove('is-logs');
  if (state.logs.socket) { state.logs.socket.close(); state.logs.socket = null; }
  clearInterval(state.logs.tilePoll); state.logs.tilePoll = null;   // stop the stat-tile poll
  state.logs.selected = null;
  $('#logs-list').replaceChildren();
  $('#ld-console').replaceChildren();
  $('#logs-search').value = '';
  setNav('agents');
  if (app.classList.contains('is-firstrun')) {
    const fr = $('#firstrun');
    fr.classList.remove('is-returning'); void fr.offsetWidth; fr.classList.add('is-returning');
  }
}

function renderSettings(setup) {
  const fw = (setup?.doctor || []).find((c) => c.check === 'import connectonion');
  const fwEl = $('#set-fw');
  if (fwEl) fwEl.textContent = fw?.ok ? `connectonion ${String(fw.detail).replace('version ', '')}` : 'not found';
  const auth = $('#set-auth');
  if (auth) { auth.innerHTML = setup?.co_auth_ok ? 'authenticated <span class="ok-check">✓</span>' : 'run `co auth`'; auth.className = `v ${setup?.co_auth_ok ? 'ok' : 'bad'}`; }
  const key = $('#set-key');
  if (key) { key.innerHTML = setup?.key_ok ? 'present <span class="ok-check">✓</span>' : 'missing'; key.className = `v ${setup?.key_ok ? 'ok' : 'bad'}`; }
  const url = $('#set-url');
  if (url) url.textContent = setup?.manager_url || 'http://127.0.0.1:9900';
  const ver = $('#set-ver');
  if (ver) ver.textContent = setup?.studio_version ? `v${setup.studio_version}` : '—';
  const store = $('#set-storage');
  if (store) store.textContent = setup?.agents_dir || '—';
  resetStorageEdit();
}

// ---- storage location (change + migrate) ----------------------------
function resetStorageEdit() {
  $('#storage-view').hidden = false;
  $('#storage-edit').hidden = true;
  $('#storage-error').hidden = true;
  $('#storage-save').disabled = false;
  $('#storage-save').textContent = 'Move here';
}

function initStorage() {
  $('#storage-change').addEventListener('click', async () => {
    const btn = $('#storage-change');
    btn.disabled = true;
    try {
      const { path } = await api.pickFolder();   // native macOS folder chooser
      $('#storage-input').value = path;
      $('#storage-error').hidden = true;
      $('#storage-view').hidden = true;
      $('#storage-edit').hidden = false;
    } catch (e) {
      if (e.status !== 409) toast(e.message, 'danger');   // 409 = dialog cancelled → ignore
    } finally {
      btn.disabled = false;
    }
  });
  $('#storage-cancel').addEventListener('click', resetStorageEdit);
  $('#storage-save').addEventListener('click', async () => {
    const path = $('#storage-input').value.trim();
    const err = $('#storage-error');
    err.hidden = true;
    if (!path) { err.textContent = 'Enter a folder path.'; err.hidden = false; return; }
    const save = $('#storage-save');
    save.disabled = true; save.textContent = 'Moving…';
    try {
      const res = await api.setStorage(path);
      state.setup = { ...(state.setup || {}), agents_dir: res.agents_dir };
      $('#set-storage').textContent = res.agents_dir;
      resetStorageEdit();
      const n = res.moved;
      toast(n ? `Moved ${n} agent${n > 1 ? 's' : ''} — restart them to bring them back online.`
              : `Storage location updated.`);
      await refreshAgents();
    } catch (e) {
      err.textContent = e.message; err.hidden = false;
      save.disabled = false; save.textContent = 'Move here';
    }
  });
}

function setAgents(list) {
  const prev = new Map(state.agents.map((a) => [a.slug, a.state]));
  state.agents = list;
  renderAgents();
  updateFleetHeader();
  if ($('#app').classList.contains('is-logs')) renderLogs();   // add/remove cards as agents start/stop/crash
  historyView?.setAgents(list); // includes stopped Agents; do not reuse Logs' running-only filter
  approvalsView?.setAgents(list);

  // keep an open drawer in sync; refetch detail when the state flips
  if (state.drawerSlug) {
    const summary = list.find((a) => a.slug === state.drawerSlug);
    if (summary) {
      if (prev.get(summary.slug) !== summary.state || !state.drawerDetail) {
        refreshDrawerDetail();
      }
    }
  }
}

async function refreshAgents() {
  try {
    setAgents(await api.listAgents());
    setConnBanner(false);
  } catch (err) {
    setConnBanner(true);
  }
}

// ---- actions --------------------------------------------------------
const ACTIONS = { start: api.startAgent, stop: api.stopAgent, restart: api.restartAgent };

async function doAction(slug, action) {
  if (state.busy.has(slug)) return;
  state.busy.add(slug);
  const card = state.cards.get(slug);
  if (card) updateCard(card, card._agent);
  syncDrawerButtons();
  try {
    await ACTIONS[action](slug);
  } catch (err) {
    toast(`${action} failed: ${err.message}`, 'danger');
  } finally {
    state.busy.delete(slug);
    await refreshAgents();
    syncDrawerButtons();
  }
}

// ---- connection / doctor banners ------------------------------------
function setConnBanner(broken) {
  $('#conn-banner').hidden = !broken;
}

function renderDoctorBanner(setup) {
  const banner = $('#doctor-banner');
  const failing = (setup?.doctor || []).filter((c) => !c.ok);
  if (!setup || !failing.length) {
    banner.hidden = true;
    return;
  }
  banner.innerHTML = '';
  banner.append('Doctor: ');
  failing.forEach((c, i) => {
    const b = document.createElement('b');
    b.textContent = c.check;
    banner.appendChild(b);
    if (i < failing.length - 1) banner.append(', ');
  });
  banner.append(' failed — the framework may have drifted. Details in an agent’s "Copy for Claude" bundle.');
  banner.hidden = false;
}

// ---- create wizard (Name → Template → Model → configuration) --------
let wizardStep = 0;

const isCoAiTemplate = () => $('#f-template')?.value === 'co-ai';
const visibleWizardSteps = () => isCoAiTemplate() ? [0, 1, 2, 4] : [0, 1, 2, 3, 4];
const wizardPosition = () => Math.max(0, visibleWizardSteps().indexOf(wizardStep));
const isFinalWizardStep = () => wizardPosition() === visibleWizardSteps().length - 1;

const CAPABILITY_RISK = { utility: 0, web: 0, image: 0, files: 1, 'file-write': 2, shell: 2, browser: 2 };
const CAPABILITY_LABEL = {
  utility: 'Utility', web: 'Web fetch', image: 'Image', files: 'File reading',
  'file-write': 'File editing', shell: 'Shell', browser: 'Browser',
};
function accessLabel(agent) {
  return agent?.preset === 'co-ai' || agent?.trust === 'careful' || agent?.trust === 'strict'
    ? 'Invite only'
    : 'Open';
}
const selectedCapabilities = () => [
  'utility',
  ...document.querySelectorAll('#create-form input[name="capability"]:checked'),
].map((item) => typeof item === 'string' ? item : item.value);
const workspaceNeeded = () => isCoAiTemplate()
  || selectedCapabilities().some((name) => ['files', 'file-write', 'shell', 'browser'].includes(name));

function syncWorkspaceField() {
  $('#f-work-dir-wrap').hidden = !workspaceNeeded();
  if (!wizardStacked && wizardStep === 4 && $('#app').classList.contains('is-creating')) paintWizard(true);
}

function customAccessPolicy() {
  const tier = Math.max(...selectedCapabilities().map((name) => CAPABILITY_RISK[name] || 0));
  if (tier === 2) return { tier, trust: 'strict', inviteOnly: true, title: 'Invite-only access', badge: 'Invite only', copy: 'New devices enter your code once.', note: 'This Agent can change local or external state. File changes and side-effecting shell commands may ask for approval; Browser control is protected at connection time.' };
  if (tier === 1) return { tier, trust: 'careful', inviteOnly: true, title: 'Invite-only access', badge: 'Invite only', copy: 'New devices enter your code once.', note: 'Approval is remembered for this device and this Agent only.' };
  if ($('#f-standard-access-invite').checked) return { tier, trust: 'careful', inviteOnly: true, title: 'Invite-only access', badge: 'Invite only', copy: 'New devices enter this code once before connecting.', note: 'Standard capabilities do not read or change local data. Approval is remembered for this device and this Agent only.' };
  return { tier, trust: 'open', inviteOnly: false, title: 'Open access', badge: 'Open', copy: 'Anyone with this Agent address can connect.', note: 'Standard capabilities do not read or change local data.' };
}

function syncCustomAccess() {
  const policy = customAccessPolicy();
  const summary = $('#f-custom-access-summary');
  const standard = policy.tier === 0;
  $('#f-standard-access-options').hidden = !standard;
  summary.hidden = standard;
  summary.dataset.risk = policy.trust;
  $('#f-custom-access-title').textContent = policy.title;
  $('#f-custom-access-copy').textContent = policy.copy;
  $('#f-custom-access-note').textContent = policy.note;
  const badge = $('#f-custom-access-badge');
  badge.textContent = policy.badge;
  badge.className = `risk-badge ${policy.tier === 0 ? 'is-safe' : 'is-strict'}`;
  $('#f-custom-invite-wrap').hidden = !policy.inviteOnly;
  if (!wizardStacked && wizardStep === 4 && $('#app').classList.contains('is-creating')) paintWizard(true);
  return policy;
}

function stepHeight(n) {
  const step = $(`.wizard-step[data-step="${n}"]`);
  return step ? step.offsetHeight : 0;
}

const CREATE_VIEW_PAD = 64;   // #create-view top+bottom padding (32px each)
// the stage is `overflow: clip`; heights come out fractional (font metrics) and
// offsetHeight rounds down, so snapping the stage to the exact step height shaves
// the last input's bottom border. A few px of bleed keeps the clip edge below it.
const STAGE_BLEED = 4;

function paintWizard(animateHeight = true) {
  const stage = $('.wizard-stage');
  const app = $('#app');
  document.querySelectorAll('.wizard-step').forEach((s, i) => s.classList.toggle('is-active', i === wizardStep));

  const targetStage = stepHeight(wizardStep) + STAGE_BLEED;

  // size the whole card to wrap the wizard: measure the inner with the stage snapped
  // to its target, then restore so the stage's own transition animates from current
  let targetCardH = 0;
  if (app.classList.contains('is-creating')) {
    const inner = $('.wizard-inner');
    const savedTrans = stage.style.transition, savedH = stage.style.height;
    stage.style.transition = 'none';
    stage.style.height = `${targetStage}px`;
    targetCardH = inner.offsetHeight + CREATE_VIEW_PAD;
    stage.style.height = savedH;
    stage.offsetHeight;
    stage.style.transition = savedTrans;
  }

  if (!animateHeight) { stage.style.transition = 'none'; }
  stage.style.height = `${targetStage}px`;
  if (!animateHeight) { stage.offsetHeight; stage.style.transition = ''; }  // commit, then restore

  if (targetCardH) app.style.height = `${targetCardH}px`;   // card follows the content (CSS-animated)

  const steps = visibleWizardSteps();
  const position = wizardPosition();
  document.querySelectorAll('.wdot').forEach((d, i) => {
    d.hidden = i >= steps.length;
    d.classList.toggle('is-active', i === position);
  });
  $('#wizard-back').textContent = wizardStep === 0 ? 'Cancel' : 'Back';
  const last = isFinalWizardStep();
  $('#wizard-next').hidden = last;
  $('#create-submit').hidden = !last;
}

function goWizard(n) {
  const steps = visibleWizardSteps();
  wizardStep = steps.includes(n) ? n : steps[0];
  paintWizard(true);
  const focusable = $(`.wizard-step[data-step="${wizardStep}"]`)?.querySelector('input, select');
  if (focusable) setTimeout(() => focusable.focus(), 80);
}

function moveWizard(direction) {
  const steps = visibleWizardSteps();
  const target = Math.max(0, Math.min(steps.length - 1, wizardPosition() + direction));
  goWizard(steps[target]);
}

function syncTemplateFields({ resetModel = false } = {}) {
  const coAi = isCoAiTemplate();
  $('#create-toolkit-step').hidden = coAi;
  $('#f-custom-access').hidden = coAi;
  $('#f-co-ai-access').hidden = !coAi;
  const templateHelp = $('#f-template-help');
  templateHelp.textContent = coAi
    ? 'A stateful coding agent with full tools and persistent context.'
    : 'A lightweight agent with the capabilities you choose.';
  $('#create-view').classList.toggle('is-co-ai', coAi);
  syncCustomAccess();
  syncWorkspaceField();
  if (resetModel) {
    $('#f-model').value = 'co/gemini-3.5-flash';
    $('#f-model-custom-wrap').hidden = true;
  }
  if (coAi && wizardStep === 3) wizardStep = 4;
  if (!wizardStacked && $('#app').classList.contains('is-creating')) paintWizard(true);
}

// mirrors the backend slug rule (creator.slugify): lowercase, non-alphanumeric → '-'
const slugify = (name) => name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');

function nameStatus(raw, excludeSlug = null) {
  const name = (raw || '').trim();
  if (!name) return { kind: 'empty' };
  const slug = slugify(name);
  if (!slug) return { kind: 'bad', msg: 'Use letters or numbers in the name.' };
  if (state.agents.some((a) => a.slug !== excludeSlug && slugify(a.name) === slug)) {
    return { kind: 'bad', msg: 'An agent with that name already exists.' };
  }
  return { kind: 'ok' };
}

function updateNameStatus() {
  const el = $('#f-name-status');
  const wrap = el?.closest('.input-wrap');
  if (!el || !wrap) return;
  const s = nameStatus($('#f-name').value);
  if (s.kind === 'ok') {
    el.textContent = '✓'; el.className = 'input-status ok'; el.hidden = false; el.removeAttribute('title'); wrap.classList.add('has-status');
  } else if (s.kind === 'bad') {
    el.textContent = '✕'; el.className = 'input-status bad'; el.hidden = false; el.title = s.msg; wrap.classList.add('has-status');
  } else {
    el.hidden = true; wrap.classList.remove('has-status');
  }
}

function validateStep(n) {
  const errEl = $('#create-error');
  errEl.hidden = true;
  if (n === 0) {
    const s = nameStatus($('#f-name').value);
    if (s.kind !== 'ok') {
      errEl.textContent = s.kind === 'empty' ? 'Give the agent a name.' : s.msg;
      errEl.hidden = false; return false;
    }
  }
  if (n === 2) {
    let model = $('#f-model').value;
    if (model === '__custom') model = $('#f-model-custom').value.trim();
    if (!model) { errEl.textContent = 'Pick or type a model.'; errEl.hidden = false; return false; }
  }
  if (n === 4) {
    const coAi = isCoAiTemplate();
    const policy = customAccessPolicy();
    const code = coAi ? $('#f-invite-code').value.trim() : $('#f-custom-invite-code').value.trim();
    if (!coAi && !policy.inviteOnly) return true;
    const codeError = inviteCodeError(code);
    if (codeError) {
      errEl.textContent = codeError;
      errEl.hidden = false; return false;
    }
  }
  return true;
}

function inviteCodeError(code) {
  if (!code) return 'Create an invite code before continuing.';
  if (!/^[A-Za-z0-9_-]{4,64}$/.test(code)) return 'Use 4–64 letters, numbers, hyphens, or underscores for the invite code.';
  return '';
}

let wizardStacked = false;
function openCreateModal(stacked = false) {
  if ($('#app').classList.contains('is-knowledge') || $('#app').classList.contains('is-approvals')) {
    closeWorkspaceViews(); setTimeout(() => openCreateModal(stacked), 300); return;
  }
  if ($('#app').classList.contains('is-history')) {
    closeHistoryView();
    setTimeout(() => openCreateModal(stacked), 300);
    return;
  }
  // From Settings → first crossfade back to the agents view, THEN open the wizard,
  // so it's a smooth Settings → Agents → New Agent sequence (never an overlap).
  if ($('#app').classList.contains('is-settings')) {
    closeSettingsModal();
    setTimeout(() => openCreateModal(stacked), 300);
    return;
  }
  if ($('#app').classList.contains('is-logs')) {   // same as Settings: crossfade back to Agents first, then the wizard
    closeLogsView();
    setTimeout(() => openCreateModal(stacked), 300);
    return;
  }
  if ($('#app').classList.contains('is-market')) {   // same: crossfade back to Agents, then open the wizard
    closeMarketView();
    setTimeout(() => openCreateModal(stacked), 300);
    return;
  }
  $('#create-form').reset();
  $('#f-model-custom-wrap').hidden = true;
  syncTemplateFields();
  $('#create-error').hidden = true;
  $('#create-submit').disabled = false;
  if (createBtnHTML) $('#create-submit').innerHTML = createBtnHTML;
  wizardStep = 0;
  wizardStacked = stacked;
  $('#create-view').classList.toggle('stacked', stacked);
  updateNameStatus();                        // clear the ✓/✕ from any prior run
  wizardFullH = $('#app').offsetHeight;      // remember the full card height to grow back to
  $('#app').classList.add('is-creating');   // slide the wizard in over the card
  if (stacked) {                             // main-interface entry: all visible steps in one scrollable form
    $('#app').style.height = '';             // full card (no per-step sizing); the form scrolls inside
    $('#wizard-back').textContent = 'Cancel';
    $('#wizard-next').hidden = true;
    $('#create-submit').hidden = false;
  } else {
    paintWizard(false);                      // first-run entry: the compact step-by-step wizard
  }
  $('#f-name').focus();
}

let wizardFullH = 0;
function closeCreateModal() {
  const app = $('#app');
  app.classList.remove('is-creating');
  $('#create-view').classList.remove('stacked');
  if (wizardFullH) {                          // animate the card back to full size, then release it
    app.style.height = `${wizardFullH}px`;
    setTimeout(() => { app.style.height = ''; }, 500);
  }
  if ($('#app').classList.contains('is-firstrun')) {   // returning to first-run → replay its entrance
    const fr = $('#firstrun');
    fr.classList.remove('is-returning');
    void fr.offsetWidth;                                // restart the animation
    fr.classList.add('is-returning');
  }
}

let createBtnHTML = '';   // the CTA's default markup (sparkle icon + "Create Now")
function initCreateModal() {
  $('#app').appendChild($('#create-view'));  // relocate the wizard inside the card
  $('#create-view').hidden = false;          // CSS opacity/visibility drives it from here
  createBtnHTML = $('#create-submit').innerHTML;
  $('#btn-new-agent').addEventListener('click', () => openCreateModal(true));        // main interface → stacked form
  $('#btn-new-agent-first').addEventListener('click', () => openCreateModal(false)); // first-run → step-by-step
  $('#wizard-back').addEventListener('click', () => {
    if (wizardStacked || wizardStep === 0) closeCreateModal();
    else moveWizard(-1);
  });
  $('#wizard-back-top').addEventListener('click', closeCreateModal);   // pinned top-left Back (stacked wizard)
  $('#wizard-next').addEventListener('click', () => {
    if (validateStep(wizardStep)) moveWizard(1);
  });
  $('#f-template').addEventListener('change', () => {
    $('#create-error').hidden = true;
    syncTemplateFields({ resetModel: true });
  });
  document.querySelectorAll('#create-form input[name="capability"]').forEach((checkbox) => {
    checkbox.addEventListener('change', () => {
      // File editing already includes read/search; keep the choices mutually exclusive.
      if (checkbox.checked && checkbox.value === 'file-write') $('#create-form input[value="files"]').checked = false;
      if (checkbox.checked && checkbox.value === 'files') $('#create-form input[value="file-write"]').checked = false;
      syncCustomAccess();
      syncWorkspaceField();
    });
  });
  document.querySelectorAll('#create-form input[name="standard-access"]').forEach((radio) => {
    radio.addEventListener('change', () => {
      $('#create-error').hidden = true;
      syncCustomAccess();
    });
  });
  $('#f-model').addEventListener('change', (e) => {
    const custom = e.target.value === '__custom';
    $('#f-model-custom-wrap').hidden = !custom;
    if (!wizardStacked) paintWizard(true);   // wizard: re-fit the step height (stacked just flows)
    if (custom) $('#f-model-custom').focus();
  });
  $('#f-work-dir-pick').addEventListener('click', async () => {
    try {
      const { path } = await api.pickWorkspace();
      $('#f-work-dir').value = path;
      $('#create-error').hidden = true;
    } catch (err) {
      if (err.status !== 409) toast(err.message || 'Could not choose the workspace', 'danger');
    }
  });
  // live name check: clear the error and re-flag ✓/✕ as they type
  $('#f-name').addEventListener('input', () => {
    $('#create-error').hidden = true;
    updateNameStatus();
  });
  // Enter in a text field advances the wizard (stacked form: don't submit early)
  const advanceOnEnter = (e) => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    if (wizardStacked) return;
    if (validateStep(wizardStep)) moveWizard(1);
  };
  $('#f-name').addEventListener('keydown', advanceOnEnter);
  $('#f-model-custom').addEventListener('keydown', advanceOnEnter);

  $('#create-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    // step-by-step wizard: Enter on an intermediate control (e.g. the Model
    // <select>, an Access radio) fires an implicit submit — treat it as "Next"
    // until the final step, so it never creates the agent early.
    if (!wizardStacked && !isFinalWizardStep()) {
      if (validateStep(wizardStep)) moveWizard(1);
      return;
    }
    const errEl = $('#create-error');
    errEl.hidden = true;

    const name = $('#f-name').value.trim();
    let model = $('#f-model').value;
    if (model === '__custom') model = $('#f-model-custom').value.trim();
    const preset = $('#f-template').value;
    const coAi = preset === 'co-ai';
    const capabilities = coAi ? [] : selectedCapabilities();
    const policy = customAccessPolicy();
    const trust = coAi ? 'strict' : policy.trust;
    const invite_code = coAi
      ? $('#f-invite-code').value.trim()
      : policy.inviteOnly ? $('#f-custom-invite-code').value.trim() : null;
    const work_dir = workspaceNeeded() ? ($('#f-work-dir').value.trim() || null) : null;

    if (!name) { errEl.textContent = 'Give the agent a name.'; errEl.hidden = false; if (!wizardStacked) goWizard(0); return; }
    if (!model) { errEl.textContent = 'Pick or type a model.'; errEl.hidden = false; if (!wizardStacked) goWizard(2); return; }
    const codeError = coAi || policy.inviteOnly ? inviteCodeError(invite_code) : '';
    if (codeError) {
      errEl.textContent = codeError;
      errEl.hidden = false; if (!wizardStacked) goWizard(4); return;
    }

    const submit = $('#create-submit');
    submit.disabled = true;
    submit.textContent = 'Creating…';
    try {
      const detail = await api.createAgent({ name, model, capabilities, trust, preset, invite_code, work_dir });
      closeCreateModal();
      await refreshAgents();
      toast(`${detail.name} created — QR ready. Press Start to bring it online.`);
    } catch (err) {
      errEl.textContent = err.message;
      errEl.hidden = false;
      submit.disabled = false;
      submit.innerHTML = createBtnHTML;
    }
  });
}

// ---- QR modal -------------------------------------------------------
function openQrModal(agent) {
  $('#qr-img').src = api.qrUrl(agent.slug);
  $('#qr-name').textContent = agent.name;
  $('#qr-addr').textContent = agent.address;
  $('#modal-qr').hidden = false;
}

function initQrModal() {
  $('#qr-close').addEventListener('click', () => { $('#modal-qr').hidden = true; });
  $('#modal-qr').addEventListener('click', (e) => {
    if (e.target === e.currentTarget) e.currentTarget.hidden = true;
  });
}

// ---- detail drawer --------------------------------------------------
function infoRow(label, valueNode) {
  const row = document.createElement('div');
  row.className = 'info-row';
  const k = document.createElement('span');
  k.className = 'k';
  k.textContent = label;
  const v = document.createElement('span');
  v.className = 'v';
  if (typeof valueNode === 'string') v.textContent = valueNode;
  else v.appendChild(valueNode);
  row.append(k, v);
  return row;
}

function copyableValue(text) {
  const wrap = document.createElement('span');
  wrap.style.cssText = 'display:inline-flex; align-items:center; gap:4px; min-width:0;';
  const code = document.createElement('code');
  // truncate the FRONT (…), keep the tail (filename) visible: direction:rtl puts the ellipsis on
  // the left; a leading LRM keeps the path's own "/" characters in LTR order (no stray trailing "/").
  code.textContent = '‎' + text;
  code.style.cssText = 'min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; direction:rtl; text-align:left;';
  const btn = document.createElement('button');
  btn.className = 'row-copy';
  btn.title = 'Copy';
  btn.setAttribute('aria-label', 'Copy');
  btn.innerHTML = ICON.copy;
  btn.addEventListener('click', (e) => { e.stopPropagation(); copyWithFeedback(btn, text); });
  wrap.append(code, btn);
  return wrap;
}

function statusValue(stateStr) {
  const v = document.createElement('span');
  const dot = document.createElement('span');
  dot.className = 'd-dot';
  const word = document.createElement('span');
  word.textContent = capitalize(stateStr);
  if (stateStr === 'online') { dot.style.background = 'var(--ok)'; v.style.color = 'var(--ok)'; }
  else if (stateStr === 'crashed') { dot.style.background = 'var(--danger)'; v.style.color = 'var(--danger)'; }
  v.append(dot, word);
  return v;
}
function idValue(address) {
  const v = document.createElement('span');
  const code = document.createElement('code');
  code.textContent = shortAddr(address);
  const btn = document.createElement('button');
  btn.className = 'row-copy';
  btn.title = 'Copy ID';
  btn.setAttribute('aria-label', 'Copy ID');
  btn.innerHTML = ICON.copy;
  btn.addEventListener('click', (e) => { e.stopPropagation(); copyWithFeedback(btn, address); });
  v.append(code, btn);
  return v;
}
function setRow(label, value) {
  const row = document.createElement('div');
  row.className = 'set-row';
  const k = document.createElement('span'); k.className = 'k'; k.textContent = label;
  const v = document.createElement('span'); v.className = 'v';
  if (value == null || value === '') v.textContent = '—';
  else if (typeof value === 'string') v.textContent = value;
  else v.appendChild(value);
  row.append(k, v);
  return row;
}
function detailGroup(title, rows) {
  const g = document.createElement('div');
  g.className = 'detail-group';
  const h = document.createElement('h3'); h.textContent = title;
  g.appendChild(h);
  for (const [label, value] of rows) g.appendChild(setRow(label, value));
  return g;
}
const SKILL_CATEGORY_LABEL = {
  'version-control': 'Version control',
  'code-quality': 'Code quality',
  'debug-understand': 'Debug & understand',
  'delivery-ops': 'Delivery & ops',
  'docs-security': 'Docs & security',
};

function skillCategoryLabel(cat) {
  return SKILL_CATEGORY_LABEL[cat]
    || String(cat || 'general').replace(/[-_]/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

// The per-agent "Skills" panel in the drawer (co-ai only) is READ-ONLY: it shows which skills the agent
// has; adding/removing happens in the sidebar Skills Market. Renders from the state.drawerSkills cache.
function skillsGroup(detail) {
  if (detail.preset !== 'co-ai') return null;
  const g = document.createElement('div');
  g.className = 'detail-group d-skills';
  const h = document.createElement('h3');
  h.textContent = 'Skills';
  g.appendChild(h);

  const data = state.drawerSkills && state.drawerSkills.slug === detail.slug ? state.drawerSkills : null;
  const note = document.createElement('p');
  note.className = 'd-skills-note';
  if (!data) {
    note.textContent = 'Loading skills…';
    g.appendChild(note);
    return g;
  }

  const marketLink = document.createElement('a');
  marketLink.className = 'd-skills-link';
  marketLink.setAttribute('role', 'button');
  marketLink.tabIndex = 0;
  marketLink.textContent = 'Skills Market →';
  marketLink.addEventListener('click', (e) => { e.stopPropagation(); openMarketView(); });

  const installed = data.installed || [];
  const titleByName = new Map((data.catalog || []).map((s) => [s.name, s.title]));
  if (!installed.length) {
    note.append('No skills yet — add some in the ', marketLink);
    g.appendChild(note);
    return g;
  }
  note.append(`${installed.length} installed · manage in the `, marketLink);
  g.appendChild(note);

  const chips = document.createElement('div');
  chips.className = 'd-skill-chips';
  for (const name of installed) {
    const chip = document.createElement('span');
    chip.className = 'd-skill-chip';
    chip.textContent = titleByName.get(name) || name;
    chips.appendChild(chip);
  }
  g.appendChild(chips);
  return g;
}

// ---- Skills Market (sidebar view) -------------------------------------------------------
function openMarketView() {
  closeWorkspaceViews();
  const app = $('#app');
  if (app.classList.contains('is-history')) closeHistoryView();
  if (app.classList.contains('is-detail')) closeDrawer();
  if (app.classList.contains('is-logs')) closeLogsView();
  if (app.classList.contains('is-settings')) closeSettingsModal();
  app.classList.add('is-market');
  setNav('skills');
  renderMarket();
}

function closeMarketView() {
  $('#app').classList.remove('is-market');
  setNav('agents');
}

async function renderMarket(preloaded) {
  const grid = $('#market-grid');
  const empty = $('#market-empty');
  if (preloaded) {
    state.market = preloaded;
  } else {
    grid.replaceChildren();
    try {
      state.market = await api.getMarket();
    } catch (err) {
      empty.hidden = false;
      empty.textContent = `Couldn't load the Skills Market: ${err.message}`;
      return;
    }
  }
  const catalog = state.market.catalog || [];
  const agents = state.market.agents || [];
  grid.replaceChildren();
  if (!catalog.length) {
    empty.hidden = false;
    empty.textContent = 'No skills in the catalog.';
    return;
  }
  empty.hidden = true;
  for (const skill of catalog) grid.appendChild(skillCard(skill, agents));
}

function skillCard(skill, agents) {
  const on = agents.filter((a) => (a.installed || []).includes(skill.name)).length;
  const card = document.createElement('div');
  card.className = 'skill-card';
  card.dataset.name = skill.name;
  const inner = document.createElement('div');
  inner.className = 'skill-card-inner';

  const front = document.createElement('div');
  front.className = 'skill-card-face skill-card-front';
  const cat = document.createElement('div');
  cat.className = 'skill-cat';
  cat.textContent = skillCategoryLabel(skill.category);
  const title = document.createElement('div');
  title.className = 'skill-title';
  title.textContent = skill.title;
  const desc = document.createElement('div');
  desc.className = 'skill-desc';
  desc.textContent = skill.description;
  const foot = document.createElement('div');
  foot.className = 'skill-foot';
  const count = document.createElement('span');
  count.className = 'skill-on-count';
  count.textContent = on ? `On ${on} agent${on > 1 ? 's' : ''}` : 'Not installed';
  const addBtn = document.createElement('button');
  addBtn.type = 'button';
  addBtn.className = 'btn btn-sm btn-primary skill-add';
  addBtn.textContent = 'Add';
  addBtn.addEventListener('click', () => card.classList.add('is-flipped'));
  foot.append(count, addBtn);
  front.append(cat, title, desc, foot);

  const back = document.createElement('div');
  back.className = 'skill-card-face skill-card-back';
  back.appendChild(skillCardBack(skill, agents, card));

  inner.append(front, back);
  card.appendChild(inner);
  return card;
}

function skillCardBack(skill, agents, card) {
  const wrap = document.createElement('div');
  wrap.className = 'skill-back';

  const head = document.createElement('div');
  head.className = 'skill-back-head';
  const htitle = document.createElement('span');
  htitle.className = 'skill-back-title';
  htitle.textContent = 'Add to agents';
  head.appendChild(htitle);
  wrap.appendChild(head);

  const list = document.createElement('div');
  list.className = 'skill-agent-list';
  if (!agents.length) {
    const none = document.createElement('p');
    none.className = 'skill-agent-none';
    none.textContent = 'No co-ai agents yet — create one first.';
    list.appendChild(none);
  } else {
    for (const a of agents) {
      const isOn = (a.installed || []).includes(skill.name);
      const row = document.createElement('div');
      row.className = 'skill-agent-row' + (isOn ? ' is-installed' : '');
      row.dataset.slug = a.slug;
      const check = document.createElement('span');
      check.className = 'skill-agent-check';
      check.textContent = isOn ? '✓' : '';
      const name = document.createElement('span');
      name.className = 'skill-agent-name';
      name.textContent = a.name;
      row.append(check, name);
      if (isOn) {
        const badge = document.createElement('span');
        badge.className = 'skill-agent-badge';
        badge.textContent = 'installed';
        row.appendChild(badge);
      } else {
        row.addEventListener('click', () => {
          const checked = row.classList.toggle('is-checked');
          check.textContent = checked ? '✓' : '';
          updateSubmitCount(wrap);
        });
      }
      list.appendChild(row);
    }
  }
  wrap.appendChild(list);

  const foot = document.createElement('div');
  foot.className = 'skill-back-foot';
  const cancel = document.createElement('button');
  cancel.type = 'button';
  cancel.className = 'btn btn-sm btn-ghost';
  cancel.textContent = 'Cancel';
  cancel.addEventListener('click', () => card.classList.remove('is-flipped'));
  const submit = document.createElement('button');
  submit.type = 'button';
  submit.className = 'btn btn-sm btn-primary skill-submit';
  submit.textContent = 'Add to 0';
  submit.disabled = true;
  submit.addEventListener('click', () => submitSkill(skill, wrap));
  foot.append(cancel, submit);
  wrap.appendChild(foot);
  return wrap;
}

function updateSubmitCount(backWrap) {
  const n = backWrap.querySelectorAll('.skill-agent-row.is-checked').length;
  const submit = backWrap.querySelector('.skill-submit');
  submit.textContent = `Add to ${n}`;
  submit.disabled = n === 0;
}

async function submitSkill(skill, backWrap) {
  const slugs = [...backWrap.querySelectorAll('.skill-agent-row.is-checked')].map((r) => r.dataset.slug);
  if (!slugs.length) return;
  const submit = backWrap.querySelector('.skill-submit');
  submit.disabled = true;
  submit.textContent = 'Adding…';
  try {
    const res = await api.installBulk(skill.name, slugs);
    const n = (res.installed || []).length;
    const running = (res.agents || []).filter((a) => slugs.includes(a.slug) && a.running).length;
    toast(`Added ${skill.title} to ${n} agent${n === 1 ? '' : 's'}${running ? ` · restart ${running} to apply` : ''}`);
    renderMarket({ catalog: state.market.catalog, agents: res.agents || state.market.agents });
  } catch (err) {
    submit.disabled = false;
    updateSubmitCount(backWrap);
    toast(`Add failed: ${err.message}`, 'danger');
  }
}

async function loadDrawerSkills(slug) {
  try {
    const data = await api.getAgentSkills(slug);
    if (state.drawerSlug !== slug) return;
    state.drawerSkills = { ...data, slug };
    if (state.drawerDetail && state.drawerDetail.slug === slug) renderDrawerFields(state.drawerDetail);
  } catch { /* leave the "Loading skills…" note; best-effort like the rest of the drawer */ }
}

function renderDrawerFields(detail) {
  $('#d-name').textContent = detail.name;
  const badge = $('#d-state');
  badge.textContent = capitalize(detail.state);
  badge.dataset.state = detail.state;

  $('#d-qr').src = api.qrUrl(detail.slug);
  $('#d-addr').textContent = detail.address;

  const info = $('#d-info');
  info.textContent = '';
  const configuration = [
    ['Template', detail.preset === 'co-ai' ? 'co ai' : 'Custom Agent'],
    ['Model', detail.model],
    ['Port', String(detail.port)],
    ['Capabilities', detail.preset === 'co-ai'
      ? 'Full coding toolkit'
      : (detail.capabilities || detail.toolkits || []).map((name) => CAPABILITY_LABEL[name] || name).join(' · ') || '—'],
    ['Access', accessLabel(detail)],
  ];
  if (detail.invite_code) {
    configuration.push(['Invite code', copyableValue(detail.invite_code)]);
  }
  info.appendChild(detailGroup('Configuration', configuration));
  info.appendChild(detailGroup('Runtime', [
    ['Status', statusValue(detail.state)],
    ['Relay', detail.relay_ok === true ? 'connected' : detail.relay_ok === false ? 'not connected' : '—'],
    ['Endpoints', detail.endpoints_announced == null ? '—' : `${detail.endpoints_announced} announced`],
  ]));
  const paths = [];
  if (detail.script_path) paths.push(['Script', copyableValue(detail.script_path)]);
  if (detail.work_dir) paths.push(['Workspace', copyableValue(detail.work_dir)]);
  if (detail.co_dir) paths.push(['co_dir', copyableValue(detail.co_dir)]);
  if (paths.length) info.appendChild(detailGroup('Paths', paths));
  info.appendChild(detailGroup('Identity', [
    ['ID', idValue(detail.address)],
    ['Created', detail.created_at || '—'],
  ]));

  const skills = skillsGroup(detail);
  if (skills) info.appendChild(skills);

  const errBox = $('#d-last-error');
  if (detail.last_error) { errBox.textContent = detail.last_error; errBox.hidden = false; }
  else errBox.hidden = true;

  syncDrawerButtons();
}

function syncDrawerButtons() {
  const detail = state.drawerDetail;
  if (!detail || !$('#app').classList.contains('is-detail')) return;
  const summary = state.agents.find((a) => a.slug === detail.slug);
  const stateNow = summary ? summary.state : detail.state;
  const busy = state.busy.has(detail.slug);

  const toggle = $('#d-toggle');
  toggle.classList.remove('btn-primary', 'btn-danger-solid');   // Start=violet, Stop=red, both solid
  if (stateNow === 'starting' || stateNow === 'creating') {
    toggle.innerHTML = '<span>Starting…</span>';
    toggle.disabled = true;
  } else if (stateNow === 'online') {
    toggle.innerHTML = `${ICON.stop}<span>Stop</span>`;
    toggle.classList.add('btn-danger-solid');
    toggle.disabled = busy;
  } else {
    toggle.innerHTML = `${ICON.play}<span>Start</span>`;
    toggle.classList.add('btn-primary');
    toggle.disabled = busy;
  }
  $('#d-restart').disabled = busy || stateNow === 'creating';

  const badge = $('#d-state');
  badge.textContent = capitalize(stateNow);
  badge.dataset.state = stateNow;
}

async function refreshDrawerDetail() {
  if (!state.drawerSlug) return;
  try {
    const detail = await api.getAgent(state.drawerSlug);
    if (detail.slug !== state.drawerSlug) return; // drawer moved on
    state.drawerDetail = detail;
    renderDrawerFields(detail);
  } catch { /* summary keeps the badge fresh; detail refresh is best-effort */ }
}

async function openDrawer(slug, prefetched = null) {
  promptEditor?.close();
  toolConfig?.close();
  state.drawerSlug = slug;
  state.drawerDetail = null;
  state.drawerSkills = null;
  closeAllCardMenus();
  $('#d-name').textContent = '…';
  $('#app').classList.add('is-detail');          // swap the detail in over the card

  const detail = prefetched || await api.getAgent(slug).catch((err) => {
    toast(`Load failed: ${err.message}`, 'danger');
    return null;
  });
  if (!detail || state.drawerSlug !== slug) return;
  state.drawerDetail = detail;
  renderDrawerFields(detail);
  void promptEditor.open(slug);
  void toolConfig.open(slug);
  if (detail.preset === 'co-ai') loadDrawerSkills(slug);
}

function closeDrawer() {
  promptEditor?.close();
  toolConfig?.close();
  state.drawerSlug = null;
  state.drawerDetail = null;
  state.drawerSkills = null;
  $('#app').classList.remove('is-detail');
  resetDeleteButton();
}

function resetDeleteButton() {
  const btn = $('#d-delete');
  const label = $('span', btn);
  if (label) label.textContent = 'Delete Agent';   // keep the trash icon; only swap the label
  btn.classList.remove('btn-danger-solid');         // .detail-delete is the default outlined state
  clearTimeout(btn._confirmTimer);
  delete btn.dataset.confirming;
}

function initDrawer() {
  promptEditor = createPromptEditor({ root: $('#prompt-editor'), api });
  toolConfig = createToolConfig({ root: $('#tool-config'), api });
  $('#app').appendChild($('#detail-view'));   // relocate the detail overlay inside the card
  $('#detail-view').hidden = false;           // CSS visibility/opacity drives it now
  $('#d-close').addEventListener('click', closeDrawer);

  // click blank space (not the panel, not a card) collapses the detail
  document.addEventListener('click', (e) => {
    if (!$('#app').classList.contains('is-detail')) return;
    if (e.target.closest('.detail-panel')) return;   // clicks inside the panel keep it open
    if (e.target.closest('.agent-card')) return;      // let cards open / switch the detail
    closeDrawer();
  });

  $('#d-copy-addr').addEventListener('click', (e) => {
    // The copy feedback swaps this button's content, detaching the clicked node — so the doc-level
    // close-handler's `e.target.closest('.detail-panel')` guard would miss and collapse the drawer.
    e.stopPropagation();
    if (state.drawerDetail) copyWithFeedback(e.currentTarget, state.drawerDetail.address);
  });

  $('#d-toggle').addEventListener('click', () => {
    if (!state.drawerDetail) return;
    const summary = state.agents.find((a) => a.slug === state.drawerSlug);
    const stateNow = summary ? summary.state : state.drawerDetail.state;
    doAction(state.drawerSlug, stateNow === 'online' ? 'stop' : 'start');
  });
  $('#d-restart').addEventListener('click', () => {
    if (state.drawerSlug) doAction(state.drawerSlug, 'restart');
  });

  // two-step confirm, then a PERMANENT delete (rmtree — identity/keys/logs, unrecoverable)
  $('#d-delete').addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    if (!state.drawerSlug) return;
    if (!btn.dataset.confirming) {
      btn.dataset.confirming = '1';
      $('span', btn).textContent = 'Delete permanently?';
      btn.classList.remove('btn-danger-ghost');
      btn.classList.add('btn-danger-solid');
      btn._confirmTimer = setTimeout(resetDeleteButton, 4000);
      return;
    }
    clearTimeout(btn._confirmTimer);
    const slug = state.drawerSlug;
    const name = state.drawerDetail?.name || slug;
    btn.disabled = true;
    try {
      await api.deleteAgent(slug);
      closeDrawer();
      await refreshAgents();
      toast(`${name} deleted`);
    } catch (err) {
      toast(`Delete failed: ${err.message}`, 'danger');
      resetDeleteButton();
    } finally {
      btn.disabled = false;
    }
  });
}

// ---- onboarding -----------------------------------------------------
function renderDoctorList(setup) {
  const list = $('#onboarding-doctor');
  list.textContent = '';
  const rows = [
    { check: 'co auth (managed key)', ok: !!setup?.co_auth_ok },
    { check: 'model key in ~/.co/keys.env', ok: !!setup?.key_ok },
    { check: 'connectonion framework', ok: !!setup?.framework_ok },
    ...(setup?.doctor || []).map((c) => ({ check: c.check, ok: c.ok, detail: c.detail })),
  ];
  for (const row of rows) {
    const li = document.createElement('li');
    li.className = row.ok ? 'ok' : 'fail';
    const mark = document.createElement('span');
    mark.className = 'mark';
    mark.textContent = row.ok ? '✓' : '✗';
    const label = document.createElement('span');
    label.textContent = row.check;
    li.append(mark, label);
    if (row.detail && !row.ok) {
      const detail = document.createElement('span');
      detail.className = 'detail';
      detail.textContent = ` — ${row.detail}`;
      li.appendChild(detail);
    }
    list.appendChild(li);
  }
}

function stopOnboardingPoll() {
  clearInterval(state.onboardingTimer);
  state.onboardingTimer = null;
}

function showOnboarding(setup) {
  state.onboardingActive = true;
  $('#app').hidden = true;
  const section = $('#onboarding');
  if (!section.dataset.built) {
    $('#onboarding-onion').appendChild(
      createOnion({ size: 120, mode: 'hero', label: 'ConnectOnion' }),
    );
    $('#onboarding-wait-onion').appendChild(
      createOnion({ size: 26, mode: 'thinking', label: 'waiting' }),
    );
    $('#onboarding-copy').addEventListener('click', (e) => {
      copyWithFeedback(e.currentTarget, 'co auth');
    });
    section.dataset.built = '1';
  }
  renderDoctorList(setup);
  section.hidden = false;

  stopOnboardingPoll();
  state.onboardingTimer = setInterval(async () => {
    let s;
    try {
      s = await api.setupStatus();
    } catch {
      return; // backend blip — keep polling
    }
    state.setup = s;
    renderDoctorList(s);
    if (s.co_auth_ok) {
      stopOnboardingPoll();
      state.onboardingActive = false;
      section.hidden = true;
      renderDoctorBanner(s);
      await playSplash($('#splash'));   // success → replay the splash
      refreshAgents();
      revealApp();                      // then crossfade splash → shell
    }
  }, 2000);
}

// ---- viewport gate (too small → onion + resize prompt) --------------
const MIN_W = 1160;   // the card is a fixed 1120×760 now; gate the app until the window can hold it
const MIN_H = 800;
const GATE_PHRASES = [
  'ConnectOnion Studio needs a little more room.',
  'Resize your window larger to continue.',
];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
let gateBuilt = false;
let gateTyping = false;
let gateToken = 0;

async function runGateTyper() {
  const token = ++gateToken;
  const el = $('#gate-type');
  if (!el) return;
  let i = 0;
  while (token === gateToken) {
    const phrase = GATE_PHRASES[i % GATE_PHRASES.length];
    for (let c = 1; c <= phrase.length; c++) {          // type in
      if (token !== gateToken) return;
      el.textContent = phrase.slice(0, c);
      await wait(46);
    }
    await wait(1500);                                    // hold
    for (let c = phrase.length - 1; c >= 0; c--) {       // backspace out (fast)
      if (token !== gateToken) return;
      el.textContent = phrase.slice(0, c);
      await wait(20);
    }
    await wait(360);
    i++;
  }
}
function stopGateTyper() {
  gateToken++;   // leave the last frame in place so it fades out with the gate
}

function checkViewport() {
  // Native shell (?desktop=1) fills the window fluidly and has no minimum size —
  // never gate it (the gate's MIN_W/MIN_H are for the fixed 1120×760 web card).
  if (document.documentElement.classList.contains('desktop')) return;
  const small = window.innerWidth < MIN_W || window.innerHeight < MIN_H;
  if (small && !gateBuilt) {
    $('#gate-onion').appendChild(createOnion({ size: 104, mode: 'assemble', label: 'ConnectOnion' }));
    gateBuilt = true;
  }
  $('#viewport-gate').classList.toggle('is-shown', small);
  document.body.classList.toggle('gated', small);
  if (small && !gateTyping) { gateTyping = true; runGateTyper(); }
  else if (!small && gateTyping) { gateTyping = false; stopGateTyper(); }
}

// Crossfade the main shell in as the welcome splash dissolves out — the same
// fade + slight rise the shell uses when the window grows past the gate.
function revealApp() {
  const app = $('#app');
  const splash = $('#splash');
  app.classList.add('is-entering');   // start faded + slightly shrunk
  app.hidden = false;
  void app.offsetWidth;               // commit the start state before transitioning
  app.classList.remove('is-entering');
  document.body.classList.add('glow-in');  // let the violet glow bloom in slowly
  splash.classList.add('is-leaving'); // dissolve the splash out over the top
  const finish = () => { splash.hidden = true; splash.classList.remove('is-leaving'); };
  splash.addEventListener('transitionend', finish, { once: true });
  setTimeout(finish, 700);            // fallback if transitionend never fires
  // enable the first-run ⇄ shell crossfade only after this first reveal settles,
  // so create/delete animate but the initial load doesn't
  setTimeout(() => document.body.classList.add('shell-ready'), 550);
  setTimeout(refitChips, 80);         // cards now have layout → truncate overflowing chips
}

// ---- update banner + modal (a newer connectonion-studio is on PyPI) ---------
const REPO_URL = 'https://github.com/EvanYifanYang/connectonion-studio';
let updateInfo = null;   // last /setup/update payload, so the modal renders without refetching
const IS_APP = !!window.__coStudioNative;   // running inside the native macOS app (Sparkle drives updates)
let appUpdate = null;   // latest Sparkle state in app mode: { version, status }
let manualChecking = false;   // a Settings "Check now" is in flight (drives its button feedback)
let manualCheckTimer = null;  // fallback reset if no Sparkle status comes back
let updateRetryTimer = null;  // fallback to re-enable "Try again" once a failed session has torn down
let updateStallTimer = null;  // watchdog: abort a download that makes no progress (no network / hung)

async function refreshUpdateBanner() {
  const banner = $('#update-banner');
  if (!banner) return;
  try { updateInfo = await api.updateStatus(); } catch { return; }   // offline / older backend: leave hidden
  if (updateInfo.update_available && updateInfo.latest) {
    $('#update-ver').textContent = `v${updateInfo.latest}`;
    banner.hidden = false;
  } else {
    banner.hidden = true;
  }
}

function openUpdateModal() {
  const modal = $('#modal-update');
  if (IS_APP) {
    if (!appUpdate) return;
    modal.classList.add('is-app');   // CSS swaps the CLI steps for the "Relaunch to update" body
    $('#um-current').textContent = '';
    $('#um-latest').textContent = appUpdate.version ? `v${appUpdate.version}` : '';
    $('#um-notes').href = appUpdate.version ? `${REPO_URL}/releases/tag/v${appUpdate.version}` : REPO_URL;
    setUpdateMode('ready');
  } else {
    const info = updateInfo;
    if (!info || !info.latest) return;
    modal.classList.remove('is-app');
    $('#um-current').textContent = info.current ? `v${info.current}` : '';
    $('#um-latest').textContent = `v${info.latest}`;
    // backend reports the command matching how the studio was installed (pipx vs pip)
    $('#um-upgrade-cmd').textContent = info.upgrade_command || 'pipx upgrade connectonion-studio';
    $('#um-notes').href = `${REPO_URL}/releases/tag/v${info.latest}`;
  }
  modal.classList.remove('is-closing', 'is-updating');   // clean slate on open
  modal.hidden = false;
}

// macOS app: Sparkle pushes update state here (via the WebView bridge) to drive the banner + modal.
// The modal is a strict 3-state machine (ready / updating / error) — see setUpdateMode.
function onSparkleUpdate(payload) {
  const banner = $('#update-banner');
  if (!banner) return;
  const modal = $('#modal-update');
  const status = (payload && payload.status) || 'idle';
  const mode = modal.classList.contains('is-updating') ? 'updating'
             : modal.classList.contains('is-update-error') ? 'error' : 'ready';
  if (status === 'checking') {
    // a check is in flight — leave the banner + modal untouched
  } else if (status === 'available' || status === 'readyToRelaunch') {
    appUpdate = { version: (payload && payload.version) || '', status };
    if (appUpdate.version) $('#update-ver').textContent = `v${appUpdate.version}`;
    banner.hidden = false;
    if (!modal.hidden) setUpdateMode('ready');   // a "Try again" re-check re-found it → back to ready
  } else if (status === 'downloading' || status === 'installing') {
    if (appUpdate) appUpdate.status = status;
    if (!modal.hidden) { setUpdateMode('updating'); updateProgress(status, payload && payload.progress); }
  } else {   // terminal: error / idle / none
    if (mode === 'updating') {
      // interrupted mid-update → error state. Retry is only safe once the session tore down ('idle').
      setUpdateMode('error', { canRetry: status === 'idle' });
    } else if (mode === 'error') {
      // a "Try again" re-check ended — restore the retryable error state (idle = torn down → retry now;
      // error/none = it failed again → disable briefly, then the timer re-enables)
      setUpdateMode('error', { canRetry: status === 'idle' });
    } else if (status === 'none') {
      // genuinely no update (not mid-update) — clear the banner + modal
      appUpdate = null; banner.hidden = true;
      if (!modal.hidden) closeUpdateModal();
    }
    // error / idle in ready mode: no-op — keep the banner (the update is still available)
  }
  reflectManualCheck(status);   // resolve the Settings "Check now" button, if one is running
}

// The one place that sets the app-mode update modal's state. ready / updating / error are exclusive.
function setUpdateMode(mode, opts) {
  opts = opts || {};
  const modal = $('#modal-update');
  clearTimeout(updateRetryTimer);
  clearTimeout(updateStallTimer);
  modal.classList.toggle('is-updating', mode === 'updating');
  modal.classList.toggle('is-update-error', mode === 'error');
  const body = $('#um-app-text'), btn = $('#um-relaunch');
  if (mode === 'ready') {
    if (body) body.textContent = 'A new version is ready. Relaunch to finish updating.';
    if (btn) { btn.textContent = 'Relaunch to update'; btn.disabled = false; }
  } else if (mode === 'updating') {
    if (opts.reset) {   // entering the cover from a click — zero the bar (not on every progress tick)
      $('#um-prog-status').textContent = 'Preparing the update';
      $('#um-prog-fill').style.width = '0%';
      $('#um-prog-pct').textContent = '';
    }
    updateStallTimer = setTimeout(onUpdateStall, 12000);   // re-armed on every updating tick; fires only if truly stuck
  } else if (mode === 'error') {
    if (body) body.textContent = "The update didn't finish. Check your connection and try again.";
    if (btn) { btn.textContent = 'Try again'; btn.disabled = !opts.canRetry; }
    // fallback: if 'idle' never arrives, still let the user retry once the teardown has surely finished
    if (!opts.canRetry) updateRetryTimer = setTimeout(() => { const b = $('#um-relaunch'); if (b) b.disabled = false; }, 2500);
  }
}

// Drive the progress bar during the blocking "updating" cover.
function updateProgress(status, progress) {
  const head = $('#um-prog-status'), fill = $('#um-prog-fill'), pct = $('#um-prog-pct');
  const hasP = typeof progress === 'number' && progress >= 0;
  if (head) head.textContent = status === 'installing' ? 'Installing the update'
                             : (hasP ? 'Downloading the update' : 'Preparing the update');
  if (hasP) {
    const w = Math.round(Math.max(0, Math.min(1, progress)) * 100);
    if (fill) fill.style.width = w + '%';
    if (pct) pct.textContent = w + '%';
  } else if (status === 'installing') {   // extract/install with no number — near-done, don't reset to 0
    if (fill) fill.style.width = '100%';
    if (pct) pct.textContent = '';
  }
}

// Watchdog: the blocking cover made no progress for 12s (no network / hung download). Abort natively so
// Sparkle tears the session down (→ "idle"/error), with a fallback that forces the error state.
function onUpdateStall() {
  const modal = $('#modal-update');
  if (!modal.classList.contains('is-updating')) return;
  if (window.__coStudio && window.__coStudio.cancelUpdate) window.__coStudio.cancelUpdate();
  setTimeout(() => { if ($('#modal-update').classList.contains('is-updating')) setUpdateMode('error', { canRetry: true }); }, 3000);
}

// Drive the Settings "Check now" button/label from the Sparkle status, only while a manual check runs.
function reflectManualCheck(status) {
  if (!manualChecking) return;
  const label = $('#settings-update-status'), btn = $('#settings-check-update');
  if (status === 'checking') { if (label) label.textContent = 'Checking...'; return; }
  const done = (msg) => {
    clearTimeout(manualCheckTimer);
    manualChecking = false;
    if (label) label.textContent = msg;
    if (btn) { btn.disabled = false; btn.textContent = 'Check now'; }
  };
  if (status === 'available' || status === 'readyToRelaunch') done('Update available');
  else if (status === 'error') done('Check failed — try again');
  else if (status === 'none' || status === 'idle') done("You're up to date");
  // downloading / installing: an update is already in progress; the banner/modal drive it
}

// Play the fade + un-blur exit before hiding, so the background clears gradually on close.
function closeUpdateModal() {
  const modal = $('#modal-update');
  if (modal.hidden || modal.classList.contains('is-closing')) return;
  if (modal.classList.contains('is-updating')) return;   // mid-update: non-dismissible, nothing the user can do
  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    modal.hidden = true;   // no exit animation to await under reduced motion
    return;
  }
  let timer;
  const finish = () => {
    clearTimeout(timer);
    modal.removeEventListener('animationend', onEnd);
    modal.classList.remove('is-closing');
    modal.hidden = true;
  };
  const onEnd = (e) => { if (e.target === modal && e.animationName === 'um-overlay-out') finish(); };
  modal.classList.add('is-closing');
  modal.addEventListener('animationend', onEnd);
  timer = setTimeout(finish, 360);   // fallback if animationend never fires
}

function initUpdateCheck() {
  const banner = $('#update-banner');
  if (!banner) return;
  banner.addEventListener('click', openUpdateModal);
  $('#um-close').addEventListener('click', closeUpdateModal);
  $('#um-close-x').addEventListener('click', closeUpdateModal);
  $('#modal-update').addEventListener('click', (e) => { if (e.target === e.currentTarget) closeUpdateModal(); });
  if (IS_APP) {
    // macOS app: Sparkle drives updates, no PyPI polling. The native bridge calls window.__coStudioUpdate.
    window.__coStudioUpdate = onSparkleUpdate;
    $('#um-relaunch').addEventListener('click', () => {
      const modal = $('#modal-update');
      if (modal.classList.contains('is-update-error')) {
        // "Try again": re-check for a fresh Sparkle session — the failed one has torn down, so this is safe
        $('#um-relaunch').disabled = true;
        $('#um-app-text').textContent = 'Checking again...';
        if (window.__coStudio && window.__coStudio.checkForUpdates) window.__coStudio.checkForUpdates();
        return;
      }
      if (!(window.__coStudio && window.__coStudio.installUpdate)) return;
      setUpdateMode('updating', { reset: true });   // lock into the blocking cover (no Close, no going back)
      window.__coStudio.installUpdate();
    });
    // The launch check runs natively (WebUpdater.start → checkForUpdatesInBackground); pull whatever it
    // already found, in case it resolved before this handler was registered (the launch race).
    if (window.__coStudio && window.__coStudio.syncUpdate) window.__coStudio.syncUpdate();
    // Settings → Software Update: a manual "Check now" (the app has no menu bar to host it).
    const upGroup = $('#settings-update-group');
    if (upGroup) upGroup.hidden = false;
    const checkBtn = $('#settings-check-update');
    if (checkBtn) checkBtn.addEventListener('click', () => {
      if (manualChecking || !(window.__coStudio && window.__coStudio.checkForUpdates)) return;
      manualChecking = true;
      const label = $('#settings-update-status');
      if (label) label.textContent = 'Checking...';
      checkBtn.disabled = true; checkBtn.textContent = 'Checking...';
      clearTimeout(manualCheckTimer);
      manualCheckTimer = setTimeout(() => {   // no status came back — reset so the button isn't stuck
        manualChecking = false;
        if (label) label.textContent = '';
        checkBtn.disabled = false; checkBtn.textContent = 'Check now';
      }, 12000);
      window.__coStudio.checkForUpdates();
    });
  } else {
    // CLI (pip/pipx): poll PyPI and show the upgrade-command modal.
    $('#um-copy-upgrade').addEventListener('click', (e) => copyWithFeedback(e.currentTarget, $('#um-upgrade-cmd').textContent));
    $('#um-copy-restart').addEventListener('click', (e) => copyWithFeedback(e.currentTarget, 'co-studio'));
    refreshUpdateBanner();
    setInterval(refreshUpdateBanner, 3 * 60 * 60 * 1000);   // re-check every 3h (backend caches PyPI hourly)
  }
}

// ---- boot -----------------------------------------------------------
async function boot() {
  checkViewport();
  // enable the shell/gate crossfade only after the initial state is committed,
  // so first load snaps to the right view instead of animating into it. (setTimeout
  // rather than rAF: rAF is fully paused in a background tab, timers still fire.)
  setTimeout(() => document.body.classList.add('anim-ready'), 60);
  window.addEventListener('resize', checkViewport);
  window.addEventListener('resize', refitChips);   // re-fit toolkit chips when columns reflow
  initTheme();
  initAppearance();
  initNav();
  initStorage();
  initSearch();
  initCreateModal();
  initQrModal();
  initDrawer();
  initUpdateCheck();

  $('#app').appendChild($('#toasts'));   // toasts live inside the card, centered at its bottom
  $('#brand-onion').appendChild(createOnion({ size: 42, label: 'ConnectOnion' }));

  document.addEventListener('click', closeAllCardMenus);   // outside-click closes any open card menu
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    closeAllCardMenus();
    if ($('#app').classList.contains('is-settings')) closeSettingsModal();
    else if ($('#app').classList.contains('is-knowledge') || $('#app').classList.contains('is-approvals')) closeWorkspaceViews();
    else if ($('#app').classList.contains('is-history')) closeHistoryView();
    else if ($('#app').classList.contains('is-logs')) closeLogsView();
    else if ($('#app').classList.contains('is-market')) closeMarketView();
    else if (!$('#modal-qr').hidden) $('#modal-qr').hidden = true;
    else if (!$('#modal-update').hidden) closeUpdateModal();
    else if ($('#app').classList.contains('is-creating')) closeCreateModal();
    else if ($('#app').classList.contains('is-detail')) closeDrawer();
  });

  // A real process launch gets the welcome animation. If macOS has retained/recreated the
  // window after the red Close button, restore the already-running app without replaying it.
  const skipSplash = window.__coStudioSkipSplash === true;
  const splashDone = skipSplash ? Promise.resolve() : playSplash($('#splash'));
  if (skipSplash) $('#splash').hidden = true;
  const setupPromise = api.setupStatus().catch(() => null);
  const agentsPromise = api.listAgents().catch(() => null);

  const [setup, agents] = await Promise.all([setupPromise, agentsPromise]);
  await splashDone;

  state.setup = setup;
  renderSideHealth(setup);
  if (setup && !setup.co_auth_ok) {
    $('#splash').hidden = true;   // splash → onboarding card (its own path)
    showOnboarding(setup);
  } else {
    // populate the shell before it fades in, then crossfade splash → shell
    renderDoctorBanner(setup);
    if (agents) {
      setAgents(agents);
    } else {
      setConnBanner(true);
      renderAgents(); // empty grid → hero empty state
    }
    revealApp();
  }

  state.statusSock = statusSocket(
    (list) => {
      if (!state.onboardingActive) setAgents(list);
    },
    async (connected) => {
      if (!connected) {
        setConnBanner(true);
        return;
      }
      setConnBanner(false);
      // on (re)connect, re-verify setup + agents once
      try {
        const s = await api.setupStatus();
        state.setup = s;
        if (!s.co_auth_ok && !state.onboardingActive) {
          showOnboarding(s);
        } else if (!state.onboardingActive) {
          renderDoctorBanner(s);
          refreshAgents();
        }
      } catch { /* banner already handles it */ }
    },
  );
}

boot();
