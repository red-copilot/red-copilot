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
# curl -v: a "> VERB /path HTTP/x.y" request line, optional "> header" lines,
# then the "< HTTP/x.y <code>" response status line. The pairing is what keeps
# a bare status line (logs, curl -i headers, prose) from becoming a fact.
_CURL_PAIR = re.compile(
    r"^> (?:GET|POST|PUT|DELETE|HEAD|OPTIONS|PATCH|TRACE) (\S+) HTTP/[\d.]+"
    r"(?:\n>[^\n]*)*\n< HTTP/[\d.]+ (\d{3})",
    re.MULTILINE,
)
# whatweb: "http://host [200 OK] HTTPServer[Apache/2.4.41], Title[Login]"
_WHATWEB = re.compile(r"^(https?://\S+)\s+\[(\d{3})(?:[^\]]*)\]\s*(\S.*)$", re.MULTILINE)
_WHATWEB_COMPONENT = re.compile(r"([A-Za-z][A-Za-z0-9_-]*)\[")
# hydra: "[22][ssh] host: 10.0.0.5   login: root   password: toor"
_HYDRA = re.compile(
    r"^\[(\d{1,5})\]\[([a-z0-9_-]+)\] host:\s*(\S+)\s+login:\s*(\S+)\s+password:\s*(\S+)",
    re.MULTILINE,
)
# sqlmap: "GET parameter 'id' is 'AND boolean-based blind...' injectable"
_SQLMAP_TECHNIQUE = re.compile(r"\b(GET|POST) parameter '([^']+)' is '([^']*)' injectable")
_SQLMAP_VULNERABLE = re.compile(r"\b(GET|POST) parameter '([^']+)' is vulnerable")
# nikto: "+ /admin/: Admin login page found."
_NIKTO = re.compile(r"^\+ (/[^:\s]+):\s*(.+)$", re.MULTILINE)


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
        # The bounds live in the one funnel, so a new parser cannot forget them
        # and no captured token can carry unbounded tool output into the graph.
        detail = detail[:_MAX_DETAIL]
        if detail in seen or len(facts) >= MAX_EXTRACTED_FACTS:
            return
        seen.add(detail)
        facts.append(
            {
                "id": _fact_id("observation", type_name, detail),
                "type": type_name,
                "content": {
                    key: value[:_MAX_DETAIL] if isinstance(value, str) else value
                    for key, value in content.items()
                },
            }
        )

    for port, proto, service in _NMAP_OPEN.findall(excerpt):
        add(
            "network.service",
            f"{proto}/{port}",
            {"port": int(port), "proto": proto, "service": service or None},
        )
    for url, status in _GOBUSTER.findall(excerpt):
        add("web.endpoint", url, {"url": url, "status": int(status)})
    for path, status in _FFUF.findall(excerpt):
        if path.startswith(("http://", "https://")):
            continue  # already covered by the gobuster pattern
        add("web.endpoint", path, {"path": path, "status": int(status)})
    for path, status in _CURL_PAIR.findall(excerpt):
        add("web.endpoint", path, {"path": path, "status": int(status)})
    for url, status, rest in _WHATWEB.findall(excerpt):
        components = _WHATWEB_COMPONENT.findall(rest)[:8]
        add(
            "web.technology",
            url,
            {"url": url, "status": int(status), "components": components},
        )
    for port, service, host, login, password in _HYDRA.findall(excerpt):
        add(
            "credential.access",
            f"{service}:{host}:{login}",
            {
                "service": service,
                "host": host,
                "port": int(port),
                "login": login,
                "password": password,
            },
        )
    for method, param, technique in _SQLMAP_TECHNIQUE.findall(excerpt):
        add(
            "web.vulnerability",
            f"{method} {param}",
            {"method": method, "parameter": param, "technique": technique[:160]},
        )
    for method, param in _SQLMAP_VULNERABLE.findall(excerpt):
        add(
            "web.vulnerability",
            f"{method} {param}",
            {"method": method, "parameter": param, "technique": "vulnerable"},
        )
    for path, note in _NIKTO.findall(excerpt):
        add("web.endpoint", path, {"path": path, "note": note.strip()[:160]})
    return facts


__all__ = ["MAX_EXTRACTED_FACTS", "extract_facts"]
