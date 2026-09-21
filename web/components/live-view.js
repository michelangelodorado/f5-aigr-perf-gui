import { API, app, navigate } from '../app.js';

let throughputChart = null;
let latencyChart = null;

export async function renderLiveView({ id }) {
  const run = await API.get(`/tests/${id}`);
  const c = run.config || {};

  app.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
      <div>
        <h1 style="font-size:24px;font-weight:700">Live Test Progress</h1>
        <div class="text-muted">${c.gpu_model || 'GPU'} · ${c.target_tokens || '?'} tokens · ${c.search_mode || 'saturation'}</div>
      </div>
      <span class="badge badge-running" id="status-badge">Running</span>
    </div>

    <div class="grid-3" id="live-stats">
      <div class="stat-card"><div class="stat-value metric-accent" id="stat-step">0</div><div class="stat-label">Current Step</div></div>
      <div class="stat-card"><div class="stat-value metric-green" id="stat-inflight">0</div><div class="stat-label">In-Flight</div></div>
      <div class="stat-card"><div class="stat-value" id="stat-completed">0</div><div class="stat-label">Completed</div></div>
    </div>

    <div class="progress-bar" style="margin:20px 0"><div class="progress-fill" id="progress-fill" style="width:0%"></div></div>

    <div class="grid-2">
      <div class="card">
        <h2>Throughput (RPS)</h2>
        <div class="chart-wrap"><canvas id="live-throughput"></canvas></div>
      </div>
      <div class="card">
        <h2>Latency (ms)</h2>
        <div class="chart-wrap"><canvas id="live-latency"></canvas></div>
      </div>
    </div>

    <div class="card">
      <h2>Step Results</h2>
      <table>
        <thead>
          <tr><th>Step</th><th>Target RPS</th><th>Offered RPS</th><th>Completed RPS</th><th>GR P50</th><th>GR P95</th><th>GR P99</th><th>RTT P95</th><th>429%</th><th>Status</th><th>Reason</th></tr>
        </thead>
        <tbody id="steps-body"></tbody>
      </table>
    </div>

    <details style="margin-top:12px">
      <summary class="text-muted" style="cursor:pointer;font-size:13px">Console Output</summary>
      <div class="console-panel" id="console-log">Waiting for output...</div>
    </details>`;

  const labels = [];
  const offeredData = [];
  const completedData = [];
  const p50Data = [];
  const p95Data = [];
  const p99Data = [];

  throughputChart = new Chart(document.getElementById('live-throughput'), {
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

  latencyChart = new Chart(document.getElementById('live-latency'), {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: 'GR P95', data: p95Data, borderColor: '#00d4ff', borderWidth: 3, tension: 0.2 },
        { label: 'GR P50', data: p50Data, borderColor: '#34d399', borderWidth: 2, tension: 0.2 },
        { label: 'GR P99', data: p99Data, borderColor: '#fbbf24', borderWidth: 2, tension: 0.2 },
      ],
    },
    options: { responsive: true, maintainAspectRatio: false, scales: { y: { beginAtZero: true, title: { display: true, text: 'ms' } } } },
  });

  const stepsBody = document.getElementById('steps-body');
  const ws = API.ws(`/tests/${id}/live`);

  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);

      if (msg.type === 'progress') {
        document.getElementById('stat-step').textContent = msg.step || '—';
        document.getElementById('stat-inflight').textContent = msg.inflight || 0;
        document.getElementById('stat-completed').textContent = msg.completed || 0;
      }

      if (msg.type === 'step_complete') {
        const lbl = `${msg.target_rps} RPS`;
        labels.push(lbl);
        offeredData.push(msg.offered_rps || 0);
        completedData.push(msg.completed_rps || 0);
        p50Data.push(msg.g_p50 || 0);
        p95Data.push(msg.g_p95 || 0);
        p99Data.push(msg.g_p99 || 0);
        throughputChart.update();
        latencyChart.update();

        const fmt = (v) => v != null ? `${Math.round(v)}` : '—';
        const isSat = msg.is_saturated;
        const badge = isSat ? 'badge-saturated' : (msg.compliant ? 'badge-pass' : 'badge-fail');
        const status = isSat ? 'SATURATED' : (msg.compliant ? 'PASS' : 'FAIL');

        stepsBody.insertAdjacentHTML('beforeend', `
          <tr>
            <td>${msg.step}</td><td><strong>${msg.target_rps}</strong></td>
            <td>${msg.offered_rps}</td><td><strong>${msg.completed_rps}</strong></td>
            <td>${fmt(msg.g_p50)}</td><td>${fmt(msg.g_p95)}</td><td>${fmt(msg.g_p99)}</td>
            <td>${fmt(msg.rtt_p95)}</td><td>${msg.pct_429 || 0}%</td>
            <td><span class="badge ${badge}">${status}</span></td>
            <td class="text-muted">${msg.reason || ''}</td>
          </tr>`);

        const total = c.max_steps || 15;
        const pct = Math.min(100, (msg.step / total) * 100);
        document.getElementById('progress-fill').style.width = `${pct}%`;
      }

      if (msg.type === 'finished') {
        document.getElementById('status-badge').textContent = msg.exit_code === 0 ? 'Completed' : 'Failed';
        document.getElementById('status-badge').className = `badge ${msg.exit_code === 0 ? 'badge-pass' : 'badge-fail'}`;
        document.getElementById('progress-fill').style.width = '100%';
        setTimeout(() => navigate(`/tests/${id}`), 2000);
      }
    } catch (e) {
      const log = document.getElementById('console-log');
      log.textContent += event.data + '\n';
    }
  };

  ws.onerror = () => {
    document.getElementById('status-badge').textContent = 'Disconnected';
    document.getElementById('status-badge').className = 'badge badge-fail';
  };
}
