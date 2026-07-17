"""
Qi Protocol — Agent Card Module

Create, sign, verify, and parse Agent Cards (契书).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from typing import Any

from .identity import QiIdentity


@dataclass
class Skill:
    """A capability declaration in an Agent Card."""
    id: str
    name: str
    description: str
    confidence: float = 1.0  # 0-1 self-assessed confidence

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Skill":
        return cls(
            id=d["id"],
            name=d["name"],
            description=d["description"],
            confidence=d.get("confidence", 1.0),
        )


@dataclass
class Endpoint:
    """A connection endpoint for a Qi Node."""
    transport: str  # "a2a", "libp2p", "http"
    url: str

    def to_dict(self) -> dict:
        return {"transport": self.transport, "url": self.url}

    @classmethod
    def from_dict(cls, d: dict) -> "Endpoint":
        return cls(transport=d["transport"], url=d["url"])


@dataclass
class AgentCard:
    """A Qi Network Agent Card (契书)."""

    node_id: str
    master_id: str
    name: str
    description: str = ""
    master_name: str = ""
    skills: list[Skill] = field(default_factory=list)
    endpoints: list[Endpoint] = field(default_factory=list)
    published_at: str = ""  # ISO 8601
    expires_at: str = ""    # ISO 8601
    qi_version: str = "0.1"
    # Optional compatibility fields
    gb_identity_code: str = ""   # GB/Z 185—2026 身份码（可选，由注册服务方分配）
    aliases: list[str] = field(default_factory=list)  # 其他身份系统中的别名

    def to_dict(self, include_signatures: bool = False) -> dict:
        """Serialize to dict (without signatures by default)."""
        d: dict[str, Any] = {
            "qi_version": self.qi_version,
            "node_id": self.node_id,
            "master_id": self.master_id,
            "published_at": self.published_at,
            "expires_at": self.expires_at,
            "identity": {
                "name": self.name,
                "description": self.description,
                "master_name": self.master_name,
            },
            "endpoints": [ep.to_dict() for ep in self.endpoints],
            "capabilities": {
                "skills": [s.to_dict() for s in self.skills],
            },
        }
        if self.gb_identity_code:
            d["gb_identity_code"] = self.gb_identity_code
        if self.aliases:
            d["aliases"] = self.aliases
        return d

    def to_json(self, indent: int = 2) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict) -> "AgentCard":
        """Parse from dict (ignores signatures)."""
        identity = d.get("identity", {})
        capabilities = d.get("capabilities", {})
        return cls(
            qi_version=d.get("qi_version", "0.1"),
            node_id=d["node_id"],
            master_id=d["master_id"],
            name=identity.get("name", ""),
            description=identity.get("description", ""),
            master_name=identity.get("master_name", ""),
            skills=[Skill.from_dict(s) for s in capabilities.get("skills", [])],
            endpoints=[Endpoint.from_dict(ep) for ep in d.get("endpoints", [])],
            published_at=d.get("published_at", ""),
            expires_at=d.get("expires_at", ""),
            gb_identity_code=d.get("gb_identity_code", ""),
            aliases=d.get("aliases", []),
        )

    @classmethod
    def from_json(cls, s: str) -> "AgentCard":
        """Parse from JSON string."""
        return cls.from_dict(json.loads(s))

    @classmethod
    def create(
        cls,
        identity: QiIdentity,
        master_identity: QiIdentity,
        name: str,
        description: str = "",
        master_name: str = "",
        skills: list[Skill] | None = None,
        endpoints: list[Endpoint] | None = None,
        ttl_days: int = 365,
    ) -> "AgentCard":
        """Create a new Agent Card for a Qi Node."""
        now = int(time.time())
        return cls(
            qi_version="0.1",
            node_id=identity.node_id,
            master_id=master_identity.node_id,
            name=name,
            description=description,
            master_name=master_name,
            skills=skills or [],
            endpoints=endpoints or [],
            published_at=_iso8601(now),
            expires_at=_iso8601(now + ttl_days * 86400),
        )

    def is_expired(self) -> bool:
        """Check if the Agent Card has expired."""
        if not self.expires_at:
            return False
        expiry = _parse_iso8601(self.expires_at)
        return time.time() > expiry

    def sign(self, master_identity: QiIdentity) -> str:
        """Sign the Agent Card with the Master's key. Returns base64url signature."""
        payload = self.to_dict(include_signatures=False)
        return master_identity.sign_json(payload)

    def verify(self, master_signature_b64url: str, master_public_key_bytes: bytes | None = None) -> bool:
        """Verify the Master's signature on this Agent Card."""
        payload = self.to_dict(include_signatures=False)
        if master_public_key_bytes is None:
            master_public_key_bytes = QiIdentity.public_key_from_node_id(self.master_id)
        return QiIdentity.verify_json(payload, master_signature_b64url, master_public_key_bytes)

    def signed_json(self, master_identity: QiIdentity, indent: int = 2) -> str:
        """Export the Agent Card as signed JSON."""
        payload = self.to_dict(include_signatures=False)
        signature = master_identity.sign_json(payload)
        signed = {
            **payload,
            "signatures": {
                "master": {
                    "algorithm": "ed25519",
                    "signature": signature,
                    "signed_at": _iso8601(int(time.time())),
                }
            },
        }
        return json.dumps(signed, indent=indent, ensure_ascii=False)


def _iso8601(ts: int) -> str:
    """Convert Unix timestamp to ISO 8601 string."""
    import datetime
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).isoformat()


def _parse_iso8601(s: str) -> float:
    """Parse ISO 8601 string to Unix timestamp."""
    import datetime
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.datetime.fromisoformat(s)
    return dt.timestamp()
