# Discovery coverage

The most expensive mistake in an authorized engagement is not a failed attempt.
It is turning "my wordlist missed it" into "this path is dead" and never coming
back. Two recorded failures from real benchmarks show why.

- A chain reached SSRF plus a token but stalled at `/admin/info`: the path was
  simply absent from a hand-rolled wordlist. Every agent on that target declared
  the endpoint space exhausted.
- A local file inclusion primitive "could not read the flag": the correct path
  was `/challenge/flag.txt`, which no hand-written candidate list covered.

Coverage is a property of your wordlists and tools, not of the target.

## Rules

- **401 and 403 mean the door exists.** They are not a dead end: retry with a
  different method, altered headers, path variants (case, `.json`, trailing
  slash, `;`, encoded separators), or with credentials you already hold.
- **A miss is not a negative.** Before concluding "not there", check whether the
  dictionary contained the word, whether recursion was enabled, and whether the
  endpoint filters on a parameter you did not send.
- **Parameters are a wordlist too.** `arjun` on every endpoint that takes input,
  and mine parameter names from JavaScript bundles, source maps, error pages and
  redirect locations.
- **Flag location priors first.** Sweep the usual flag paths (`/challenge/flag.txt`,
  `/flag.txt`, `/admin/info`, `/internal/info`, ...) after any file-read or SSRF
  primitive before reaching for a large dictionary.
- **Structured tool output beats memory.** `ffuf -of json -o out.json`,
  `feroxbuster --json`, and scripts that print status, length and elapsed time
  per candidate make the one anomalous response visible.

## Tool matrix

| Task | Tool | Wordlist / flags |
|---|---|---|
| Directory and route discovery | `ffuf`, `feroxbuster`, `gobuster` | `/usr/share/seclists/Discovery/Web-Content/`, `dirb/common.txt`, `dirb/big.txt` |
| Recursive content discovery | `feroxbuster -u URL -w LIST --depth 3` | SecLists `raft-*` |
| Parameter discovery | `arjun -u URL -m GET,POST` | — |
| Flag locations | `curl`/`ffuf` over a short built-in path list | challenge paths first |
| Credentials | `hydra`, `john`, `hashcat` | `/usr/share/wordlists/rockyou.txt` |
| AD / SMB | `netexec`, `impacket-*`, `certipy-ad`, `bloodhound.py`, `smbclient`, `rpcclient` | — |
| Kerberos users | `impacket-GetNPUsers`, `kerbrute` (if present) | — |
| Binary hardening / RE | `checksec`, `r2 -A`, `ROPgadget`, `patchelf`, `objdump` | — |
| Cloud / K8s | `awscli`, `kubectl` | credentials from the target |
| Through a tunnel | `ssh -D 1080 -N user@host`, or `socat TCP-LISTEN:PORT,fork TCP:HOST:PORT` | use only for the authorized target |
