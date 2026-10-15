"""Unit tests for deterministic tool-output fact extraction (PLN-06 formats)."""
from __future__ import annotations

from harness.extract import MAX_EXTRACTED_FACTS, extract_facts


def _by_type(excerpt: str, type_name: str) -> list[dict]:
    return [
        fact["content"]
        for fact in extract_facts({"output_excerpt": excerpt})
        if fact["type"] == type_name
    ]


class TestCurlVerbose:
    def test_request_response_pair_becomes_endpoint(self):
        excerpt = (
            "*   Trying 10.0.0.5:80...\n"
            "* Connected to 10.0.0.5\n"
            "> GET /admin HTTP/1.1\n"
            "> Host: 10.0.0.5\n"
            "> User-Agent: curl/8.5.0\n"
            ">\n"
            "< HTTP/1.1 200 OK\n"
            "< Server: Apache/2.4.41\n"
        )
        endpoints = _by_type(excerpt, "web.endpoint")
        assert {"path": "/admin", "status": 200} in endpoints

    def test_multiple_pairs_each_become_endpoints(self):
        excerpt = (
            "> HEAD /login HTTP/1.1\n"
            ">\n"
            "< HTTP/1.1 302 Found\n"
            "> GET /static/app.js HTTP/1.1\n"
            ">\n"
            "< HTTP/1.1 404 Not Found\n"
        )
        endpoints = _by_type(excerpt, "web.endpoint")
        assert {item["path"] for item in endpoints} == {"/login", "/static/app.js"}
        assert {item["status"] for item in endpoints} == {302, 404}

    def test_bare_status_line_is_not_an_endpoint(self):
        # curl -i headers, prose or logs mentioning a status code have no
        # request-line context, so they must not produce facts.
        excerpt = (
            "HTTP/1.1 200 OK\n"
            "Date: Mon, 12 Oct 2026\n"
            "\n"
            "The server returned HTTP/1.1 301 earlier.\n"
            "GET /maybe HTTP/1.1 was typed by hand.\n"
        )
        assert _by_type(excerpt, "web.endpoint") == []

    def test_request_without_response_status_is_not_an_endpoint(self):
        excerpt = "> GET /never-answered HTTP/1.1\n> Host: x\n\n< (no response)\n"
        assert _by_type(excerpt, "web.endpoint") == []


class TestWhatweb:
    def test_line_becomes_technology_observation(self):
        excerpt = (
            "http://10.0.0.5 [200 OK] Country[RESERVED][ZA], "
            "HTTPServer[Ubuntu Linux][Apache/2.4.41 (Ubuntu)], IP[10.0.0.5], "
            "Title[Login Page]\n"
        )
        technologies = _by_type(excerpt, "web.technology")
        assert len(technologies) == 1
        assert technologies[0]["url"] == "http://10.0.0.5"
        assert technologies[0]["status"] == 200
        assert "HTTPServer" in technologies[0]["components"]
        assert "Title" in technologies[0]["components"]

    def test_non_url_line_is_ignored(self):
        excerpt = "something [200 OK] Apache[2.4]\n"
        assert _by_type(excerpt, "web.technology") == []


class TestHydra:
    def test_credential_line_becomes_access_observation(self):
        excerpt = "[22][ssh] host: 10.0.0.5   login: root   password: toor\n"
        credentials = _by_type(excerpt, "credential.access")
        assert credentials == [
            {
                "service": "ssh",
                "host": "10.0.0.5",
                "port": 22,
                "login": "root",
                "password": "toor",
            }
        ]

    def test_progress_noise_is_not_a_credential(self):
        excerpt = "[ATTEMPT] target 10.0.0.5 - login \"root\" - pass \"toor\" - 1 of 3\n"
        assert _by_type(excerpt, "credential.access") == []


class TestSqlmap:
    def test_injectable_parameter_becomes_vulnerability(self):
        excerpt = (
            "[WARNING] GET parameter 'id' is 'AND boolean-based blind - WHERE or "
            "HAVING clause' injectable, with 134 calls\n"
        )
        vulnerabilities = _by_type(excerpt, "web.vulnerability")
        assert len(vulnerabilities) == 1
        assert vulnerabilities[0]["method"] == "GET"
        assert vulnerabilities[0]["parameter"] == "id"
        assert "boolean-based blind" in vulnerabilities[0]["technique"]

    def test_vulnerable_parameter_becomes_vulnerability(self):
        excerpt = "[CRITICAL] POST parameter 'user' is vulnerable. Do you want to continue? [y/N]\n"
        vulnerabilities = _by_type(excerpt, "web.vulnerability")
        assert vulnerabilities == [
            {"method": "POST", "parameter": "user", "technique": "vulnerable"}
        ]

    def test_prose_about_parameters_is_ignored(self):
        excerpt = "the parameter 'id' might be injectable according to the docs\n"
        assert _by_type(excerpt, "web.vulnerability") == []


class TestNikto:
    def test_finding_line_becomes_endpoint(self):
        excerpt = (
            "+ /admin/: Admin login page found.\n"
            "+ /config.php: Configuration file may contain database credentials.\n"
            "- The X-Frame-Options header is not set.\n"
        )
        endpoints = _by_type(excerpt, "web.endpoint")
        assert {item["path"] for item in endpoints} == {"/admin/", "/config.php"}
        assert any(item.get("note") == "Admin login page found." for item in endpoints)

    def test_plus_line_without_path_is_ignored(self):
        excerpt = "+ OSVDB-3092: /admin/: Admin login page found.\n+ some note: no path here\n"
        assert _by_type(excerpt, "web.endpoint") == []


class TestMixedAndBounded:
    def test_original_formats_still_parse_alongside_new_ones(self):
        excerpt = (
            "80/tcp   open  http    Apache httpd 2.4.41\n"
            "http://10.0.0.5/admin  (Status: 200) [Size: 1234]\n"
            "> GET /from-curl HTTP/1.1\n< HTTP/1.1 200 OK\n"
        )
        facts = extract_facts({"output_excerpt": excerpt})
        types = {fact["type"] for fact in facts}
        assert types == {"network.service", "web.endpoint"}

    def test_fact_count_is_capped(self):
        excerpt = "\n".join(
            f"[22][ssh] host: 10.0.0.{i % 256}   login: user{i}   password: pw{i}"
            for i in range(60)
        )
        assert len(extract_facts({"output_excerpt": excerpt})) == MAX_EXTRACTED_FACTS
