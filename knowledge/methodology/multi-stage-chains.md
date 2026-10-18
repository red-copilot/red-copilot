# Multi-stage chains

Some targets are not a single vulnerability. They are a chain: gain a foothold,
use what you found to reach the next system, and only then reach the objective.
A solver that treats such a target as one more web challenge burns the whole
budget probing the entry point over and over and never guesses the flag.

Observed failure mode worth avoiding: four full attempts on a relay challenge,
each submitting an answer the platform rejected, because the chain was never
completed before guessing.

## Recognise the shape

Treat a target as multi-stage when any of these is true:

- the objective mentions more than one flag, or a "secret" reachable only
  through an internal service;
- the entry point exposes a proxy, a job runner, an SSRF sink, a VPN/tunnel
  endpoint, or credentials for a second system;
- after the first foothold the target shows a private network segment.

## Work the chain in stages, and persist each stage

1. **Foothold.** Do not spend the budget trying to make the entry point give the
   final answer. Get any of: a shell, code execution, an SSRF primitive, valid
   credentials, a session token, or read access to a config file.
2. **Write the foothold down.** Before using it, append to your notes: how you
   got in, the exact request/payload, the credential or session value, and what
   host it is valid on. The next session — yours or your own retry after a
   timeout — starts from these notes, not from zero.
3. **Enumerate inward.** From the foothold, discover internal hosts and
   services (`ip addr`, `/etc/hosts`, cloud metadata, config files listing
   backends, `nmap -Pn` on the internal ranges the target reveals). Record every
   internal address with how you learned it.
4. **Pivot.** Reach the internal service with a forward (`ssh -L`,
   `socat`, `chisel`) or by replaying an internal URL through the SSRF. Keep the
   pivot command in your notes; a re-established tunnel is much cheaper than
   rediscovering the foothold.
5. **Escalate stage by stage.** Each stage's output becomes the next stage's
   input (credential, token, internal URL, file). Verify each stage's result
   before moving on; a failed assumption at stage 2 wastes all later work.
6. **Extract the objective last.** Only after the chain reaches the target
   service does the flag become available. Submit it then.

## Discipline that separates success from a wasted attempt

- **Never guess an objective answer from the entry point.** If the challenge
  says the secret is in an internal service, an answer derived from the entry
  point is a guess and will be rejected.
- **Keep a stage ledger** in notes: stage → state (done / blocked) → evidence →
  next action. When the session is cut, the ledger is the handoff.
- **On a retry, read `notes/prior-summary.md` first.** It holds the previous
  attempt's confirmed stages and ruled-out paths. Resume at the first blocked
  stage instead of re-running reconnaissance.
- **Time-box each stage.** If a stage has consumed a third of the budget with
  no new fact, change technique or record the stage as blocked and report what
  is missing. A documented blocked stage is worth more than a guessed flag.
