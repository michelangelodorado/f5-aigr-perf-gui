import asyncio
import csv
import json
import os
import shutil
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="F5 AI Guardrails Performance Tool")

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
RESULTS_DIR = DATA_DIR / "results"
PROMPTS_DIR = DATA_DIR / "prompts"
DEFAULT_PROMPTS = Path("/app/f5_perf_prompts.csv")
SCRIPTS_DIR = Path("/app")

RESULTS_DIR.mkdir(parents=True, exist_ok=True)
PROMPTS_DIR.mkdir(parents=True, exist_ok=True)

if not (PROMPTS_DIR / "f5_perf_prompts.csv").exists() and DEFAULT_PROMPTS.exists():
    shutil.copy2(DEFAULT_PROMPTS, PROMPTS_DIR / "f5_perf_prompts.csv")

current_test: dict = {"process": None, "run_id": None}


@app.get("/api/health")
async def health():
    running = current_test["process"] is not None and current_test["process"].poll() is None
    return {"status": "ok", "test_running": running, "run_id": current_test["run_id"] if running else None}


class TestConfig(BaseModel):
    search_mode: str = "saturation"
    f5_api_url: str = os.getenv("F5_API_URL", "")
    f5_token: str = os.getenv("F5_BEARER_TOKEN", "")
    prompt_file: str = "f5_perf_prompts.csv"
    target_tokens: int = 250
    prefix_cache_rate: Optional[float] = None
    start_rps: float = 5.0
    step_rps: float = 1.0
    min_rps: float = 10.0
    max_rps: float = 100.0
    rps_tolerance: float = 2.0
    step_duration: str = "30s"
    step_warmup: str = "15s"
    max_steps: int = 15
    cooldown: str = "5s"
    gpu_model: str = ""
    gpu_count: int = 0
    target_latency_ms: float = 500.0
    sla_metric: str = "guardrail_p95"
    max_429_pct: float = 2.0
    max_error_pct: float = 0.0
    max_timeouts: int = 0
    timeout: float = 120.0
    verify_tls: bool = False
    prompt_mode: str = "unique"
    guardrails_version: str = ""
    environment: str = ""
    notes: str = ""


def build_cli_args(config: TestConfig, reports_dir: str, progress_file: str) -> list[str]:
    args = [
        "python", str(SCRIPTS_DIR / "f5_find_max_rps.py"),
        "--f5-api-url", config.f5_api_url,
        "--f5-token", config.f5_token,
        "--prompt-file", str(PROMPTS_DIR / config.prompt_file),
        "--target-tokens", str(config.target_tokens),
        "--start-rps", str(config.start_rps),
        "--step-rps", str(config.step_rps),
        "--step-duration", config.step_duration,
        "--step-warmup", config.step_warmup,
        "--max-steps", str(config.max_steps),
        "--cooldown", config.cooldown,
        "--gpu-model", config.gpu_model,
        "--gpu-count", str(config.gpu_count),
        "--target-latency-ms", str(config.target_latency_ms),
        "--sla-metric", config.sla_metric,
        "--max-429-pct", str(config.max_429_pct),
        "--max-error-pct", str(config.max_error_pct),
        "--max-timeouts", str(config.max_timeouts),
        "--timeout", str(config.timeout),
        "--prompt-mode", config.prompt_mode,
        "--reports-dir", reports_dir,
        "--progress-file", progress_file,
        "--skip-preflight",
        "--random-seed", "42",
        "--min-rps", str(config.min_rps),
        "--max-rps", str(config.max_rps),
        "--rps-tolerance", str(config.rps_tolerance),
    ]
    if config.search_mode == "saturation":
        args.append("--find-saturation")
    elif config.search_mode == "binary":
        args.extend(["--search-mode", "binary"])
    if config.prefix_cache_rate is not None:
        args.extend(["--prefix-cache-rate", str(config.prefix_cache_rate)])
    if config.verify_tls:
        args.append("--verify-tls")
    if config.guardrails_version:
        args.extend(["--guardrails-version", config.guardrails_version])
    if config.environment:
        args.extend(["--environment", config.environment])
    if config.notes:
        args.extend(["--notes", config.notes])
    return args


@app.post("/api/tests")
async def start_test(config: TestConfig):
    if current_test["process"] is not None and current_test["process"].poll() is None:
        raise HTTPException(409, "A test is already running")

    if not config.f5_api_url:
        raise HTTPException(400, "f5_api_url is required")
    if not config.f5_token:
        raise HTTPException(400, "f5_token is required")

    prompt_path = PROMPTS_DIR / config.prompt_file
    if not prompt_path.exists():
        raise HTTPException(400, f"Prompt file not found: {config.prompt_file}")

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    run_dir = RESULTS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    config_path = run_dir / "config.json"
    config_path.write_text(json.dumps(config.model_dump(), indent=2, default=str), encoding="utf-8")

    progress_file = str(run_dir / "progress.jsonl")
    stdout_log = open(run_dir / "stdout.log", "w")

    cli_args = build_cli_args(config, str(run_dir), progress_file)

    proc = subprocess.Popen(
        cli_args,
        stdout=stdout_log,
        stderr=subprocess.STDOUT,
        cwd=str(SCRIPTS_DIR),
    )
    current_test["process"] = proc
    current_test["run_id"] = run_id
    current_test["stdout_log"] = stdout_log

    return {"run_id": run_id, "status": "running"}


def get_run_status(run_dir: Path, run_id: str) -> dict:
    status = "completed"
    if current_test["run_id"] == run_id:
        proc = current_test["process"]
        if proc is not None and proc.poll() is None:
            status = "running"
        elif proc is not None and proc.returncode != 0:
            status = "failed"

    config = {}
    config_path = run_dir / "config.json"
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    summary = None
    for sub in sorted(run_dir.iterdir()):
        if sub.is_dir():
            sj = sub / "summary.json"
            if sj.exists():
                try:
                    summary = json.loads(sj.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    pass
                break

    return {
        "run_id": run_id,
        "status": status,
        "config": config,
        "summary": summary,
        "created": run_dir.stat().st_ctime if run_dir.exists() else 0,
    }


@app.get("/api/tests")
async def list_tests():
    if not RESULTS_DIR.exists():
        return []
    runs = []
    for d in sorted(RESULTS_DIR.iterdir(), reverse=True):
        if d.is_dir() and not d.name.startswith("."):
            runs.append(get_run_status(d, d.name))
    return runs


@app.get("/api/tests/{run_id}")
async def get_test(run_id: str):
    run_dir = RESULTS_DIR / run_id
    if not run_dir.exists():
        raise HTTPException(404, "Run not found")
    return get_run_status(run_dir, run_id)


@app.delete("/api/tests/{run_id}")
async def delete_test(run_id: str):
    run_dir = RESULTS_DIR / run_id
    if not run_dir.exists():
        raise HTTPException(404, "Run not found")
    if current_test["run_id"] == run_id:
        proc = current_test["process"]
        if proc is not None and proc.poll() is None:
            raise HTTPException(409, "Cannot delete a running test")
    shutil.rmtree(run_dir)
    return {"deleted": run_id}


@app.get("/api/tests/{run_id}/report")
async def get_report(run_id: str):
    run_dir = RESULTS_DIR / run_id
    if not run_dir.exists():
        raise HTTPException(404, "Run not found")
    for sub in sorted(run_dir.iterdir()):
        if sub.is_dir():
            for f in sub.iterdir():
                if f.suffix == ".html":
                    return HTMLResponse(f.read_text(encoding="utf-8"))
    raise HTTPException(404, "Report not generated yet")


@app.post("/api/tests/compare")
async def compare_tests(body: dict):
    ids = body.get("ids", [])
    if len(ids) < 2 or len(ids) > 3:
        raise HTTPException(400, "Provide 2-3 run IDs")
    runs = []
    for run_id in ids:
        run_dir = RESULTS_DIR / run_id
        if not run_dir.exists():
            raise HTTPException(404, f"Run not found: {run_id}")
        runs.append(get_run_status(run_dir, run_id))
    return runs


@app.websocket("/api/tests/{run_id}/live")
async def live_progress(ws: WebSocket, run_id: str):
    await ws.accept()
    run_dir = RESULTS_DIR / run_id
    progress_path = run_dir / "progress.jsonl"
    file_pos = 0

    try:
        while True:
            is_running = (
                current_test["run_id"] == run_id
                and current_test["process"] is not None
                and current_test["process"].poll() is None
            )

            if progress_path.exists():
                with open(progress_path, "r") as f:
                    f.seek(file_pos)
                    new_lines = f.readlines()
                    file_pos = f.tell()
                for line in new_lines:
                    line = line.strip()
                    if line:
                        await ws.send_text(line)

            if not is_running:
                if progress_path.exists():
                    with open(progress_path, "r") as f:
                        f.seek(file_pos)
                        remaining = f.readlines()
                    for line in remaining:
                        line = line.strip()
                        if line:
                            await ws.send_text(line)

                proc = current_test["process"] if current_test["run_id"] == run_id else None
                exit_code = proc.returncode if proc else 0
                await ws.send_text(json.dumps({"type": "finished", "exit_code": exit_code}))
                break

            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        pass


@app.get("/api/prompts")
async def list_prompts():
    prompts = []
    for f in sorted(PROMPTS_DIR.iterdir()):
        if f.suffix == ".csv":
            row_count = 0
            categories = {}
            try:
                with f.open("r", encoding="utf-8-sig", newline="") as fh:
                    reader = csv.DictReader(fh)
                    for row in reader:
                        row_count += 1
                        cat = (row.get("category") or "uncategorized").strip()
                        categories[cat] = categories.get(cat, 0) + 1
            except Exception:
                pass
            prompts.append({
                "name": f.name,
                "rows": row_count,
                "categories": categories,
                "size_bytes": f.stat().st_size,
            })
    return prompts


@app.post("/api/prompts")
async def upload_prompt(file: UploadFile):
    if not file.filename or not file.filename.endswith(".csv"):
        raise HTTPException(400, "Only CSV files are accepted")
    dest = PROMPTS_DIR / file.filename
    content = await file.read()
    dest.write_bytes(content)
    return {"name": file.filename, "size_bytes": len(content)}


@app.delete("/api/prompts/{name}")
async def delete_prompt(name: str):
    path = PROMPTS_DIR / name
    if not path.exists():
        raise HTTPException(404, "Prompt file not found")
    path.unlink()
    return {"deleted": name}


@app.get("/api/prompts/{name}/preview")
async def preview_prompt(name: str, limit: int = 20):
    path = PROMPTS_DIR / name
    if not path.exists():
        raise HTTPException(404, "Prompt file not found")
    rows = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames or []
            for i, row in enumerate(reader):
                if i >= limit:
                    break
                rows.append(row)
    except Exception as e:
        raise HTTPException(500, f"Error reading CSV: {e}")
    return {"headers": headers, "rows": rows}


@app.get("/api/defaults")
async def get_defaults():
    return {
        "f5_api_url": os.getenv("F5_API_URL", ""),
        "f5_token": os.getenv("F5_BEARER_TOKEN", ""),
    }


# Static file serving — only mount if web/ directory exists (created by Task 3)
if (SCRIPTS_DIR / "web").exists():
    app.mount("/", StaticFiles(directory=str(SCRIPTS_DIR / "web"), html=True), name="static")
