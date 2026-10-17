"""Fact extraction from well-known recon tool output."""
from __future__ import annotations

import hashlib

from harness.extract import MAX_EXTRACTED_FACTS, extract_facts


def _facts(output: str) -> list[dict]:
    return extract_facts({"output_excerpt": output})


def _by_type(facts: list[dict], type_name: str) -> list[dict]:
    return [fact for fact in facts if fact["type"] == type_name]


def test_curl_request_response_pair_becomes_one_endpoint() -> None:
    facts = _facts(
        "> GET /admin HTTP/1.1\n"
        "> Host: 10.0.0.5\n"
        "< HTTP/1.1 403 Forbidden\n"
        "< Content-Type: text/html\n"
    )

    endpoints = _by_type(facts, "web.endpoint")
    assert len(endpoints) == 1
    assert endpoints[0]["content"] == {"path": "/admin", "status": 403}


def test_bare_status_line_is_not_a_fact() -> None:
    """A status line without a matching request line stays out of the graph."""
    assert _by_type(_facts("< HTTP/1.1 403 Forbidden\n"), "web.endpoint") == []


def test_whatweb_fact_keeps_bounded_components() -> None:
    facts = _by_type(
        _facts("http://10.0.0.5 [200 OK] HTTPServer[Apache/2.4.41], Title[Login]\n"),
        "web.technology",
    )

    assert len(facts) == 1
    assert facts[0]["content"]["components"] == ["HTTPServer", "Title"]
    assert facts[0]["content"]["status"] == 200


def test_hydra_credentials_stay_out_of_world_content_in_plaintext() -> None:
    """A cracked password is a finding, not something run artifacts must carry."""
    facts = _by_type(
        _facts("[22][ssh] host: 10.0.0.5   login: root   password: toor\n"),
        "credential.access",
    )

    assert len(facts) == 1
    content = facts[0]["content"]
    assert content["service"] == "ssh"
    assert content["host"] == "10.0.0.5"
    assert content["login"] == "root"
    assert content["port"] == 22
    assert "password" not in content
    assert content["secret_sha256"] == hashlib.sha256(b"toor").hexdigest()[:16]


def test_same_credential_different_secret_keeps_the_same_fact_id() -> None:
    """The id names the access, so the secret cannot leak through it."""
    first = _by_type(
        _facts("[22][ssh] host: 10.0.0.5   login: root   password: toor\n"),
        "credential.access",
    )
    second = _by_type(
        _facts("[22][ssh] host: 10.0.0.5   login: root   password: hunter2\n"),
        "credential.access",
    )

    assert first[0]["id"] == second[0]["id"]
    assert first[0]["content"]["secret_sha256"] != second[0]["content"]["secret_sha256"]


def test_sqlmap_and_nikto_findings_are_bounded() -> None:
    facts = _facts(
        "GET parameter 'id' is 'AND boolean-based blind' injectable\n"
        "+ /admin/: Admin login page found.\n"
    )

    vulnerabilities = _by_type(facts, "web.vulnerability")
    assert vulnerabilities[0]["content"]["parameter"] == "id"
    assert len(vulnerabilities[0]["content"]["technique"]) <= 160
    assert _by_type(facts, "web.endpoint")[0]["content"]["path"] == "/admin/"


def test_extraction_funnel_bounds_the_number_of_facts() -> None:
    output = "".join(
        f"{port}/tcp   open  unknown\n" for port in range(1, MAX_EXTRACTED_FACTS * 3)
    )

    facts = _facts(output)

    assert len(facts) == MAX_EXTRACTED_FACTS
