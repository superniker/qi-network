"""
Qi Client — Unified API.

One import for any AI agent platform:
    from qi_client import QiClient

    client = QiClient(identity, addresses=["1.2.3.4:9733"])
    client.connect()                  # bootstrap + DHT join
    client.publish(agent_card_json)   # registry + DHT store
    client.search("ocr")              # find agents by capability
    client.find("did:key:z...")       # locate agent via DHT
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

from qinetwork import QiIdentity, AgentCard

from .bootstrap import (
    BootstrapClient, BootstrapPeer, BootstrapResult, FindResult, StoreResult,
)
from .registry import (
    RegistryClient, AgentSummary, AgentDetail, SearchResult,
)

logger = logging.getLogger("qi-client")


@dataclass
class QiConfig:
    """Configuration for a Qi Network client."""
    bootstrap_url: str = "http://qi-network.net:7883"
    registry_url: str = "http://qi-network.net:7881"
    timeout: int = 10
    auto_connect: bool = True          # connect on client creation
    auto_publish: bool = False         # publish on client creation


@dataclass
class ConnectResult:
    """Result of connecting to the Qi Network."""
    success: bool
    bootstrap_node_id: str = ""
    peers_discovered: int = 0
    error: str = ""


@dataclass
class PublishResult:
    """Result of publishing an Agent Card."""
    registry: bool = False             # registered in registry
    dht_stored: bool = False           # stored in DHT
    registry_error: str = ""
    dht_error: str = ""


class QiClient:
    """Qi Network client — connects any agent to the network.

    Cross-platform: Linux, macOS, Windows. Zero system dependencies.
    Only requires Python 3.10+ and qi-core.

    Usage:
        from qinetwork import QiIdentity
        from qi_client import QiClient

        identity = QiIdentity.generate()
        client = QiClient(
            identity=identity,
            addresses=["2409:abcd::1:9733"],
        )
        client.connect()         # join the DHT
        client.publish(card)     # announce to registry + DHT
        results = client.search("ocr")  # find peers
    """

    def __init__(
        self,
        identity: QiIdentity,
        addresses: list[str] | None = None,
        config: QiConfig | None = None,
    ):
        """
        Args:
            identity:  QiIdentity with Ed25519 keypair
            addresses: Your agent's reachable addresses (IPv6, IPv4, ...)
                       Ordered by priority: public IPv6 → public IPv4 → private IPv4
            config:    Optional configuration overrides
        """
        self.identity = identity
        self.addresses = addresses or []
        self.config = config or QiConfig()

        self.bootstrap = BootstrapClient(
            base_url=self.config.bootstrap_url,
            timeout=self.config.timeout,
        )
        self.registry = RegistryClient(
            base_url=self.config.registry_url,
            timeout=self.config.timeout,
        )

        self._connected = False
        self._peers: list[BootstrapPeer] = []

        if self.config.auto_connect and self.addresses:
            self.connect()

    # ── Network Connection ──────────────────────────────────

    def connect(self) -> ConnectResult:
        """Connect to the Qi Network via bootstrap.

        Announces self to the bootstrap node and retrieves initial peers.
        After this, the agent is part of the DHT network.

        Returns:
            ConnectResult with status and discovered peer count.
        """
        if not self.addresses:
            return ConnectResult(
                success=False,
                error="No addresses configured. Set client.addresses before connecting.",
            )

        try:
            result = self.bootstrap.bootstrap(
                did=self.identity.node_id,
                addresses=self.addresses,
            )
            self._peers = result.peers
            self._connected = True
            logger.info(
                f"Qi Network connected via {self.config.bootstrap_url}. "
                f"Discovered {result.count} peers."
            )
            return ConnectResult(
                success=True,
                bootstrap_node_id=result.bootstrap_node_id,
                peers_discovered=result.count,
            )
        except ConnectionError as e:
            logger.warning(f"Qi Network bootstrap failed: {e}")
            return ConnectResult(success=False, error=str(e))

    # ── Publishing ──────────────────────────────────────────

    def publish(self, agent_card_json: str) -> PublishResult:
        """Publish Agent Card to registry + DHT.

        This makes the agent discoverable:
        - Registry: searchable by name, description, skills
        - DHT: retrievable by DID (distributed, no central point of failure)

        Args:
            agent_card_json: Signed Agent Card JSON string (from AgentCard.signed_json())

        Returns:
            PublishResult with per-service status.
        """
        result = PublishResult()

        # 1. Register with registry (searchable)
        try:
            self.registry.register(agent_card_json, verify_signature=True)
            result.registry = True
            logger.info("Agent Card registered in registry")
        except ValueError as e:
            # 409 Already exists — try update
            try:
                self.registry.update(self.identity.node_id, agent_card_json)
                result.registry = True
                logger.info("Agent Card updated in registry")
            except Exception as e2:
                result.registry_error = str(e2)
                logger.warning(f"Registry update failed: {e2}")
        except Exception as e:
            result.registry_error = str(e)
            logger.warning(f"Registry registration failed: {e}")

        # 2. Store in DHT (distributed, survives registry outage)
        try:
            key = BootstrapClient.key_from_did(self.identity.node_id)
            self.bootstrap.store(key, agent_card_json, self.identity.node_id)
            result.dht_stored = True
            logger.info("Agent Card stored in DHT")
        except Exception as e:
            result.dht_error = str(e)
            logger.warning(f"DHT store failed: {e}")

        return result

    # ── Discovery ───────────────────────────────────────────

    def search(
        self,
        query: str = "",
        skill: str = "",
        verified: bool | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> SearchResult:
        """Search the registry for agents.

        Use cases:
          - "Who has OCR capability?"  → client.search(skill="ocr")
          - "Find document processing agents" → client.search("document")
          - "Verified agents only" → client.search(skill="code-review", verified=True)

        Args:
            query:    Free-text search (name + description + skills)
            skill:    Filter by exact skill name
            verified: Only show verified agents
            page:     Page number
            page_size: Results per page

        Returns:
            SearchResult with matching agents.
        """
        return self.registry.search(
            query=query,
            skill=skill,
            verified=verified,
            page=page,
            page_size=page_size,
        )

    def find(self, did: str) -> FindResult:
        """Find an agent by DID via the DHT.

        Tries the bootstrap node first. If not found there,
        returns closer peers — the caller can retry on those.

        Args:
            did: The agent's DID:key identifier

        Returns:
            FindResult with status "found" (Agent Card JSON) or "not_found".
        """
        key = BootstrapClient.key_from_did(did)
        return self.bootstrap.find_value(key)

    def get_agent(self, did: str) -> AgentDetail | None:
        """Get agent details from the registry by DID.

        Args:
            did: The agent's DID:key

        Returns:
            AgentDetail or None if not found.
        """
        try:
            return self.registry.get_agent(did)
        except LookupError:
            return None

    # ── Status ──────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def peers(self) -> list[BootstrapPeer]:
        return self._peers

    def health(self) -> dict:
        """Check health of both registry and bootstrap."""
        return {
            "registry": self.registry.health(),
            "bootstrap": self.bootstrap.health(),
            "connected": self._connected,
            "node_id": self.identity.node_id,
            "addresses": self.addresses,
            "peers": len(self._peers),
        }
