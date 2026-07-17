"""
Qi Bootstrap — Kademlia DHT Node.

Implements a Kademlia DHT node:
  - 160-bit Node IDs (SHA-256 of DID:key, truncated to 20 bytes)
  - K-buckets routing table (k=8, like Mainline DHT)
  - XOR distance metric
  - find_node: return k closest peers to a target ID
  - store: store a value on nodes closest to its key
  - find_value: return stored value or closest nodes
  - Periodic stale-peer and stale-value eviction
"""

from __future__ import annotations

import hashlib
import heapq
import time
from dataclasses import dataclass, field
from typing import Optional


# ─── Constants ────────────────────────────────────────────────

K = 8                     # k-bucket size (Mainline DHT uses 8)
B = 160                   # bits in Node ID
ALPHA = 3                 # concurrency for iterative lookups
STALE_TIMEOUT = 15 * 60   # 15 minutes — evict peers unseen longer than this
VALUE_TTL = 24 * 3600     # 24 hours — stored values expire
ID_BYTES = B // 8         # 20 bytes


# ─── Node ID ──────────────────────────────────────────────────

def node_id_from_did(did: str) -> bytes:
    """Derive a 160-bit Kademlia Node ID from a DID:key.

    SHA-256 of the UTF-8 DID string, truncated to 20 bytes.
    This is deterministic and reproducible — any peer can verify.
    """
    return hashlib.sha256(did.encode("utf-8")).digest()[:ID_BYTES]


def xor_distance(a: bytes, b: bytes) -> int:
    """XOR metric: the Kademlia distance between two Node IDs."""
    return int.from_bytes(a, "big") ^ int.from_bytes(b, "big")


# ─── Data Types ───────────────────────────────────────────────

@dataclass
class PeerInfo:
    """A known peer in the DHT network."""
    node_id: bytes                    # 20-byte Kademlia Node ID
    did: str                          # DID:key identifier
    addresses: list[str] = field(default_factory=list)  # [ip:port, ...], caller-ordered
    last_seen: float = field(default_factory=time.time)

    @property
    def node_id_hex(self) -> str:
        return self.node_id.hex()

    @property
    def primary_address(self) -> str:
        """First address (highest priority as ordered by the reporting agent)."""
        return self.addresses[0] if self.addresses else ""


@dataclass
class KBucket:
    """A k-bucket: stores up to K peers within a specific distance range.

    Kademlia maintains one bucket per bit of the ID space (0..159).
    Each bucket covers distance range [2^i, 2^{i+1}).
    """
    index: int                     # which bit this bucket covers
    peers: list[PeerInfo] = field(default_factory=list)

    @property
    def range_min(self) -> int:
        return 1 << self.index

    @property
    def range_max(self) -> int:
        return (1 << (self.index + 1)) - 1

    def full(self) -> bool:
        return len(self.peers) >= K

    def contains(self, distance: int) -> bool:
        return self.range_min <= distance <= self.range_max

    def add(self, peer: PeerInfo):
        """Add a peer. If already present, move to end (most-recently-seen).
        If bucket is full, evict the least-recently-seen peer."""
        # Remove if already present (will be re-added at end)
        self.peers = [p for p in self.peers if p.node_id != peer.node_id]

        if len(self.peers) >= K:
            # Evict the oldest peer
            self.peers.pop(0)

        self.peers.append(peer)

    def get_closest(self, target: bytes, count: int = K) -> list[PeerInfo]:
        """Return up to `count` peers closest to target, sorted by distance."""
        scored = [(xor_distance(p.node_id, target), p) for p in self.peers]
        scored.sort(key=lambda x: x[0])
        return [p for _, p in scored[:count]]


# ─── DHT Key-Value Store ──────────────────────────────────────

@dataclass
class StoredValue:
    """A value stored in the DHT."""
    key: bytes           # 20-byte key
    value: str            # stored data (JSON)
    publisher_did: str    # DID:key of the publisher
    stored_at: float      # when it was stored
    expires_at: float     # when it expires

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at

    @property
    def key_hex(self) -> str:
        return self.key.hex()


class DhtStore:
    """In-memory DHT key-value store with TTL expiration.

    Kademlia semantics: values are stored on the K nodes whose IDs
    are closest to the key. Each node independently decides whether
    it's among the closest and therefore responsible for the value.
    """

    def __init__(self, own_id: bytes):
        self.own_id = own_id
        self._store: dict[bytes, StoredValue] = {}

    def put(self, key: bytes, value: str, publisher_did: str) -> StoredValue:
        """Store a value. Overwrites if key already exists."""
        now = time.time()
        sv = StoredValue(
            key=key,
            value=value,
            publisher_did=publisher_did,
            stored_at=now,
            expires_at=now + VALUE_TTL,
        )
        self._store[key] = sv
        return sv

    def get(self, key: bytes) -> StoredValue | None:
        """Get a value by key. Returns None if not found or expired."""
        sv = self._store.get(key)
        if sv is None:
            return None
        if sv.expired:
            del self._store[key]
            return None
        return sv

    def delete(self, key: bytes) -> bool:
        """Delete a value. Returns False if not found."""
        if key in self._store:
            del self._store[key]
            return True
        return False

    def evict_expired(self) -> int:
        """Remove all expired values. Returns count evicted."""
        expired = [k for k, v in self._store.items() if v.expired]
        for k in expired:
            del self._store[k]
        return len(expired)

    def stats(self) -> dict:
        """Return store statistics."""
        self.evict_expired()
        return {
            "total_values": len(self._store),
            "value_ttl_hours": VALUE_TTL // 3600,
        }


# ─── Routing Table ────────────────────────────────────────────

class RoutingTable:
    """Kademlia routing table: 160 k-buckets + DHT key-value store."""

    def __init__(self, own_id: bytes):
        self.own_id = own_id
        self.buckets: list[KBucket] = [KBucket(i) for i in range(B)]
        self.store = DhtStore(own_id)

    def _bucket_for(self, node_id: bytes) -> KBucket:
        """Find the correct bucket for a given Node ID based on XOR distance."""
        distance = xor_distance(self.own_id, node_id)
        if distance == 0:
            raise ValueError("Cannot add own Node ID to routing table")
        # The bucket index is the position of the highest set bit in distance
        bucket_index = distance.bit_length() - 1
        return self.buckets[bucket_index]

    def add_peer(self, peer: PeerInfo):
        """Add or refresh a peer in the routing table."""
        try:
            bucket = self._bucket_for(peer.node_id)
        except ValueError:
            return  # own node
        bucket.add(peer)

    def remove_peer(self, node_id: bytes):
        """Remove a peer from all buckets."""
        for bucket in self.buckets:
            bucket.peers = [p for p in bucket.peers if p.node_id != node_id]

    def get_peer(self, node_id: bytes) -> Optional[PeerInfo]:
        """Look up a specific peer."""
        for bucket in self.buckets:
            for p in bucket.peers:
                if p.node_id == node_id:
                    return p
        return None

    def touch(self, node_id: bytes):
        """Update last_seen for a peer."""
        peer = self.get_peer(node_id)
        if peer:
            peer.last_seen = time.time()
            # Re-insert to move to end of bucket (most-recently-seen)
            self.add_peer(peer)

    def find_closest(self, target: bytes, count: int = K) -> list[PeerInfo]:
        """Return up to `count` peers closest to target across all buckets.

        Uses a min-heap for efficient top-N selection.
        """
        heap: list[tuple[int, int, PeerInfo]] = []  # (distance, tiebreaker, peer)
        counter = 0
        for bucket in self.buckets:
            for peer in bucket.peers:
                distance = xor_distance(peer.node_id, target)
                if distance == 0:
                    continue  # skip self
                heapq.heappush(heap, (distance, counter, peer))
                counter += 1

        return [p for _, _, p in heapq.nsmallest(count, heap)]

    def evict_stale(self) -> int:
        """Remove peers unseen for longer than STALE_TIMEOUT.
        Returns count of evicted peers."""
        now = time.time()
        removed = 0
        for bucket in self.buckets:
            before = len(bucket.peers)
            bucket.peers = [p for p in bucket.peers if (now - p.last_seen) < STALE_TIMEOUT]
            removed += before - len(bucket.peers)
        # Also evict expired values
        self.store.evict_expired()
        return removed

    def stats(self) -> dict:
        """Return routing table + DHT store statistics."""
        total = sum(len(b.peers) for b in self.buckets)
        non_empty = sum(1 for b in self.buckets if b.peers)
        return {
            "total_peers": total,
            "non_empty_buckets": non_empty,
            "total_buckets": B,
            "k": K,
            "dht_store": self.store.stats(),
        }
