# Cloud surfaces

Findings that live at the boundary between the application and the provider.
The common thread is that the *identity* the workload carries is more valuable
than any single endpoint.

## What to look for

- **Metadata endpoints**: a server-side request that reaches the instance
  metadata service can hand over temporary credentials. Treat any SSRF signal
  as a potential identity boundary crossing.
- **Workload identity**: environment variables, mounted token files, instance
  profiles, service-account bindings.
- **Storage boundaries**: public buckets, signed URL parameters, predictable
  object keys, permissive bucket policies.
- **Control-plane exposure**: management consoles or APIs reachable from the
  workload network, and what they accept.

## Signals

| Signal | Why it matters |
| --- | --- |
| Request to a link-local or internal address succeeds | metadata reachable |
| Credentials or tokens found in env, files or responses | identity to test |
| Object listing without authentication | storage boundary failure |
| Same endpoint differs by region/project/tenant identifier | boundary confusion |

## Boundaries

- Never use discovered cloud credentials outside the authorized engagement
  scope, even if they are technically valid.
- Do not enumerate other tenants, buckets or projects to "see what is there";
  one object that proves the boundary is enough.
- Record where a credential came from, never the credential itself.
