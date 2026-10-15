# Skill catalogue

Skills are plain markdown. Each `<skill-id>/SKILL.md` describes *when* a
technique applies, *what* it should establish, and the operating rules that
bound it. The Agent reads them on demand with its own file tools
(`ls` / `cat`), exactly like any other knowledge directory.

There is no executable semantic here and nothing parses these files: the
Harness no longer ranks skills, computes a frontier, or injects a plan. A
skill is a note to the model, not a contract the runtime enforces. Anything
that must be enforced (scope, budget, destructive actions, verification
authority) lives in the Harness, not in the catalogue.

Point an agent at a catalogue with the `knowledge` field in its YAML, for
example:

```yaml
knowledge:
  - skills/offensive
  - knowledge
```

Each root is mounted read-only into the Agent container under `/knowledge/<name>`
and exported as `HARNESS_KNOWLEDGE_DIRS`, so the catalogue is visible but not
writable.

`skills/offensive/` is the live offensive catalogue used by
`agents/examples/zc-offensive.yaml`; `skills/examples/` holds the cross-domain
examples used by the offline fixtures.
