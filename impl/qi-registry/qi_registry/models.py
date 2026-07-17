"""
Qi Registry — Database models.

Stores Agent Cards, API keys, and audit logs.
"""

import datetime
import uuid
from sqlalchemy import create_engine, Column, String, Float, Boolean, DateTime, Text, Index
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from pathlib import Path


class Base(DeclarativeBase):
    pass


class AgentRecord(Base):
    """A registered Agent Card."""
    __tablename__ = "agents"

    # Primary identifier — node_id (did:key)
    node_id = Column(String(256), primary_key=True)

    # Optional GB/Z 185 identity code
    gb_identity_code = Column(String(256), nullable=True, index=True, default="")

    # Agent Card fields
    name = Column(String(256), nullable=False, index=True)
    master_name = Column(String(256), default="")
    description = Column(Text, default="")
    qi_version = Column(String(16), default="0.1")

    # Full Agent Card JSON (signed, original)
    card_json = Column(Text, nullable=False)

    # Skills as JSON array string (denormalized for search)
    skills_json = Column(Text, default="[]")

    # Status
    active = Column(Boolean, default=True)
    verified = Column(Boolean, default=False)  # Master signature verified

    # Timestamps
    registered_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)
    last_seen_at = Column(DateTime, default=datetime.datetime.utcnow)

    # Trust
    trust_score = Column(Float, default=0.0)
    interactions_count = Column(Float, default=0)

    __table_args__ = (
        Index("idx_agents_active_skills", "active", "skills_json"),
    )


class ApiKey(Base):
    """API key for authentication."""
    __tablename__ = "api_keys"

    key_hash = Column(String(128), primary_key=True)
    name = Column(String(256), default="")
    node_id = Column(String(256), nullable=True)  # Optional: bind to a specific agent
    permissions = Column(Text, default="read")  # "read" or "read,write,admin"
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class AuditLog(Base):
    """Audit log for registry operations."""
    __tablename__ = "audit_logs"

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex[:16])
    action = Column(String(64), nullable=False)  # register, update, delete, search
    node_id = Column(String(256), nullable=True)
    detail = Column(Text, default="")
    ip_address = Column(String(64), default="")
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)


def get_engine(db_path: str = "qi_registry.db"):
    """Create SQLite engine."""
    path = Path(db_path)
    return create_engine(
        f"sqlite:///{path.absolute()}",
        connect_args={"check_same_thread": False},
        echo=False,
    )


def init_db(db_path: str = "qi_registry.db"):
    """Create all tables."""
    engine = get_engine(db_path)
    Base.metadata.create_all(engine)
    return engine


def get_session(engine):
    """Create a new session."""
    Session = sessionmaker(bind=engine)
    return Session()
