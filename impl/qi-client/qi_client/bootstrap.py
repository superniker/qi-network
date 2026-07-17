"""
Qi Client — Bootstrap API.

HTTP client for the qi-bootstrap service:
  - POST /bootstrap  →  announce self + get initial peers
  - POST /store      →  store a value in the DHT
  - GET  /value      →  retrieve a value or closest peers
"""

from __future__ import annotations

import hashlib
import json
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BootstrapPeer:
    """A peer returned by the bootstrap node."""
    did: str
    node_id: str
    addresses: list[str] = field(default_factory=list)


@dataclass
class BootstrapResult:
    """Result of a bootstrap handshake."""
    bootstrap_node_id: str
    peers: list[BootstrapPeer]
    count: int


@dataclass
class StoreResult:
    """Result of storing a value in the DHT."""
    key: str
    expires_in_seconds: int


@dataclass
class FindResult:
    """Result of a DHT value lookup."""
    status: str           # "found" or "not_found"
    key: str
    value: str | None = None
    publisher_did: str | None = None
    closest_peers: list[BootstrapPeer] = field(default_factory=list)


class BootstrapClient:
    """HTTP client for qi-bootstrap."""

    def __init__(self, base_url: str = "http://qi-network.net:8733", timeout: int = 10):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _post(self, path: str, data: dict) -> dict:
        """POST JSON, return parsed response."""
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")
            raise ConnectionError(f"Bootstrap {path} failed: HTTP {e.code} — {error_body}")
        except urllib.error.URLError as e:
            raise ConnectionError(f"Bootstrap {path} unreachable: {e.reason}")

    def _get(self, path: str) -> dict:
        """GET, return parsed response."""
        req = urllib.request.Request(f"{self.base_url}{path}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")
            raise ConnectionError(f"Bootstrap {path} failed: HTTP {e.code} — {error_body}")
        except urllib.error.URLError as e:
            raise ConnectionError(f"Bootstrap {path} unreachable: {e.reason}")

    def bootstrap(self, did: str, addresses: list[str]) -> BootstrapResult:
        """Announce self to bootstrap and get initial peers.

        Args:
            did:       The agent's DID:key identifier
            addresses: Priority-ordered list of reachable addresses (IPv6, IPv4, ...)

        Returns:
            BootstrapResult with the bootstrap node ID and initial peer list.
        """
        resp = self._post("/bootstrap", {"did": did, "addresses": addresses})
        peers = [
            BootstrapPeer(
                did=p["did"],
                node_id=p["node_id"],
                addresses=p.get("addresses", p.get("address", [])),
            )
            for p in resp.get("peers", [])
        ]
        return BootstrapResult(
            bootstrap_node_id=resp.get("node_id", ""),
            peers=peers,
            count=resp.get("count", 0),
        )

    def store(self, key: bytes, value: str, did: str) -> StoreResult:
        """Store a value in the DHT.

        Args:
            key:   20-byte key (SHA-256 of DID, truncated)
            value: JSON string to store (Agent Card, etc.)
            did:   Publisher's DID:key

        Returns:
            StoreResult with the key and TTL.
        """
        resp = self._post("/store", {
            "key": key.hex(),
            "value": value,
            "did": did,
        })
        return StoreResult(
            key=resp["key"],
            expires_in_seconds=resp["expires_in_seconds"],
        )

    def find_value(self, key: bytes) -> FindResult:
        """Retrieve a value from the DHT.

        If the value is not stored on this bootstrap node, returns the
        K closest peers — the caller should retry on those.

        Args:
            key: 20-byte key to look up

        Returns:
            FindResult with status "found" or "not_found".
        """
        resp = self._get(f"/value?key={key.hex()}")
        status = resp.get("status", "not_found")
        if status == "found":
            return FindResult(
                status="found",
                key=resp["key"],
                value=resp["value"],
                publisher_did=resp.get("publisher_did"),
            )
        closest = [
            BootstrapPeer(
                did=p["did"],
                node_id=p["node_id"],
                addresses=p.get("addresses", []),
            )
            for p in resp.get("closest_peers", [])
        ]
        return FindResult(
            status="not_found",
            key=resp.get("key", key.hex()),
            closest_peers=closest,
        )

    @staticmethod
    def key_from_did(did: str) -> bytes:
        """Derive DHT storage key from a DID:key.

        SHA-256 of the UTF-8 DID string, truncated to 20 bytes.
        Same algorithm as bootstrap's Node ID derivation.
        """
        return hashlib.sha256(did.encode("utf-8")).digest()[:20]

    def health(self) -> bool:
        """Check if the bootstrap node is reachable."""
        try:
            resp = self._get("/health")
            return resp.get("status") == "ok"
        except ConnectionError:
            return False
