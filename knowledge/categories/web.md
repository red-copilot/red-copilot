# Web surfaces

Where web findings come from, and which families to consider first. Payload
text lives in the fetched corpora (see the knowledge index); this page is the
map, not the ammunition.

## Input surfaces

Rank them by how likely they are to reach server-side processing:

1. **Parameters with visible effect** — anything that changes a response,
   a record, or a job.
2. **Authentication and session material** — cookies, tokens, reset flows,
   invite and recovery endpoints.
3. **Structured bodies** — JSON fields, nested objects, arrays, type-confused
   values (`"1"` vs `1`, missing vs null).
4. **Headers the app trusts** — `X-Forwarded-*`, `X-Original-URL`,
   `X-HTTP-Method-Override`, tenant or user identifiers.
5. **Path segments and routing** — encoded separators, trailing dots and
   slashes, case variants, double encoding.

## Family map

| Family | What the signal looks like |
| --- | --- |
| SQL/NoSQL injection | boolean or timing difference under a controlled input change |
| Command injection | output of a second command, delay, or out-of-band callback |
| Template injection | evaluated expression reflected in the output |
| Path traversal / file read | content of a file you did not upload |
| File upload | upload accepted and later reachable or executed |
| SSRF | server-side request observed at a listener you control |
| Deserialization | object behaviour change tied to a crafted body |
| Access control | another identity's object read or modified |
| Client-side injection | payload reflected into an executing context |

## Tells that you are close

- The same endpoint answers differently for two identities.
- An error message names a component, query language or framework.
- A parameter is accepted in one location but ignored in another.
- A response length changes by exactly the length of your input.

## Boundaries

- Read one object to prove access control; do not enumerate.
- Prefer callbacks you control inside the authorized range.
- Stop at the first sign of destructive side effects, and record the state
  change you caused.
