# Knowledge index

Read-only reference material for the Agent. Everything here is plain markdown the
Agent reads with its own tools (`ls` / `cat`); the Harness never parses, indexes
or plans over it. There is no vector store and no retrieval service.

Start with the methodology pages, then the category you are working in. The
category pages list which families to consider and where the bulk corpora live.

## Methodology

- [recon-to-report.md](methodology/recon-to-report.md) — the working loop:
  asset, hypothesis, verification, submission, and when to abandon a path.
- [evidence-and-reporting.md](methodology/evidence-and-reporting.md) — what
  counts as evidence, how to record it, and what a minimal report contains.
- [discovery-coverage.md](methodology/discovery-coverage.md) — why a miss is
  not a negative, flag-path priors, and the tool matrix for expanding coverage.
- [exploitation-recipes.md](methodology/exploitation-recipes.md) — concrete
  commands for tunnels, AD, known CVE, Java, reversing and cloud work.
- [vshell-workflow.md](methodology/vshell-workflow.md) — VShell listener,
  client, host, tunnel and command flow, including the deferred MCP exposure.
- [multi-stage-chains.md](methodology/multi-stage-chains.md) — foothold,
  persistence, lateral movement and pivot for relay-style targets; how to
  resume a blocked chain from notes instead of re-running recon.

## Categories

- [web.md](categories/web.md) — HTTP surfaces, injection families, client-side.
- [network.md](categories/network.md) — services, protocols, and their tells.
- [cloud.md](categories/cloud.md) — metadata endpoints, identity boundaries.
- [ad-and-credentials.md](categories/ad-and-credentials.md) — directory services,
  credential handling and lateral movement constraints.

## Bulk corpora (not stored in this repository)

Large upstream catalogues are fetched into `.harness/knowledge-cache/` before a
run and mounted alongside this directory with `--knowledge-root`. They are not
committed, and nothing in the runtime downloads them:

- `PayloadsAllTheThings` — payload families and bypass lists.
- `HowToHunt` — methodology notes per vulnerability class.
- `nuclei-templates` — detection templates and their matchers.
- `CheatSheetSeries` — OWASP cheat sheets per vulnerability class (CC-BY-SA-4.0).
- `SecLists` — wordlists for brute force, discovery and fuzzing (MIT).

See `scripts/fetch-knowledge.sh` for the pinned revisions.

## Staging rules

- Staged locally before the run. The runtime never downloads knowledge, and
  no vector store or retrieval index is involved.
- Mounted read-only (`/knowledge/<name>`), so an Agent cannot modify the
  catalogue it attacks with.
- Declared per agent, not globally:

```yaml
knowledge:
  - skills/offensive
  - knowledge
```

- Only point `--knowledge-root` at data you would hand an untrusted container.
  Keep credentials, live target data and personal data out of this tree.
