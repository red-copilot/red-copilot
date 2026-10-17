# Network services

Service-level work: getting from "a port is open" to "this service has an
attack surface worth testing".

## Service profile

For each open port, record enough that a later session does not have to
re-probe it:

- protocol and handshake result (not just "open");
- banner or version, and how you obtained it (banner, probe, TLS certificate);
- authentication requirement and what happens without credentials;
- whether a proxy, load balancer or WAF sits in front;
- the time window you observed it in.

## High-value families

| Service class | First questions |
| --- | --- |
| Web front ends | paths, parameters, identity handling (see the web category) |
| Databases | reachable at all? authentication surface? protocol version? |
| Directory services | anonymous bind? enumeration surface? (see the AD category) |
| File and message services | anonymous access? default shares? |
| Management planes | is it exposed to the target network only, or beyond? |
| Remote access | authentication policy, session reuse, lockout behaviour |

## Version claims

A version string is a lead, not a finding. Before treating a known issue as
reachable, confirm:

1. the version claim matches the actual service behaviour;
2. the vulnerable feature is enabled and reachable;
3. the exploitation path stays within the authorized scope.

## Boundaries

- Rate-limit scans; never run full-port, full-speed sweeps against an
  unknown-scale target.
- Treat anything that could interrupt availability (large payloads, resource
  exhaustion shapes, protocol fuzzing) as out of bounds by default.
- Record closed and filtered ranges too: "scanned, nothing there" prevents the
  next session from repeating the work.
