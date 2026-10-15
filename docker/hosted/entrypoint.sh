#!/bin/sh
# Managed-evaluation entrypoint: the platform starts this container with
# BENCHMARK_BASE_URL / BENCHMARK_TOKEN (and the model credential) injected,
# and the run begins immediately with no operator in the loop.
set -eu

: "${BENCHMARK_BASE_URL:?BENCHMARK_BASE_URL must be injected by the platform}"
: "${BENCHMARK_TOKEN:?BENCHMARK_TOKEN must be injected by the platform}"
: "${DEEPSEEK_API_KEY:?DEEPSEEK_API_KEY must be injected by the platform}"

# The card lists a 360-minute task window; leave a margin for startup and the
# finalize grace window instead of using the full eight-hour CLI default.
WALL_TIME="${HARNESS_WALL_TIME:-20400}"

exec harness tsec \
  --agent /opt/red-harness/agents/hosted/pi-deepseek.yaml \
  --skills-root /opt/red-harness/skills \
  --runs-root /work/tsec-runs \
  --all \
  --allow-host-agent \
  --wall-time "$WALL_TIME"
