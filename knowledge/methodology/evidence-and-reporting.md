# Evidence and reporting

What counts as evidence, how to record it, and what the final report must
contain. The Harness grades nothing here; the platform does. This page is about
producing something a reviewer can re-run.

## Evidence tiers

| Tier | Example | Usable as |
| --- | --- | --- |
| Claim | "the parameter looks injectable" | hypothesis only |
| Execution | command exited 0, response returned | telemetry |
| Observation | a reproducible difference caused by your input | evidence |
| Verified | an authorized evaluator accepted the result | conclusion |

A tool exit code is never evidence. A response body is evidence only when you
can name the difference it shows and what produced it.

## Minimum viable evidence

For each confirmed item, record:

- **Target**: host, port, method, path (exactly as used).
- **Identity**: which account/session/token was in play.
- **Preconditions**: anything that must exist first (an uploaded file, a created
  object, a claimed intent).
- **Request**: the smallest request that reproduces it.
- **Observation**: the difference, quoted narrowly — a header value, a status
  code, a length change, a timed delay, a callback record.
- **Negative control**: the same request with the trigger removed.

If you cannot write the negative control, you probably have a coincidence.

## Recording during the run

- Append observations to the agent event file as `world.observe`, with the
  trust level you actually have.
- Keep the reproducible request in an artifact file rather than pasting it into
  notes; reference the artifact by path.
- Write excluded paths as facts too: "X returned the same response with and
  without Y" is a durable, reusable result.
- Never place credentials, session cookies or personal data in notes,
  artifacts or reports. Reference where they live instead.

## Report shape

1. **Title**: vulnerability class + location.
2. **Impact**: what an attacker gains, in one or two sentences.
3. **Reproduction**: numbered steps from a clean state, with the minimal request.
4. **Evidence**: the observation and the negative control.
5. **Scope note**: which authorized target this applies to.
6. **Fix direction**: the control that would have prevented it.

Keep it short enough to be read at a glance. A reviewer should be able to
reproduce the finding without asking you a question.

## Failure modes worth naming

- **Narrative instead of evidence**: chronological logs with no single minimal
  request.
- **Over-collection**: ten pages of response body where one header would do.
- **Unbounded impact claims**: "this could lead to RCE" with no chain shown.
- **Silent retries**: re-running a destructive test without recording that the
  first attempt already changed the target's state.
