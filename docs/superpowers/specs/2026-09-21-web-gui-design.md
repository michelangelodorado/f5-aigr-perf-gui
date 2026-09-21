# F5 AI Guardrails Performance Tool — Web GUI Design Spec

**Date**: 2026-09-21
**Status**: Draft
**Audience**: Customer demos and stakeholders
**Approach**: FastAPI backend + embedded static SPA, subprocess-isolated benchmark execution

---

## 1. Overview

Add a web GUI to the f5-aigr-perf containerized performance testing tool. The GUI allows users to configure and launch benchmark tests, view results with interactive charts, compare multiple runs side-by-side, and manage prompt CSV files — all through a browser.

The benchmark engine runs as an isolated subprocess (its own Python process and event loop) to guarantee that GUI overhead never affects timing accuracy. CLI and GUI produce identical benchmark numbers.

## 2. Architecture

### 2.1 Container Layout

Single Docker container with two modes:

- **GUI mode** (default): `docker run -p 8080:8080 f5-aigr-perf` — starts FastAPI/uvicorn web server
- **CLI mode**: `docker run f5-aigr-perf cli <args>` — passes args to `f5_find_max_rps.py`
- **Benchmark mode**: `docker run f5-aigr-perf benchmark <args>` — passes args to `f5_guardrails_perf.py`

### 2.2 File Structure

```
/app
├── f5_guardrails_perf.py        # benchmark engine (existing, +progress-file flag)
├── f5_find_max_rps.py           # capacity finder (existing, +progress-file flag)
├── f5_perf_prompts.csv          # default prompt corpus
├── server.py                    # FastAPI app + WebSocket + REST API
├── web/
│   ├── index.html               # SPA shell
│   ├── app.js                   # routing, state, API calls
│   ├── style.css                # dark theme matching existing reports
│   └── components/
│       ├── test-form.js         # test configuration form
│       ├── live-view.js         # real-time progress during test
│       ├── results-viewer.js    # single run results display
│       ├── compare-view.js      # side-by-side run comparison
│       └── prompts-manager.js   # CSV upload/preview/delete
├── data/                        # volume mount point
│   ├── prompts/                 # uploaded CSV files
│   └── results/                 # test run outputs
├── entrypoint.sh                # routes between GUI and CLI mode
├── Dockerfile
├── .dockerignore
└── requirements.txt
```

### 2.3 Subprocess Isolation Model

The web server never imports or calls the benchmark engine directly.

1. `POST /api/tests` receives JSON config
2. Server translates JSON to CLI flags and writes `config.json` to the run directory
3. Server spawns: `python f5_find_max_rps.py --progress-file <path>/progress.jsonl --reports-dir <path>/ <flags...>`
4. Subprocess runs in its own Python process with its own dedicated event loop
5. Server tracks subprocess PID, polls `returncode` for completion
6. One test at a time (queued, not parallel) to avoid overlapping results during demos

### 2.4 Live Progress Mechanism

Engine writes JSON lines to `progress.jsonl` (new `--progress-file` flag):

```json
{"ts": 1695312000.1, "type": "progress", "step": 2, "dispatched": 150, "completed": 120, "inflight": 30, "elapsed_s": 15.2, "phase": "measured"}
{"ts": 1695312045.3, "type": "step_complete", "step": 2, "target_rps": 20.0, "compliant": true, "g_p95": 142.3, "reason": "Compliant"}
{"ts": 1695312090.0, "type": "finished", "exit_code": 0}
```

WebSocket handler tails this file (seek to last read position every ~500ms) and forwards new lines to the browser. When `--progress-file` is not set (CLI mode), no file is written — zero behavior change.

### 2.5 Run Directory Structure

Each test run produces:

```
/app/data/results/<timestamp>-<slug>/
├── config.json        # parameters used (written by server before spawning)
├── progress.jsonl     # live progress log (written by engine)
├── summary.json       # aggregated results (written by f5_find_max_rps.py)
├── report.html        # self-contained HTML report (written by engine)
├── raw.csv            # per-request data (written by engine)
└── stdout.log         # captured subprocess stdout
```

## 3. Backend API

### 3.1 REST Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/health` | Container health check |
| `POST` | `/api/tests` | Start a new test run (JSON body with all CLI params) |
| `GET` | `/api/tests` | List all past test runs with summary metadata |
| `GET` | `/api/tests/{id}` | Full results for a specific run |
| `DELETE` | `/api/tests/{id}` | Delete a run and its artifacts |
| `GET` | `/api/tests/{id}/report` | Serve the generated HTML report |
| `POST` | `/api/tests/compare` | Accept 2-3 run IDs, return merged data for side-by-side charts |
| `GET` | `/api/prompts` | List available prompt CSV files |
| `POST` | `/api/prompts` | Upload a new CSV prompt file |
| `DELETE` | `/api/prompts/{name}` | Delete an uploaded prompt file |

### 3.2 WebSocket

| Path | Purpose |
|------|---------|
| `WS /api/tests/{id}/live` | Tails progress.jsonl and streams to browser |

### 3.3 Test Configuration Schema

`POST /api/tests` JSON body maps 1:1 to CLI flags:

```json
{
  "search_mode": "saturation",
  "f5_api_url": "https://aisec-eks.nextcnf.com/backend/v1/scans",
  "f5_token": "...",
  "prompt_file": "f5_perf_prompts.csv",
  "target_tokens": 250,
  "prefix_cache_rate": 40,
  "start_rps": 5,
  "step_rps": 1,
  "step_duration": "30s",
  "step_warmup": "15s",
  "max_steps": 15,
  "cooldown": "5s",
  "gpu_model": "NVIDIA H100",
  "gpu_count": 1,
  "target_latency_ms": 500,
  "sla_metric": "guardrail_p95",
  "max_429_pct": 2.0,
  "max_error_pct": 0.0,
  "max_timeouts": 0,
  "timeout": 120,
  "verify_tls": false,
  "notes": ""
}
```

Server validates, translates to CLI args, spawns subprocess.

## 4. Frontend Design

### 4.1 Visual Identity

Dark theme matching the existing HTML report output:
- Background: `#0b0f1a`
- Surface: `#111827`, `#1a2236`
- Border: `#1e2d45`
- Accent: `#00d4ff` (cyan)
- Success: `#34d399` (green)
- Warning: `#fbbf24` (amber)
- Error: `#f87171` (red)
- Text: `#e2e8f0`
- Muted: `#64748b`

Font: Inter / system sans-serif. Monospace for metrics.

### 4.2 Tech Stack

- Vanilla JS with ES modules (no framework, no build step)
- Chart.js from CDN (same version used in existing reports)
- Hash-based SPA routing
- Served as static files by FastAPI

### 4.3 Pages

**Dashboard (`#/`)**
- Recent test runs as cards: run name, GPU model, token count, status (running/completed/failed), key result (max RPS), timestamp
- "New Test" button in top right
- Running test shows live progress bar with current step
- Checkboxes on cards for selecting runs to compare

**New Test (`#/tests/new`)**
- Form with grouped sections:
  - **Target**: API URL, bearer token (masked input, pre-filled from env vars)
  - **Search Mode**: Toggle Saturation / SLA-Bounded / Binary — relevant fields show/hide contextually
  - **Workload**: Token count, prompt file dropdown, prefix cache rate slider, prompt mode
  - **Step Config**: Start RPS, step RPS, duration, warmup, max steps, cooldown
  - **Hardware Metadata**: GPU model, GPU count, guardrails version, environment, notes
- Preset dropdown: "250-token Saturation", "500-token Saturation", "250-token SLA 500ms", "500-token SLA 500ms" — pre-fills form as starting point (fully editable)
- "Start Test" button with input validation

**Live Test View (`#/tests/{id}/live`)**
- Auto-navigated to when a test starts
- Real-time Chart.js charts fed by WebSocket:
  - Throughput: offered vs completed RPS, building step by step
  - Latency: P50/P95/P99 growing as steps complete
- Step results table filling in row by row
- Current step progress: dispatched/completed/inflight counters
- Collapsible console log panel (raw stdout)

**Results View (`#/tests/{id}`)**
- Summary card: max RPS, GPU model, tokens, cache rate, pass/fail verdict
- Embedded Chart.js charts (throughput + latency curves)
- Step comparison table
- Buttons: "View Full Report" (opens HTML), "Download CSV", "Delete Run"

**Compare View (`#/compare?ids=a,b,c`)**
- Side-by-side summary cards
- Overlaid charts: latency curves from multiple runs on one chart, throughput overlaid
- Diff table: key metrics in columns per run

**Prompts Manager (`#/prompts`)**
- List of uploaded CSV files with row count and category breakdown
- Upload via button or drag-and-drop
- Click to preview first ~20 rows
- Delete per file

## 5. Changes to Existing Scripts

Total: ~35 lines across both files. All changes are additive and gated behind the new flag.

### 5.1 `f5_guardrails_perf.py`

- Add `--progress-file <path>` CLI flag (default: None)
- When set, write a JSON line at each `progress_interval` tick inside `run_open_loop_async` with: dispatched count, completed count, current inflight, elapsed time, phase
- Write a JSON line after `aggregate()` with summary data
- When not set: zero behavior change

### 5.2 `f5_find_max_rps.py`

- Accept `--progress-file` and pass through to the engine
- Write a `step_complete` JSON line after each step evaluation with: step number, target RPS, compliance verdict, key latency metrics, reason
- Write `summary.json` alongside existing HTML/CSV outputs
- When `--progress-file` not set: zero behavior change

## 6. Docker Configuration

### 6.1 Dockerfile

- Base: `python:3.12-slim`
- Install: `aiohttp`, `fastapi`, `uvicorn[standard]` via requirements.txt
- Copy: scripts, web/, entrypoint.sh
- Expose: 8080
- Volume: `/app/data`

### 6.2 Entrypoint

`entrypoint.sh` routes by first argument:
- No args or empty: `uvicorn server:app --host 0.0.0.0 --port ${PORT:-8080}`
- `cli`: shift, exec `python f5_find_max_rps.py "$@"`
- `benchmark`: shift, exec `python f5_guardrails_perf.py "$@"`

### 6.3 Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `F5_API_URL` | Pre-fill target endpoint in GUI and CLI | Hardcoded default in scripts |
| `F5_BEARER_TOKEN` | Pre-fill auth token | Hardcoded default in scripts |
| `PORT` | Web server listen port | 8080 |

### 6.4 .dockerignore

Excludes: `wheels_linux/`, `results/`, `data/`, `__pycache__/`, `.DS_Store`, `docs/`

## 7. Usage Examples

```bash
# Build
docker build -t f5-aigr-perf .

# GUI mode
docker run -p 8080:8080 -v $(pwd)/data:/app/data f5-aigr-perf

# GUI with pre-configured endpoint
docker run -p 8080:8080 \
  -e F5_API_URL=https://my-endpoint/v1/scans \
  -e F5_BEARER_TOKEN=mytoken \
  -v $(pwd)/data:/app/data f5-aigr-perf

# CLI mode (identical accuracy to running scripts directly)
docker run -v $(pwd)/data:/app/data f5-aigr-perf cli \
  --prompt-file f5_perf_prompts.csv --find-saturation \
  --start-rps 5 --step-rps 1 --step-duration 30s \
  --target-tokens 250 --gpu-model "NVIDIA H100" --prefix-cache-rate 40

# Single benchmark run via CLI
docker run -v $(pwd)/data:/app/data f5-aigr-perf benchmark \
  --rps 30 --duration 60s --prompt-file f5_perf_prompts.csv
```
