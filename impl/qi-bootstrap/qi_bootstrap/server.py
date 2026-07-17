"""
Qi Bootstrap — HTTP API Server.

Endpoints:
  POST /bootstrap   — Join the DHT: register self with bootstrap, get initial peer list
  GET  /peers       — List known peers (debug/admin)
  GET  /health      — Health check
  GET  /stats       — Routing table statistics
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

from .node import (
    RoutingTable, PeerInfo, node_id_from_did, K, STALE_TIMEOUT, ALPHA, ID_BYTES,
)

logger = logging.getLogger("qi-bootstrap")

# ─── Config ──────────────────────────────────────────────────

HOST = os.environ.get("QI_BOOTSTRAP_HOST", "0.0.0.0")
PORT = int(os.environ.get("QI_BOOTSTRAP_PORT", "7883"))

# ─── Global state ────────────────────────────────────────────

# Own node identity — derived from our DID:key
_bootstrap_did = os.environ.get("QI_BOOTSTRAP_DID", "")
_own_id: bytes = node_id_from_did(_bootstrap_did.encode("utf-8")) if _bootstrap_did else os.urandom(20)

routing_table = RoutingTable(_own_id)


# ─── Periodic cleanup ────────────────────────────────────────

_last_stale_eviction = time.time()


def _evict_if_needed():
    """Evict stale peers periodically (once per minute)."""
    global _last_stale_eviction
    now = time.time()
    if now - _last_stale_eviction > 60:
        removed = routing_table.evict_stale()
        if removed:
            logger.info(f"Evicted {removed} stale peers")
        _last_stale_eviction = now


# ─── HTTP Handlers (raw dict-based, framework-free) ──────────

def _json_response(status: int, body: dict) -> tuple[int, str]:
    """Serialize a JSON response."""
    import json
    return status, json.dumps(body, ensure_ascii=False, indent=2)


def _parse_body(data: bytes) -> dict:
    """Parse JSON request body."""
    import json
    try:
        return json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f"Invalid JSON: {e}")


def handle_bootstrap(body: dict) -> tuple[int, str]:
    """Handle POST /bootstrap.

    Request:
      {
        "did": "did:key:z...",              // The new node's DID:key
        "addresses": [                       // Priority-ordered list of reachable addresses
          "2409:8a00:abcd::1:9733",          // ① public IPv6 (direct connect, no NAT)
          "1.2.3.4:9733",                    // ② public IPv4 (direct connect)
          "192.168.1.5:9733"                 // ③ private IPv4 (NAT, needs hole punching)
        ]
      }

      // Legacy single-address fallback:
      // "address": "1.2.3.4:9733"  →  internally converted to ["1.2.3.4:9733"]

    Response:
      {
        "node_id": "a1b2c3...",              // Hex of our Node ID
        "peers": [                             // K closest peers to the new node
          {"did": "did:key:z...", "addresses": ["..."], "node_id": "a1b2c3..."},
          ...
        ],
        "count": 3
      }
    """
    _evict_if_needed()

    did = body.get("did", "")
    # Support both "addresses" (new) and "address" (legacy)
    addresses: list[str] = body.get("addresses", [])
    if not addresses and "address" in body:
        addresses = [body["address"]]

    if not did or not addresses:
        return _json_response(400, {"error": "missing required fields: did, addresses (or address)"})

    peer_id = node_id_from_did(did)
    peer = PeerInfo(node_id=peer_id, did=did, addresses=addresses)

    # Add to routing table
    routing_table.add_peer(peer)

    # Find K closest peers to the new node (these become its initial neighbors)
    closest = routing_table.find_closest(peer_id, count=K)
    # The new node itself will be in the routing table now, filter it out
    peers_list = [
        {"did": p.did, "addresses": p.addresses, "node_id": p.node_id_hex}
        for p in closest
        if p.node_id != peer_id
    ][:K]

    logger.info(f"Bootstrap: {did[:30]}... @ {addresses[0]} — returning {len(peers_list)} peers")

    return _json_response(200, {
        "node_id": _own_id.hex(),
        "peers": peers_list,
        "count": len(peers_list),
    })


def handle_peers() -> tuple[int, str]:
    """Handle GET /peers — list all known peers."""
    _evict_if_needed()

    all_peers = []
    for bucket in routing_table.buckets:
        for p in bucket.peers:
            all_peers.append({
                "node_id": p.node_id_hex,
                "did": p.did[:60] + "..." if len(p.did) > 60 else p.did,
                "addresses": p.addresses,
                "last_seen_ago": round(time.time() - p.last_seen, 1),
            })

    return _json_response(200, {
        "total": len(all_peers),
        "peers": all_peers,
    })


def handle_stats() -> tuple[int, str]:
    """Handle GET /stats — routing table stats."""
    _evict_if_needed()
    return _json_response(200, routing_table.stats())


def handle_health() -> tuple[int, str]:
    """Handle GET /health."""
    return _json_response(200, {"status": "ok", "service": "qi-bootstrap"})


def handle_store(body: dict) -> tuple[int, str]:
    """Handle POST /store.

    Request:
      {
        "key": "a1b2c3...",              // 40-char hex of the 20-byte key
        "value": "{...}",                 // JSON string — the data to store (Agent Card, etc.)
        "did": "did:key:z...",           // publisher's DID:key
        "signature": "..."               // Ed25519 signature over key+value (optional for now)
      }

    Response:
      {
        "status": "stored",
        "key": "a1b2c3...",
        "expires_in_seconds": 86400
      }
    """
    key_hex = body.get("key", "")
    value = body.get("value", "")
    did = body.get("did", "")

    if not key_hex or not value or not did:
        return _json_response(400, {"error": "missing required fields: key, value, did"})

    try:
        key = bytes.fromhex(key_hex)
    except ValueError:
        return _json_response(400, {"error": "invalid key: must be hex-encoded"})

    if len(key) != ID_BYTES:
        return _json_response(400, {"error": f"invalid key length: expected {ID_BYTES} bytes, got {len(key)}"})

    sv = routing_table.store.put(key, value, did)
    logger.info(f"Store: key={key_hex[:12]}... from {did[:30]}...")

    return _json_response(200, {
        "status": "stored",
        "key": key_hex,
        "expires_in_seconds": int(sv.expires_at - time.time()),
    })


def handle_find_value(key_hex: str) -> tuple[int, str]:
    """Handle GET /value?key=...

    Response (found):
      {
        "status": "found",
        "key": "a1b2c3...",
        "value": "{...}",
        "publisher_did": "did:key:z...",
        "stored_at": 1234567890.0,
        "expires_at": 1234567890.0
      }

    Response (not found — returns closest peers for routing):
      {
        "status": "not_found",
        "key": "a1b2c3...",
        "closest_peers": [...]
      }
    """
    _evict_if_needed()

    if not key_hex:
        return _json_response(400, {"error": "missing query parameter: key"})

    try:
        key = bytes.fromhex(key_hex)
    except ValueError:
        return _json_response(400, {"error": "invalid key: must be hex-encoded"})

    if len(key) != ID_BYTES:
        return _json_response(400, {"error": f"invalid key length: expected {ID_BYTES} bytes, got {len(key)}"})

    sv = routing_table.store.get(key)
    if sv is not None:
        return _json_response(200, {
            "status": "found",
            "key": sv.key_hex,
            "value": sv.value,
            "publisher_did": sv.publisher_did,
            "stored_at": sv.stored_at,
            "expires_at": sv.expires_at,
        })

    # Not found locally — return closest peers so the requester can route to them
    closest = routing_table.find_closest(key, count=K)
    peers_list = [
        {"did": p.did, "addresses": p.addresses, "node_id": p.node_id_hex}
        for p in closest
    ]

    return _json_response(200, {
        "status": "not_found",
        "key": key_hex,
        "closest_peers": peers_list,
        "hint": "Retry find_value on these peers — they are closer to the key",
    })


# ─── Router ──────────────────────────────────────────────────

ROUTES = {
    ("GET", "/"): lambda query=None: _json_response(200, {
        "service": "qi-bootstrap",
        "version": "0.1.0",
        "protocol": "Qi Network — Kademlia DHT Bootstrap",
        "node_id": _own_id.hex(),
    }),
    ("GET", "/health"): lambda query=None: handle_health(),
    ("GET", "/stats"): lambda query=None: handle_stats(),
    ("GET", "/peers"): lambda query=None: handle_peers(),
    ("GET", "/value"): lambda query: handle_find_value(query.get("key", "")),
    ("POST", "/bootstrap"): lambda body, query=None: handle_bootstrap(body),
    ("POST", "/store"): lambda body, query=None: handle_store(body),
}


def route(method: str, path: str, body: Optional[bytes], query: dict[str, str] | None = None) -> tuple[int, str]:
    """Route a request to the correct handler."""
    key = (method.upper(), path)
    handler = ROUTES.get(key)
    if handler is None:
        return _json_response(404, {"error": f"not found: {method} {path}"})

    try:
        import inspect
        sig = inspect.signature(handler)
        params = sig.parameters

        kwargs = {}
        if "body" in params:
            kwargs["body"] = _parse_body(body) if body else {}
        if "query" in params:
            kwargs["query"] = query or {}

        return handler(**kwargs)
    except ValueError as e:
        return _json_response(400, {"error": str(e)})
    except Exception as e:
        logger.exception(f"Handler error: {e}")
        return _json_response(500, {"error": "internal server error"})


# ─── HTTP Server (stdlib only — no FastAPI dependency) ───────

class BootstrapServer:
    """Minimal async HTTP server using asyncio — no framework dependency."""

    def __init__(self, host: str = HOST, port: int = PORT):
        self.host = host
        self.port = port

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        """Handle a single HTTP connection."""
        try:
            # Read request line and headers
            raw = b""
            while b"\r\n\r\n" not in raw:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=10)
                if not chunk:
                    return
                raw += chunk

            header_end = raw.index(b"\r\n\r\n")
            header_bytes = raw[:header_end]
            body_raw = raw[header_end + 4:]

            header_text = header_bytes.decode("utf-8", errors="replace")
            lines = header_text.split("\r\n")
            if not lines:
                return

            # Parse request line
            request_line = lines[0]
            parts = request_line.split(" ")
            if len(parts) < 2:
                return
            method = parts[0]
            full_path = parts[1]

            # Split path and query string
            query: dict[str, str] = {}
            if "?" in full_path:
                path, qs = full_path.split("?", 1)
                for param in qs.split("&"):
                    if "=" in param:
                        k, v = param.split("=", 1)
                        from urllib.parse import unquote
                        query[unquote(k)] = unquote(v)
            else:
                path = full_path

            # Parse Content-Length for body
            content_length = 0
            for line in lines[1:]:
                if line.lower().startswith("content-length:"):
                    content_length = int(line.split(":", 1)[1].strip())

            # Read remaining body if needed
            body = body_raw
            while len(body) < content_length:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=10)
                if not chunk:
                    break
                body += chunk

            body = body[:content_length] if content_length > 0 else None

            # Route and respond
            status, response_body = route(method, path, body, query)
            response_bytes = response_body.encode("utf-8")

            status_text = {200: "OK", 201: "Created", 204: "No Content",
                          400: "Bad Request", 404: "Not Found", 409: "Conflict",
                          500: "Internal Server Error"}.get(status, "Unknown")

            writer.write(
                f"HTTP/1.1 {status} {status_text}\r\n"
                f"Content-Type: application/json; charset=utf-8\r\n"
                f"Content-Length: {len(response_bytes)}\r\n"
                f"Access-Control-Allow-Origin: *\r\n"
                f"Connection: close\r\n"
                f"\r\n".encode("utf-8")
            )
            writer.write(response_bytes)
            await writer.drain()

        except asyncio.TimeoutError:
            pass
        except Exception as e:
            logger.exception(f"Connection error: {e}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def start(self):
        """Start the HTTP server."""
        server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        logger.info(f"Qi Bootstrap listening on {self.host}:{self.port}")
        logger.info(f"Node ID: {_own_id.hex()}")

        async with server:
            await server.serve_forever()


# ─── Entry point ─────────────────────────────────────────────

def main():
    """Start the bootstrap server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    logger.info("Starting Qi Bootstrap v0.1.0")
    logger.info(f"Node ID: {_own_id.hex()}")

    server = BootstrapServer()
    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        logger.info("Qi Bootstrap shutting down")


if __name__ == "__main__":
    main()
