#!/usr/bin/env bash
# Competition runbook:第三届智能攻防挑战赛(托管模式-Agent 评测).
#
# Usage:
#   BENCHMARK_BASE_URL=... BENCHMARK_TOKEN=... OPENAI_API_KEY=... \
#     ./scripts/tsec-competition.sh [extra harness tsec options]
#
# The evaluation platform bounds one task at eight hours and gives a team a
# single evaluation chance. This script refuses to start when the usual
# prerequisites are missing so the one shot is not wasted on a setup error.
set -euo pipefail

cd "$(dirname "$0")/.."

fail() { echo "PREFLIGHT FAIL: $*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || fail "docker not found on PATH"
[ -n "${BENCHMARK_BASE_URL:-}" ] || fail "BENCHMARK_BASE_URL is not set"
[ -n "${BENCHMARK_TOKEN:-}" ] || fail "BENCHMARK_TOKEN is not set"
[ -n "${OPENAI_API_KEY:-}" ] || fail "OPENAI_API_KEY is not set (direct-provider mode)"

if ! docker image inspect harness/pi-kali:local >/dev/null 2>&1; then
  fail "image harness/pi-kali:local is missing; build it first:
  docker build -t harness/pi-kali:local docker/pi-kali"
fi

echo "preflight ok: docker, image, and credentials present"
echo "starting full run (--all, wall-time = 8h platform window)"

exec "$(command -v python)" -m harness.cli tsec \
  --agent agents/examples/pi-tsec.yaml \
  --all \
  --wall-time 28800 \
  "$@"
