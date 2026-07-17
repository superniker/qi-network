"""
Qi Protocol — Message Envelope Module

Defines the Qi message format: envelope, types, and signing.
"""

from __future__ import annotations

import json
import uuid
import time
from typing import Any

from .identity import QiIdentity


# Message type constants
class MessageType:
    DISCOVERY_AGENT_CARD = "qi/discovery/agent_card"
    DISCOVERY_PEER_RECOMMEND = "qi/discovery/peer_recommend"
    TRUST_CLAIM = "qi/trust/claim"
    TRUST_ENDORSE = "qi/trust/endorse"
    COVENANT_PROPOSE = "qi/covenant/propose"
    COVENANT_COUNTER = "qi/covenant/counter"
    COVENANT_SATISFY = "qi/covenant/satisfy"
    COVENANT_SATISFY_RESPONSE = "qi/covenant/satisfy_response"
    COVENANT_COMMIT = "qi/covenant/commit"
    COVENANT_VERIFY = "qi/covenant/verify"


class QiMessage:
    """A Qi protocol message envelope."""

    def __init__(
        self,
        msg_type: str,
        from_node: str,
        to_node: str,
        payload: dict[str, Any] | None = None,
        message_id: str | None = None,
        timestamp: str | None = None,
        qi_version: str = "0.1",
    ):
        self.qi_version = qi_version
        self.message_id = message_id or f"urn:qi:msg:{uuid.uuid4()}"
        self.from_node = from_node
        self.to_node = to_node
        self.timestamp = timestamp or _iso8601(int(time.time()))
        self.type = msg_type
        self.payload = payload or {}

    def to_dict(self) -> dict:
        return {
            "qi_version": self.qi_version,
            "message_id": self.message_id,
            "from": self.from_node,
            "to": self.to_node,
            "timestamp": self.timestamp,
            "type": self.type,
            "payload": self.payload,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict) -> "QiMessage":
        return cls(
            qi_version=d.get("qi_version", "0.1"),
            message_id=d.get("message_id", ""),
            msg_type=d["type"],
            from_node=d["from"],
            to_node=d["to"],
            timestamp=d.get("timestamp", ""),
            payload=d.get("payload", {}),
        )

    @classmethod
    def from_json(cls, s: str) -> "QiMessage":
        return cls.from_dict(json.loads(s))

    def sign(self, identity: QiIdentity) -> str:
        """Sign the message payload. Returns base64url signature."""
        return identity.sign_json(self.payload)

    def verify(self, signature_b64url: str, public_key_bytes: bytes) -> bool:
        """Verify a signature on the message payload."""
        return QiIdentity.verify_json(self.payload, signature_b64url, public_key_bytes)

    # --- Factory methods for common message types ---

    @classmethod
    def propose_covenant(
        cls,
        from_node: str,
        to_node: str,
        covenant_id: str,
        mode: str,  # "commission", "partnership", etc.
        goal: str,
        input_urls: list[str] | None = None,
        expected_output: str = "",
        deadline: str | None = None,
    ) -> "QiMessage":
        return cls(
            msg_type=MessageType.COVENANT_PROPOSE,
            from_node=from_node,
            to_node=to_node,
            payload={
                "covenant_id": covenant_id,
                "mode": mode,
                "proposal": {
                    "goal": goal,
                    "input_urls": input_urls or [],
                    "expected_output": expected_output,
                    "deadline": deadline or "",
                },
            },
        )

    @classmethod
    def counter_covenant(
        cls,
        from_node: str,
        to_node: str,
        covenant_id: str,
        adjustments: dict[str, Any],
    ) -> "QiMessage":
        return cls(
            msg_type=MessageType.COVENANT_COUNTER,
            from_node=from_node,
            to_node=to_node,
            payload={
                "covenant_id": covenant_id,
                "response": {
                    "status": "counter",
                    "adjustments": adjustments,
                },
            },
        )

    @classmethod
    def commit_covenant(
        cls,
        from_node: str,
        to_node: str,
        covenant_id: str,
        finalized_proposal: dict[str, Any],
    ) -> "QiMessage":
        return cls(
            msg_type=MessageType.COVENANT_COMMIT,
            from_node=from_node,
            to_node=to_node,
            payload={
                "covenant_id": covenant_id,
                "commitment": {
                    "status": "accepted",
                    "finalized_proposal": finalized_proposal,
                },
            },
        )

    @classmethod
    def verify_covenant(
        cls,
        from_node: str,
        to_node: str,
        covenant_id: str,
        verdict_status: str,  # "accepted", "rejected"
        comments: str = "",
        trust_delta: float | None = None,
    ) -> "QiMessage":
        payload: dict[str, Any] = {
            "covenant_id": covenant_id,
            "verdict": {
                "status": verdict_status,
                "comments": comments,
            },
        }
        if trust_delta is not None:
            payload["verdict"]["trust_update"] = {
                "delta": trust_delta,
            }
        return cls(
            msg_type=MessageType.COVENANT_VERIFY,
            from_node=from_node,
            to_node=to_node,
            payload=payload,
        )

    @classmethod
    def satisfy_covenant(
        cls,
        from_node: str,
        to_node: str,
        covenant_id: str,
        conditions: list[dict],
    ) -> "QiMessage":
        """Submit proof that conditions have been satisfied."""
        return cls(
            msg_type=MessageType.COVENANT_SATISFY,
            from_node=from_node,
            to_node=to_node,
            payload={
                "covenant_id": covenant_id,
                "conditions": conditions,
            },
        )


def _iso8601(ts: int) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).isoformat()
