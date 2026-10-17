#!/bin/sh
# Managed-evaluation entrypoint: the platform starts this container with
# BENCHMARK_BASE_URL / BENCHMARK_TOKEN (and the model credential) injected,
# and the run begins immediately with no operator in the loop.
set -eu

: "${BENCHMARK_BASE_URL:?BENCHMARK_BASE_URL must be injected by the platform}"
: "${BENCHMARK_TOKEN:?BENCHMARK_TOKEN must be injected by the platform}"
: "${DEEPSEEK_API_KEY:?DEEPSEEK_API_KEY must be injected by the platform}"

# Time budgets: HARNESS_WALL_TIME is the per-challenge soft limit (a stuck
# case finalizes and frees its slot); HARNESS_TOTAL_WALL_TIME is the global
# window for the whole set, which stops dispatching near the end so the
# finalize grace fits. Defaults fit the ranking set's 360-minute window; the
# competition expects ~10h - set HARNESS_TOTAL_WALL_TIME=34800 there.
# The solver can dispatch focused sub-agents (harness subagent); point them
# at this agent spec by default.
export HARNESS_AGENT="/opt/red-harness/agents/hosted/pi-deepseek.yaml"

WALL_TIME="${HARNESS_WALL_TIME:-1800}"
TOTAL_WALL_TIME="${HARNESS_TOTAL_WALL_TIME:-20400}"
CONCURRENCY="${HARNESS_CONCURRENCY:-3}"
# Failed or timed-out challenges are retried on later passes while the
# window lasts, so a single stuck attempt never forfeits a question.
MAX_PASSES="${HARNESS_MAX_PASSES:-3}"

exec harness tsec \
  --agent /opt/red-harness/agents/hosted/pi-deepseek.yaml \
  --knowledge-root /opt/red-harness/knowledge \
  --runs-root /work/tsec-runs \
  --all \
  --allow-host-agent \
  --wall-time "$WALL_TIME" \
  --total-wall-time "$TOTAL_WALL_TIME" \
  --concurrency "$CONCURRENCY" \
  --max-passes "$MAX_PASSES"
