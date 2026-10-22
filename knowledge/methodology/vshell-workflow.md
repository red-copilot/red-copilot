# VShell workflow (multi-stage control)

The runner ships VShell (a C2 console) plus its MCP bridge on loopback. The
tools are exposed with `deferred` exposure, so their schemas cost nothing until
you need them:

1. Call `tool_search` with a query such as `vshell listener tunnel execute`
   to load the `mcp__vshell__*` tools.
2. `add_listener` — mode `tcp`, `listen_addr` **must be `0.0.0.0:<port>` or the
   container IP from `hostname -I`, never `127.0.0.1`** (the target has to
   connect back). Note the `vkey` and `encrypt_salt` you chose.
3. `generate_client` — `kind=stageless` (needs `listener_id`) or `kind=listen`
   with your own host/port; `arch=linux_amd64` / `windows_amd64.exe`;
   `save_path` inside the working directory.
4. Deliver the client through the foothold you already have (webshell, RCE,
   file write, `wget` from a port you serve) and execute it on the target.
5. `list_hosts` → `host_id`; then `execute_command` (real non-interactive
   shell: avoid `vim`/`top`/`passwd`), `list_directory`, `read_file`,
   `upload_file`/`download_file`.
6. `add_tunnel` — `mode=socks5` bound to that `client_id` and a local port;
   point tools that cannot speak SOCKS at it through
   `proxychains4 -f /opt/red-harness/proxychains.conf <tool>`.

Guardrails: only this case's authorized targets; never the platform, other
containers or non-scope addresses. If a step does not answer within 30-60s,
fall back to plain bash + curl/ssh/chisel instead of waiting.
