# From recon to report

The working loop for an authorized engagement. Read this before starting a new
target; it is the shortest description of how the pieces fit together.

## 1. Scope first

Everything you do must be traceable to an authorized target. Before the first
probe, know:

- which hosts, CIDRs or URLs are in scope, and which are explicitly out;
- which identity you hold (anonymous, low-privilege account, token);
- what the objective actually rewards (a flag, a confirmed finding, a report).

If an action cannot be tied back to that list, it does not belong in the run.

## 2. Asset before vulnerability

Turn names into assets with an ownership chain: name → resolution → service →
owner. Only then does the asset become a subject for hypotheses.

Signals that you are still in this phase, not the next one:

- you cannot say which port carries which service;
- you have endpoints but no idea which ones write state;
- the same host appears under three spellings and you treat them as three hosts.

## 3. Hypothesis with a falsifier

A hypothesis is only useful if it can be wrong. Write it as:

```text
because <observation>, I expect <input> to cause <observable difference>;
if instead <other outcome>, the hypothesis is refuted.
```

Two habits keep this honest:

- one variable per test — you should be able to name what changed;
- one falsifier per hypothesis — a path with no way to fail will be walked
  forever.

## 4. Verification, not activity

Three statements that must never be collapsed:

1. the request succeeded;
2. the target showed the expected difference;
3. the platform accepted the result.

Only the third is a conclusion. The first is telemetry, the second is evidence.

Minimise before you generalise: one request, one parameter, one observable
difference. A control request that does *not* trigger the difference is usually
the strongest part of the evidence.

## 5. Submission

- Submit the value you observed, not a value you derived, unless the objective
  says otherwise.
- Submit once. A rejected candidate is information about your hypothesis, not
  an invitation to resend.
- Submit as soon as you have a candidate, not when you stop working. A finding
  that never reaches the submission channel scores zero, and the time left after
  an accepted flag is worth more than more confirmation of the one you hold.
- Record the outcome (accepted, rejected, score delta) back into world state so
  later decisions can see it.

## 6. When to abandon a path

Abandon — and write down why — when any of these holds:

- the same command family has produced no new fact for several attempts;
- the hypothesis has been refuted, or its falsifier already appeared;
- the input you were attacking is no longer reachable;
- the remaining cost (time, budget, hint penalty) exceeds the score at stake;
- one call has already run for minutes: a single scan that holds the window for
  half an hour is a decision to spend the run on one command, so bound it,
  narrow it, or replace it with a cheaper probe;
- the notes have not grown while the action count has: reading and re-reading the
  same context is not progress, and an empty page at the end is the shape of a
  run that was never recorded.

Recording *what was ruled out* is worth as much as recording a finding: it is
the only thing that stops the next session from re-walking the same path.

## 7. Handing over

Before stopping, leave the next reader — or the next session — three things:

1. confirmed facts with their evidence;
2. hypotheses still open, and what would confirm or refute each;
3. paths already excluded, and the observation that excluded them.

Prose without those three is a transcript, not a hand-off.
