"""
Qi Client — Registry API.

HTTP client for the qi-registry service:
  - POST /api/v1/agents        →  register Agent Card
  - GET  /api/v1/agents        →  search agents by capability/name
  - GET  /api/v1/agents/{did}  →  get agent details
  - GET  /api/v1/stats         →  registry statistics
"""

from __future__ import annotations

import json
import urllib.request
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AgentSummary:
    """Brief agent info from search results."""
    node_id: str
    name: str
    master_name: str = ""
    description: str = ""
    skills: list[str] = field(default_factory=list)
    verified: bool = False
    trust_score: float = 0.0
    interactions_count: int = 0
    registered_at: str = ""
    last_seen_at: str = ""


@dataclass
class SearchResult:
    """Results from a registry search."""
    agents: list[AgentSummary]
    total: int
    page: int
    page_size: int
    has_more: bool


@dataclass
class AgentDetail:
    """Full agent info from a direct lookup."""
    node_id: str
    name: str
    master_name: str = ""
    description: str = ""
    skills: list[str] = field(default_factory=list)
    verified: bool = False
    trust_score: float = 0.0
    interactions_count: int = 0
    registered_at: str = ""
    last_seen_at: str = ""
    card_json: str = ""


class RegistryClient:
    """HTTP client for qi-registry."""

    def __init__(self, base_url: str = "http://qi-network.net:8731", timeout: int = 10):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        """Send an HTTP request and return parsed JSON."""
        data = json.dumps(body).encode("utf-8") if body else None
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"} if data else {},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                # 204 No Content → return empty dict
                if resp.status == 204:
                    return {}
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")
            if e.code == 404:
                raise LookupError(f"Not found: {path} — {error_body}")
            if e.code == 409:
                raise ValueError(f"Already exists: {path} — {error_body}")
            raise ConnectionError(f"Registry {method} {path} failed: HTTP {e.code} — {error_body}")
        except urllib.error.URLError as e:
            raise ConnectionError(f"Registry unreachable: {e.reason}")

    def register(self, card_json: str, verify_signature: bool = True) -> dict:
        """Register an Agent Card with the registry.

        Args:
            card_json:       Signed Agent Card JSON string
            verify_signature: Whether the registry should verify the Master signature

        Returns:
            {"status": "registered", "node_id": "...", "verified": bool}

        Raises:
            ValueError: Agent already registered (409)
            ConnectionError: Registry unreachable
        """
        return self._request("POST", "/api/v1/agents", {
            "card_json": card_json,
            "verify_signature": verify_signature,
        })

    def update(self, node_id: str, card_json: str) -> dict:
        """Update an existing Agent Card.

        Args:
            node_id:   The agent's DID:key
            card_json: Updated signed Agent Card JSON

        Returns:
            {"status": "updated", "node_id": "...", "verified": bool}
        """
        return self._request("PUT", f"/api/v1/agents/{node_id}", {
            "card_json": card_json,
        })

    def search(
        self,
        query: str = "",
        skill: str = "",
        verified: bool | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> SearchResult:
        """Search the registry for agents.

        Args:
            query:    Natural language search (searches name + description + skills)
            skill:    Filter by skill name
            verified: Filter by verification status (None = all)
            page:     Page number (1-indexed)
            page_size: Results per page (max 100)

        Returns:
            SearchResult with matching agents.
        """
        params = []
        if query:
            params.append(f"q={urllib.parse.quote(query)}")
        if skill:
            params.append(f"skill={urllib.parse.quote(skill)}")
        if verified is not None:
            params.append(f"verified={'true' if verified else 'false'}")
        params.append(f"page={page}")
        params.append(f"page_size={page_size}")

        path = f"/api/v1/agents?{'&'.join(params)}"
        resp = self._request("GET", path)
        agents = [
            AgentSummary(
                node_id=a["node_id"],
                name=a["name"],
                master_name=a.get("master_name", ""),
                description=a.get("description", ""),
                skills=a.get("skills", []),
                verified=a.get("verified", False),
                trust_score=a.get("trust_score", 0.0),
                interactions_count=a.get("interactions_count", 0),
                registered_at=a.get("registered_at", ""),
                last_seen_at=a.get("last_seen_at", ""),
            )
            for a in resp.get("agents", [])
        ]
        return SearchResult(
            agents=agents,
            total=resp.get("total", 0),
            page=resp.get("page", page),
            page_size=resp.get("page_size", page_size),
            has_more=resp.get("has_more", False),
        )

    def get_agent(self, node_id: str) -> AgentDetail:
        """Get full details for a specific agent.

        Args:
            node_id: The agent's DID:key

        Returns:
            AgentDetail with full card JSON.

        Raises:
            LookupError: Agent not found (404)
        """
        resp = self._request("GET", f"/api/v1/agents/{node_id}")
        return AgentDetail(
            node_id=resp["node_id"],
            name=resp["name"],
            master_name=resp.get("master_name", ""),
            description=resp.get("description", ""),
            skills=resp.get("skills", []),
            verified=resp.get("verified", False),
            trust_score=resp.get("trust_score", 0.0),
            interactions_count=resp.get("interactions_count", 0),
            registered_at=resp.get("registered_at", ""),
            last_seen_at=resp.get("last_seen_at", ""),
            card_json=resp.get("card_json", ""),
        )

    def health(self) -> bool:
        """Check if the registry is reachable."""
        try:
            resp = self._request("GET", "/health")
            return resp.get("status") == "ok"
        except ConnectionError:
            return False
