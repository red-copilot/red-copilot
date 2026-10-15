# Knowledge staging

Drop read-only reference material here before a run: payload catalogues
(PayloadsAllTheThings, OWASP cheat sheets), tool documentation, writeups, or
engagement-specific notes. The Agent reads these files on demand with its own
tools; the Harness never parses, indexes or plans over them.

Rules:

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

Keep credentials, live target data and anything you would not hand an
untrusted container out of this tree.
