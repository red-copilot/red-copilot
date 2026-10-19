#!/usr/bin/env bash
# Start the VShell console and the vshell-mcp SSE bridge on loopback.
# Best effort: a failure here never blocks the evaluation run.
set -u

VSHELL_DIR=/opt/vshell
CONF="$VSHELL_DIR/conf/setting.conf"
PASS="$(head -c 48 /dev/urandom | tr -dc 'A-Za-z0-9' | head -c 20)"

if [ ! -x "$VSHELL_DIR/v_linux_amd64" ] || [ ! -x "$VSHELL_DIR/vshell-mcp" ]; then
  echo "[vshell] binaries missing; MCP tools disabled"
  exit 0
fi
if [ ! -f "$CONF" ]; then
  echo "[vshell] conf missing; MCP tools disabled"
  exit 0
fi

# Loopback only + a per-run password; the console must never be reachable
# from the benchmark network.
sed -i \
  -e 's/^web_ip=.*/web_ip=127.0.0.1/' \
  -e 's/^web_port=.*/web_port=8082/' \
  -e 's/^web_basic_auth=.*/web_basic_auth=false/' \
  -e "s/^web_password=.*/web_password=$PASS/" \
  "$CONF"

(cd "$VSHELL_DIR" && ./v_linux_amd64 > /work/vshell-server.log 2>&1 &)

i=0
while [ "$i" -lt 60 ]; do
  if (echo > /dev/tcp/127.0.0.1/8082) 2>/dev/null; then break; fi
  i=$((i + 1)); sleep 0.5
done
if ! (echo > /dev/tcp/127.0.0.1/8082) 2>/dev/null; then
  echo "[vshell] console did not start; MCP tools disabled (see /work/vshell-server.log)"
  exit 0
fi

"$VSHELL_DIR/vshell-mcp" -h http://127.0.0.1:8082 -u admin -p "$PASS" -port 19080 \
  > /work/vshell-mcp.log 2>&1 &

i=0
while [ "$i" -lt 60 ]; do
  if (echo > /dev/tcp/127.0.0.1/19080) 2>/dev/null; then
    echo "[vshell] MCP bridge ready on 127.0.0.1:19080 (27 tools)"
    exit 0
  fi
  i=$((i + 1)); sleep 0.5
done
echo "[vshell] MCP bridge did not start (see /work/vshell-mcp.log)"
exit 0
