# VShell assets (not committed)

Place the vendor files here before building the hosted image:

- `v_linux_amd64` — the VShell server binary (linux/amd64 build)
- `vshell-mcp` — the vshell_mcp bridge binary (SSE, from zgsfsec/vshell_mcp)
- `conf/setting.conf`, `conf/server.pem`, `conf/server.key` — VShell config

These are intentionally gitignored: the server binary is ~500 MB and the
configuration carries a vendor license. The entrypoint randomizes the web
password at container start and binds the console to 127.0.0.1.
