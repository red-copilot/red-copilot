"""Deterministic fact extraction from tool output.

The fact/intent graph only grows when something records new knowledge. Waiting
for the Agent to call ``world.observe`` left the graph nearly static, so the
Harness derives the high-signal lines of well-known recon tools itself.

Conservative by design: only distinctive, regular formats are matched, output
is already bounded and sanitized by the adapter, and every derived fact is
recorded as Harness-observed evidence with a content-addressed id so repeats do
not duplicate.
"""
from __future__ import annotations

import hashlib
import re

MAX_EXTRACTED_FACTS = 25
_MAX_DETAIL = 200

# nmap: "80/tcp   open  http    Apache httpd 2.4.41"
_NMAP_OPEN = re.compile(r"^(\d{1,5})/(tcp|udp)\s+open\s*(\S*)", re.MULTILINE)
# gobuster/dirb: "http://host/admin   (Status: 200) [Size: 1234]"
_GOBUSTER = re.compile(r"^(https?://\S+?)\s+\(Status:\s*(\d{3})\)", re.MULTILINE)
# ffuf text output: "admin   [Status: 200, Size: 1234, Words: 5, ...]"
_FFUF = re.compile(r"^(\S+)\s+\[Status:\s*(\d{3})[,\]]", re.MULTILINE)


def _fact_id(kind: str, type_name: str, detail: str) -> str:
    digest = hashlib.sha256(f"{kind}|{type_name}|{detail}".encode()).hexdigest()
    return f"extract:{digest[:16]}"


def extract_facts(data: dict) -> list[dict]:
    """Derive World observation payloads from one normalized tool result."""
    excerpt = data.get("output_excerpt")
    if not isinstance(excerpt, str) or not excerpt:
        return []

    facts: list[dict] = []
    seen: set[str] = set()

    def add(type_name: str, detail: str, content: dict) -> None:
        if detail in seen or len(facts) >= MAX_EXTRACTED_FACTS:
            return
        seen.add(detail)
        facts.append(
            {
                "id": _fact_id("observation", type_name, detail),
                "type": type_name,
                "content": content,
            }
        )

    for port, proto, service in _NMAP_OPEN.findall(excerpt):
        detail = f"{proto}/{port}"
        add(
            "network.service",
            detail,
            {"port": int(port), "proto": proto, "service": service or None},
        )
    for url, status in _GOBUSTER.findall(excerpt):
        detail = url[:_MAX_DETAIL]
        add("web.endpoint", detail, {"url": detail, "status": int(status)})
    for path, status in _FFUF.findall(excerpt):
        if path.startswith(("http://", "https://")):
            continue  # already covered by the gobuster pattern
        detail = path[:_MAX_DETAIL]
        add("web.endpoint", detail, {"path": detail, "status": int(status)})
    return facts


__all__ = ["MAX_EXTRACTED_FACTS", "extract_facts"]
