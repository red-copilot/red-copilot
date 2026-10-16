# Directory services and credentials

Working with identity infrastructure without turning a foothold into damage.

## Directory services

Questions worth answering in order:

1. Is anonymous or null authentication accepted, and what does it return?
2. Which objects are readable to the identity you hold (users, groups, trusts,
   password policy)?
3. Are there service accounts whose names imply privilege, and are they
   reachable from this network position?
4. What is the lockout policy, and how many authentication attempts does the
   budget actually allow?

Enumeration order matters: identify the boundary first, decide the target
second, and only then spend attempts on authentication.

## Credentials

| Where credentials appear | What to record |
| --- | --- |
| Config files, env vars, CI definitions | location and the identity they belong to |
| Responses, logs, error traces | location, never the value |
| Shares and repositories | path and the account that owns it |
| Tickets and session material | ticket type, validity window, where it can be used |

Never write the credential value itself into notes, artifacts or the final
report. Record the pointer; keep the secret out of every durable channel.

## Moving with an identity

- Prefer the cheapest proof that the identity works: a single authenticated
  request, one object read, one service ticket request.
- Respect lockout policy in your attempt budget — repeated failed binds are
  both noisy and self-defeating.
- Do not create accounts, change passwords, or persist access unless the
  objective explicitly authorizes it and the Harness scope allows it.

## Signals that a path is exhausted

- Anonymous enumeration returns the same shape as authenticated enumeration.
- The service answers identically for valid and invalid principals (no
  user-existence oracle).
- Lockout risk is reached before any credential signal appears — stop and
  record the limit instead of pushing through it.
