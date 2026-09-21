import { API, app, navigate, showToast } from '../app.js';

let selectedIds = new Set();

export async function renderDashboard() {
  const runs = await API.get('/tests');
  selectedIds = new Set();

  if (runs.length === 0) {
    app.innerHTML = `
      <div class="empty-state">
        <h3>No test runs yet</h3>
        <p class="text-muted">Start your first performance test to see results here.</p>
        <br>
        <button class="btn btn-primary" onclick="location.hash='#/tests/new'">New Test</button>
      </div>`;
    return;
  }

  app.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
      <h1 style="font-size:24px;font-weight:700">Test Runs</h1>
      <div style="display:flex;gap:8px">
        <button class="btn" id="compare-btn" disabled>Compare Selected</button>
        <button class="btn btn-primary" onclick="location.hash='#/tests/new'">New Test</button>
      </div>
    </div>
    <div id="runs-list"></div>`;

  const list = document.getElementById('runs-list');

  for (const run of runs) {
    const s = run.summary;
    const c = run.config;
    const isRunning = run.status === 'running';
    const gpu = c.gpu_model || 'Unspecified GPU';
    const tokens = c.target_tokens || '?';
    const mode = c.search_mode === 'saturation' ? 'Saturation' : `SLA ${c.target_latency_ms || 500}ms`;
    const cache = c.prefix_cache_rate != null ? `${c.prefix_cache_rate}% cache` : 'Unique';

    let resultText = '';
    let badgeClass = 'badge-pass';
    if (isRunning) {
      resultText = 'Running...';
      badgeClass = 'badge-running';
    } else if (run.status === 'failed') {
      resultText = 'Failed';
      badgeClass = 'badge-fail';
    } else if (s) {
      if (s.saturated_rps != null) {
        resultText = `Saturated @ ${s.saturated_rps} RPS`;
        badgeClass = 'badge-saturated';
      }
      if (s.best_rps != null) {
        resultText = `Max: ${s.best_rps} RPS`;
        badgeClass = 'badge-pass';
      }
      if (!s.best_rps && !s.saturated_rps) {
        resultText = 'No compliant RPS';
        badgeClass = 'badge-fail';
      }
    } else {
      resultText = 'Completed';
    }

    const ts = new Date(run.created * 1000).toLocaleString();

    const card = document.createElement('div');
    card.className = 'run-card';
    card.innerHTML = `
      <div class="run-card-left">
        <label class="checkbox-label" onclick="event.stopPropagation()">
          <input type="checkbox" data-id="${run.run_id}" ${isRunning ? 'disabled' : ''}>
        </label>
        <div>
          <div style="font-weight:600;font-size:14px">${gpu} · ${tokens} tokens · ${mode}</div>
          <div class="text-muted">${run.run_id} · ${cache} · ${ts}</div>
        </div>
      </div>
      <div class="run-card-right">
        <span class="badge ${badgeClass}">${resultText}</span>
      </div>`;

    card.addEventListener('click', () => {
      if (isRunning) {
        navigate(`/tests/${run.run_id}/live`);
      } else {
        navigate(`/tests/${run.run_id}`);
      }
    });

    const cb = card.querySelector('input[type="checkbox"]');
    cb.addEventListener('change', () => {
      if (cb.checked) selectedIds.add(run.run_id);
      else selectedIds.delete(run.run_id);
      document.getElementById('compare-btn').disabled = selectedIds.size < 2;
    });

    list.appendChild(card);
  }

  document.getElementById('compare-btn').addEventListener('click', () => {
    if (selectedIds.size >= 2 && selectedIds.size <= 3) {
      navigate(`/compare?ids=${[...selectedIds].join(',')}`);
    } else {
      showToast('Select 2-3 runs to compare');
    }
  });
}
