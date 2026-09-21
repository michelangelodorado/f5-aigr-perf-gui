#!/usr/bin/env python3
# =============================================================================
# f5_find_max_rps.py
# Automated SLA-Bounded Capacity & Maximum RPS Finder for F5 AI Guardrails
#
# Purpose:
#   Automatically discover the maximum sustainable RPS that satisfies a given
#   latency SLA (e.g. Guardrails P95 <= 300 ms) and error budget (e.g. 0% 429s).
#
# Search strategies:
#   - ladder (default): Steps upward from --start-rps by --step-rps until SLA
#     is violated, 429 threshold is exceeded, or timeouts occur.
#   - binary: Bisection search between --min-rps and --max-rps to quickly
#     converge on the exact inflection threshold within --rps-tolerance.
#
# Output:
#   - Live console step table with real-time compliance verdicts.
#   - Consolidated CSV summary of all evaluated RPS steps.
#   - Self-contained HTML report with an interactive Chart.js curve comparing
#     Guardrails Latency, RTT, and Error Rates against the horizontal SLA line.
# =============================================================================

import argparse
import csv
import html
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# Import benchmarking engine from f5_guardrails_perf
try:
    from f5_guardrails_perf import (
        DEFAULT_API_URL,
        DEFAULT_F5_BEARER_TOKEN,
        DEFAULT_REPORTS_DIR,
        DEFAULT_TARGET_TOKENS,
        DEFAULT_TIMEOUT_SECONDS,
        DEFAULT_MAX_INFLIGHT,
        DEFAULT_CONNECTION_LIMIT,
        SCRIPT_VERSION,
        parse_duration,
        format_duration,
        load_prompt_templates,
        prepare_request_plan,
        run_open_loop,
        aggregate,
        write_raw_csv,
        write_summary_csv,
        write_html,
        make_run_paths,
        preflight_probe,
    )
except ImportError as exc:
    raise SystemExit(
        f"ERROR: Could not import benchmarking engine from f5_guardrails_perf.py: {exc}\n"
        "Ensure f5_guardrails_perf.py is present in the same directory."
    ) from exc


def parse_args():
    p = argparse.ArgumentParser(
        description="Automated SLA-Bounded Capacity & Maximum RPS Finder for F5 AI Guardrails",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Ladder search: find max RPS where Guardrails P95 <= 300ms (10, 20, 30, 40... RPS)
  %(prog)s \\
    --prompt-file f5_perf_prompts.csv \\
    --target-latency-ms 300 \\
    --sla-metric guardrails_p95 \\
    --start-rps 10 --step-rps 10 --step-duration 60s \\
    --gpu-model "NVIDIA H100" --gpu-count 1

  # Binary search: pinpoint max RPS between 20 and 80 RPS within 2 RPS tolerance
  %(prog)s \\
    --prompt-file f5_perf_prompts.csv \\
    --search-mode binary \\
    --target-latency-ms 300 \\
    --sla-metric guardrails_p95 \\
    --min-rps 20 --max-rps 80 --rps-tolerance 2 \\
    --step-duration 90s --step-warmup 15s \\
    --gpu-model "NVIDIA H100"
""",
    )
    # SLA & Search Options
    p.add_argument("--target-latency-ms", type=float, default=300.0,
                   help="Maximum allowable latency SLA budget in milliseconds (default: 300)")
    p.add_argument("--sla-metric",
                   choices=("guardrails_p95", "guardrails_p50", "guardrails_p99",
                            "guardrail_p95", "guardrail_p50", "guardrail_p99",
                            "rtt_p95", "rtt_p50"),
                   default="guardrails_p95",
                   help="Metric to test against target SLA budget (default: guardrails_p95)")
    p.add_argument("--search-mode", choices=("ladder", "binary", "saturation"), default="ladder",
                   help="Search strategy: ladder (step upward to SLA), binary (bisection to SLA), or saturation (detect GPU 100%% bottleneck & throughput ceiling) (default: ladder)")
    p.add_argument("--find-saturation", action="store_true",
                   help="Shortcut to enable --search-mode saturation")
    p.add_argument("--plateau-ratio", type=float, default=0.90,
                   help="Throughput ratio (2xx Completed RPS / Offered RPS) below which GPU saturation is flagged (default: 0.90, i.e. 90%%)")
    p.add_argument("--latency-jump-factor", type=float, default=2.0,
                   help="Latency increase multiplier over previous step that triggers hockey-stick knee detection (default: 2.0x)")
    p.add_argument("--marginal-gain-threshold", type=float, default=0.20,
                   help="Marginal completion efficiency (delta completed / delta offered) below which throughput is considered stalled (default: 0.20, i.e. 20%%)")
    p.add_argument("--start-rps", type=float, default=10.0,
                   help="Starting RPS for ladder search (default: 10)")
    p.add_argument("--step-rps", type=float, default=10.0,
                   help="RPS increment per step in ladder search (default: 10)")
    p.add_argument("--min-rps", type=float, default=10.0,
                   help="Lower bound RPS for binary search (default: 10)")
    p.add_argument("--max-rps", type=float, default=100.0,
                   help="Upper bound / safety ceiling RPS (default: 100)")
    p.add_argument("--rps-tolerance", type=float, default=2.0,
                   help="Convergence tolerance in RPS for binary search (default: 2)")
    p.add_argument("--max-steps", type=int, default=15,
                   help="Maximum number of test steps before stopping (default: 15)")

    # Error & Safety Ceilings
    p.add_argument("--max-429-pct", type=float, default=2.0,
                   help="Maximum allowable HTTP 429 rate in %% before marking step as failed (default: 2.0%%)")
    p.add_argument("--max-error-pct", type=float, default=0.0,
                   help="Maximum allowable server error / HTTP 5xx rate in %% before failing step (default: 0.0%%)")
    p.add_argument("--max-timeouts", type=int, default=0,
                   help="Maximum allowable timeouts before marking step as failed (default: 0)")
    p.add_argument("--skip-preflight", action="store_true",
                   help="Skip pre-flight connectivity and health check probe")
    p.add_argument("--cooldown", type=parse_duration, default=5.0,
                   help="Cooldown settling pause in seconds between test steps (default: 5s)")

    # Step Execution Parameters
    p.add_argument("--step-duration", type=parse_duration, default=60.0,
                   help="Test duration for each evaluated RPS step (default: 60s)")
    p.add_argument("--step-warmup", type=parse_duration, default=15.0,
                   help="Warmup duration before measurement for each step (default: 15s)")

    # Workload / Benchmark Options (passed to benchmark runner)
    p.add_argument("--f5-api-url", default=os.getenv("F5_API_URL", DEFAULT_API_URL))
    p.add_argument("--f5-token", default=os.getenv("F5_BEARER_TOKEN", DEFAULT_F5_BEARER_TOKEN))
    p.add_argument("--target-tokens", type=int, default=DEFAULT_TARGET_TOKENS)
    p.add_argument("--tokenizer-model", default=None)
    p.add_argument("--tokenizer-local-files-only", action="store_true")
    p.add_argument("--prompt-text", default=None)
    p.add_argument("--prompt-file", default=None, help="CSV: prompt,expected,category")
    p.add_argument("--prompt-mode", choices=("unique", "repeat"), default="unique")
    p.add_argument("--prefix-cache-rate", "--cache-friendly-percent", dest="cache_friendly_percent",
                   type=float, default=None, metavar="PCT")
    p.add_argument("--random-seed", type=int, default=42)
    p.add_argument("--max-inflight", type=int, default=DEFAULT_MAX_INFLIGHT)
    p.add_argument("--connection-limit", type=int, default=DEFAULT_CONNECTION_LIMIT)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    p.add_argument("--verify-tls", action="store_true")
    p.add_argument("--verbose-scan", action="store_true")
    p.add_argument("--gpu-model", default="")
    p.add_argument("--gpu-count", type=int, default=0)
    p.add_argument("--guardrails-version", "--guardrail-version", "--scanner-version", dest="guardrails_version", default="")
    p.add_argument("--environment", default="")
    p.add_argument("--notes", default="")
    p.add_argument("--reports-dir", default=DEFAULT_REPORTS_DIR)
    p.add_argument("--html-max-rows", type=int, default=1000)
    p.add_argument("--progress-file", default=None,
                   help="Path to write JSON-lines progress events (for GUI integration)")

    args = p.parse_args()
    if args.find_saturation:
        args.search_mode = "saturation"
    return args


def format_error_summary(summary: dict) -> str:
    status_counts = summary.get("status_counts", {})
    errs = []
    for code, count in sorted(status_counts.items()):
        if not (200 <= code < 300):
            tag = f"HTTP {code}" if code > 0 else "Client/Timeout"
            errs.append(f"{tag}: {count}")
    return ", ".join(errs) if errs else "None"


def extract_metric(summary: dict, metric_name: str) -> Optional[float]:
    guardrails = summary.get("guardrails_latency", summary.get("guardrail_latency", summary.get("scanner_latency", {})))
    rtt = summary.get("processed_rtt", {})
    count = guardrails.get("count", 0)
    norm = metric_name.lower().replace("guardrails_", "guardrail_")
    if count == 0 and "guardrail" in norm:
        return None
    if summary.get("processed_2xx", 0) == 0 and "rtt" in norm:
        return None
    if norm == "guardrail_p95":
        return guardrails.get("p95")
    if norm == "guardrail_p50":
        return guardrails.get("p50")
    if norm == "guardrail_p99":
        return guardrails.get("p99")
    if norm == "rtt_p95":
        return rtt.get("p95")
    if norm == "rtt_p50":
        return rtt.get("p50")
    return guardrails.get("p95")


def evaluate_step_compliance(
    summary: dict,
    load: dict,
    target_latency: float,
    metric_name: str,
    max_429_pct: float,
    max_timeouts: int,
    max_error_pct: float = 0.0,
) -> tuple[bool, str]:
    if not load["test_valid"]:
        return False, f"Invalid test: {load.get('saturation_reason', 'Generator saturated')}"

    processed_2xx = summary.get("processed_2xx", 0)
    total_completed = summary.get("completed_results", 0) or 1
    guardrails_count = summary.get("guardrails_latency", summary.get("guardrail_latency", {})).get("count", 0)
    other_http_errors = summary.get("other_http_errors", 0)
    client_errors = summary.get("client_errors", 0)
    rate_limited_pct = summary.get("rate_limited_pct", 0.0)
    timeouts = summary.get("timeouts", 0)
    err_summary = format_error_summary(summary)

    # 1. Zero 2xx success is an immediate hard fail
    if processed_2xx == 0:
        return False, f"0% success (0 HTTP 2xx; {err_summary})"

    # 2. Zero guardrails samples is an immediate hard fail
    if guardrails_count == 0:
        return False, f"0 guardrails samples ({err_summary})"

    reasons = []

    # 3. Server errors check (e.g. 502, 500, 503)
    error_count = other_http_errors + client_errors
    error_pct = (error_count / total_completed) * 100.0
    if error_pct > max_error_pct:
        reasons.append(f"Server errors ({error_pct:.1f}% > {max_error_pct:.1f}%: {err_summary})")

    # 4. Latency SLA check
    observed_latency = extract_metric(summary, metric_name)
    if observed_latency is None:
        reasons.append(f"No valid {metric_name} latency measured")
    elif observed_latency > target_latency:
        reasons.append(f"{metric_name} ({observed_latency:.1f}ms > {target_latency:.0f}ms)")

    # 5. Rate limit (429) check
    if rate_limited_pct > max_429_pct:
        reasons.append(f"HTTP 429 ({rate_limited_pct:.1f}% > {max_429_pct:.1f}%)")

    # 6. Timeout check
    if timeouts > max_timeouts:
        reasons.append(f"Timeouts ({timeouts} > {max_timeouts})")

    if reasons:
        return False, "; ".join(reasons)
    return True, "Compliant"


def evaluate_step_saturation(
    current_summary: dict,
    current_load: dict,
    prev_summary: Optional[dict] = None,
    prev_load: Optional[dict] = None,
    plateau_ratio: float = 0.90,
    latency_jump_factor: float = 2.0,
    marginal_gain_threshold: float = 0.20,
) -> tuple[bool, str, dict]:
    duration = current_load.get("duration_seconds", 0.0) or 1.0
    offered_rps = current_load.get("offered_rps", 0.0)
    processed_2xx = current_summary.get("processed_2xx", 0)
    completed_rps = processed_2xx / duration if duration > 0 else 0.0
    efficiency = (completed_rps / offered_rps) if offered_rps > 0 else 1.0

    curr_g = current_summary.get("guardrails_latency", current_summary.get("guardrail_latency", {}))
    curr_p95 = curr_g.get("p95", 0.0) or 0.0

    reasons = []
    marginal_gain = None
    latency_jump = None

    if prev_summary and prev_load:
        prev_duration = prev_load.get("duration_seconds", 0.0) or 1.0
        prev_offered = prev_load.get("offered_rps", 0.0)
        prev_2xx = prev_summary.get("processed_2xx", 0)
        prev_completed = prev_2xx / prev_duration if prev_duration > 0 else 0.0
        prev_g = prev_summary.get("guardrails_latency", prev_summary.get("guardrail_latency", {}))
        prev_p95 = prev_g.get("p95", 0.0) or 0.0

        delta_offered = offered_rps - prev_offered
        delta_completed = completed_rps - prev_completed

        if delta_offered >= 2.0:
            marginal_gain = delta_completed / delta_offered
            if marginal_gain < marginal_gain_threshold:
                reasons.append(
                    f"Throughput stalled (marginal gain {delta_completed:.1f} RPS for +{delta_offered:.1f} RPS offered, {marginal_gain*100:.1f}% < {marginal_gain_threshold*100:.0f}%)"
                )

        if prev_p95 > 0 and curr_p95 > 0:
            latency_jump = curr_p95 / prev_p95
            if latency_jump >= latency_jump_factor and (curr_p95 - prev_p95) >= 100.0:
                reasons.append(
                    f"Latency hockey-stick knee (P95 jumped {latency_jump:.1f}x from {prev_p95:.0f}ms to {curr_p95:.0f}ms)"
                )

    if efficiency < plateau_ratio:
        reasons.append(
            f"Throughput plateau (completed {completed_rps:.1f} RPS vs {offered_rps:.1f} offered, {efficiency*100:.1f}% < {plateau_ratio*100:.0f}%)"
        )

    if current_load.get("generator_saturated"):
        reasons.append(f"In-flight queue explosion: {current_load.get('saturation_reason', 'Generator saturated')}")

    timeouts = current_summary.get("timeouts", 0)
    if timeouts > 0:
        reasons.append(f"Server timeouts: {timeouts} requests timed out")

    is_sat = len(reasons) > 0
    sat_reason = "; ".join(reasons) if is_sat else "Unsaturated"

    metrics = {
        "completed_rps": completed_rps,
        "efficiency_pct": efficiency * 100.0,
        "marginal_gain": marginal_gain,
        "latency_jump": latency_jump,
    }
    return is_sat, sat_reason, metrics


def print_step_row(
    step_num: int,
    target_rps: float,
    offered_rps: float,
    completed_rps: float,
    start_time: str,
    end_time: str,
    g_p50: Optional[float],
    g_p95: Optional[float],
    g_p99: Optional[float],
    rtt_p95: Optional[float],
    success_pct: float,
    pct_429: float,
    error_summary: str,
    compliant: bool,
    reason: str,
    is_saturated: bool = False,
):
    if is_saturated:
        status_str = "🔴 SATURATED"
    elif compliant:
        status_str = "✅ PASS"
    else:
        status_str = "❌ FAIL"
    s_clk = start_time.split(" ")[-1] if " " in start_time else start_time
    e_clk = end_time.split(" ")[-1] if " " in end_time else end_time
    p50_s = f"{g_p50:11.0f} ms" if g_p50 is not None else "         — ms"
    p95_s = f"{g_p95:11.0f} ms" if g_p95 is not None else "         — ms"
    p99_s = f"{g_p99:11.0f} ms" if g_p99 is not None else "         — ms"
    rtt_s = f"{rtt_p95:6.0f} ms" if rtt_p95 is not None else "    — ms"
    err_s = (error_summary[:17] + "..") if len(error_summary) > 19 else error_summary
    status_col = f"{status_str} ({reason})"[:36]
    print(
        f"| {step_num:4d} | {target_rps:7.1f} | {offered_rps:11.2f} | {completed_rps:9.2f} | "
        f"{s_clk:8s} | {e_clk:8s} | "
        f"{p50_s:14s} | {p95_s:14s} | {p99_s:14s} | {rtt_s:9s} | "
        f"{success_pct:6.1f}% | {pct_429:6.1f}% | {err_s:19s} | {status_col:36s} |"
    )


def generate_max_rps_html(report_path: Path, args, history: list, best_step: Optional[dict], search_start_time: str, search_end_time: str, saturated_step: Optional[dict] = None):
    step_labels = [f"{s['target_rps']:.1f} RPS" for s in history]
    g_p95s = [round(s['summary'].get('guardrails_latency', s['summary'].get('guardrail_latency', {})).get('p95', 0), 1) if s['summary'].get('guardrails_latency', s['summary'].get('guardrail_latency', {})).get('count', 0) > 0 else 0 for s in history]
    g_p50s = [round(s['summary'].get('guardrails_latency', s['summary'].get('guardrail_latency', {})).get('p50', 0), 1) if s['summary'].get('guardrails_latency', s['summary'].get('guardrail_latency', {})).get('count', 0) > 0 else 0 for s in history]
    rtt_p95s = [round(s['summary']['processed_rtt']['p95'], 1) if s['summary'].get('processed_2xx', 0) > 0 else 0 for s in history]
    pct_429s = [round(s['summary']['rate_limited_pct'], 2) for s in history]
    target_line = [args.target_latency_ms] * len(history)
    is_sat_mode = (args.search_mode == "saturation" or saturated_step is not None)

    latency_datasets = []
    if not is_sat_mode:
        latency_datasets.append({
            "label": f"SLA Ceiling ({args.target_latency_ms:.0f} ms)",
            "data": target_line,
            "borderColor": "#f87171",
            "borderDash": [6, 6],
            "borderWidth": 2,
            "pointRadius": 0,
            "fill": False
        })
    latency_datasets.extend([
        {
            "label": "Guardrails Latency P95",
            "data": g_p95s,
            "borderColor": "#00d4ff",
            "backgroundColor": "rgba(0,212,255,0.1)",
            "borderWidth": 3,
            "fill": True,
            "tension": 0.2
        },
        {
            "label": "Guardrails Latency P50",
            "data": g_p50s,
            "borderColor": "#34d399",
            "borderWidth": 2,
            "tension": 0.2
        },
        {
            "label": "Client Round-Trip (RTT) P95",
            "data": rtt_p95s,
            "borderColor": "#fbbf24",
            "borderWidth": 2,
            "tension": 0.2
        }
    ])

    offered_rpss = [round(s['load']['offered_rps'], 2) for s in history]
    completed_rpss = [
        round(s['summary'].get('processed_2xx', 0) / (s['load'].get('duration_seconds', 0.0) or 1.0), 2)
        for s in history
    ]
    peak_inflights = [s['load'].get('peak_inflight', 0) for s in history]

    rows_html = []
    for s in history:
        is_sat = s.get("is_saturated", False)
        badge_cls = "bad" if is_sat or not s["compliant"] else "ok"
        status_text = "SATURATED" if is_sat else ("PASS" if s["compliant"] else "FAIL")
        g = s["summary"].get("guardrails_latency", s["summary"].get("guardrail_latency", {}))
        rtt = s["summary"]["processed_rtt"]
        g_count = g.get("count", 0)
        p50_s = f"{g['p50']:.0f}" if g_count > 0 else "—"
        p95_s = f"{g['p95']:.0f}" if g_count > 0 else "—"
        p99_s = f"{g['p99']:.0f}" if g_count > 0 else "—"
        rtt_s = f"{rtt['p95']:.0f}" if s["summary"].get("processed_2xx", 0) > 0 else "—"
        total_reqs = s["summary"].get("completed_results", 0) or 1
        pct_2xx = (s["summary"].get("processed_2xx", 0) / total_reqs) * 100.0
        dur = s['load'].get('duration_seconds', 0.0) or 1.0
        comp_rps = s['summary'].get('processed_2xx', 0) / dur
        eff_pct = (comp_rps / s['load']['offered_rps'] * 100.0) if s['load']['offered_rps'] > 0 else 100.0
        err_s = format_error_summary(s["summary"])
        s_start = s.get("start_time", "")
        s_end = s.get("end_time", "")

        rows_html.append(
            f"<tr><td>{s['step']}</td><td><strong>{s['target_rps']:.1f}</strong></td>"
            f"<td>{s['load']['offered_rps']:.2f}</td><td><strong>{comp_rps:.2f}</strong></td>"
            f"<td>{eff_pct:.1f}%</td><td>{s_start}</td><td>{s_end}</td>"
            f"<td>{p50_s}</td><td>{p95_s}</td><td>{p99_s}</td><td>{rtt_s}</td>"
            f"<td>{pct_2xx:.1f}%</td><td>{s['summary']['rate_limited_pct']:.2f}%</td>"
            f"<td>{html.escape(err_s)}</td>"
            f"<td><span class='badge {badge_cls}'>{status_text}</span></td>"
            f"<td class='note'>{html.escape(s['reason'])}</td></tr>"
        )

    prefix_cache_str = (
        f"{args.cache_friendly_percent:.1f}%"
        if args.cache_friendly_percent is not None
        else "0% (Unique)"
    )
    cache_desc = f" ({args.cache_friendly_percent:.0f}% prefix cache rate)" if args.cache_friendly_percent is not None else ""

    if saturated_step:
        sat_rps = saturated_step["target_rps"]
        sat_dur = saturated_step["load"].get("duration_seconds", 0.0) or 1.0
        sat_comp_rps = saturated_step["summary"].get("processed_2xx", 0) / sat_dur
        sat_reason = saturated_step.get("saturation_reason", saturated_step.get("reason", ""))
        pre_sat_rps = best_step["target_rps"] if best_step else 0.0
        safe_rps = pre_sat_rps * 0.85
        best_g = best_step["summary"].get("guardrails_latency", best_step["summary"].get("guardrail_latency", {})) if best_step else {}
        best_answer = (
            f"<div style='margin-bottom:12px'><span style='color:#f87171;font-weight:700;font-size:17px'>🔥 GPU 100% Saturation Detected at {sat_rps:.1f} RPS</span></div>"
            f"The GPU bottleneck / throughput ceiling was reached at <strong>{sat_rps:.1f} RPS</strong> "
            f"(physical 2xx completion ceiling: <strong>~{sat_comp_rps:.1f} req/s</strong>).<br>"
            f"<strong>Saturation Trigger:</strong> {html.escape(sat_reason)}<br><br>"
            f"<strong>🏆 Maximum Sustainable Capacity (Pre-Saturation): {pre_sat_rps:.1f} RPS{cache_desc}</strong><br>"
            f"At {pre_sat_rps:.1f} RPS, guardrails latency was <strong>{best_g.get('p95', 0):.1f} ms</strong> (P50: {best_g.get('p50', 0):.1f} ms) "
            f"with <strong>100% throughput efficiency</strong> and 0 timeouts.<br>"
            f"<strong>💡 Recommended Safe Operational Target (85% Headroom):</strong> <strong>~{safe_rps:.1f} RPS</strong>"
            if best_step else
            f"<span style='color:#f87171;font-weight:700'>🔥 GPU saturated immediately at the lowest evaluated rate ({sat_rps:.1f} RPS).</span><br>"
            f"Reason: {html.escape(sat_reason)}"
        )
    elif best_step:
        best_answer = (
            f"<strong>🏆 Maximum Compliant Capacity: {best_step['target_rps']:.1f} RPS{cache_desc}</strong><br>"
            f"At {best_step['target_rps']:.1f} RPS, {args.sla_metric} was "
            f"<strong>{extract_metric(best_step['summary'], args.sla_metric):.1f} ms</strong> "
            f"(under {args.target_latency_ms:.0f} ms SLA) with "
            f"<strong>{best_step['summary'].get('rate_limited_pct', 0.0):.2f}%</strong> HTTP 429 rate and "
            f"<strong>{best_step['summary'].get('timeouts', 0)}</strong> timeouts.<br>"
            f"Offered {best_step['load']['offered_rps']:.2f} RPS with peak in-flight concurrency of {best_step['load']['peak_inflight']}."
        )
    else:
        best_answer = f"<strong>❌ No evaluated RPS met the {args.target_latency_ms:.0f} ms SLA.</strong> Consider testing a lower starting RPS."

    is_sat_mode = (args.search_mode == "saturation" or saturated_step is not None)
    hero_tag = "F5 AI Guardrails · GPU Saturation & Capacity Finder" if is_sat_mode else "F5 AI Guardrails · SLA-Bounded Capacity Finder"
    hero_title = "GPU 100% Saturation & Capacity Report" if is_sat_mode else f"Maximum Sustainable RPS Under {args.target_latency_ms:.0f} ms SLA"

    if is_sat_mode:
        sla_meta_item = "<span>Goal: GPU 100% Saturation &amp; Throughput Ceiling</span>"
        latency_card_title = "Latency Progression &amp; Hockey-Stick Knee Curve"
        latency_card_note = "Steep vertical rise in latency marks internal queue buildup as the GPU reaches 100% compute/memory bandwidth saturation."
    else:
        sla_meta_item = f"<span>SLA Budget: {args.target_latency_ms:.0f} ms ({args.sla_metric})</span>"
        latency_card_title = "Latency vs. Offered RPS Capacity Curve"
        latency_card_note = f"Horizontal dashed line indicates your target SLA budget ({args.target_latency_ms:.0f} ms)."

    content = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>F5 AI Guardrails Max RPS Capacity Report</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root{{--bg:#0b0f1a;--surface:#111827;--surface2:#1a2236;--border:#1e2d45;--accent:#00d4ff;--green:#34d399;--amber:#fbbf24;--red:#f87171;--text:#e2e8f0;--muted:#64748b}}
*{{box-sizing:border-box}} body{{margin:0;font-family:Inter,Arial,sans-serif;background:var(--bg);color:var(--text)}}
.hero{{padding:40px;background:linear-gradient(135deg,#0b0f1a,#0f172a,#111827);border-bottom:1px solid var(--border)}}
.hero-tag{{color:var(--accent);letter-spacing:2px;text-transform:uppercase;font-size:11px}} h1{{margin:8px 0;font-size:32px}}
.hero-meta{{color:var(--muted);font-family:monospace;font-size:12px;display:flex;gap:18px;flex-wrap:wrap}}
.main{{max-width:1400px;margin:auto;padding:30px 40px}}
.card{{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:22px;margin-bottom:22px}}
.callout{{background:var(--surface2);border-left:4px solid var(--accent);padding:18px;border-radius:6px;line-height:1.7;font-size:15px}}
.chart-wrap{{height:380px;margin-top:10px}}
table{{width:100%;border-collapse:collapse;font-size:13px}} th{{background:var(--surface2);color:var(--muted);padding:10px;text-align:left}}
td{{padding:10px;border-bottom:1px solid var(--border);font-family:monospace}}
.badge{{padding:3px 8px;border-radius:4px;font-weight:600}}
.ok{{color:var(--green);background:rgba(52,211,153,.15)}} .bad{{color:var(--red);background:rgba(248,113,113,.15)}}
.note{{color:var(--muted);font-size:12px;font-family:sans-serif}}
</style></head><body>
<div class="hero">
  <div class="hero-tag">{hero_tag}</div>
  <h1>{hero_title}</h1>
  <div class="hero-meta">
    <span>Start: {search_start_time}</span>
    <span>End: {search_end_time}</span>
    <span>Target URL: {html.escape(args.f5_api_url)}</span>
    <span>GPU: {html.escape(args.gpu_model or 'Unspecified')}</span>
    {sla_meta_item}
    <span>Payload: {args.target_tokens} tokens</span>
    <span>Prefix Cache Rate: {prefix_cache_str}</span>
    <span>Search Strategy: {args.search_mode.upper()}</span>
  </div>
</div>
<div class="main">
  <div class="card">
    <h2 style="margin-top:0">Overall Summary</h2>
    <div class="callout">{best_answer}</div>
  </div>

  <div class="card">
    <h2 style="margin-top:0">Throughput &amp; GPU Saturation Curve (Offered vs. Completed 2xx RPS)</h2>
    <div class="note">The point where Completed 2xx RPS (cyan line) diverges or flattens below Offered RPS (dashed line) marks the GPU 100% capacity limit.</div>
    <div class="chart-wrap"><canvas id="throughputChart"></canvas></div>
  </div>

  <div class="card">
    <h2 style="margin-top:0">{latency_card_title}</h2>
    <div class="note">{latency_card_note}</div>
    <div class="chart-wrap"><canvas id="slaChart"></canvas></div>
  </div>

  <div class="card">
    <h2 style="margin-top:0">Evaluated Steps Comparison</h2>
    <table>
      <thead>
        <tr>
          <th>Step</th><th>Target RPS</th><th>Offered RPS</th><th>2xx Completed RPS</th><th>Efficiency</th><th>Start Time</th><th>End Time</th>
          <th>Guardrails P50(ms)</th><th>Guardrails P95(ms)</th><th>Guardrails P99(ms)</th><th>RTT P95(ms)</th>
          <th>2xx Rate</th><th>429 Rate</th><th>Server Errors</th><th>Compliance</th><th>Details</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows_html)}
      </tbody>
    </table>
  </div>
</div>
<script>
new Chart(document.getElementById('throughputChart'), {{
  type: 'line',
  data: {{
    labels: {json.dumps(step_labels)},
    datasets: [
      {{
        label: 'Offered Workload (Target)',
        data: {json.dumps(offered_rpss)},
        borderColor: '#94a3b8',
        borderDash: [5, 5],
        borderWidth: 2,
        pointRadius: 3,
        fill: false
      }},
      {{
        label: 'Completed 2xx Throughput (RPS)',
        data: {json.dumps(completed_rpss)},
        borderColor: '#00d4ff',
        backgroundColor: 'rgba(0,212,255,0.15)',
        borderWidth: 3,
        pointRadius: 5,
        pointHoverRadius: 7,
        fill: true,
        tension: 0.15
      }},
      {{
        label: 'Peak In-Flight Requests',
        data: {json.dumps(peak_inflights)},
        borderColor: '#a78bfa',
        borderWidth: 2,
        pointRadius: 3,
        yAxisID: 'y1',
        fill: false
      }}
    ]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    scales: {{
      y: {{
        title: {{ display: true, text: 'Throughput (RPS)' }},
        beginAtZero: true
      }},
      y1: {{
        position: 'right',
        title: {{ display: true, text: 'Peak In-Flight Concurrency' }},
        beginAtZero: true,
        grid: {{ drawOnChartArea: false }}
      }},
      x: {{
        title: {{ display: true, text: 'Evaluated Workload' }}
      }}
    }}
  }}
}});

new Chart(document.getElementById('slaChart'), {{
  type: 'line',
  data: {{
    labels: {json.dumps(step_labels)},
    datasets: {json.dumps(latency_datasets)}
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    scales: {{
      y: {{
        title: {{ display: true, text: 'Latency (ms)' }},
        beginAtZero: true
      }},
      x: {{
        title: {{ display: true, text: 'Evaluated Workload' }}
      }}
    }}
  }}
}});
</script>
</body></html>"""
    report_path.write_text(content, encoding="utf-8")


def write_history_csv(path: Path, history: list, args, best_step: Optional[dict], search_start_time: str, search_end_time: str, saturated_step: Optional[dict] = None):
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["F5 AI Guardrails Max RPS Capacity Finder Summary"])
        w.writerow(["Target URL", args.f5_api_url])
        w.writerow(["Test Start Time", search_start_time])
        w.writerow(["Test End Time", search_end_time])
        w.writerow(["GPU Model", args.gpu_model])
        w.writerow(["Guardrails Version", getattr(args, "guardrails_version", "") or getattr(args, "guardrail_version", "")])
        w.writerow(["Prefix Cache Rate", f"{args.cache_friendly_percent:.1f}%" if args.cache_friendly_percent is not None else "0% (Unique)"])
        w.writerow(["Search Mode", args.search_mode.upper()])
        if saturated_step:
            sat_dur = saturated_step["load"].get("duration_seconds", 0.0) or 1.0
            sat_comp = saturated_step["summary"].get("processed_2xx", 0) / sat_dur
            w.writerow(["GPU 100% Saturation Detected", "YES"])
            w.writerow(["GPU Saturation RPS", f"{saturated_step['target_rps']:.1f} RPS"])
            w.writerow(["Saturation Trigger", saturated_step.get("saturation_reason", saturated_step.get("reason", ""))])
            w.writerow(["Physical Completed 2xx Ceiling", f"{sat_comp:.2f} req/s"])
            w.writerow(["Max Sustainable Capacity", f"{best_step['target_rps']:.1f} RPS" if best_step else "None"])
        else:
            w.writerow(["GPU 100% Saturation Detected", "NO"])
            w.writerow(["SLA Target", f"{args.target_latency_ms:.0f} ms", args.sla_metric])
            w.writerow(["Max Compliant RPS", f"{best_step['target_rps']:.1f}" if best_step else "None"])
        w.writerow([])
        w.writerow([
            "Step", "Target RPS", "Offered RPS", "2xx Completed RPS", "Throughput Efficiency (%)", "Start Time", "End Time",
            "Guardrails P50 (ms)", "Guardrails P95 (ms)", "Guardrails P99 (ms)", "RTT P95 (ms)",
            "HTTP 2xx Rate (%)", "HTTP 429 Rate (%)", "Timeouts", "Server Errors", "Compliance", "Failure Reason"
        ])
        for s in history:
            g = s["summary"].get("guardrails_latency", s["summary"].get("guardrail_latency", {}))
            rtt = s["summary"]["processed_rtt"]
            g_count = g.get("count", 0)
            p50_val = f"{g['p50']:.1f}" if g_count > 0 else ""
            p95_val = f"{g['p95']:.1f}" if g_count > 0 else ""
            p99_val = f"{g['p99']:.1f}" if g_count > 0 else ""
            rtt_val = f"{rtt['p95']:.1f}" if s["summary"].get("processed_2xx", 0) > 0 else ""
            total_reqs = s["summary"].get("completed_results", 0) or 1
            pct_2xx = (s["summary"].get("processed_2xx", 0) / total_reqs) * 100.0
            dur = s["load"].get("duration_seconds", 0.0) or 1.0
            comp_rps = s["summary"].get("processed_2xx", 0) / dur
            eff_pct = (comp_rps / s["load"]["offered_rps"] * 100.0) if s["load"]["offered_rps"] > 0 else 100.0
            err_str = format_error_summary(s["summary"])
            comp_status = "SATURATED" if s.get("is_saturated") else ("PASS" if s["compliant"] else "FAIL")

            w.writerow([
                s["step"],
                f"{s['target_rps']:.1f}",
                f"{s['load']['offered_rps']:.2f}",
                f"{comp_rps:.2f}",
                f"{eff_pct:.1f}",
                s.get("start_time", ""),
                s.get("end_time", ""),
                p50_val,
                p95_val,
                p99_val,
                rtt_val,
                f"{pct_2xx:.2f}",
                f"{s['summary']['rate_limited_pct']:.2f}",
                s["summary"]["timeouts"],
                err_str,
                comp_status,
                s["reason"]
            ])


def main():
    args = parse_args()
    if not args.f5_token:
        raise SystemExit("ERROR: Bearer token is empty. Provide --f5-token or set F5_BEARER_TOKEN.")
    if args.cache_friendly_percent is not None and not (0.0 <= args.cache_friendly_percent <= 100.0):
        raise SystemExit("ERROR: --prefix-cache-rate must be between 0 and 100")

    print("=" * 95)
    print("F5 AI GUARDRAILS MAX RPS CAPACITY FINDER")
    if args.search_mode == "saturation":
        print("Goal: Detect GPU 100% Saturation Bottleneck, Knee Point & Maximum Throughput")
    else:
        print(f"Goal: Find maximum RPS where {args.sla_metric.upper()} <= {args.target_latency_ms:.0f} ms")
    print("=" * 95)
    if args.search_mode == "saturation":
        print("Search Mode:            SATURATION (GPU 100% Bottleneck Finder)")
        print(f"Throughput Plateau:     < {args.plateau_ratio * 100:.0f}% completion efficiency")
        print(f"Latency Knee Jump:      >= {args.latency_jump_factor:.1f}x P95 jump")
        print(f"Marginal Stalling:      < {args.marginal_gain_threshold * 100:.0f}% marginal gain")
    else:
        print(f"Target Latency Budget:  {args.target_latency_ms:.0f} ms ({args.sla_metric})")
        print(f"Search Mode:            {args.search_mode.upper()}")
    print(f"Step Duration:          {format_duration(args.step_duration)} (Warmup: {format_duration(args.step_warmup)})")
    print(f"Step Error Ceilings:    429 Rate <= {args.max_429_pct:.1f}%, Timeouts <= {args.max_timeouts}")
    print(f"Payload Size:           {args.target_tokens} target tokens")
    print(f"Prefix Cache Rate:      {f'{args.cache_friendly_percent:.1f}%' if args.cache_friendly_percent is not None else '0% (Unique)'}")
    print(f"GPU Model:              {args.gpu_model or 'Not specified'}")
    print(f"Target URL:             {args.f5_api_url}")
    print("=" * 95)

    # Load templates once
    templates = load_prompt_templates(args.prompt_text, args.prompt_file)
    print(f"Loaded {len(templates)} benchmark prompt templates.\n")

    if not args.skip_preflight:
        print(f"Pre-flight health check: probing {args.f5_api_url} ...")
        ok, status, err_text, cai = preflight_probe(
            args.f5_api_url, args.f5_token, args.verify_tls, timeout_seconds=10.0
        )
        if not ok:
            print(f"\n❌ PRE-FLIGHT HEALTH CHECK FAILED (HTTP {status})")
            print(f"   Response from server: {err_text}")
            print("   The backend endpoint is returning errors and is not ready for load testing.")
            print("   Please check your model pod / KubeAI status before running capacity search.")
            print("   (Pass --skip-preflight to force execution anyway)\n")
            raise SystemExit(1)
        lat_str = f", guardrails latency: {cai:.1f} ms" if cai is not None else ""
        print(f"✅ Endpoint healthy (HTTP {status}{lat_str})\n")

    search_start_dt = datetime.now()
    search_start_time = search_start_dt.strftime("%Y-%m-%d %H:%M:%S")

    history = []
    best_step = None
    prev_summary = None
    prev_load = None
    saturated_step = None

    # Table Header
    print("+" + "-"*6 + "+" + "-"*9 + "+" + "-"*13 + "+" + "-"*11 + "+" + "-"*10 + "+" + "-"*10 + "+" + "-"*16 + "+" + "-"*16 + "+" + "-"*16 + "+" + "-"*11 + "+" + "-"*9 + "+" + "-"*9 + "+" + "-"*21 + "+" + "-"*38 + "+")
    print("| Step | Set RPS | Offered RPS |  2xx RPS  |  Start   |   End    | Guardrails P50 | Guardrails P95 | Guardrails P99 |  RTT P95  | 2xx (%) | 429 (%) | Server Errors       | Status / Saturation Reason            |")
    print("+" + "-"*6 + "+" + "-"*9 + "+" + "-"*13 + "+" + "-"*11 + "+" + "-"*10 + "+" + "-"*10 + "+" + "-"*16 + "+" + "-"*16 + "+" + "-"*16 + "+" + "-"*11 + "+" + "-"*9 + "+" + "-"*9 + "+" + "-"*21 + "+" + "-"*38 + "+")

    step_count = 0
    effective_prompt_mode = "mixed" if args.cache_friendly_percent is not None else args.prompt_mode

    # -------------------------------------------------------------------------
    # Execution Loop: Ladder / Saturation Mode
    # -------------------------------------------------------------------------
    if args.search_mode in ("ladder", "saturation"):
        current_rps = args.start_rps
        while step_count < args.max_steps and current_rps <= args.max_rps:
            step_count += 1

            warmup_reqs = int(round(current_rps * args.step_warmup)) if args.step_warmup > 0 else 0
            measured_reqs = int(round(current_rps * args.step_duration))

            w_plan, _ = prepare_request_plan(
                templates, warmup_reqs, args.target_tokens, args.tokenizer_model,
                args.tokenizer_local_files_only, args.random_seed, args.prompt_mode,
                args.cache_friendly_percent, 0
            )
            m_plan, t_info = prepare_request_plan(
                templates, measured_reqs, args.target_tokens, args.tokenizer_model,
                args.tokenizer_local_files_only, args.random_seed + 1, args.prompt_mode,
                args.cache_friendly_percent, warmup_reqs
            )

            step_start_dt = datetime.now()
            results, load = run_open_loop(
                warmup_plan=w_plan,
                measured_plan=m_plan,
                target_rps=current_rps,
                warmup_seconds=args.step_warmup,
                duration_seconds=args.step_duration,
                max_inflight=args.max_inflight,
                connection_limit=args.connection_limit,
                api_url=args.f5_api_url,
                bearer_token=args.f5_token,
                timeout_seconds=args.timeout,
                verify_tls=args.verify_tls,
                verbose=args.verbose_scan,
                progress_interval=0,  # keep terminal quiet during step
                progress_file=getattr(args, 'progress_file', None),
            )
            step_end_dt = datetime.now()
            summary = aggregate(results, load)

            is_sat, sat_reason, sat_metrics = evaluate_step_saturation(
                summary, load, prev_summary, prev_load,
                plateau_ratio=args.plateau_ratio,
                latency_jump_factor=args.latency_jump_factor,
                marginal_gain_threshold=args.marginal_gain_threshold,
            )

            sla_compliant, sla_reason = evaluate_step_compliance(
                summary, load, args.target_latency_ms, args.sla_metric,
                args.max_429_pct, args.max_timeouts, args.max_error_pct
            )

            if args.search_mode == "saturation":
                compliant = not is_sat
                reason = "Healthy throughput" if compliant else sat_reason
            else:
                if is_sat:
                    compliant = False
                    reason = f"{sla_reason}; Saturation: {sat_reason}" if not sla_compliant else f"Saturation: {sat_reason}"
                else:
                    compliant = sla_compliant
                    reason = sla_reason

            g = summary.get("guardrails_latency", summary.get("guardrail_latency", {}))
            g_count = g.get("count", 0)
            g_p50 = g.get("p50") if g_count > 0 else None
            g_p95 = g.get("p95") if g_count > 0 else None
            g_p99 = g.get("p99") if g_count > 0 else None
            rtt_p95 = summary.get("processed_rtt", {}).get("p95") if summary.get("processed_2xx", 0) > 0 else None
            total_reqs = summary.get("completed_results", 0) or 1
            success_pct = (summary.get("processed_2xx", 0) / total_reqs) * 100.0
            err_summary = format_error_summary(summary)

            step_start_time = load.get("start_time") or step_start_dt.strftime("%Y-%m-%d %H:%M:%S")
            step_end_time = load.get("end_time") or step_end_dt.strftime("%Y-%m-%d %H:%M:%S")

            step_data = {
                "step": step_count,
                "target_rps": current_rps,
                "start_time": step_start_time,
                "end_time": step_end_time,
                "load": load,
                "summary": summary,
                "token_info": t_info,
                "compliant": compliant,
                "reason": reason,
                "is_saturated": is_sat,
                "saturation_reason": sat_reason,
                "saturation_metrics": sat_metrics,
            }
            history.append(step_data)

            if is_sat and saturated_step is None:
                saturated_step = step_data

            print_step_row(
                step_count, current_rps, load["offered_rps"], sat_metrics["completed_rps"],
                step_start_time, step_end_time,
                g_p50, g_p95, g_p99, rtt_p95,
                success_pct, summary.get("rate_limited_pct", 0.0),
                err_summary, compliant, reason,
                is_saturated=is_sat
            )

            if getattr(args, 'progress_file', None):
                try:
                    with open(args.progress_file, "a") as pf:
                        pf.write(json.dumps({
                            "ts": time.time(),
                            "type": "step_complete",
                            "step": step_count,
                            "target_rps": current_rps,
                            "offered_rps": round(load["offered_rps"], 2),
                            "completed_rps": round(sat_metrics["completed_rps"], 2),
                            "compliant": compliant,
                            "is_saturated": is_sat,
                            "g_p50": g_p50,
                            "g_p95": g_p95,
                            "g_p99": g_p99,
                            "rtt_p95": rtt_p95,
                            "success_pct": round(success_pct, 2),
                            "pct_429": round(summary.get("rate_limited_pct", 0.0), 2),
                            "reason": reason,
                        }) + "\n")
                        pf.flush()
                except OSError:
                    pass

            prev_summary = summary
            prev_load = load

            if summary.get("processed_2xx", 0) == 0:
                print(f"\n❌ CIRCUIT BREAKER TRIGGERED: 0 successful HTTP 2xx responses at Step {step_count} ({err_summary}).")
                print("   The backend service appears completely broken or down. Aborting further RPS ramp-up.\n")
                break

            if compliant:
                best_step = step_data
                current_rps += args.step_rps
                if args.cooldown > 0:
                    time.sleep(args.cooldown)
            else:
                break

    # -------------------------------------------------------------------------
    # Execution Loop: Binary Search Mode
    # -------------------------------------------------------------------------
    elif args.search_mode == "binary":
        low_rps = args.min_rps
        high_rps = args.max_rps

        while step_count < args.max_steps and (high_rps - low_rps) > args.rps_tolerance:
            step_count += 1
            mid_rps = round((low_rps + high_rps) / 2.0, 1)

            warmup_reqs = int(round(mid_rps * args.step_warmup)) if args.step_warmup > 0 else 0
            measured_reqs = int(round(mid_rps * args.step_duration))

            w_plan, _ = prepare_request_plan(
                templates, warmup_reqs, args.target_tokens, args.tokenizer_model,
                args.tokenizer_local_files_only, args.random_seed, args.prompt_mode,
                args.cache_friendly_percent, 0
            )
            m_plan, t_info = prepare_request_plan(
                templates, measured_reqs, args.target_tokens, args.tokenizer_model,
                args.tokenizer_local_files_only, args.random_seed + 1, args.prompt_mode,
                args.cache_friendly_percent, warmup_reqs
            )

            step_start_dt = datetime.now()
            results, load = run_open_loop(
                warmup_plan=w_plan,
                measured_plan=m_plan,
                target_rps=mid_rps,
                warmup_seconds=args.step_warmup,
                duration_seconds=args.step_duration,
                max_inflight=args.max_inflight,
                connection_limit=args.connection_limit,
                api_url=args.f5_api_url,
                bearer_token=args.f5_token,
                timeout_seconds=args.timeout,
                verify_tls=args.verify_tls,
                verbose=args.verbose_scan,
                progress_interval=0,
                progress_file=getattr(args, 'progress_file', None),
            )
            step_end_dt = datetime.now()
            summary = aggregate(results, load)

            is_sat, sat_reason, sat_metrics = evaluate_step_saturation(
                summary, load, prev_summary, prev_load,
                plateau_ratio=args.plateau_ratio,
                latency_jump_factor=args.latency_jump_factor,
                marginal_gain_threshold=args.marginal_gain_threshold,
            )

            sla_compliant, sla_reason = evaluate_step_compliance(
                summary, load, args.target_latency_ms, args.sla_metric,
                args.max_429_pct, args.max_timeouts, args.max_error_pct
            )

            if is_sat:
                compliant = False
                reason = f"{sla_reason}; Saturation: {sat_reason}" if not sla_compliant else f"Saturation: {sat_reason}"
            else:
                compliant = sla_compliant
                reason = sla_reason

            g = summary.get("guardrails_latency", summary.get("guardrail_latency", {}))
            g_count = g.get("count", 0)
            g_p50 = g.get("p50") if g_count > 0 else None
            g_p95 = g.get("p95") if g_count > 0 else None
            g_p99 = g.get("p99") if g_count > 0 else None
            rtt_p95 = summary.get("processed_rtt", {}).get("p95") if summary.get("processed_2xx", 0) > 0 else None
            total_reqs = summary.get("completed_results", 0) or 1
            success_pct = (summary.get("processed_2xx", 0) / total_reqs) * 100.0
            err_summary = format_error_summary(summary)

            step_start_time = load.get("start_time") or step_start_dt.strftime("%Y-%m-%d %H:%M:%S")
            step_end_time = load.get("end_time") or step_end_dt.strftime("%Y-%m-%d %H:%M:%S")

            step_data = {
                "step": step_count,
                "target_rps": mid_rps,
                "start_time": step_start_time,
                "end_time": step_end_time,
                "load": load,
                "summary": summary,
                "token_info": t_info,
                "compliant": compliant,
                "reason": reason,
                "is_saturated": is_sat,
                "saturation_reason": sat_reason,
                "saturation_metrics": sat_metrics,
            }
            history.append(step_data)

            if is_sat and saturated_step is None:
                saturated_step = step_data

            print_step_row(
                step_count, mid_rps, load["offered_rps"], sat_metrics["completed_rps"],
                step_start_time, step_end_time,
                g_p50, g_p95, g_p99, rtt_p95,
                success_pct, summary.get("rate_limited_pct", 0.0),
                err_summary, compliant, reason,
                is_saturated=is_sat
            )

            if getattr(args, 'progress_file', None):
                try:
                    with open(args.progress_file, "a") as pf:
                        pf.write(json.dumps({
                            "ts": time.time(),
                            "type": "step_complete",
                            "step": step_count,
                            "target_rps": mid_rps,
                            "offered_rps": round(load["offered_rps"], 2),
                            "completed_rps": round(sat_metrics["completed_rps"], 2),
                            "compliant": compliant,
                            "is_saturated": is_sat,
                            "g_p50": g_p50,
                            "g_p95": g_p95,
                            "g_p99": g_p99,
                            "rtt_p95": rtt_p95,
                            "success_pct": round(success_pct, 2),
                            "pct_429": round(summary.get("rate_limited_pct", 0.0), 2),
                            "reason": reason,
                        }) + "\n")
                        pf.flush()
                except OSError:
                    pass

            prev_summary = summary
            prev_load = load

            if summary.get("processed_2xx", 0) == 0:
                print(f"\n❌ CIRCUIT BREAKER TRIGGERED: 0 successful HTTP 2xx responses at Step {step_count} ({err_summary}).")
                print("   The backend service appears completely broken or down. Aborting further RPS ramp-up.\n")
                break

            if compliant:
                best_step = step_data
                low_rps = mid_rps  # Can handle at least this rate, search higher
            else:
                high_rps = mid_rps # Exceeded SLA or saturated, search lower

            if args.cooldown > 0:
                time.sleep(args.cooldown)

    print("+" + "-"*6 + "+" + "-"*9 + "+" + "-"*13 + "+" + "-"*11 + "+" + "-"*10 + "+" + "-"*10 + "+" + "-"*16 + "+" + "-"*16 + "+" + "-"*16 + "+" + "-"*11 + "+" + "-"*9 + "+" + "-"*9 + "+" + "-"*21 + "+" + "-"*38 + "+")

    # Generate Reports
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = "".join(c.lower() if c.isalnum() else "-" for c in (args.gpu_model or "gpu-unspecified"))
    slug = "-".join(filter(None, slug.split("-")))[:50]
    cache_tag = f"-prefix{args.cache_friendly_percent:g}pct" if args.cache_friendly_percent is not None else ""
    mode_tag = "saturation" if args.search_mode == "saturation" else f"{args.target_latency_ms:.0f}ms"
    out_dir = Path(args.reports_dir) / f"max-rps-search-{slug}{cache_tag}-{mode_tag}-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    html_path = out_dir / "max_rps_capacity_report.html"
    csv_path = out_dir / "max_rps_capacity_summary.csv"

    search_end_dt = datetime.now()
    search_end_time = search_end_dt.strftime("%Y-%m-%d %H:%M:%S")

    generate_max_rps_html(html_path, args, history, best_step, search_start_time, search_end_time, saturated_step=saturated_step)
    write_history_csv(csv_path, history, args, best_step, search_start_time, search_end_time, saturated_step=saturated_step)

    summary_json_path = out_dir / "summary.json"
    summary_data = {
        "search_mode": args.search_mode,
        "gpu_model": args.gpu_model,
        "gpu_count": args.gpu_count,
        "target_tokens": args.target_tokens,
        "prefix_cache_rate": args.cache_friendly_percent,
        "target_latency_ms": args.target_latency_ms,
        "sla_metric": args.sla_metric,
        "steps": len(history),
        "best_rps": best_step["target_rps"] if best_step else None,
        "best_g_p95": (best_step["summary"].get("guardrails_latency", best_step["summary"].get("guardrail_latency", {})).get("p95")) if best_step else None,
        "saturated_rps": saturated_step["target_rps"] if saturated_step else None,
        "saturation_reason": (saturated_step.get("saturation_reason", "") if saturated_step else None),
        "history": [{
            "step": s["step"],
            "target_rps": s["target_rps"],
            "offered_rps": round(s["load"]["offered_rps"], 2),
            "completed_rps": round(s["summary"].get("processed_2xx", 0) / (s["load"].get("duration_seconds", 1.0) or 1.0), 2),
            "compliant": s["compliant"],
            "is_saturated": s.get("is_saturated", False),
            "g_p50": s["summary"].get("guardrails_latency", s["summary"].get("guardrail_latency", {})).get("p50"),
            "g_p95": s["summary"].get("guardrails_latency", s["summary"].get("guardrail_latency", {})).get("p95"),
            "g_p99": s["summary"].get("guardrails_latency", s["summary"].get("guardrail_latency", {})).get("p99"),
            "rtt_p95": s["summary"].get("processed_rtt", {}).get("p95"),
            "pct_429": round(s["summary"].get("rate_limited_pct", 0.0), 2),
            "success_pct": round((s["summary"].get("processed_2xx", 0) / (s["summary"].get("completed_results", 0) or 1)) * 100.0, 2),
            "reason": s["reason"],
        } for s in history],
        "start_time": search_start_time,
        "end_time": search_end_time,
    }
    summary_json_path.write_text(json.dumps(summary_data, indent=2), encoding="utf-8")

    if getattr(args, 'progress_file', None):
        try:
            with open(args.progress_file, "a") as pf:
                pf.write(json.dumps({
                    "ts": time.time(),
                    "type": "finished",
                    "exit_code": 0,
                    "best_rps": best_step["target_rps"] if best_step else None,
                    "reports_dir": str(out_dir),
                }) + "\n")
                pf.flush()
        except OSError:
            pass

    print("\n" + "=" * 80)
    print("CAPACITY SEARCH RESULT")
    print("=" * 80)
    print(f"Target URL:             {args.f5_api_url}")
    print(f"Test Start Time:        {search_start_time}")
    print(f"Test End Time:          {search_end_time}")
    print(f"Total Search Duration:  {(search_end_dt - search_start_dt).total_seconds():.1f}s")
    print(f"Prefix Cache Rate:      {f'{args.cache_friendly_percent:.1f}%' if args.cache_friendly_percent is not None else '0% (Unique)'}")
    print(f"Search Mode:            {args.search_mode.upper()}")

    if saturated_step:
        sat_rps = saturated_step["target_rps"]
        sat_dur = saturated_step["load"].get("duration_seconds", 0.0) or 1.0
        sat_comp_rps = saturated_step["summary"].get("processed_2xx", 0) / sat_dur
        sat_reason = saturated_step.get("saturation_reason", saturated_step.get("reason", ""))
        print(f"\n🔥 GPU 100% SATURATION DETECTED: {sat_rps:.1f} RPS")
        print(f"   - Saturation Trigger:   {sat_reason}")
        print(f"   - Physical GPU Ceiling: ~{sat_comp_rps:.1f} completed 2xx req/s")

    if best_step:
        best_g = best_step["summary"].get("guardrails_latency", best_step["summary"].get("guardrail_latency", {}))
        best_rtt = best_step["summary"]["processed_rtt"]
        best_dur = best_step["load"].get("duration_seconds", 0.0) or 1.0
        best_comp = best_step["summary"].get("processed_2xx", 0) / best_dur
        title = "MAXIMUM SUSTAINABLE CAPACITY (PRE-SATURATION)" if saturated_step or args.search_mode == "saturation" else "MAXIMUM COMPLIANT CAPACITY"
        print(f"\n🏆 {title}: {best_step['target_rps']:.1f} RPS")
        print(f"   At {best_step['target_rps']:.1f} RPS:")
        print(f"   - 2xx Completion Rate:  {best_comp:.1f} req/s ({(best_comp / best_step['load']['offered_rps']) * 100:.1f}% efficiency)")
        if args.search_mode != "saturation":
            print(f"   - {args.sla_metric.upper()}:     {extract_metric(best_step['summary'], args.sla_metric):.1f} ms (Target Budget <= {args.target_latency_ms:.0f} ms)")
        print(f"   - Guardrails P50:       {best_g['p50']:.1f} ms")
        print(f"   - Guardrails P95:       {best_g['p95']:.1f} ms")
        print(f"   - Guardrails P99:       {best_g['p99']:.1f} ms")
        print(f"   - Client RTT P95:      {best_rtt['p95']:.1f} ms")
        print(f"   - HTTP 429 Rate:       {best_step['summary']['rate_limited_pct']:.2f}%")
        print(f"   - Peak In-flight:      {best_step['load']['peak_inflight']} concurrent requests")
        safe_rps = best_step['target_rps'] * 0.85
        print(f"\n💡 Recommended Safe Production Ceiling (85% Headroom): ~{safe_rps:.1f} RPS")
    else:
        print(f"\n❌ No tested RPS satisfied the capacity criteria.")
        if history:
            print(f"   The lowest tested rate ({history[0]['target_rps']:.1f} RPS) failed compliance or saturated.")

    print("\nReports Generated:")
    print(f"   HTML: {html_path.resolve()}")
    print(f"   CSV:  {csv_path.resolve()}")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    main()
