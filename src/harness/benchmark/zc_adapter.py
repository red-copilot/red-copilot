"""BenchmarkAdapter and Client for Tencent ZC crowdsourced testing platform."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..models import ObjectiveSpec
from .base import (
    BenchmarkAdapter,
    BenchmarkCase,
    BenchmarkSession,
    BenchmarkTarget,
    EvaluationResult,
    Submission,
    SubmissionResult,
)


@dataclass
class ZCProject:
    """Project / target scope on the ZC platform."""

    code: str
    name: str
    targets: list[str] = field(default_factory=list)
    vuln_types: list[str] = field(default_factory=list)
    status: str = "open"


@dataclass
class ZCVulnSubmission:
    """Result of a vulnerability submission to ZC."""

    vuln_code: str
    status: str
    message: str = ""


@dataclass
class ZCGatewayRoute:
    """Registered route on the ZC AI Gateway for LLM audit tracing."""

    route_id: str
    session_id: str
    gateway_url: str


class ZCClient:
    """Async client for zc.tencent.com REST API with offline/mock support."""

    BASE_URL = "https://zc.tencent.com/api/v1"

    def __init__(
        self,
        token: str = "mock-token",
        *,
        base_url: str | None = None,
        timeout: float = 30.0,
        mock_mode: bool | None = None,
    ) -> None:
        self._token = token
        self._base_url = (base_url or self.BASE_URL).rstrip("/")
        self._timeout = timeout
        self._mock_mode = (
            mock_mode if mock_mode is not None
            else (token.startswith(("mock-", "test-")) or not token)
        )
        self._client: httpx.AsyncClient | None = None
        self._mock_projects: dict[str, ZCProject] = {
            "proj-hackathon-t3": ZCProject(
                code="proj-hackathon-t3",
                name="Tencent Cloud Penetration Hackathon - Track 3",
                targets=["https://demo.target.zc.internal", "10.0.0.100"],
                vuln_types=["web.rce", "web.sqli", "web.ssrf", "web.logic"],
                status="open",
            )
        }
        self._mock_submissions: dict[str, dict[str, Any]] = {}

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=self._timeout,
            )
        return self._client

    async def search_projects(self) -> list[ZCProject]:
        """Search available open projects."""
        if self._mock_mode:
            return list(self._mock_projects.values())

        client = self._get_client()
        response = await client.post("/whitehat/project/search", json={})
        response.raise_for_status()
        data = response.json().get("data", [])
        return [
            ZCProject(
                code=item["project_code"],
                name=item["project_name"],
                targets=item.get("targets", []),
                vuln_types=item.get("vuln_types", []),
                status=item.get("status", "open"),
            )
            for item in data
        ]

    async def project_detail(self, code: str) -> dict[str, Any]:
        """Retrieve project details and scope."""
        if self._mock_mode:
            proj = self._mock_projects.get(code)
            if not proj:
                return {}
            return {
                "project_code": proj.code,
                "project_name": proj.name,
                "targets": proj.targets,
                "vuln_types": proj.vuln_types,
                "status": proj.status,
            }

        client = self._get_client()
        response = await client.post(f"/whitehat/project/detail/{code}", json={})
        response.raise_for_status()
        return response.json().get("data", {})

    async def apply_project(self, code: str) -> bool:
        """Apply for participation in an authorized project."""
        if self._mock_mode:
            return code in self._mock_projects

        client = self._get_client()
        response = await client.post("/whitehat/project/apply", json={"project_code": code})
        response.raise_for_status()
        return bool(response.json().get("success", True))

    async def create_gateway_route(
        self,
        upstream_url: str = "https://api.openai.com/v1",
    ) -> ZCGatewayRoute:
        """Register an AI Gateway route for LLM audit tracing."""
        session_id = f"zc-session-{uuid.uuid4().hex[:12]}"
        route_id = f"route-{uuid.uuid4().hex[:8]}"

        if self._mock_mode:
            return ZCGatewayRoute(
                route_id=route_id,
                session_id=session_id,
                gateway_url=f"https://zc.tencent.com/ai_gateway/{session_id}/v1",
            )

        client = self._get_client()
        response = await client.post(
            "/whitehat/ai_gateway/routes/create",
            json={"upstream_url": upstream_url, "session_id": session_id},
        )
        response.raise_for_status()
        payload = response.json().get("data", {})
        return ZCGatewayRoute(
            route_id=payload.get("route_id", route_id),
            session_id=payload.get("session_id", session_id),
            gateway_url=payload.get("gateway_url", f"{self._base_url}/ai_gateway/{session_id}/v1"),
        )

    async def submit_vuln(self, payload: dict[str, Any]) -> ZCVulnSubmission:
        """Submit a vulnerability report to ZC platform."""
        vuln_code = f"VULN-{uuid.uuid4().hex[:8].upper()}"

        if self._mock_mode:
            self._mock_submissions[vuln_code] = {
                "vuln_code": vuln_code,
                "status": "pending_triage",
                "payload": payload,
            }
            return ZCVulnSubmission(
                vuln_code=vuln_code,
                status="pending_triage",
                message="Report submitted successfully in mock mode",
            )

        client = self._get_client()
        response = await client.post("/whitehat/vuln/submit_vuln", json=payload)
        response.raise_for_status()
        data = response.json().get("data", {})
        return ZCVulnSubmission(
            vuln_code=data.get("vuln_code", vuln_code),
            status=data.get("status", "submitted"),
            message=data.get("message", "Vulnerability submitted"),
        )

    async def update_vuln(self, vuln_code: str, payload: dict[str, Any]) -> bool:
        """Supplement or update an existing vulnerability report."""
        if self._mock_mode:
            if vuln_code in self._mock_submissions:
                self._mock_submissions[vuln_code]["payload"].update(payload)
                return True
            return False

        client = self._get_client()
        response = await client.post(
            "/whitehat/vuln/update_vuln",
            json={"vuln_code": vuln_code, **payload},
        )
        response.raise_for_status()
        return bool(response.json().get("success", True))

    async def gateway_sessions(self, session_id: str) -> dict[str, Any]:
        """Fetch audit log metrics for the AI Gateway session."""
        if self._mock_mode:
            return {
                "session_id": session_id,
                "model_calls": 12,
                "prompt_tokens": 15000,
                "completion_tokens": 3000,
                "autonomy_score": 0.28,
            }

        client = self._get_client()
        response = await client.post(
            "/whitehat/ai_gateway/session_detail",
            json={"session_id": session_id},
        )
        response.raise_for_status()
        return response.json().get("data", {})

    async def close(self) -> None:
        """Close the underlying HTTP client session."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _build_objective(detail: dict[str, Any], route_url: str) -> str:
    """Build standardized agent objective text for ZC testing."""
    name = detail.get("project_name", "Tencent ZC Hackathon")
    targets = detail.get("targets", [])
    vuln_types = detail.get("vuln_types", [])
    lines = [
        f"You are participating in an authorized security project: {name}.",
        f"Authorized target scope: {', '.join(targets)}",
        f"In-scope vulnerability categories: {', '.join(vuln_types)}",
        "",
        "Rules of Engagement:",
        "- Restrict all scanning and testing strictly to authorized targets.",
        "- High-frequency scanning, brute-forcing, and DoS are strictly prohibited.",
        "- Perform minimal necessary verification only: demonstrate impact with harmless PoC.",
        "- When finding a vulnerability, report it by appending a JSON line to $HARNESS_SUBMISSION_INBOX",
        "  with type 'vuln_report' and metadata containing title, vuln_type, target, details, and poc.",
        f"- AI Gateway audit route is active at: {route_url}",
    ]
    return "\n".join(lines)


class ZCBenchmarkAdapter(BenchmarkAdapter):
    """Adapter for Tencent ZC crowdsourced testing hackathon (Track 3 & CTF)."""

    def __init__(
        self,
        client: ZCClient,
        *,
        project_code: str | None = None,
        upstream_llm_url: str = "https://api.openai.com/v1",
    ) -> None:
        self.client = client
        self.project_code = project_code
        self.upstream_llm_url = upstream_llm_url
        self._raw_cases: dict[str, BenchmarkCase] = {}
        self._sessions: dict[str, dict[str, Any]] = {}

    async def discover(self) -> list[BenchmarkCase]:
        """Discover available ZC projects / challenges."""
        projects = await self.client.search_projects()
        cases: list[BenchmarkCase] = []
        self._raw_cases.clear()
        for p in projects:
            case = BenchmarkCase(
                id=p.code,
                benchmark="zc",
                domain="web",
                difficulty="medium",
                metadata={
                    "name": p.name,
                    "targets": p.targets,
                    "vuln_types": p.vuln_types,
                    "status": p.status,
                },
            )
            self._raw_cases[p.code] = case
            cases.append(case)
        return cases

    async def provision(self, case: BenchmarkCase) -> BenchmarkSession:
        """Enroll into the target project and initialize AI Gateway route."""
        await self.client.apply_project(case.id)
        detail = await self.client.project_detail(case.id)
        route = await self.client.create_gateway_route(self.upstream_llm_url)

        target_addrs = detail.get("targets", case.metadata.get("targets", []))
        targets = [
            BenchmarkTarget(
                id=f"target-{i}",
                address=str(addr),
                metadata={"source": "zc.project_detail"},
            )
            for i, addr in enumerate(target_addrs, start=1)
        ]

        objective_text = _build_objective(detail, route.gateway_url)

        session = BenchmarkSession(
            id=f"zc-session-{uuid.uuid4().hex}",
            benchmark="zc",
            case_id=case.id,
            objective=ObjectiveSpec(description=objective_text),
            targets=targets,
            metadata={
                **case.metadata,
                "project_code": case.id,
                "gateway_route_id": route.route_id,
                "gateway_session_id": route.session_id,
                "gateway_url": route.gateway_url,
            },
        )
        self._sessions[session.id] = {
            "case": case,
            "submissions": [],
            "closed": False,
            "session_id": route.session_id,
        }
        return session

    async def submit(
        self,
        session: BenchmarkSession,
        submission: Submission,
    ) -> SubmissionResult:
        """Submit a candidate flag or a vulnerability report to ZC."""
        state = self._sessions.get(session.id)
        if state is None:
            raise KeyError(f"unknown session: {session.id}")

        if submission.type == "vuln_report":
            payload = {
                "project_code": session.case_id,
                "title": submission.metadata.get("title", f"Vulnerability on {session.case_id}"),
                "vuln_type": submission.metadata.get("vuln_type", "web.injection"),
                "target": submission.value,
                "details": submission.metadata.get("details", ""),
                "poc": submission.metadata.get("poc", ""),
            }
            res = await self.client.submit_vuln(payload)
            result = SubmissionResult(
                accepted=True,
                score_delta=100.0,
                completed=False,
                metadata={
                    "vuln_code": res.vuln_code,
                    "status": res.status,
                    "message": res.message,
                },
            )
            state["submissions"].append(result)
            return result

        if submission.type == "flag":
            accepted = submission.value.startswith("flag{") or "zc{" in submission.value
            result = SubmissionResult(
                accepted=accepted,
                score_delta=50.0 if accepted else 0.0,
                completed=accepted,
                metadata={"flag": submission.value, "correct": accepted},
            )
            state["submissions"].append(result)
            return result

        return SubmissionResult(
            accepted=False,
            metadata={"reason": f"unsupported submission type: {submission.type}"},
        )

    async def evaluate(self, session: BenchmarkSession) -> EvaluationResult:
        """Evaluate session results, aggregating scores and milestones."""
        state = self._sessions.get(session.id)
        if state is None:
            return EvaluationResult(success=False, score=0.0, message="Session not found")

        submissions: list[SubmissionResult] = state["submissions"]
        accepted_subs = [s for s in submissions if s.accepted]
        total_score = sum(s.score_delta for s in accepted_subs)

        gateway_data = await self.client.gateway_sessions(state["session_id"])
        autonomy_score = gateway_data.get("autonomy_score", 0.0)
        final_score = total_score * (1.0 + autonomy_score)

        return EvaluationResult(
            success=len(accepted_subs) > 0,
            score=final_score,
            message=f"Accepted {len(accepted_subs)} submissions with autonomy bonus {autonomy_score:.1%}",
            milestones={
                "has_accepted_submission": len(accepted_subs) > 0,
                "gateway_verified": gateway_data.get("model_calls", 0) > 0,
            },
            metadata={
                "accepted_count": len(accepted_subs),
                "total_submissions": len(submissions),
                "base_score": total_score,
                "autonomy_bonus": autonomy_score,
                "final_score": final_score,
            },
        )

    async def teardown(self, session: BenchmarkSession) -> None:
        """Tear down and release benchmark resources."""
        state = self._sessions.get(session.id)
        if state is None or state["closed"]:
            return
        state["closed"] = True
        await self.client.close()

