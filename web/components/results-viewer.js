import { API, app, navigate, showToast } from '../app.js';

export async function renderResults({ id }) {
  const run = await API.get(`/tests/${id}`);
  const c = run.config || {};
  const s = run.summary;

  if (!s) {
    app.innerHTML = `
      <div class="card">
        <h2>Test Run: ${id}</h2>
        <p>Status: <span class="badge ${run.status === 'running' ? 'badge-running' : 'badge-fail'}">${run.status}</span></p>
        ${run.status === 'running' ? `<p style="margin-top:12px"><button class="btn" onclick="location.hash='#/tests/${id}/live'">View Live Progress</button></p>` : ''}
        <p class="text-muted" style="margin-top:12px">No summary data available yet.</p>
      </div>`;
    return;
  }

  const isSat = s.search_mode === 'saturation' || s.saturated_rps != null;
  const gpu = c.gpu_model || s.gpu_model || 'Unspecified';
  const tokens = c.target_tokens || s.target_tokens || '?';
  const cache = c.prefix_cache_rate != null ? `${c.prefix_cache_rate}%` : (s.prefix_cache_rate != null ? `${s.prefix_cache_rate}%` : 'Unique');

  let headline = '';
  if (s.saturated_rps != null && s.best_rps != null) {
    headline = `GPU saturated at ${s.saturated_rps} RPS. Max sustainable capacity: <span class="metric-accent">${s.best_rps} RPS</span>`;
  } else if (s.best_rps != null) {
    headline = `Maximum compliant capacity: <span class="metric-accent">${s.best_rps} RPS</span>`;
  } else {
    headline = `<span class="metric-red">No evaluated RPS met the criteria.</span>`;
  }

  const history = s.history || [];
  const labels = history.map(h => `${h.target_rps} RPS`);
  const offeredData = history.map(h => h.offered_rps);
  const completedData = history.map(h => h.completed_rps);
  const g_p50s = history.map(h => h.g_p50 || 0);
  const g_p95s = history.map(h => h.g_p95 || 0);
  const g_p99s = history.map(h => h.g_p99 || 0);

  const stepsHtml = history.map(h => {
    const fmt = (v) => v != null ? `${Math.round(v)}` : '—';
    const sat = h.is_saturated;
    const badge = sat ? 'badge-saturated' : (h.compliant ? 'badge-pass' : 'badge-fail');
    const status = sat ? 'SATURATED' : (h.compliant ? 'PASS' : 'FAIL');
    return `<tr>
      <td>${h.step}</td><td><strong>${h.target_rps}</strong></td>
      <td>${h.offered_rps}</td><td><strong>${h.completed_rps}</strong></td>
      <td>${fmt(h.g_p50)}</td><td>${fmt(h.g_p95)}</td><td>${fmt(h.g_p99)}</td>
      <td>${fmt(h.rtt_p95)}</td><td>${h.pct_429 || 0}%</td><td>${h.success_pct || 0}%</td>
      <td><span class="badge ${badge}">${status}</span></td>
      <td class="text-muted">${h.reason || ''}</td>
    </tr>`;
  }).join('');

  app.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
      <div>
        <h1 style="font-size:24px;font-weight:700">Test Results</h1>
        <div class="text-muted">${gpu} · ${tokens} tokens · ${cache} cache · ${s.search_mode || 'saturation'}</div>
      </div>
      <div style="display:flex;gap:8px">
        <button class="btn" id="view-report-btn">View Full Report</button>
        <button class="btn btn-danger" id="delete-btn">Delete</button>
        <button class="btn" onclick="location.hash='#/'">Back</button>
      </div>
    </div>

    <div class="card">
      <div class="callout">${headline}</div>
    </div>

    <div class="grid-2" style="margin-bottom:20px">
      <div class="stat-card">
        <div class="stat-value metric-accent">${s.best_rps != null ? s.best_rps + ' RPS' : '—'}</div>
        <div class="stat-label">${isSat ? 'Max Sustainable (Pre-Saturation)' : 'Max Compliant RPS'}</div>
      </div>
      <div class="stat-card">
        <div class="stat-value ${s.best_g_p95 != null ? 'metric-green' : ''}">${s.best_g_p95 != null ? Math.round(s.best_g_p95) + ' ms' : '—'}</div>
        <div class="stat-label">Guardrails P95 @ Best RPS</div>
      </div>
    </div>

    <div class="grid-2">
      <div class="card">
        <h2>Throughput Curve</h2>
        <div class="chart-wrap"><canvas id="result-throughput"></canvas></div>
      </div>
      <div class="card">
        <h2>Latency Curve</h2>
        <div class="chart-wrap"><canvas id="result-latency"></canvas></div>
      </div>
    </div>

    <div class="card">
      <h2>Step Results</h2>
      <table>
        <thead>
          <tr><th>Step</th><th>Target RPS</th><th>Offered</th><th>Completed</th><th>GR P50</th><th>GR P95</th><th>GR P99</th><th>RTT P95</th><th>429%</th><th>2xx%</th><th>Status</th><th>Details</th></tr>
        </thead>
        <tbody>${stepsHtml}</tbody>
      </table>
    </div>

    <div class="text-muted" style="margin-top:12px">
      Run ID: ${id} · ${s.start_time || ''} to ${s.end_time || ''} · ${s.steps || 0} steps
    </div>`;

  new Chart(document.getElementById('result-throughput'), {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: 'Offered RPS', data: offeredData, borderColor: '#94a3b8', borderDash: [5, 5], borderWidth: 2, pointRadius: 3, fill: false },
        { label: 'Completed 2xx RPS', data: completedData, borderColor: '#00d4ff', backgroundColor: 'rgba(0,212,255,0.1)', borderWidth: 3, pointRadius: 5, fill: true, tension: 0.15 },
      ],
    },
    options: { responsive: true, maintainAspectRatio: false, scales: { y: { beginAtZero: true, title: { display: true, text: 'RPS' } } } },
  });

  const latencyDatasets = [];
  if (!isSat && c.target_latency_ms) {
    latencyDatasets.push({
      label: `SLA Ceiling (${c.target_latency_ms} ms)`,
      data: Array(labels.length).fill(c.target_latency_ms),
      borderColor: '#f87171', borderDash: [6, 6], borderWidth: 2, pointRadius: 0, fill: false,
    });
  }
  latencyDatasets.push(
    { label: 'GR P95', data: g_p95s, borderColor: '#00d4ff', backgroundColor: 'rgba(0,212,255,0.1)', borderWidth: 3, fill: true, tension: 0.2 },
    { label: 'GR P50', data: g_p50s, borderColor: '#34d399', borderWidth: 2, tension: 0.2 },
    { label: 'GR P99', data: g_p99s, borderColor: '#fbbf24', borderWidth: 2, tension: 0.2 },
  );

  new Chart(document.getElementById('result-latency'), {
    type: 'line',
    data: { labels, datasets: latencyDatasets },
    options: { responsive: true, maintainAspectRatio: false, scales: { y: { beginAtZero: true, title: { display: true, text: 'ms' } } } },
  });

  document.getElementById('view-report-btn').addEventListener('click', () => {
    window.open(`/api/tests/${id}/report`, '_blank');
  });

  document.getElementById('delete-btn').addEventListener('click', async () => {
    if (!confirm('Delete this test run?')) return;
    try {
      await API.del(`/tests/${id}`);
      showToast('Run deleted');
      navigate('/');
    } catch (e) {
      showToast(`Error: ${e.message}`);
    }
  });
}
