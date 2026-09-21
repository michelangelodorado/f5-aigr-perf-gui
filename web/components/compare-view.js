import { API, app } from '../app.js';

const COLORS = ['#00d4ff', '#34d399', '#fbbf24'];

export async function renderCompare() {
  const params = new URLSearchParams(location.hash.split('?')[1] || '');
  const ids = (params.get('ids') || '').split(',').filter(Boolean);

  if (ids.length < 2) {
    app.innerHTML = '<div class="empty-state"><h3>Select 2-3 runs from the dashboard to compare</h3></div>';
    return;
  }

  const runs = await API.post('/tests/compare', { ids });

  const summaryCards = runs.map((r, i) => {
    const s = r.summary || {};
    const c = r.config || {};
    return `
      <div class="stat-card" style="border-top:3px solid ${COLORS[i]}">
        <div style="font-size:12px;color:${COLORS[i]};font-weight:600;margin-bottom:8px">${c.gpu_model || 'GPU'} · ${c.target_tokens || '?'} tok</div>
        <div class="stat-value">${s.best_rps != null ? s.best_rps + ' RPS' : '—'}</div>
        <div class="stat-label">${s.search_mode === 'saturation' || s.saturated_rps ? 'Max Sustainable' : 'Max Compliant'}</div>
        <div class="text-muted" style="margin-top:6px">${c.prefix_cache_rate != null ? c.prefix_cache_rate + '% cache' : 'Unique'}</div>
      </div>`;
  }).join('');

  const allLabels = new Set();
  runs.forEach(r => {
    (r.summary?.history || []).forEach(h => allLabels.add(h.target_rps));
  });
  const labels = [...allLabels].sort((a, b) => a - b).map(v => `${v} RPS`);
  const rpsValues = [...allLabels].sort((a, b) => a - b);

  const throughputDatasets = runs.map((r, i) => {
    const hist = r.summary?.history || [];
    const histMap = Object.fromEntries(hist.map(h => [h.target_rps, h]));
    return {
      label: `${r.config?.gpu_model || 'Run'} ${r.config?.target_tokens || '?'}tok`,
      data: rpsValues.map(rps => histMap[rps]?.completed_rps ?? null),
      borderColor: COLORS[i],
      borderWidth: 3,
      pointRadius: 4,
      tension: 0.15,
      spanGaps: true,
    };
  });

  const latencyDatasets = runs.map((r, i) => {
    const hist = r.summary?.history || [];
    const histMap = Object.fromEntries(hist.map(h => [h.target_rps, h]));
    return {
      label: `${r.config?.gpu_model || 'Run'} ${r.config?.target_tokens || '?'}tok P95`,
      data: rpsValues.map(rps => histMap[rps]?.g_p95 ?? null),
      borderColor: COLORS[i],
      borderWidth: 3,
      pointRadius: 4,
      tension: 0.2,
      spanGaps: true,
    };
  });

  const diffRows = runs.map((r, i) => {
    const s = r.summary || {};
    const c = r.config || {};
    return `<tr>
      <td style="color:${COLORS[i]};font-weight:600">${c.gpu_model || '—'}</td>
      <td>${c.target_tokens || '—'}</td>
      <td>${c.prefix_cache_rate != null ? c.prefix_cache_rate + '%' : 'Unique'}</td>
      <td>${c.search_mode || '—'}</td>
      <td><strong>${s.best_rps != null ? s.best_rps : '—'}</strong></td>
      <td>${s.best_g_p95 != null ? Math.round(s.best_g_p95) + ' ms' : '—'}</td>
      <td>${s.saturated_rps != null ? s.saturated_rps : '—'}</td>
      <td>${s.steps || '—'}</td>
    </tr>`;
  }).join('');

  app.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
      <h1 style="font-size:24px;font-weight:700">Compare Runs</h1>
      <button class="btn" onclick="location.hash='#/'">Back</button>
    </div>

    <div class="grid-${runs.length}" style="margin-bottom:20px">${summaryCards}</div>

    <div class="grid-2">
      <div class="card">
        <h2>Throughput Comparison</h2>
        <div class="chart-wrap"><canvas id="cmp-throughput"></canvas></div>
      </div>
      <div class="card">
        <h2>Latency Comparison (P95)</h2>
        <div class="chart-wrap"><canvas id="cmp-latency"></canvas></div>
      </div>
    </div>

    <div class="card">
      <h2>Key Metrics</h2>
      <table>
        <thead><tr><th>GPU</th><th>Tokens</th><th>Cache</th><th>Mode</th><th>Best RPS</th><th>GR P95</th><th>Saturated @</th><th>Steps</th></tr></thead>
        <tbody>${diffRows}</tbody>
      </table>
    </div>`;

  new Chart(document.getElementById('cmp-throughput'), {
    type: 'line',
    data: { labels, datasets: throughputDatasets },
    options: { responsive: true, maintainAspectRatio: false, scales: { y: { beginAtZero: true, title: { display: true, text: 'Completed RPS' } } } },
  });

  new Chart(document.getElementById('cmp-latency'), {
    type: 'line',
    data: { labels, datasets: latencyDatasets },
    options: { responsive: true, maintainAspectRatio: false, scales: { y: { beginAtZero: true, title: { display: true, text: 'Latency (ms)' } } } },
  });
}
