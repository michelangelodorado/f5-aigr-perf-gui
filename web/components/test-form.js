import { API, app, navigate, showToast } from '../app.js';

const PRESETS = [
  { label: '250-token Saturation (H100)', search_mode: 'saturation', target_tokens: 250, start_rps: 5, step_rps: 1, step_duration: '30s', step_warmup: '15s', gpu_model: 'NVIDIA H100', prefix_cache_rate: 40, gpu_count: 1 },
  { label: '500-token Saturation (H100)', search_mode: 'saturation', target_tokens: 500, start_rps: 5, step_rps: 1, step_duration: '30s', step_warmup: '15s', gpu_model: 'NVIDIA H100', prefix_cache_rate: 40, gpu_count: 1 },
  { label: '250-token SLA 500ms (H100)', search_mode: 'ladder', target_tokens: 250, target_latency_ms: 500, sla_metric: 'guardrail_p95', start_rps: 2, step_rps: 1, step_duration: '30s', step_warmup: '15s', gpu_model: 'NVIDIA H100', prefix_cache_rate: 40, gpu_count: 1 },
  { label: '500-token SLA 500ms (H100)', search_mode: 'ladder', target_tokens: 500, target_latency_ms: 500, sla_metric: 'guardrail_p95', start_rps: 2, step_rps: 1, step_duration: '30s', step_warmup: '15s', gpu_model: 'NVIDIA H100', prefix_cache_rate: 40, gpu_count: 1 },
];

export async function renderTestForm() {
  const [prompts, defaults] = await Promise.all([
    API.get('/prompts'),
    API.get('/defaults'),
  ]);

  const promptOptions = prompts.map(p => `<option value="${p.name}">${p.name} (${p.rows} prompts)</option>`).join('');
  const presetOptions = PRESETS.map((p, i) => `<option value="${i}">${p.label}</option>`).join('');

  app.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
      <h1 style="font-size:24px;font-weight:700">New Test</h1>
      <div class="form-group" style="margin-bottom:0;width:300px">
        <label>Load Preset</label>
        <select id="preset-select">
          <option value="">Custom Configuration</option>
          ${presetOptions}
        </select>
      </div>
    </div>
    <form id="test-form">
      <div class="card">
        <div class="form-section">
          <div class="form-section-title">Target Endpoint</div>
          <div class="form-row">
            <div class="form-group">
              <label>API URL</label>
              <input name="f5_api_url" type="url" value="${defaults.f5_api_url || ''}" placeholder="https://..." required>
            </div>
            <div class="form-group">
              <label>Bearer Token</label>
              <input name="f5_token" type="password" value="${defaults.f5_token || ''}" placeholder="Token" required>
            </div>
          </div>
        </div>

        <div class="form-section">
          <div class="form-section-title">Search Mode</div>
          <div class="form-row-3">
            <div class="form-group">
              <label>Mode</label>
              <select name="search_mode" id="search-mode">
                <option value="saturation">Saturation (GPU 100% Finder)</option>
                <option value="ladder">Ladder (SLA-Bounded)</option>
                <option value="binary">Binary Search (SLA-Bounded)</option>
              </select>
            </div>
            <div class="form-group sla-field">
              <label>Target Latency (ms)</label>
              <input name="target_latency_ms" type="number" value="500" min="1">
            </div>
            <div class="form-group sla-field">
              <label>SLA Metric</label>
              <select name="sla_metric">
                <option value="guardrail_p95">Guardrails P95</option>
                <option value="guardrail_p50">Guardrails P50</option>
                <option value="guardrail_p99">Guardrails P99</option>
                <option value="rtt_p95">RTT P95</option>
              </select>
            </div>
          </div>
        </div>

        <div class="form-section">
          <div class="form-section-title">Workload</div>
          <div class="form-row-3">
            <div class="form-group">
              <label>Target Tokens</label>
              <input name="target_tokens" type="number" value="250" min="1" required>
            </div>
            <div class="form-group">
              <label>Prompt File</label>
              <select name="prompt_file">${promptOptions}</select>
            </div>
            <div class="form-group">
              <label>Prefix Cache Rate (%)</label>
              <input name="prefix_cache_rate" type="number" value="40" min="0" max="100" step="1">
            </div>
          </div>
          <div class="form-row-3">
            <div class="form-group">
              <label>Prompt Mode</label>
              <select name="prompt_mode">
                <option value="unique">Unique (cache-resistant)</option>
                <option value="repeat">Repeat (cache-friendly)</option>
              </select>
            </div>
          </div>
        </div>

        <div class="form-section">
          <div class="form-section-title">Step Configuration</div>
          <div class="form-row-3">
            <div class="form-group">
              <label>Start RPS</label>
              <input name="start_rps" type="number" value="5" min="0.1" step="0.1" required>
            </div>
            <div class="form-group">
              <label>Step RPS</label>
              <input name="step_rps" type="number" value="1" min="0.1" step="0.1" required>
            </div>
            <div class="form-group">
              <label>Max Steps</label>
              <input name="max_steps" type="number" value="15" min="1">
            </div>
          </div>
          <div class="form-row-3">
            <div class="form-group">
              <label>Step Duration</label>
              <input name="step_duration" value="30s">
            </div>
            <div class="form-group">
              <label>Step Warmup</label>
              <input name="step_warmup" value="15s">
            </div>
            <div class="form-group">
              <label>Cooldown</label>
              <input name="cooldown" value="5s">
            </div>
          </div>
          <div class="form-row-3 binary-field" style="display:none">
            <div class="form-group">
              <label>Min RPS (Binary)</label>
              <input name="min_rps" type="number" value="10" min="0.1" step="0.1">
            </div>
            <div class="form-group">
              <label>Max RPS</label>
              <input name="max_rps" type="number" value="100" min="1" step="1">
            </div>
            <div class="form-group">
              <label>RPS Tolerance (Binary)</label>
              <input name="rps_tolerance" type="number" value="2" min="0.1" step="0.1">
            </div>
          </div>
        </div>

        <div class="form-section">
          <div class="form-section-title">Error Thresholds</div>
          <div class="form-row-3">
            <div class="form-group">
              <label>Max 429 Rate (%)</label>
              <input name="max_429_pct" type="number" value="2.0" min="0" step="0.1">
            </div>
            <div class="form-group">
              <label>Max Error Rate (%)</label>
              <input name="max_error_pct" type="number" value="0" min="0" step="0.1">
            </div>
            <div class="form-group">
              <label>Max Timeouts</label>
              <input name="max_timeouts" type="number" value="0" min="0">
            </div>
          </div>
        </div>

        <div class="form-section">
          <div class="form-section-title">Hardware Metadata</div>
          <div class="form-row-3">
            <div class="form-group">
              <label>GPU Model</label>
              <input name="gpu_model" value="NVIDIA H100">
            </div>
            <div class="form-group">
              <label>GPU Count</label>
              <input name="gpu_count" type="number" value="1" min="0">
            </div>
            <div class="form-group">
              <label>Guardrails Version</label>
              <input name="guardrails_version" value="">
            </div>
          </div>
          <div class="form-row">
            <div class="form-group">
              <label>Environment</label>
              <input name="environment" value="">
            </div>
            <div class="form-group">
              <label>Notes</label>
              <input name="notes" value="">
            </div>
          </div>
        </div>

        <div class="form-section">
          <div class="form-section-title">Advanced</div>
          <div class="form-row-3">
            <div class="form-group">
              <label>Request Timeout (s)</label>
              <input name="timeout" type="number" value="120" min="1">
            </div>
            <div class="form-group" style="display:flex;align-items:end">
              <label class="checkbox-label">
                <input type="checkbox" name="verify_tls"> Verify TLS
              </label>
            </div>
          </div>
        </div>
      </div>

      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:8px">
        <button type="button" class="btn" onclick="location.hash='#/'">Cancel</button>
        <button type="submit" class="btn btn-primary" id="start-btn">Start Test</button>
      </div>
    </form>`;

  const form = document.getElementById('test-form');
  const modeSelect = document.getElementById('search-mode');

  function updateVisibility() {
    const mode = modeSelect.value;
    document.querySelectorAll('.sla-field').forEach(el => {
      el.style.display = mode === 'saturation' ? 'none' : '';
    });
    document.querySelectorAll('.binary-field').forEach(el => {
      el.style.display = mode === 'binary' ? '' : 'none';
    });
  }
  modeSelect.addEventListener('change', updateVisibility);
  updateVisibility();

  document.getElementById('preset-select').addEventListener('change', (e) => {
    if (e.target.value === '') return;
    const preset = PRESETS[parseInt(e.target.value)];
    for (const [key, val] of Object.entries(preset)) {
      if (key === 'label') continue;
      const input = form.querySelector(`[name="${key}"]`);
      if (input) {
        if (input.type === 'checkbox') input.checked = !!val;
        else input.value = val;
      }
    }
    updateVisibility();
  });

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = document.getElementById('start-btn');
    btn.disabled = true;
    btn.textContent = 'Starting...';

    const data = {};
    const fd = new FormData(form);
    for (const [k, v] of fd.entries()) {
      if (k === 'verify_tls') { data[k] = true; continue; }
      const input = form.querySelector(`[name="${k}"]`);
      if (input && input.type === 'number') data[k] = parseFloat(v);
      else data[k] = v;
    }
    if (!fd.has('verify_tls')) data.verify_tls = false;

    if (data.prefix_cache_rate === 0 || data.prefix_cache_rate === '') {
      data.prefix_cache_rate = null;
    }

    try {
      const result = await API.post('/tests', data);
      navigate(`/tests/${result.run_id}/live`);
    } catch (err) {
      showToast(`Error: ${err.message}`);
      btn.disabled = false;
      btn.textContent = 'Start Test';
    }
  });
}
