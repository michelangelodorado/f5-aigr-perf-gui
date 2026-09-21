const API = {
  async get(path) {
    const r = await fetch(`/api${path}`, { cache: 'no-store' });
    if (!r.ok) throw new Error(`GET ${path}: ${r.status}`);
    return r.json();
  },
  async post(path, body) {
    const r = await fetch(`/api${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({ detail: r.statusText }));
      throw new Error(err.detail || `POST ${path}: ${r.status}`);
    }
    return r.json();
  },
  async del(path) {
    const r = await fetch(`/api${path}`, { method: 'DELETE' });
    if (!r.ok) throw new Error(`DELETE ${path}: ${r.status}`);
    return r.json();
  },
  async upload(path, file) {
    const form = new FormData();
    form.append('file', file);
    const r = await fetch(`/api${path}`, { method: 'POST', body: form });
    if (!r.ok) throw new Error(`UPLOAD ${path}: ${r.status}`);
    return r.json();
  },
  ws(path) {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return new WebSocket(`${proto}//${location.host}/api${path}`);
  },
};

const app = document.getElementById('app');

function showToast(msg, duration = 3000) {
  const el = document.createElement('div');
  el.className = 'toast';
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), duration);
}

const routes = {};

function registerRoute(pattern, handler) {
  routes[pattern] = handler;
}

function navigate(hash) {
  location.hash = hash;
}

async function handleRoute() {
  const hash = location.hash.slice(1) || '/';

  document.querySelectorAll('.nav-link').forEach(a => {
    a.classList.toggle('active', a.dataset.route === hash || hash.startsWith(a.dataset.route + '/'));
  });

  for (const [pattern, handler] of Object.entries(routes)) {
    const regex = new RegExp('^' + pattern.replace(/\{(\w+)\}/g, '(?<$1>[^/]+)') + '$');
    const match = hash.match(regex);
    if (match) {
      try {
        await handler(match.groups || {});
      } catch (e) {
        app.innerHTML = `<div class="card"><h2>Error</h2><p>${e.message}</p></div>`;
      }
      return;
    }
  }
  app.innerHTML = '<div class="empty-state"><h3>Page not found</h3></div>';
}

window.addEventListener('hashchange', handleRoute);

document.addEventListener('click', (e) => {
  const link = e.target.closest('a.nav-link');
  if (link) {
    const target = link.getAttribute('href')?.slice(1) || '/';
    const current = location.hash.slice(1) || '/';
    if (target === current) {
      e.preventDefault();
      handleRoute();
    }
  }
});

export { API, app, showToast, registerRoute, navigate, handleRoute };

import { renderDashboard } from './components/dashboard.js';
import { renderTestForm } from './components/test-form.js';
import { renderLiveView } from './components/live-view.js';
import { renderResults } from './components/results-viewer.js';
import { renderCompare } from './components/compare-view.js';
import { renderPrompts } from './components/prompts-manager.js';

registerRoute('/', renderDashboard);
registerRoute('/tests/new', renderTestForm);
registerRoute('/tests/{id}/live', renderLiveView);
registerRoute('/tests/{id}', renderResults);
registerRoute('/compare', renderCompare);
registerRoute('/prompts', renderPrompts);

handleRoute();
