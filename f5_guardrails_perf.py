#!/usr/bin/env python3
# =============================================================================
# f5_guardrails_perf.py
# F5 AI Guardrails Async Open-Loop Fixed-RPS Performance Benchmark
#
# Purpose:
#   Answer a reproducible question such as:
#     "What guardrails latency does F5 AI Guardrails achieve at 30 offered RPS
#      with a 250-token payload on an NVIDIA L40S GPU?"
#
# Metrics tracked:
#   - Scheduled arrivals vs actual dispatched arrivals (detects generator starvation)
#   - Exact HTTP 429 rate and retry-after header tracking
#   - Peak concurrent in-flight requests observed
#   - Server-reported guardrails latency P50/P95/P99
#   - Round-trip minus guardrails-time overhead
#
# Why async/open-loop:
#   - RPS is the independent test input.
#   - Requests are scheduled at a fixed arrival rate without waiting for prior
#     requests to complete.
#   - High server latency therefore increases observed in-flight concurrency
#     instead of silently reducing the offered RPS.
#   - A max-inflight value remains only as a safety fuse. If it is reached, the
#     run is marked INVALID and scheduling stops instead of skipping slots and
#     pretending the lower rate was the intended test.
#
# Key measurements:
#   - Target RPS and actual offered/dispatched RPS
#   - HTTP 2xx, 429, other HTTP errors, client timeouts/errors
#   - End-to-end HTTP RTT P50/P95/P99
#   - Server-reported guardrails latency P50/P95/P99
#   - Round-trip minus guardrails-time overhead
#   - Peak observed in-flight requests
#   - Scheduler delay P50/P95/P99
#   - Completion throughput during the measured window
#
# Prompt corpus:
#   CSV columns: prompt, expected, category
#   expected=true means expected blocked; expected=false means expected allowed.
#   --prompt-mode unique: every scheduled request receives a random numeric
#     Benchmark-ID near the beginning to reduce vLLM user-prefix cache reuse.
#   --prompt-mode repeat: no per-request random value is added; each CSV template
#     is sent identically whenever it is reused, making the workload cache-friendly.
#   --prefix-cache-rate N: mixed workload mode. N percent of requests use the
#     repeat/cache-friendly form and the remainder use the unique/cache-resistant
#     form. This controls the REQUEST MIX, not the actual vLLM prefix-cache hit rate.
#
# Token sizing:
#   --tokenizer-model <HF model/tokenizer> gives exact token sizing.
#   Without it, --target-tokens is a whitespace-unit approximation and the
#   report states that clearly.
# =============================================================================

SCRIPT_VERSION = "2.3.0"
SCRIPT_NAME = "f5_guardrails_perf.py"

import argparse
import asyncio
import csv
import html
import json
import os
import random
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import aiohttp
except ImportError as exc:
    raise SystemExit(
        "ERROR: aiohttp is required for the async fixed-RPS generator.\n"
        "Install it with: pip install aiohttp"
    ) from exc

# -----------------------------------------------------------------------------
# Defaults
# -----------------------------------------------------------------------------
DEFAULT_API_URL = "https://aigr.nextcnf.com/backend/v1/scans"
DEFAULT_F5_BEARER_TOKEN = "MDFhMGMyYTEtZjFlZC03MGI5LWJmN2ItY2VjMTIxOGE2YWU5/CWe7EF4IXG42dAyYn7GFJmbnOJxuBo0OJIDS07cWNN1aPxmcs0Lhfujh5ZPoYcZu3f5JhF6abhWX6S4OGAg"

DEFAULT_RPS = 30.0
DEFAULT_DURATION_SECONDS = 300.0
DEFAULT_WARMUP_SECONDS = 0.0
DEFAULT_TARGET_TOKENS = 250
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_MAX_INFLIGHT = 5000
DEFAULT_CONNECTION_LIMIT = 0      # aiohttp: 0 = unlimited connector connections
DEFAULT_PROGRESS_INTERVAL = 5.0
DEFAULT_REPORTS_DIR = "results/performance"
DEFAULT_HTML_MAX_ROWS = 1000

BASE_PROMPT = (
    "This is a benign performance benchmark request for an AI safety guardrail. "
    "Analyse the supplied text normally and return the configured guardrail result. "
    "The content is intentionally neutral and contains no harmful instructions, "
    "sensitive personal data, secrets, or policy bypass attempts."
)


@dataclass
class PromptTemplate:
    prompt: str
    expected_blocked: bool
    category: str
    source_index: int


@dataclass
class PreparedRequest:
    unique_id: str
    cache_profile: str
    prompt: str
    expected_blocked: bool
    category: str
    template_index: int
    token_count: int


@dataclass
class RequestResult:
    request_id: int
    phase: str
    unique_id: str
    cache_profile: str
    expected_blocked: bool
    category: str
    token_count: int
    scheduled_offset_ms: float
    start_offset_ms: float
    completion_offset_ms: float
    schedule_delay_ms: float
    http_status: int
    outcome: str
    response_time_ms: float
    cai_time_ms: Optional[float]
    overhead_ms: Optional[float]
    response_bytes: int
    error: str
    retry_after: str
    inflight_at_start: int
    prompt_pool_index: int


# -----------------------------------------------------------------------------
# Utility helpers
# -----------------------------------------------------------------------------
def parse_duration(value: str) -> float:
    text = str(value).strip().lower()
    if not text:
        raise argparse.ArgumentTypeError("Duration cannot be empty")
    units = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
    for suffix in ("ms", "s", "m", "h"):
        if text.endswith(suffix):
            try:
                return float(text[:-len(suffix)]) * units[suffix]
            except ValueError as exc:
                raise argparse.ArgumentTypeError(f"Invalid duration: {value}") from exc
    try:
        return float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid duration: {value}") from exc


def format_duration(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.2f}h"
    if seconds >= 60:
        return f"{seconds / 60:.2f}m"
    return f"{seconds:.2f}s"


def percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (len(sorted_values) - 1) * pct
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = rank - lower
    return float(sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight)


def latency_stats(values) -> dict:
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return {k: 0.0 for k in ("avg", "p50", "p95", "p99", "min", "max", "stdev")} | {"count": 0}
    return {
        "count": len(vals),
        "avg": statistics.mean(vals),
        "p50": percentile(vals, 0.50),
        "p95": percentile(vals, 0.95),
        "p99": percentile(vals, 0.99),
        "min": vals[0],
        "max": vals[-1],
        "stdev": statistics.pstdev(vals) if len(vals) > 1 else 0.0,
    }


def safe_float_header(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return None


# -----------------------------------------------------------------------------
# Prompt loading / unique payload construction
# -----------------------------------------------------------------------------
def _parse_expected(value: str) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "blocked", "block"}


def load_prompt_templates(prompt_text: Optional[str], prompt_file: Optional[str]) -> list[PromptTemplate]:
    if prompt_file:
        path = Path(prompt_file)
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            fields = [str(x or "").strip().lower() for x in (reader.fieldnames or [])]
            if "prompt" in fields:
                keymap = {str(k).strip().lower(): k for k in (reader.fieldnames or [])}
                pkey = keymap["prompt"]
                ekey = keymap.get("expected")
                ckey = keymap.get("category")
                out = []
                for i, row in enumerate(reader, 1):
                    prompt = str(row.get(pkey, "") or "").strip()
                    if not prompt:
                        continue
                    expected = _parse_expected(row.get(ekey, "false")) if ekey else False
                    category = str(row.get(ckey, "") or "").strip() if ckey else ""
                    out.append(PromptTemplate(prompt, expected, category, i - 1))
                if not out:
                    raise SystemExit(f"ERROR: no prompts found in CSV: {prompt_file}")
                return out

        # Legacy plain-text file fallback
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            raise SystemExit(f"ERROR: prompt file is empty: {prompt_file}")
        return [PromptTemplate(text, False, "unspecified", 0)]

    if prompt_text:
        return [PromptTemplate(prompt_text.strip(), False, "unspecified", 0)]
    return [PromptTemplate(BASE_PROMPT, False, "benign", 0)]


def interleave_templates(templates: list[PromptTemplate]) -> list[PromptTemplate]:
    blocked = [t for t in templates if t.expected_blocked]
    allowed = [t for t in templates if not t.expected_blocked]
    if not blocked or not allowed:
        return list(templates)
    ordered = []
    for i in range(max(len(blocked), len(allowed))):
        if i < len(blocked):
            ordered.append(blocked[i])
        if i < len(allowed):
            ordered.append(allowed[i])
    return ordered


def materialize_prompt(template: str, prompt_mode: str, unique_id: str = "") -> str:
    """Build the user payload for cache A/B testing.

    unique:
      Inject a different random numeric Benchmark-ID near the beginning of every
      request. This intentionally breaks the user-controlled common prefix as
      early as possible.

    repeat:
      Do not inject a per-request value. If the CSV contains {{UNIQUE_ID}}, replace
      it with the constant value STATIC so a given template is byte-for-byte
      identical every time it is reused.
    """
    if prompt_mode == "unique":
        if "{{UNIQUE_ID}}" in template:
            return template.replace("{{UNIQUE_ID}}", unique_id)
        return f"Benchmark-ID {unique_id}. {template}"

    # Cache-friendly repeated mode. Keep every reuse of a template identical.
    if "{{UNIQUE_ID}}" in template:
        return template.replace("{{UNIQUE_ID}}", "STATIC")
    return template


def size_whitespace_prompt(full_prompt: str, target_tokens: int) -> tuple[str, int]:
    words = full_prompt.split()
    filler = "benchmark neutral guardrail latency performance request payload token".split()
    while len(words) < target_tokens:
        words.extend(filler)
    words = words[:target_tokens]
    return " ".join(words), len(words)


def load_hf_tokenizer(model_name: str, local_files_only: bool = False):
    try:
        from transformers import AutoTokenizer
    except ImportError as exc:
        raise SystemExit(
            "ERROR: --tokenizer-model requires transformers.\n"
            "Install: pip install transformers sentencepiece"
        ) from exc
    try:
        return AutoTokenizer.from_pretrained(model_name, local_files_only=local_files_only)
    except Exception as exc:
        raise SystemExit(f"ERROR loading tokenizer '{model_name}': {exc}") from exc


def size_exact_token_prompt(tokenizer, full_prompt: str, target_tokens: int) -> tuple[str, int]:
    if target_tokens <= 0:
        raise ValueError("target_tokens must be > 0")
    filler = (
        " benchmark neutral guardrail latency performance request payload measurement "
        " reproducible throughput guardrail evaluation ordinary text"
    )
    source = full_prompt + (filler * max(8, target_tokens // 4 + 4))
    ids = tokenizer.encode(source, add_special_tokens=False)
    candidate = tokenizer.decode(ids[:target_tokens], skip_special_tokens=True,
                                 clean_up_tokenization_spaces=False)
    for _ in range(30):
        cids = tokenizer.encode(candidate, add_special_tokens=False)
        if len(cids) == target_tokens:
            return candidate, len(cids)
        if len(cids) > target_tokens:
            candidate = tokenizer.decode(cids[:target_tokens], skip_special_tokens=True,
                                         clean_up_tokenization_spaces=False)
        else:
            candidate += filler
            cids = tokenizer.encode(candidate, add_special_tokens=False)
            candidate = tokenizer.decode(cids[:target_tokens], skip_special_tokens=True,
                                         clean_up_tokenization_spaces=False)
    final = len(tokenizer.encode(candidate, add_special_tokens=False))
    if final != target_tokens:
        raise RuntimeError(f"Could not construct exactly {target_tokens} tokens; final={final}")
    return candidate, final


def prepare_request_plan(templates: list[PromptTemplate], total_requests: int,
                         target_tokens: int, tokenizer_model: Optional[str],
                         tokenizer_local_only: bool, random_seed: int = 42,
                         prompt_mode: str = "unique",
                         cache_friendly_percent: Optional[float] = None,
                         request_id_offset: int = 0) -> tuple[list[PreparedRequest], dict]:
    """Pre-build request payloads before the timed test.

    When cache_friendly_percent is supplied, the plan becomes a deterministic
    mixed workload: exactly (within rounding) that percentage of requests use
    the repeated/cache-friendly form and the rest use per-request unique IDs.

    IMPORTANT: this percentage describes the generated workload mix. It is NOT
    the actual vLLM prefix-cache hit rate, which depends on block alignment,
    eviction, common internal prefixes, KV-cache pressure, and runtime behavior.
    """
    mixed = cache_friendly_percent is not None
    if mixed and not (0.0 <= cache_friendly_percent <= 100.0):
        raise ValueError("cache_friendly_percent must be between 0 and 100")

    effective_mode = "mixed" if mixed else prompt_mode
    if total_requests <= 0:
        return [], {
            "method": "n/a", "tokenizer_model": tokenizer_model or "",
            "requested_tokens": target_tokens, "verified_min_tokens": 0,
            "verified_max_tokens": 0, "exact": False, "prompt_template_count": len(templates),
            "prepared_requests": 0, "unique_request_ids": prompt_mode == "unique",
            "prompt_mode": effective_mode,
            "cache_friendly_target_pct": cache_friendly_percent,
            "cache_friendly_requests": 0, "cache_resistant_requests": 0,
            "cache_friendly_actual_pct": 0.0,
            "expected_blocked_requests": 0, "expected_allowed_requests": 0,
            "category_counts": {},
        }

    ordered = interleave_templates(templates)
    rng = random.Random(random_seed)
    tokenizer = load_hf_tokenizer(tokenizer_model, tokenizer_local_only) if tokenizer_model else None
    method = "Hugging Face tokenizer (exact model-token count)" if tokenizer else \
             "Whitespace-unit approximation (NOT model-token exact)"

    # Choose an exact-sized, seeded subset so the requested cache-friendly mix
    # is reproducible and not clustered at the start/end of the run.
    cache_indices = set()
    if mixed:
        cache_count = int(round(total_requests * float(cache_friendly_percent) / 100.0))
        if cache_count > 0:
            cache_indices = set(rng.sample(range(total_requests), cache_count))

    plan = []
    counts = []
    expected_counts = Counter()
    category_counts = Counter()
    profile_counts = Counter()

    for i in range(total_requests):
        template = ordered[i % len(ordered)]
        request_number = request_id_offset + i + 1
        request_mode = ("repeat" if i in cache_indices else "unique") if mixed else prompt_mode

        if request_mode == "unique":
            random_number = rng.randrange(10**11, 10**12)
            unique_id = f"perf-{request_number:08d}-{random_number}"
        else:
            unique_id = "STATIC"

        full = materialize_prompt(template.prompt, request_mode, unique_id)
        if tokenizer:
            prompt, count = size_exact_token_prompt(tokenizer, full, target_tokens)
        else:
            prompt, count = size_whitespace_prompt(full, target_tokens)

        plan.append(PreparedRequest(
            unique_id=unique_id,
            cache_profile=request_mode,
            prompt=prompt,
            expected_blocked=template.expected_blocked,
            category=template.category,
            template_index=template.source_index,
            token_count=count,
        ))
        counts.append(count)
        profile_counts[request_mode] += 1
        expected_counts["blocked" if template.expected_blocked else "allowed"] += 1
        category_counts[template.category or "(uncategorised)"] += 1

    friendly = profile_counts["repeat"]
    resistant = profile_counts["unique"]
    actual_pct = friendly / len(plan) * 100.0 if plan else 0.0

    return plan, {
        "method": method,
        "tokenizer_model": tokenizer_model or "",
        "requested_tokens": target_tokens,
        "verified_min_tokens": min(counts),
        "verified_max_tokens": max(counts),
        "exact": bool(tokenizer) and min(counts) == max(counts) == target_tokens,
        "prompt_template_count": len(templates),
        "prepared_requests": len(plan),
        "unique_request_ids": resistant > 0,
        "prompt_mode": effective_mode,
        "cache_friendly_target_pct": cache_friendly_percent,
        "cache_friendly_requests": friendly,
        "cache_resistant_requests": resistant,
        "cache_friendly_actual_pct": actual_pct,
        "expected_blocked_requests": expected_counts["blocked"],
        "expected_allowed_requests": expected_counts["allowed"],
        "category_counts": dict(category_counts),
    }


# -----------------------------------------------------------------------------
# Pre-flight Health Check
# -----------------------------------------------------------------------------
async def preflight_probe_async(
    api_url: str,
    bearer_token: str,
    verify_tls: bool,
    timeout_seconds: float = 10.0,
) -> tuple[bool, int, str, Optional[float]]:
    connector = aiohttp.TCPConnector(ssl=verify_tls if verify_tls else False)
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    body = {"input": "Benchmark preflight health check probe"}
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {bearer_token}",
    }
    try:
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async with session.post(api_url, json=body, headers=headers) as resp:
                data = await resp.read()
                status = resp.status
                cai_time = safe_float_header(resp.headers.get("x-cai-time"))
                body_text = data[:300].decode("utf-8", errors="replace").replace("\n", " ")
                return (200 <= status < 300), status, body_text, cai_time
    except asyncio.TimeoutError:
        return False, 0, "Connection timed out", None
    except aiohttp.ClientError as exc:
        return False, 0, f"Client error: {exc}", None
    except Exception as exc:
        return False, 0, f"Error: {exc}", None


def preflight_probe(api_url: str, bearer_token: str, verify_tls: bool, timeout_seconds: float = 10.0) -> tuple[bool, int, str, Optional[float]]:
    return asyncio.run(preflight_probe_async(api_url, bearer_token, verify_tls, timeout_seconds))


# -----------------------------------------------------------------------------
# Async F5 API call
# -----------------------------------------------------------------------------
async def call_guardrails_async(
    session: aiohttp.ClientSession,
    state: dict,
    request_id: int,
    phase: str,
    prepared: PreparedRequest,
    scheduled_time: float,
    measurement_start: float,
    api_url: str,
    bearer_token: str,
    verbose: bool,
) -> RequestResult:
    state["current"] += 1
    inflight_at_start = state["current"]
    state["peak"] = max(state["peak"], state["current"])

    started = time.perf_counter()
    status = 0
    outcome = ""
    cai_time_ms = None
    response_bytes = 0
    error = ""
    retry_after = ""

    body = {"input": prepared.prompt}
    if verbose:
        body["verbose"] = "true"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {bearer_token}",
    }

    try:
        async with session.post(api_url, json=body, headers=headers) as response:
            payload = await response.read()
            ended = time.perf_counter()
            status = response.status
            response_bytes = len(payload)
            retry_after = response.headers.get("Retry-After", "")
            cai_time_ms = safe_float_header(response.headers.get("x-cai-time"))

            if 200 <= status < 300:
                try:
                    data = json.loads(payload.decode("utf-8", errors="replace"))
                    outcome = str(data.get("result", {}).get("outcome", "")).lower()
                except Exception:
                    outcome = "2xx-non-json"
            elif status == 429:
                outcome = "rate-limited"
            else:
                outcome = f"http-{status}"
                error = payload[:300].decode("utf-8", errors="replace").replace("\n", " ")

    except asyncio.TimeoutError as exc:
        ended = time.perf_counter()
        outcome = "timeout"
        error = str(exc) or "request timed out"
    except aiohttp.ClientError as exc:
        ended = time.perf_counter()
        outcome = "request-error"
        error = str(exc)
    except Exception as exc:
        ended = time.perf_counter()
        outcome = "client-error"
        error = str(exc)
    finally:
        state["current"] -= 1

    rtt_ms = (ended - started) * 1000.0
    overhead_ms = None
    if 200 <= status < 300 and cai_time_ms is not None:
        overhead_ms = max(0.0, rtt_ms - cai_time_ms)

    return RequestResult(
        request_id=request_id,
        phase=phase,
        unique_id=prepared.unique_id,
        cache_profile=prepared.cache_profile,
        expected_blocked=prepared.expected_blocked,
        category=prepared.category,
        token_count=prepared.token_count,
        scheduled_offset_ms=(scheduled_time - measurement_start) * 1000.0,
        start_offset_ms=(started - measurement_start) * 1000.0,
        completion_offset_ms=(ended - measurement_start) * 1000.0,
        schedule_delay_ms=max(0.0, (started - scheduled_time) * 1000.0),
        http_status=status,
        outcome=outcome,
        response_time_ms=rtt_ms,
        cai_time_ms=cai_time_ms,
        overhead_ms=overhead_ms,
        response_bytes=response_bytes,
        error=error,
        retry_after=retry_after,
        inflight_at_start=inflight_at_start,
        prompt_pool_index=prepared.template_index,
    )


# -----------------------------------------------------------------------------
# True async open-loop fixed-RPS scheduler
# -----------------------------------------------------------------------------
async def run_open_loop_async(
    warmup_plan: list[PreparedRequest],
    measured_plan: list[PreparedRequest],
    target_rps: float,
    warmup_seconds: float,
    duration_seconds: float,
    max_inflight: int,
    connection_limit: int,
    api_url: str,
    bearer_token: str,
    timeout_seconds: float,
    verify_tls: bool,
    verbose: bool,
    progress_interval: float,
    progress_file: str = None,
) -> tuple[list[RequestResult], dict]:
    if target_rps <= 0:
        raise ValueError("target_rps must be > 0")
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be > 0")
    if max_inflight < 0:
        raise ValueError("max_inflight cannot be negative; use 0 for unlimited")
    if connection_limit < 0:
        raise ValueError("connection_limit cannot be negative; use 0 for unlimited")

    interval = 1.0 / target_rps
    warmup_count = len(warmup_plan)
    measured_count = len(measured_plan)
    plan = warmup_plan + measured_plan

    connector = aiohttp.TCPConnector(
        ssl=verify_tls if verify_tls else False,
        limit=connection_limit,
        limit_per_host=connection_limit,
        enable_cleanup_closed=True,
        ttl_dns_cache=300,
    )
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    state = {"current": 0, "peak": 0}
    tasks: set[asyncio.Task] = set()
    all_results: list[RequestResult] = []
    dispatched_warmup = 0
    dispatched_measured = 0
    generator_saturated = False
    saturation_reason = ""
    scheduler_late_slots = 0
    benchmark_start_dt = datetime.now()
    benchmark_start = time.perf_counter()
    measurement_start = benchmark_start + warmup_seconds
    generation_end = measurement_start + duration_seconds
    last_progress = benchmark_start

    print(
        f"Async open-loop: {target_rps:.2f} RPS | warmup={warmup_seconds:.1f}s | "
        f"measured={duration_seconds:.1f}s | measured arrivals={measured_count} | "
        f"max-inflight={'unlimited' if max_inflight == 0 else max_inflight} | "
        f"connection-limit={'unlimited' if connection_limit == 0 else connection_limit}"
    )

    def reap_done():
        done = {t for t in tasks if t.done()}
        if done:
            tasks.difference_update(done)
            for task in done:
                try:
                    all_results.append(task.result())
                except Exception as exc:
                    print(f"Async worker error: {exc}")

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        for slot, prepared in enumerate(plan):
            phase = "warmup" if slot < warmup_count else "measured"
            scheduled_time = benchmark_start + slot * interval

            delay = scheduled_time - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)

            reap_done()
            now = time.perf_counter()
            if now - scheduled_time >= interval:
                scheduler_late_slots += 1

            active = len(tasks)
            if max_inflight > 0 and active >= max_inflight:
                generator_saturated = True
                saturation_reason = (
                    f"max-inflight safety fuse reached ({max_inflight}) at slot {slot + 1}; "
                    "run is invalid as a fixed-RPS benchmark"
                )
                print(f"\nERROR: {saturation_reason}")
                break

            task = asyncio.create_task(
                call_guardrails_async(
                    session=session,
                    state=state,
                    request_id=slot + 1,
                    phase=phase,
                    prepared=prepared,
                    scheduled_time=scheduled_time,
                    measurement_start=measurement_start,
                    api_url=api_url,
                    bearer_token=bearer_token,
                    verbose=verbose,
                )
            )
            tasks.add(task)
            if phase == "warmup":
                dispatched_warmup += 1
            else:
                dispatched_measured += 1

            if progress_interval > 0 and now - last_progress >= progress_interval:
                elapsed_total = now - benchmark_start
                measured_elapsed = max(0.0, min(duration_seconds, now - measurement_start))
                completed = len(all_results)
                c429 = sum(1 for r in all_results if r.http_status == 429)
                timeouts = sum(1 for r in all_results if r.outcome == "timeout")
                print(
                    f"  total={elapsed_total:7.1f}s measured={measured_elapsed:7.1f}/{duration_seconds:.1f}s "
                    f"dispatched={dispatched_measured}/{measured_count} "
                    f"done={completed} in-flight={len(tasks)} peak={state['peak']} "
                    f"429={c429} timeout={timeouts}"
                )
                if progress_file:
                    import json as _json
                    try:
                        with open(progress_file, "a") as pf:
                            pf.write(_json.dumps({
                                "ts": time.time(),
                                "type": "progress",
                                "step": 0,
                                "dispatched": dispatched_measured,
                                "completed": completed,
                                "inflight": len(tasks),
                                "peak_inflight": state["peak"],
                                "elapsed_s": round(elapsed_total, 2),
                                "phase": "warmup" if now < measurement_start else "measured",
                            }) + "\n")
                            pf.flush()
                    except OSError:
                        pass
                last_progress = now

            await asyncio.sleep(0)

        remaining = generation_end - time.perf_counter()
        if remaining > 0 and not generator_saturated:
            await asyncio.sleep(remaining)

        drain_start = time.perf_counter()
        while tasks:
            reap_done()
            if tasks:
                await asyncio.sleep(0.01)
        drain_end = time.perf_counter()
        drain_end_dt = datetime.now()

    reap_done()

    measured_results = sorted((r for r in all_results if r.phase == "measured"), key=lambda r: r.request_id)
    offered_rps = dispatched_measured / duration_seconds if duration_seconds else 0.0
    drain_seconds = max(0.0, drain_end - generation_end)

    completions_during_window = [
        r for r in all_results
        if 0.0 <= r.completion_offset_ms <= duration_seconds * 1000.0
        and 200 <= r.http_status < 300
    ]

    load_stats = {
        "target_rps": target_rps,
        "start_time": benchmark_start_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "end_time": drain_end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "start_timestamp": benchmark_start_dt.isoformat(),
        "end_timestamp": drain_end_dt.isoformat(),
        "duration_seconds": duration_seconds,
        "warmup_seconds": warmup_seconds,
        "target_requests": measured_count,
        "attempted_requests": dispatched_measured,
        "warmup_target_requests": warmup_count,
        "warmup_attempted_requests": dispatched_warmup,
        "missed_slots": max(0, measured_count - dispatched_measured),
        "scheduler_late_slots": scheduler_late_slots,
        "offered_rps": offered_rps,
        "peak_inflight": state["peak"],
        "max_inflight": max_inflight,
        "connection_limit": connection_limit,
        "drain_seconds": drain_seconds,
        "wall_seconds": drain_end - benchmark_start,
        "generator_saturated": generator_saturated,
        "saturation_reason": saturation_reason,
        "test_valid": (not generator_saturated and dispatched_measured == measured_count),
        "completion_2xx_during_window": len(completions_during_window),
        "completion_rps_during_window": len(completions_during_window) / duration_seconds,
    }
    return measured_results, load_stats


def run_open_loop(*args, **kwargs):
    return asyncio.run(run_open_loop_async(*args, **kwargs))


# -----------------------------------------------------------------------------
# Aggregation
# -----------------------------------------------------------------------------
def aggregate(results: list[RequestResult], load_stats: dict) -> dict:
    status_counts = Counter(r.http_status for r in results)
    outcome_counts = Counter(r.outcome for r in results)

    processed = [r for r in results if 200 <= r.http_status < 300]
    rate_limited = [r for r in results if r.http_status == 429]
    other_http = [r for r in results if r.http_status not in (0, 429) and not (200 <= r.http_status < 300)]
    client_errors = [r for r in results if r.http_status == 0]
    timeouts = [r for r in results if r.outcome == "timeout"]

    processed_rtt = latency_stats([r.response_time_ms for r in processed])
    guardrail = latency_stats([r.cai_time_ms for r in processed if r.cai_time_ms is not None])
    scanner = guardrail
    overhead = latency_stats([r.overhead_ms for r in processed if r.overhead_ms is not None])
    rate_limit_rtt = latency_stats([r.response_time_ms for r in rate_limited])
    schedule_delay = latency_stats([r.schedule_delay_ms for r in results])

    duration = load_stats.get("duration_seconds", 0.0)
    attempted = load_stats.get("attempted_requests", len(results)) or 1

    expected_blocked = [r for r in processed if r.expected_blocked]
    expected_allowed = [r for r in processed if not r.expected_blocked]
    cache_friendly_all = [r for r in results if r.cache_profile == "repeat"]
    cache_resistant_all = [r for r in results if r.cache_profile == "unique"]
    cache_friendly_processed = [r for r in processed if r.cache_profile == "repeat"]
    cache_resistant_processed = [r for r in processed if r.cache_profile == "unique"]

    return {
        "status_counts": dict(status_counts),
        "outcome_counts": dict(outcome_counts),
        "completed_results": len(results),
        "processed_2xx": len(processed),
        "rate_limited_429": len(rate_limited),
        "other_http_errors": len(other_http),
        "client_errors": len(client_errors),
        "timeouts": len(timeouts),
        "accepted_pct": len(processed) / attempted * 100.0,
        "rate_limited_pct": len(rate_limited) / attempted * 100.0,
        "accepted_rps": len(processed) / duration if duration else 0.0,
        "rate_limited_rps": len(rate_limited) / duration if duration else 0.0,
        "processed_rtt": processed_rtt,
        "guardrails_latency": guardrail,
        "guardrail_latency": guardrail,
        "scanner_latency": scanner,
        "overhead": overhead,
        "rate_limit_rtt": rate_limit_rtt,
        "schedule_delay": schedule_delay,
        "blocked_guardrails_latency": latency_stats([r.cai_time_ms for r in expected_blocked if r.cai_time_ms is not None]),
        "allowed_guardrails_latency": latency_stats([r.cai_time_ms for r in expected_allowed if r.cai_time_ms is not None]),
        "blocked_guardrail_latency": latency_stats([r.cai_time_ms for r in expected_blocked if r.cai_time_ms is not None]),
        "allowed_guardrail_latency": latency_stats([r.cai_time_ms for r in expected_allowed if r.cai_time_ms is not None]),
        "blocked_scanner_latency": latency_stats([r.cai_time_ms for r in expected_blocked if r.cai_time_ms is not None]),
        "allowed_scanner_latency": latency_stats([r.cai_time_ms for r in expected_allowed if r.cai_time_ms is not None]),
        "cache_friendly_requests": len(cache_friendly_all),
        "cache_resistant_requests": len(cache_resistant_all),
        "cache_friendly_pct": len(cache_friendly_all) / len(results) * 100.0 if results else 0.0,
        "cache_friendly_guardrails_latency": latency_stats([r.cai_time_ms for r in cache_friendly_processed if r.cai_time_ms is not None]),
        "cache_resistant_guardrails_latency": latency_stats([r.cai_time_ms for r in cache_resistant_processed if r.cai_time_ms is not None]),
        "cache_friendly_guardrail_latency": latency_stats([r.cai_time_ms for r in cache_friendly_processed if r.cai_time_ms is not None]),
        "cache_resistant_guardrail_latency": latency_stats([r.cai_time_ms for r in cache_resistant_processed if r.cai_time_ms is not None]),
        "cache_friendly_scanner_latency": latency_stats([r.cai_time_ms for r in cache_friendly_processed if r.cai_time_ms is not None]),
        "cache_resistant_scanner_latency": latency_stats([r.cai_time_ms for r in cache_resistant_processed if r.cai_time_ms is not None]),
        "start_time": load_stats.get("start_time", ""),
        "end_time": load_stats.get("end_time", ""),
    }


def bucket_time_series(results: list[RequestResult], bucket_seconds: int = 5) -> list[dict]:
    buckets = defaultdict(list)
    for r in results:
        sec = max(0.0, r.start_offset_ms / 1000.0)
        start = int(sec // bucket_seconds) * bucket_seconds
        buckets[start].append(r)
    out = []
    for start in sorted(buckets):
        rows = buckets[start]
        processed = [r for r in rows if 200 <= r.http_status < 300]
        guardrail_vals = [r.cai_time_ms for r in processed if r.cai_time_ms is not None]
        rtt_vals = [r.response_time_ms for r in processed]
        g_stats = latency_stats(guardrail_vals)
        out.append({
            "label": f"{start}-{start + bucket_seconds}s",
            "attempted": len(rows),
            "processed": len(processed),
            "rate_limited": sum(1 for r in rows if r.http_status == 429),
            "timeouts": sum(1 for r in rows if r.outcome == "timeout"),
            "processed_rps": len(processed) / bucket_seconds,
            "guardrails_p50": g_stats["p50"],
            "guardrails_p95": g_stats["p95"],
            "guardrail_p50": g_stats["p50"],
            "guardrail_p95": g_stats["p95"],
            "scanner_p50": g_stats["p50"],
            "scanner_p95": g_stats["p95"],
            "rtt_p95": latency_stats(rtt_vals)["p95"],
        })
    return out


# -----------------------------------------------------------------------------
# Reports
# -----------------------------------------------------------------------------
def make_run_paths(reports_dir: str, gpu_model: str, rps: float, target_tokens: int, prompt_mode: str):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = "".join(c.lower() if c.isalnum() else "-" for c in (gpu_model or "gpu-unspecified"))
    slug = "-".join(filter(None, slug.split("-")))[:50]
    stem = f"guardrails-perf-async-{prompt_mode}-{slug}-{rps:g}rps-{target_tokens}tok-{stamp}"
    run_dir = Path(reports_dir) / stem
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir, run_dir / f"{stem}.html", run_dir / f"{stem}.csv", run_dir / f"{stem}-summary.csv"


def write_raw_csv(path: Path, results: list[RequestResult]):
    fields = [
        "request_id", "phase", "unique_id", "cache_profile", "expected_blocked", "category", "token_count",
        "scheduled_offset_ms", "start_offset_ms", "completion_offset_ms", "schedule_delay_ms",
        "http_status", "outcome", "response_time_ms", "cai_time_ms", "overhead_ms",
        "response_bytes", "retry_after", "inflight_at_start", "prompt_pool_index", "error",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in results:
            d = asdict(r)
            w.writerow({k: d.get(k, "") for k in fields})


def write_summary_csv(path: Path, metadata: dict, token_info: dict, load: dict, summary: dict):
    rows = []
    def add(section, metric, value, unit=""):
        rows.append([section, metric, value, unit])

    add("Test", "Script Version", SCRIPT_VERSION)
    add("Test", "Target URL", metadata["api_url"])
    add("Test", "Test Start Time", metadata.get("start_time") or load.get("start_time", ""))
    add("Test", "Test End Time", metadata.get("end_time") or load.get("end_time", ""))
    add("Test", "Test Duration", f"{load.get('wall_seconds', 0.0):.2f}", "s")
    add("Test", "Timestamp", metadata["timestamp"])
    add("Test", "API URL", metadata["api_url"])
    add("Test", "Test Valid", load["test_valid"])
    add("Environment", "GPU Model", metadata.get("gpu_model", ""))
    add("Environment", "GPU Count", metadata.get("gpu_count", ""))
    add("Environment", "Guardrails Version", metadata.get("guardrails_version") or metadata.get("guardrail_version") or metadata.get("scanner_version", ""))
    add("Environment", "Environment", metadata.get("environment", ""))

    add("Payload", "Requested Tokens", token_info["requested_tokens"], "tokens")
    add("Payload", "Sizing Method", token_info["method"])
    add("Payload", "Tokenizer", token_info.get("tokenizer_model", ""))
    add("Payload", "Exact Token Sizing", token_info.get("exact", False))
    add("Payload", "Prompt Templates", token_info.get("prompt_template_count", 0))
    add("Payload", "Prompt Mode", token_info.get("prompt_mode", "unique"))
    add("Payload", "Prefix Cache Rate", f"{token_info.get('cache_friendly_target_pct', 0):.2f}" if token_info.get("cache_friendly_target_pct") is not None else "0.00", "%")
    add("Payload", "Per-request Unique IDs", token_info.get("unique_request_ids", False))
    if token_info.get("cache_friendly_target_pct") is not None:
        add("Payload", "Requested Cache-friendly Mix", f"{token_info.get('cache_friendly_target_pct', 0):.2f}", "%")
        add("Payload", "Prepared Cache-friendly Mix", f"{token_info.get('cache_friendly_actual_pct', 0):.2f}", "%")
        add("Payload", "Cache-friendly Requests", token_info.get("cache_friendly_requests", 0), "requests")
        add("Payload", "Cache-resistant Requests", token_info.get("cache_resistant_requests", 0), "requests")
        add("Payload", "Cache Mix Meaning", "Generated request mix; NOT actual vLLM prefix-cache hit rate")
    add("Payload", "Prepared Requests", token_info.get("prepared_requests", 0))

    add("Load", "Target RPS", load["target_rps"], "req/s")
    add("Load", "Actual Offered RPS", f"{load['offered_rps']:.4f}", "req/s")
    add("Load", "Measured Duration", f"{load['duration_seconds']:.3f}", "s")
    add("Load", "Scheduled Requests", load["target_requests"], "requests")
    add("Load", "Dispatched Requests", load["attempted_requests"], "requests")
    add("Load", "Generator Missed Requests", load["missed_slots"], "requests")
    add("Load", "Scheduler Late Slots", load["scheduler_late_slots"], "requests")
    add("Load", "Peak In-flight", load["peak_inflight"], "requests")
    add("Load", "Max In-flight Safety Fuse", load["max_inflight"], "requests")
    add("Load", "Connection Limit", load["connection_limit"], "connections")
    add("Load", "Generator Saturated", load["generator_saturated"])
    add("Load", "Saturation Reason", load["saturation_reason"])
    add("Load", "Drain Time", f"{load['drain_seconds']:.3f}", "s")
    add("Load", "2xx Completion RPS During Window", f"{load['completion_rps_during_window']:.4f}", "req/s")

    add("Outcome", "Processed HTTP 2xx", summary["processed_2xx"], "requests")
    add("Outcome", "Eventual 2xx / Offered Window", f"{summary['accepted_rps']:.4f}", "req/s")
    add("Outcome", "HTTP 429", summary["rate_limited_429"], "requests")
    add("Outcome", "HTTP 429 Rate", f"{summary['rate_limited_pct']:.4f}", "%")
    add("Outcome", "Timeouts", summary["timeouts"], "requests")
    add("Outcome", "Other HTTP Errors", summary["other_http_errors"], "requests")
    add("Outcome", "Client Errors", summary["client_errors"], "requests")

    total_samples = summary.get("completed_results", 0) or load.get("attempted_requests", 0)
    for s, count in sorted(summary.get("status_counts", {}).items(), key=lambda x: str(x[0])):
        status_label = f"HTTP {s}" if s != 0 else "HTTP 0 (Client/Timeout)"
        pct = (count / total_samples * 100.0) if total_samples else 0.0
        add("HTTP Status Distribution", status_label, count, f"{pct:.2f}%")

    for outcome, count in sorted(summary.get("outcome_counts", {}).items(), key=lambda x: -x[1]):
        pct = (count / total_samples * 100.0) if total_samples else 0.0
        add("Guardrails Outcome Distribution", outcome.upper(), count, f"{pct:.2f}%")

    for name, stats in [
        ("Guardrails Latency", summary.get("guardrails_latency", summary.get("guardrail_latency", summary["scanner_latency"]))),
        ("Processed Round-trip", summary["processed_rtt"]),
        ("RTT Minus Guardrails", summary["overhead"]),
        ("Scheduler Delay", summary["schedule_delay"]),
        ("Expected Blocked Guardrails", summary.get("blocked_guardrails_latency", summary.get("blocked_guardrail_latency", summary["blocked_scanner_latency"]))),
        ("Expected Allowed Guardrails", summary.get("allowed_guardrails_latency", summary.get("allowed_guardrail_latency", summary["allowed_scanner_latency"]))),
        ("Cache-friendly Guardrails", summary.get("cache_friendly_guardrails_latency", summary.get("cache_friendly_guardrail_latency", summary["cache_friendly_scanner_latency"]))),
        ("Cache-resistant Guardrails", summary.get("cache_resistant_guardrails_latency", summary.get("cache_resistant_guardrail_latency", summary["cache_resistant_scanner_latency"]))),
    ]:
        for metric in ("count", "avg", "p50", "p95", "p99", "min", "max", "stdev"):
            unit = "samples" if metric == "count" else "ms"
            val = stats[metric] if metric == "count" else f"{stats[metric]:.3f}"
            add(name, metric.upper(), val, unit)

    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Section", "Metric", "Value", "Unit"])
        w.writerows(rows)


def stat_cards(stats: dict) -> str:
    return "".join(
        f'<div class="time-stat"><div class="time-stat-val">{stats[k]:.0f}ms</div>'
        f'<div class="time-stat-lbl">{label}</div></div>'
        for label, k in [("Average", "avg"), ("P50", "p50"), ("P95", "p95"),
                         ("P99", "p99"), ("Min", "min"), ("Max", "max")]
    )


def write_html(path: Path, results: list[RequestResult], metadata: dict, token_info: dict,
               load: dict, summary: dict, html_max_rows: int):
    guardrail = summary.get("guardrails_latency", summary.get("guardrail_latency", summary["scanner_latency"]))
    rtt = summary["processed_rtt"]
    overhead = summary["overhead"]
    sched = summary["schedule_delay"]
    buckets = bucket_time_series(results, 5)

    display = results
    truncated = False
    if html_max_rows > 0 and len(results) > html_max_rows:
        half = html_max_rows // 2
        display = results[:half] + results[-(html_max_rows - half):]
        truncated = True

    total_results = len(results)

    # Health and outcome distribution
    outcome_order = ["cleared", "flagged", "rate-limited", "timeout"]
    all_outcomes = [o for o in outcome_order if o in summary.get("outcome_counts", {})]
    for o in summary.get("outcome_counts", {}):
        if o not in all_outcomes:
            all_outcomes.append(o)

    outcome_bars = []
    outcome_cards = []
    outcome_counts = summary.get("outcome_counts", {})
    for o in all_outcomes:
        count = outcome_counts.get(o, 0)
        pct = (count / total_results * 100.0) if total_results else 0.0
        badge_cls = (
            "ok" if o in ("cleared", "allowed", "passed") else
            "purple" if o in ("flagged", "blocked") else
            "warn" if o in ("rate-limited", "warn") else
            "bad"
        )
        bg_cls = (
            "bg-ok" if o in ("cleared", "allowed", "passed") else
            "bg-purple" if o in ("flagged", "blocked") else
            "bg-warn" if o in ("rate-limited", "warn") else
            "bg-bad"
        )
        if pct > 0:
            outcome_bars.append(f'<div class="dist-bar-seg {bg_cls}" style="width:{pct:.2f}%" title="{html.escape(o)}: {count} ({pct:.1f}%)"></div>')
        outcome_cards.append(
            f'<div class="dist-item"><div class="dist-item-title"><span class="badge {badge_cls}">{html.escape(o)}</span></div>'
            f'<div class="dist-item-val">{count}<span class="dist-item-pct">({pct:.1f}%)</span></div></div>'
        )

    # HTTP Status breakdown
    status_cards = []
    status_counts = summary.get("status_counts", {})
    def status_sort_key(s):
        if 200 <= s < 300: return (0, s)
        if s == 429: return (1, s)
        if s > 0: return (2, s)
        return (3, s)

    for s in sorted(status_counts.keys(), key=status_sort_key):
        count = status_counts[s]
        pct = (count / total_results * 100.0) if total_results else 0.0
        badge_cls = "ok" if 200 <= s < 300 else "warn" if s == 429 else "bad"
        desc = (
            "200 OK" if s == 200 else
            "429 Too Many Requests" if s == 429 else
            "0 (Timeout / Client Drop)" if s == 0 else
            f"HTTP {s}"
        )
        status_cards.append(
            f'<div class="dist-item"><div class="dist-item-title"><span class="badge {badge_cls}">{html.escape(desc)}</span></div>'
            f'<div class="dist-item-val">{count}<span class="dist-item-pct">({pct:.1f}%)</span></div></div>'
        )

    row_html = []
    for r in display:
        cls = "ok" if 200 <= r.http_status < 300 else "warn" if r.http_status == 429 else "bad"
        out_cls = (
            "ok" if r.outcome in ("cleared", "allowed", "passed") else
            "purple" if r.outcome in ("flagged", "blocked") else
            "warn" if r.outcome in ("rate-limited", "warn") else
            "bad"
        )
        cai = "—" if r.cai_time_ms is None else f"{r.cai_time_ms:.0f}"
        ovh = "—" if r.overhead_ms is None else f"{r.overhead_ms:.0f}"
        http_display = str(r.http_status) if r.http_status > 0 else "0 (err)"
        row_html.append(
            f"<tr><td>{r.request_id}</td><td>{r.start_offset_ms/1000:.3f}</td><td>{html.escape(r.cache_profile)}</td>"
            f"<td><span class='badge {cls}'>{http_display}</span></td>"
            f"<td><span class='badge {out_cls}'>{html.escape(r.outcome)}</span></td><td>{r.response_time_ms:.0f}</td><td>{cai}</td>"
            f"<td>{ovh}</td><td>{r.inflight_at_start}</td><td>{r.schedule_delay_ms:.1f}</td></tr>"
        )

    validity = "VALID" if load["test_valid"] else "INVALID"
    validity_cls = "ok" if load["test_valid"] else "bad"
    exact_note = (
        f"Exact token sizing verified with <strong>{html.escape(token_info.get('tokenizer_model',''))}</strong>."
        if token_info.get("exact") else
        "Payload size is a <strong>whitespace-unit approximation</strong>, not an exact model-token count. "
        "Use <code>--tokenizer-model</code> for an exact token benchmark."
    )
    cache_mix_note = ""
    if token_info.get("cache_friendly_target_pct") is not None:
        cache_mix_note = (
            f"<br><strong>Cache workload mix:</strong> {token_info.get('cache_friendly_actual_pct',0):.2f}% "
            f"of measured payloads are repeat/cache-friendly and the remainder are unique/cache-resistant. "
            f"This is a generated workload mix, <strong>not a guaranteed vLLM prefix-cache hit rate</strong>."
        )

    prefix_cache_rate_display = (
        f"{token_info.get('cache_friendly_actual_pct', 0):.1f}%"
        if token_info.get('cache_friendly_target_pct') is not None
        else "0% (Unique)"
    )
    prefix_cache_rate_exact = (
        f"{token_info.get('cache_friendly_actual_pct', 0):.2f}%"
        if token_info.get('cache_friendly_target_pct') is not None
        else "0.00% (Unique)"
    )

    saturation_note = ""
    if load["generator_saturated"]:
        saturation_note = (
            f'<div class="callout danger"><strong>Generator safety fuse reached:</strong> '
            f'{html.escape(load["saturation_reason"])}. This run must not be reported as a valid '
            f'{load["target_rps"]:.2f}-RPS benchmark.</div>'
        )

    conclusion = (
        f"At <strong>{load['offered_rps']:.2f} actual offered RPS</strong> "
        f"(target {load['target_rps']:.2f}) for <strong>{format_duration(load['duration_seconds'])}</strong>, "
        f"guardrails latency was <strong>P50 {guardrail['p50']:.0f} ms</strong>, "
        f"<strong>P95 {guardrail['p95']:.0f} ms</strong>, and <strong>P99 {guardrail['p99']:.0f} ms</strong> "
        f"across <strong>{guardrail['count']}</strong> successful samples. "
        f"Peak observed in-flight was <strong>{load['peak_inflight']}</strong>. "
        f"HTTP 429 rate was <strong>{summary['rate_limited_pct']:.2f}%</strong>; "
        f"timeouts were <strong>{summary['timeouts']}</strong>. "
        + (f"The generated workload used <strong>{token_info.get('cache_friendly_actual_pct',0):.1f}% cache-friendly/repeated requests</strong> and the remainder cache-resistant/unique requests. " if token_info.get('cache_friendly_target_pct') is not None else "")
        + f"<br><br><strong>How to read the percentiles:</strong> "
        f"P50 is the median, meaning 50% of scans completed at or below that latency; "
        f"P95 means 95% completed at or below that latency; and P99 means 99% completed at or below it. "
        f"Lower values are better, while P95 and P99 highlight slower tail-latency experienced by a smaller portion of requests."
    )

    labels = json.dumps([b["label"] for b in buckets])
    sp50 = json.dumps([round(b.get("guardrails_p50", b.get("guardrail_p50", b.get("scanner_p50", 0))), 2) for b in buckets])
    sp95 = json.dumps([round(b.get("guardrails_p95", b.get("guardrail_p95", b.get("scanner_p95", 0))), 2) for b in buckets])
    rp95 = json.dumps([round(b["rtt_p95"], 2) for b in buckets])
    crps = json.dumps([round(b["processed_rps"], 3) for b in buckets])
    c429 = json.dumps([b["rate_limited"] for b in buckets])
    tout = json.dumps([b["timeouts"] for b in buckets])

    content = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>F5 AI Guardrails Async Performance Benchmark</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root{{--bg:#0b0f1a;--surface:#111827;--surface2:#1a2236;--border:#1e2d45;--accent:#00d4ff;--green:#34d399;--amber:#fbbf24;--red:#f87171;--purple:#c084fc;--text:#e2e8f0;--muted:#64748b;--dim:#94a3b8}}
*{{box-sizing:border-box}} body{{margin:0;font-family:Inter,Arial,sans-serif;background:var(--bg);color:var(--text)}}
.hero{{padding:42px 40px 30px;border-bottom:1px solid var(--border);background:linear-gradient(135deg,#0b0f1a,#0f172a,#111827)}}
.hero-tag{{color:var(--accent);letter-spacing:2.5px;text-transform:uppercase;font-size:11px}} h1{{margin:10px 0 8px;font-size:34px}}
.hero-meta{{color:var(--muted);font-family:monospace;font-size:12px;display:flex;gap:18px;flex-wrap:wrap}}
.main{{max-width:1500px;margin:auto;padding:30px 40px}} .metric-band{{display:grid;grid-template-columns:repeat(7,1fr);gap:12px;margin-bottom:22px}}
.metric-card,.card,.chart-card{{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:20px}} .metric-card{{text-align:center}}
.metric-value{{font-size:29px;font-weight:800;color:var(--accent)}} .metric-label{{margin-top:6px;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:1.2px}}
.card{{margin-bottom:22px}} .card h2,.chart-card h2{{margin:0 0 16px;font-size:17px}} .time-grid{{display:grid;grid-template-columns:repeat(6,1fr);gap:12px}}
.time-stat{{padding:15px;border-radius:9px;background:var(--surface2);text-align:center}} .time-stat-val{{font-size:22px;font-weight:800;color:var(--accent)}} .time-stat-lbl{{margin-top:5px;color:var(--muted);font-family:monospace;font-size:10px;text-transform:uppercase}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:22px}} .chart-wrap{{height:280px}}
.callout{{border-left:3px solid var(--accent);background:var(--surface2);padding:16px;line-height:1.65;color:var(--dim);margin-bottom:18px}} .callout strong{{color:var(--text)}} .danger{{border-left-color:var(--red)}}
.info-grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}} .info{{background:var(--surface2);padding:12px 14px;border-radius:8px}} .info span{{display:block;color:var(--muted);font-size:10px;text-transform:uppercase}} .info strong{{display:block;margin-top:4px;font-family:monospace;font-size:13px;word-break:break-word}}
.table-wrap{{overflow:auto;max-height:650px}} table{{width:100%;border-collapse:collapse;font-size:12px}} th{{position:sticky;top:0;background:var(--surface2);color:var(--muted);padding:9px;text-align:left}} td{{padding:8px 9px;border-bottom:1px solid rgba(30,45,69,.55);font-family:monospace}}
.badge{{padding:2px 7px;border-radius:4px;font-weight:600;display:inline-block}} .ok{{color:var(--green);background:rgba(52,211,153,.12)}} .warn{{color:var(--amber);background:rgba(251,191,36,.12)}} .bad{{color:var(--red);background:rgba(248,113,113,.12)}} .purple{{color:var(--purple);background:rgba(192,132,252,.12)}} .note{{color:var(--muted);font-size:12px;line-height:1.55;margin-top:12px}} code{{color:var(--accent)}}
.dist-bar-wrap{{height:14px;background:var(--surface2);border-radius:7px;display:flex;overflow:hidden;margin:12px 0 16px;border:1px solid var(--border)}} .dist-bar-seg{{height:100%;min-width:2px}}
.bg-ok{{background:var(--green)}} .bg-purple{{background:var(--purple)}} .bg-warn{{background:var(--amber)}} .bg-bad{{background:var(--red)}}
.dist-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px}}
.dist-item{{background:var(--surface2);padding:10px 14px;border-radius:8px;display:flex;align-items:center;justify-content:space-between;border:1px solid rgba(255,255,255,.03)}}
.dist-item-title{{font-size:12px;display:flex;align-items:center;gap:8px}} .dist-item-val{{font-size:13px;font-weight:700;font-family:monospace}} .dist-item-pct{{color:var(--muted);font-size:11px;font-weight:400;margin-left:5px}}
.footer{{text-align:center;color:var(--muted);padding:25px;font-family:monospace;font-size:11px}}
@media(max-width:1100px){{.metric-band,.time-grid{{grid-template-columns:repeat(3,1fr)}}.grid2{{grid-template-columns:1fr}}}} @media(max-width:650px){{.main,.hero{{padding-left:18px;padding-right:18px}}.metric-band,.time-grid{{grid-template-columns:repeat(2,1fr)}}.info-grid{{grid-template-columns:1fr}}}}
</style></head><body>
<div class="hero"><div class="hero-tag">F5 AI Guardrails · Async Open-Loop Performance Benchmark</div><h1>Guardrails Latency Under Fixed RPS</h1>
<div class="hero-meta"><span>Start: {load.get('start_time', metadata.get('start_time',''))}</span><span>End: {load.get('end_time', metadata.get('end_time',''))}</span><span>Target URL: {html.escape(metadata['api_url'])}</span><span>GPU: {html.escape(metadata.get('gpu_model') or 'Not specified')}</span><span>{load['target_rps']:.2f} target RPS × {format_duration(load['duration_seconds'])}</span><span>{token_info['requested_tokens']} target tokens</span><span>Prompt mode: {html.escape(token_info.get('prompt_mode','unique').upper())}</span><span>Prefix Cache Rate: {prefix_cache_rate_display}</span><span class="badge {validity_cls}">{validity}</span></div></div>
<div class="main">
{saturation_note}
<div class="metric-band">
<div class="metric-card"><div class="metric-value">{load['target_rps']:.2f}</div><div class="metric-label">Target RPS</div></div>
<div class="metric-card"><div class="metric-value">{load['offered_rps']:.2f}</div><div class="metric-label">Actual Offered RPS</div></div>
<div class="metric-card"><div class="metric-value">{load['completion_rps_during_window']:.2f}</div><div class="metric-label">2xx Completion RPS</div></div>
<div class="metric-card"><div class="metric-value">{guardrail['p95']:.0f}ms</div><div class="metric-label">Guardrails P95</div></div>
<div class="metric-card"><div class="metric-value">{guardrail['p99']:.0f}ms</div><div class="metric-label">Guardrails P99</div></div>
<div class="metric-card"><div class="metric-value">{summary['rate_limited_pct']:.2f}%</div><div class="metric-label">HTTP 429</div></div>
<div class="metric-card"><div class="metric-value">{load['peak_inflight']}</div><div class="metric-label">Peak In-flight</div></div>
</div>
<div class="card"><h2>Overall Summary</h2><div class="callout">{conclusion}</div></div>
<div class="card"><h2>🔬 Guardrails Latency</h2><div class="note" style="margin-top:-7px;margin-bottom:14px">Time spent inside F5 AI Guardrails processing the request. This is the primary server-side guardrails performance metric; lower is better.</div><div class="time-grid">{stat_cards(guardrail)}</div><div class="note">Calculated from successful HTTP 2xx responses that reported guardrails timing. Samples: {guardrail['count']}.</div></div>
<div class="card"><h2>⏱️ Processed Request Round-trip Time</h2><div class="note" style="margin-top:-7px;margin-bottom:14px">Total client-observed time from sending a request until the Guardrails response is received. It includes guardrails latency plus API, gateway, network and client-side overhead.</div><div class="time-grid">{stat_cards(rtt)}</div></div>
<div class="card"><h2>↔️ Round-trip Minus Guardrails Time</h2><div class="note" style="margin-top:-7px;margin-bottom:14px">The difference between total round-trip time and guardrails latency. It approximates non-guardrails overhead such as API, gateway, network and serialization time; it is not a pure network measurement.</div><div class="time-grid">{stat_cards(overhead)}</div></div>
<div class="card"><h2>🎯 Scheduler Delay</h2><div class="note" style="margin-top:-7px;margin-bottom:14px">How late the load generator dispatched each request compared with its intended fixed-RPS schedule. Values near zero confirm the client is maintaining the requested arrival rate.</div><div class="time-grid">{stat_cards(sched)}</div></div>
<div class="grid2"><div class="chart-card"><h2>Latency Over Time (5s buckets)</h2><div class="chart-wrap"><canvas id="latencyChart"></canvas></div></div><div class="chart-card"><h2>Processed RPS / 429 / Timeouts</h2><div class="chart-wrap"><canvas id="rpsChart"></canvas></div></div></div>
<div class="card"><h2>Test Conditions</h2><div class="info-grid">
<div class="info"><span>Test Start Time</span><strong>{load.get('start_time', metadata.get('start_time', ''))}</strong></div>
<div class="info"><span>Test End Time</span><strong>{load.get('end_time', metadata.get('end_time', ''))}</strong></div>
<div class="info"><span>GPU Model</span><strong>{html.escape(metadata.get('gpu_model') or 'Not specified')}</strong></div>
<div class="info"><span>GPU Count</span><strong>{metadata.get('gpu_count') or 'Not specified'}</strong></div>
<div class="info"><span>Guardrails Version</span><strong>{html.escape(metadata.get('guardrails_version') or metadata.get('guardrail_version') or metadata.get('scanner_version') or 'Not specified')}</strong></div>
<div class="info"><span>Environment</span><strong>{html.escape(metadata.get('environment') or 'Not specified')}</strong></div>
<div class="info"><span>Target RPS</span><strong>{load['target_rps']:.2f} req/s</strong></div>
<div class="info"><span>Actual Offered RPS</span><strong>{load['offered_rps']:.4f} req/s</strong></div>
<div class="info"><span>Measured Duration</span><strong>{format_duration(load['duration_seconds'])}</strong></div>
<div class="info"><span>Warmup</span><strong>{format_duration(load['warmup_seconds'])}</strong></div>
<div class="info"><span>Target Payload</span><strong>{token_info['requested_tokens']} tokens</strong></div>
<div class="info"><span>Token Sizing</span><strong>{html.escape(token_info['method'])}</strong></div>
<div class="info"><span>Tokenizer</span><strong>{html.escape(token_info.get('tokenizer_model') or 'Not supplied')}</strong></div>
<div class="info"><span>Prompt Templates</span><strong>{token_info['prompt_template_count']}</strong></div>
<div class="info"><span>Prompt Mode</span><strong>{html.escape(token_info.get('prompt_mode', 'unique').upper())}</strong></div>
<div class="info"><span>Prefix Cache Rate</span><strong>{prefix_cache_rate_exact}</strong></div>
{f'<div class="info"><span>Target Cache Mix</span><strong>{token_info.get("cache_friendly_target_pct",0):.2f}%</strong></div><div class="info"><span>Prepared Cache Mix</span><strong>{token_info.get("cache_friendly_actual_pct",0):.2f}%</strong></div>' if token_info.get('cache_friendly_target_pct') is not None else ''}
<div class="info"><span>Per-request Random ID</span><strong>{'Yes' if token_info.get('unique_request_ids') else 'No'}</strong></div>
<div class="info"><span>Max In-flight Safety Fuse</span><strong>{'Unlimited' if load['max_inflight']==0 else load['max_inflight']}</strong></div>
<div class="info"><span>Peak Observed In-flight</span><strong>{load['peak_inflight']}</strong></div>
<div class="info"><span>Connection Limit</span><strong>{'Unlimited' if load['connection_limit']==0 else load['connection_limit']}</strong></div>
<div class="info"><span>Target URL</span><strong>{html.escape(metadata['api_url'])}</strong></div>
</div><div class="note">{exact_note}{cache_mix_note}</div></div>
<div class="card"><h2>Load / Outcome Summary</h2><div class="info-grid">
<div class="info"><span>Scheduled Measured Requests</span><strong>{load['target_requests']}</strong></div>
<div class="info"><span>Dispatched Measured Requests</span><strong>{load['attempted_requests']}</strong></div>
<div class="info"><span>Generator Missed Requests</span><strong>{load['missed_slots']}</strong></div>
<div class="info"><span>Scheduler Late Slots</span><strong>{load['scheduler_late_slots']}</strong></div>
<div class="info"><span>Processed HTTP 2xx</span><strong>{summary['processed_2xx']}</strong></div>
<div class="info"><span>HTTP 429</span><strong>{summary['rate_limited_429']} ({summary['rate_limited_pct']:.2f}%)</strong></div>
<div class="info"><span>Timeouts</span><strong>{summary['timeouts']}</strong></div>
<div class="info"><span>Other HTTP Errors</span><strong>{summary['other_http_errors']}</strong></div>
<div class="info"><span>Client Errors</span><strong>{summary['client_errors']}</strong></div>
<div class="info"><span>Drain Time</span><strong>{load['drain_seconds']:.2f}s</strong></div>
</div></div>
<div class="card"><h2>📊 Response Health & Outcome Distribution</h2>
<div class="note" style="margin-top:-7px;margin-bottom:14px">Overall health, classification outcome, and HTTP status distribution across all <strong>{total_results}</strong> measured responses.</div>
<div class="dist-bar-wrap">{''.join(outcome_bars)}</div>
<div style="margin-top:16px">
<div style="font-size:11px;font-weight:700;color:var(--dim);text-transform:uppercase;letter-spacing:1px;margin-bottom:8px">Guardrails Classification Outcomes</div>
<div class="dist-grid">{''.join(outcome_cards)}</div>
</div>
<div style="margin-top:20px">
<div style="font-size:11px;font-weight:700;color:var(--dim);text-transform:uppercase;letter-spacing:1px;margin-bottom:8px">HTTP Status Codes</div>
<div class="dist-grid">{''.join(status_cards)}</div>
</div>
</div>
<div class="card"><h2>Request Samples</h2><div class="table-wrap"><table><thead><tr><th>#</th><th>Start(s)</th><th>Cache Profile</th><th>HTTP</th><th>Outcome</th><th>RTT(ms)</th><th>Guardrails Latency(ms)</th><th>Overhead(ms)</th><th>In-flight</th><th>Schedule Delay(ms)</th></tr></thead><tbody>{''.join(row_html)}</tbody></table></div>
<div class="note">{'HTML table truncated; raw CSV contains every measured request.' if truncated else 'All measured requests shown.'}</div></div>
</div><div class="footer">Generated by {SCRIPT_NAME} v{SCRIPT_VERSION}</div>
<script>
new Chart(document.getElementById('latencyChart'),{{type:'line',data:{{labels:{labels},datasets:[{{label:'Guardrails P50',data:{sp50}}},{{label:'Guardrails P95',data:{sp95}}},{{label:'RTT P95',data:{rp95}}}]}},options:{{responsive:true,maintainAspectRatio:false,scales:{{y:{{title:{{display:true,text:'ms'}}}}}}}}}});
new Chart(document.getElementById('rpsChart'),{{type:'line',data:{{labels:{labels},datasets:[{{label:'2xx completion RPS (bucket)',data:{crps}}},{{label:'429 count',data:{c429}}},{{label:'Timeout count',data:{tout}}}]}},options:{{responsive:true,maintainAspectRatio:false}}}});
</script></body></html>"""
    path.write_text(content, encoding="utf-8")


def print_stats(title: str, stats: dict):
    print(f"\n{title}")
    print("-" * 80)
    print(f"Samples: {stats['count']}")
    print(f"Average: {stats['avg']:.2f} ms")
    print(f"P50:     {stats['p50']:.2f} ms")
    print(f"P95:     {stats['p95']:.2f} ms")
    print(f"P99:     {stats['p99']:.2f} ms")
    print(f"Min:     {stats['min']:.2f} ms")
    print(f"Max:     {stats['max']:.2f} ms")


def print_summary(metadata: dict, token_info: dict, load: dict, summary: dict):
    print("\n" + "=" * 80)
    print("PERFORMANCE TEST SUMMARY")
    print("=" * 80)
    print(f"Test Valid:              {load['test_valid']}")
    print(f"Target URL:              {metadata['api_url']}")
    print(f"Test Start Time:         {load.get('start_time', metadata.get('start_time', ''))}")
    print(f"Test End Time:           {load.get('end_time', metadata.get('end_time', ''))}")
    print(f"Test Duration:           {load.get('wall_seconds', 0.0):.2f}s")
    cache_rate_str = (
        f"{token_info.get('cache_friendly_actual_pct', 0):.2f}%"
        if token_info.get("cache_friendly_target_pct") is not None
        else "0.00% (Unique)"
    )
    print(f"Prefix Cache Rate:       {cache_rate_str}")
    print(f"Target RPS:              {load['target_rps']:.2f}")
    print(f"Actual Offered RPS:      {load['offered_rps']:.2f}")
    print(f"2xx Completion RPS:      {load['completion_rps_during_window']:.2f}")
    print(f"Measured Duration:       {format_duration(load['duration_seconds'])}")
    print(f"Scheduled Requests:      {load['target_requests']}")
    print(f"Dispatched Requests:     {load['attempted_requests']}")
    print(f"Generator Missed:        {load['missed_slots']}")
    print(f"Peak In-flight:          {load['peak_inflight']}")
    print(f"HTTP 2xx:                {summary['processed_2xx']}")
    print(f"HTTP 429:                {summary['rate_limited_429']} ({summary['rate_limited_pct']:.2f}%)")
    print(f"Timeouts:                {summary['timeouts']}")
    print(f"Other HTTP Errors:       {summary['other_http_errors']}")
    if load["generator_saturated"]:
        print(f"Generator Saturation:    {load['saturation_reason']}")
    print_stats("🔬 GUARDRAILS LATENCY", summary.get("guardrails_latency", summary.get("guardrail_latency", summary["scanner_latency"])))
    print_stats("⏱️  PROCESSED REQUEST ROUND-TRIP", summary["processed_rtt"])
    print_stats("🎯 SCHEDULER DELAY", summary["schedule_delay"])

    s = summary.get("guardrails_latency", summary.get("guardrail_latency", summary["scanner_latency"]))
    print("\nOVERALL SUMMARY")
    print("-" * 80)
    validity = "VALID" if load["test_valid"] else "INVALID"
    print(
        f"[{validity}] At {load['offered_rps']:.2f} actual offered RPS "
        f"(target {load['target_rps']:.2f}) with a {token_info['requested_tokens']}-token target "
        f"payload on {metadata.get('gpu_model') or 'the specified GPU'}, guardrails latency was "
        f"P50={s['p50']:.2f} ms, P95={s['p95']:.2f} ms, P99={s['p99']:.2f} ms "
        f"({s['count']} guardrails-latency samples)."
    )
    print("=" * 80)


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(
        description="F5 AI Guardrails async open-loop fixed-RPS performance benchmark",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example: 30 RPS for 5 minutes with 30-second warmup
  %(prog)s \\
    --prompt-file f5_perf_prompts.csv \\
    --f5-api-url https://host/backend/v1/scans \\
    --rps 30 --duration 5m --warmup 30s \\
    --target-tokens 250 \\
    --gpu-model "NVIDIA L40S" --gpu-count 1

For exact 250 model tokens, also specify:
  --tokenizer-model <Hugging-Face-tokenizer-or-model>

Important:
  * --max-inflight is a safety fuse, NOT target concurrency.
  * If the fuse is reached, the run is marked INVALID and scheduling stops.
  * --connection-limit 0 means aiohttp does not impose a connector-side limit.
  * HTTP 429 responses are counted but excluded from guardrails latency percentiles.
  * Cache A/B: use --prompt-mode unique and --prompt-mode repeat with all other settings identical.
  * Mixed cache workload: --prefix-cache-rate 30 makes about 30%% of requests
    repeat/cache-friendly and 70%% unique/cache-resistant. This is NOT a guarantee
    of a 30%% actual vLLM prefix-cache hit rate.
""",
    )
    p.add_argument("--f5-api-url", default=os.getenv("F5_API_URL", DEFAULT_API_URL))
    p.add_argument("--f5-token", default=os.getenv("F5_BEARER_TOKEN", DEFAULT_F5_BEARER_TOKEN),
                   help="Priority: CLI > F5_BEARER_TOKEN env > script default")
    p.add_argument("--rps", type=float, default=DEFAULT_RPS)
    p.add_argument("--duration", type=parse_duration, default=DEFAULT_DURATION_SECONDS)
    p.add_argument("--warmup", type=parse_duration, default=DEFAULT_WARMUP_SECONDS)
    p.add_argument("--target-tokens", type=int, default=DEFAULT_TARGET_TOKENS)
    p.add_argument("--tokenizer-model", default=None)
    p.add_argument("--tokenizer-local-files-only", action="store_true")
    p.add_argument("--prompt-text", default=None)
    p.add_argument("--prompt-file", default=None, help="CSV: prompt,expected,category")
    p.add_argument("--prompt-mode", choices=("unique", "repeat"), default="unique",
                   help="unique = random numeric ID per request; repeat = identical template reuse (default: unique)")
    p.add_argument("--prefix-cache-rate", "--cache-friendly-percent", dest="cache_friendly_percent",
                   type=float, default=None, metavar="PCT",
                   help="0-100: percentage of requests generated in repeat/cache-friendly form; "
                        "overrides --prompt-mode for workload construction. This controls the request mix, "
                        "not the actual vLLM cache-hit rate.")
    p.add_argument("--random-seed", type=int, default=42)
    p.add_argument("--max-inflight", type=int, default=DEFAULT_MAX_INFLIGHT,
                   help="Safety fuse; 0 = unlimited (use cautiously)")
    p.add_argument("--connection-limit", type=int, default=DEFAULT_CONNECTION_LIMIT,
                   help="aiohttp connector limit; 0 = unlimited")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS,
                   help="Per-request total timeout in seconds; default 120")
    p.add_argument("--verify-tls", action="store_true")
    p.add_argument("--verbose-scan", action="store_true")
    p.add_argument("--gpu-model", default="")
    p.add_argument("--gpu-count", type=int, default=0)
    p.add_argument("--guardrails-version", "--guardrail-version", "--scanner-version", dest="guardrails_version", default="")
    p.add_argument("--environment", default="")
    p.add_argument("--notes", default="")
    p.add_argument("--reports-dir", default=DEFAULT_REPORTS_DIR)
    p.add_argument("--html-max-rows", type=int, default=DEFAULT_HTML_MAX_ROWS)
    p.add_argument("--progress-interval", type=float, default=DEFAULT_PROGRESS_INTERVAL)
    p.add_argument("--progress-file", default=None,
                   help="Path to write JSON-lines progress events (for GUI integration)")
    p.add_argument("--skip-preflight", action="store_true",
                   help="Skip pre-flight connectivity and health check")
    return p.parse_args()


def main():
    args = parse_args()
    if not args.f5_token:
        raise SystemExit("ERROR: bearer token is empty")
    if args.rps <= 0 or args.duration <= 0:
        raise SystemExit("ERROR: --rps and --duration must be > 0")
    if args.max_inflight < 0 or args.connection_limit < 0:
        raise SystemExit("ERROR: --max-inflight and --connection-limit cannot be negative")
    if args.timeout <= 0:
        raise SystemExit("ERROR: --timeout must be > 0")
    if args.cache_friendly_percent is not None and not (0.0 <= args.cache_friendly_percent <= 100.0):
        raise SystemExit("ERROR: --prefix-cache-rate must be between 0 and 100")

    if not args.skip_preflight:
        print(f"Pre-flight health check: probing {args.f5_api_url} ...")
        ok, status, err_text, cai = preflight_probe(
            args.f5_api_url, args.f5_token, args.verify_tls, timeout_seconds=10.0
        )
        if not ok:
            print(f"\n❌ PRE-FLIGHT HEALTH CHECK FAILED (HTTP {status})")
            print(f"   Response from server: {err_text}")
            print("   The endpoint is not responding with HTTP 200 OK.")
            print("   Aborting benchmark before sending traffic.")
            print("   (Pass --skip-preflight to force execution anyway)\n")
            raise SystemExit(1)
        lat_str = f", guardrails latency: {cai:.1f} ms" if cai is not None else ""
        print(f"✅ Endpoint healthy (HTTP {status}{lat_str})\n")

    templates = load_prompt_templates(args.prompt_text, args.prompt_file)
    warmup_requests = int(round(args.rps * args.warmup)) if args.warmup > 0 else 0
    measured_requests = int(round(args.rps * args.duration))

    warmup_plan, warmup_token_info = prepare_request_plan(
        templates, warmup_requests, args.target_tokens, args.tokenizer_model,
        args.tokenizer_local_files_only, args.random_seed, args.prompt_mode,
        args.cache_friendly_percent, 0,
    )
    measured_plan, token_info = prepare_request_plan(
        templates, measured_requests, args.target_tokens, args.tokenizer_model,
        args.tokenizer_local_files_only, args.random_seed + 1, args.prompt_mode,
        args.cache_friendly_percent, warmup_requests,
    )
    full_plan = warmup_plan + measured_plan
    token_info["prepared_requests"] = len(full_plan)
    token_info["measured_prepared_requests"] = len(measured_plan)

    effective_prompt_mode = "mixed" if args.cache_friendly_percent is not None else args.prompt_mode

    metadata = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "api_url": args.f5_api_url,
        "gpu_model": args.gpu_model,
        "gpu_count": args.gpu_count,
        "guardrails_version": args.guardrails_version,
        "guardrail_version": args.guardrails_version,
        "scanner_version": args.guardrails_version,
        "environment": args.environment,
        "notes": args.notes,
        "verbose": args.verbose_scan,
        "prompt_mode": effective_prompt_mode,
        "cache_friendly_percent": args.cache_friendly_percent,
    }

    run_dir, html_path, raw_csv_path, summary_csv_path = make_run_paths(
        args.reports_dir, args.gpu_model, args.rps, args.target_tokens,
        (f"mixed{args.cache_friendly_percent:g}pct" if args.cache_friendly_percent is not None else args.prompt_mode)
    )

    print("=" * 80)
    print(f"{SCRIPT_NAME} v{SCRIPT_VERSION}")
    print("F5 AI Guardrails Async Open-Loop Performance Benchmark")
    print("=" * 80)
    print(f"Target URL:           {args.f5_api_url}")
    print(f"GPU Model:            {args.gpu_model or 'Not specified'}")
    print(f"GPU Count:            {args.gpu_count or 'Not specified'}")
    print(f"Target RPS:           {args.rps:.2f}")
    print(f"Measured Duration:    {format_duration(args.duration)}")
    print(f"Warmup:               {format_duration(args.warmup)}")
    print(f"Target Tokens:        {args.target_tokens}")
    print(f"Token Sizing:         {token_info['method']}")
    print(f"Prompt Templates:     {len(templates)}")
    print(f"Prompt Mode:          {effective_prompt_mode.upper()}")
    if args.cache_friendly_percent is not None:
        print(f"Prefix Cache Rate:    {token_info['cache_friendly_actual_pct']:.2f}% "
              f"({token_info['cache_friendly_requests']}/{len(measured_plan)} measured requests)")
        print("Cache Mix Semantics:  Generated workload mix; NOT guaranteed vLLM cache-hit rate")
    else:
        print("Prefix Cache Rate:    0.00% (Unique prompts)")
    print(f"Prepared Requests:    {len(full_plan)} total ({len(measured_plan)} measured)")
    print(f"Request Timeout:      {args.timeout:.1f}s")
    print(f"Max In-flight Fuse:   {'Unlimited' if args.max_inflight == 0 else args.max_inflight}")
    print(f"Connection Limit:     {'Unlimited' if args.connection_limit == 0 else args.connection_limit}")
    print(f"Report Folder:        {run_dir.resolve()}")
    print("=" * 80)

    if not token_info.get("exact"):
        print(f"WARNING: {args.target_tokens} is a whitespace-unit approximation, not exact model tokens.")
        print("         Use --tokenizer-model for a formal exact-token GPU benchmark.")

    results, load = run_open_loop(
        warmup_plan=warmup_plan,
        measured_plan=measured_plan,
        target_rps=args.rps,
        warmup_seconds=args.warmup,
        duration_seconds=args.duration,
        max_inflight=args.max_inflight,
        connection_limit=args.connection_limit,
        api_url=args.f5_api_url,
        bearer_token=args.f5_token,
        timeout_seconds=args.timeout,
        verify_tls=args.verify_tls,
        verbose=args.verbose_scan,
        progress_interval=args.progress_interval,
        progress_file=getattr(args, 'progress_file', None),
    )
    summary = aggregate(results, load)
    metadata["start_time"] = load.get("start_time", "")
    metadata["end_time"] = load.get("end_time", "")

    write_raw_csv(raw_csv_path, results)
    write_summary_csv(summary_csv_path, metadata, token_info, load, summary)
    write_html(html_path, results, metadata, token_info, load, summary, args.html_max_rows)
    print_summary(metadata, token_info, load, summary)

    print("\nReports")
    print("-" * 80)
    print(f"HTML:        {html_path}")
    print(f"Raw CSV:     {raw_csv_path}")
    print(f"Summary CSV: {summary_csv_path}")

    if not load["test_valid"]:
        print("\nWARNING: TEST INVALID as a fixed-RPS benchmark because the generator could not dispatch every scheduled arrival.")
    if summary.get("guardrails_latency", summary.get("guardrail_latency", summary["scanner_latency"]))["count"] == 0:
        print("WARNING: no successful guardrails-latency samples were captured.")


if __name__ == "__main__":
    main()
