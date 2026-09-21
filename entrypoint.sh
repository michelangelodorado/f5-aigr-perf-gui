#!/bin/sh
set -e

mkdir -p /app/data/results /app/data/prompts

if [ ! -f /app/data/prompts/f5_perf_prompts.csv ] && [ -f /app/f5_perf_prompts.csv ]; then
  cp /app/f5_perf_prompts.csv /app/data/prompts/f5_perf_prompts.csv
fi

case "${1:-}" in
  cli)
    shift
    exec python /app/f5_find_max_rps.py "$@"
    ;;
  benchmark)
    shift
    exec python /app/f5_guardrails_perf.py "$@"
    ;;
  *)
    exec uvicorn server:app --host 0.0.0.0 --port "${PORT:-8080}"
    ;;
esac
