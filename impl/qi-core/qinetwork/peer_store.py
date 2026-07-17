"""
Qi Protocol — Peer Store Module

Local SQLite database for storing known Qi peers and their Agent Cards.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from dataclasses import dataclass

from .agent_card import AgentCard, Skill, Endpoint


@dataclass
class PeerRecord:
    """A stored peer with its Agent Card and metadata."""
    node_id: str
    name: str
    master_name: str
    agent_card_json: str
    added_at: float  # Unix timestamp
    last_seen_at: float
    trust_score: float = 0.0

    @property
    def agent_card(self) -> AgentCard:
        return AgentCard.from_json(self.agent_card_json)


class PeerStore:
    """SQLite-backed store of known Qi peers."""

    def __init__(self, db_path: str | Path = "~/.qi/peers.db"):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS peers (
                    node_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    master_name TEXT DEFAULT '',
                    agent_card_json TEXT NOT NULL,
                    added_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    trust_score REAL DEFAULT 0.0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS interactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    peer_node_id TEXT NOT NULL,
                    covenant_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    verdict TEXT,
                    timestamp REAL NOT NULL,
                    FOREIGN KEY (peer_node_id) REFERENCES peers(node_id)
                )
            """)
            conn.commit()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def add_peer(self, card: AgentCard) -> PeerRecord:
        """Add or update a peer from their Agent Card."""
        now = time.time()
        record = PeerRecord(
            node_id=card.node_id,
            name=card.name,
            master_name=card.master_name,
            agent_card_json=card.to_json(),
            added_at=now,
            last_seen_at=now,
        )
        with self._conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO peers
                (node_id, name, master_name, agent_card_json, added_at, last_seen_at, trust_score)
                VALUES (?, ?, ?, ?, COALESCE((SELECT added_at FROM peers WHERE node_id=?), ?), ?, 0.0)
            """, (record.node_id, record.name, record.master_name, record.agent_card_json,
                  record.node_id, now, now))
            conn.commit()
        return record

    def get_peer(self, node_id: str) -> PeerRecord | None:
        """Get a peer by Node ID."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM peers WHERE node_id = ?", (node_id,)
            ).fetchone()
            if row is None:
                return None
            return PeerRecord(
                node_id=row["node_id"],
                name=row["name"],
                master_name=row["master_name"],
                agent_card_json=row["agent_card_json"],
                added_at=row["added_at"],
                last_seen_at=row["last_seen_at"],
                trust_score=row["trust_score"],
            )

    def list_peers(self) -> list[PeerRecord]:
        """List all known peers."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM peers ORDER BY last_seen_at DESC"
            ).fetchall()
            return [
                PeerRecord(
                    node_id=r["node_id"],
                    name=r["name"],
                    master_name=r["master_name"],
                    agent_card_json=r["agent_card_json"],
                    added_at=r["added_at"],
                    last_seen_at=r["last_seen_at"],
                    trust_score=r["trust_score"],
                )
                for r in rows
            ]

    def remove_peer(self, node_id: str) -> bool:
        """Remove a peer. Returns False if not found."""
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM peers WHERE node_id = ?", (node_id,))
            conn.commit()
            return cur.rowcount > 0

    def touch(self, node_id: str):
        """Update last_seen_at for a peer."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE peers SET last_seen_at = ? WHERE node_id = ?",
                (time.time(), node_id),
            )
            conn.commit()

    def record_interaction(
        self,
        peer_node_id: str,
        covenant_id: str,
        mode: str,
        verdict: str | None = None,
    ):
        """Record a covenant interaction with a peer."""
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO interactions (peer_node_id, covenant_id, mode, verdict, timestamp)
                VALUES (?, ?, ?, ?, ?)
            """, (peer_node_id, covenant_id, mode, verdict, time.time()))
            conn.commit()

    def get_interactions(self, peer_node_id: str, limit: int = 50) -> list[dict]:
        """Get recent interactions with a peer."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM interactions WHERE peer_node_id = ? ORDER BY timestamp DESC LIMIT ?",
                (peer_node_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]
