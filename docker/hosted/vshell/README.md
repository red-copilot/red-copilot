# VShell assets (not committed)

Place the vendor files here before building the hosted image:

- `v_linux_amd64` — the VShell server binary (linux/amd64 build)
- `vshell-mcp` — the vshell_mcp bridge binary (SSE, from zgsfsec/vshell_mcp)
- `conf/setting.conf`, `conf/server.pem`, `conf/server.key` — VShell config

These are intentionally gitignored: the server binary is ~500 MB and the
configuration carries a vendor license. The entrypoint randomizes the web
password at container start and binds the console to 127.0.0.1.

## Local patch: vshell-mcp 1.0.1 (terminal exec)

`vshell-mcp.patch` (see `vshell-mcp.patch` in this directory) fixes
`execute_command` against shells that never print a prompt:

- the initial prompt wait is removed (a read timeout corrupts the
  gorilla/websocket connection), the init command is sent immediately;
- begin/end markers are emitted with `printf` so terminal echo cannot
  contain the full marker and fool the extractor.

Build: `go build -o vshell-mcp .` in a checkout of
https://github.com/zgsfsec/vshell_mcp with the patch applied.
