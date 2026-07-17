"""
Qi Registry — FastAPI application.

Provides:
  - Agent Card registration, update, deletion (GB/Z 185 Part 4: 智能体描述管理方)
  - Agent discovery and search (GB/Z 185 Part 5: 智能体发现服务)
  - Statistics
  - Health check
"""

import json
import datetime
import logging
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from .models import init_db, get_engine, get_session, AgentRecord, AuditLog
from .schemas import (
    AgentCardRegister, AgentCardUpdate,
    AgentSummary, AgentDetail, AgentListResponse,
    StatsResponse, ErrorResponse,
)

# ─── Config ────────────────────────────────────────────────

import os
DB_PATH = os.environ.get("QI_REGISTRY_DB", "qi_registry.db")
API_KEY = os.environ.get("QI_REGISTRY_API_KEY", "")  # "" = no auth required
HOST = os.environ.get("QI_REGISTRY_HOST", "0.0.0.0")
PORT = int(os.environ.get("QI_REGISTRY_PORT", "7881"))

logger = logging.getLogger("qi-registry")


# ─── Lifespan ──────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = init_db(DB_PATH)
    app.state.engine = engine
    logger.info(f"Qi Registry started. DB: {DB_PATH}")
    yield


app = FastAPI(
    title="Qi Registry",
    description="契网注册中心 — Agent Card registration and discovery",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Dependencies ──────────────────────────────────────────

def get_db() -> Session:
    engine = app.state.engine
    session = get_session(engine)
    try:
        yield session
    finally:
        session.close()


def verify_auth(request: Request):
    """Simple API key auth if configured."""
    if not API_KEY:
        return
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {API_KEY}":
        raise HTTPException(status_code=401, detail="Invalid API key")


def log_action(db: Session, action: str, node_id: str = "", detail: str = "", ip: str = ""):
    """Record an audit log entry."""
    try:
        log = AuditLog(action=action, node_id=node_id, detail=detail[:500], ip_address=ip)
        db.add(log)
        db.commit()
    except Exception:
        pass


# ─── Health ─────────────────────────────────────────────────

@app.get("/")
def root():
    return {"service": "qi-registry", "version": "0.1.0", "protocol": "Qi Network"}


@app.get("/health")
def health():
    return {"status": "ok"}


# ─── Agent CRUD ────────────────────────────────────────────

@app.post("/api/v1/agents", status_code=201)
def register_agent(
    body: AgentCardRegister,
    db: Session = Depends(get_db),
    request: Request = None,
):
    """Register an Agent Card. GB/Z 185 Part 4: 智能体描述管理方 - 注册."""
    verify_auth(request) if request else None

    # Parse and validate
    try:
        card = _parse_card(body.card_json)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid Agent Card JSON: {e}")

    # Check if already exists
    existing = db.query(AgentRecord).filter(AgentRecord.node_id == card["node_id"]).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Agent {card['node_id'][:20]}... already registered")

    # Verify signature if requested
    verified = False
    if body.verify_signature:
        verified = _verify_card_signature(body.card_json)

    # Extract skills for search
    skills = _extract_skills(body.card_json)

    record = AgentRecord(
        node_id=card["node_id"],
        gb_identity_code=card.get("gb_identity_code", ""),
        name=card.get("identity", {}).get("name", ""),
        master_name=card.get("identity", {}).get("master_name", ""),
        description=card.get("identity", {}).get("description", ""),
        card_json=body.card_json,
        skills_json=json.dumps(skills),
        verified=verified,
        registered_at=datetime.datetime.utcnow(),
        last_seen_at=datetime.datetime.utcnow(),
    )
    db.add(record)
    db.commit()

    log_action(db, "register", card["node_id"], f"name={record.name} verified={verified}",
               request.client.host if request else "")

    return {
        "status": "registered",
        "node_id": record.node_id,
        "verified": verified,
    }


@app.get("/api/v1/agents/{node_id}")
def get_agent(node_id: str, db: Session = Depends(get_db)):
    """Get a single agent's full details."""
    record = db.query(AgentRecord).filter(
        AgentRecord.node_id == node_id, AgentRecord.active == True
    ).first()
    if not record:
        raise HTTPException(status_code=404, detail="Agent not found")

    return AgentDetail(
        node_id=record.node_id,
        gb_identity_code=record.gb_identity_code,
        name=record.name,
        master_name=record.master_name,
        description=record.description,
        skills=json.loads(record.skills_json),
        verified=record.verified,
        trust_score=record.trust_score,
        interactions_count=record.interactions_count,
        registered_at=record.registered_at.isoformat() if record.registered_at else "",
        last_seen_at=record.last_seen_at.isoformat() if record.last_seen_at else "",
        card_json=record.card_json,
    )


@app.put("/api/v1/agents/{node_id}")
def update_agent(
    node_id: str,
    body: AgentCardUpdate,
    db: Session = Depends(get_db),
    request: Request = None,
):
    """Update an Agent Card. GB/Z 185 Part 4: 变更."""
    verify_auth(request) if request else None

    record = db.query(AgentRecord).filter(AgentRecord.node_id == node_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="Agent not found")

    try:
        card = _parse_card(body.card_json)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid Agent Card JSON: {e}")

    skills = _extract_skills(body.card_json)
    verified = _verify_card_signature(body.card_json)

    record.card_json = body.card_json
    record.skills_json = json.dumps(skills)
    record.name = card.get("identity", {}).get("name", record.name)
    record.description = card.get("identity", {}).get("description", record.description)
    record.verified = verified
    record.updated_at = datetime.datetime.utcnow()
    db.commit()

    log_action(db, "update", node_id, f"verified={verified}",
               request.client.host if request else "")

    return {"status": "updated", "node_id": node_id, "verified": verified}


@app.delete("/api/v1/agents/{node_id}", status_code=204)
def delete_agent(
    node_id: str,
    db: Session = Depends(get_db),
    request: Request = None,
):
    """Deactivate an Agent Card."""
    verify_auth(request) if request else None

    record = db.query(AgentRecord).filter(AgentRecord.node_id == node_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="Agent not found")

    record.active = False
    db.commit()
    log_action(db, "delete", node_id, ip=request.client.host if request else "")


# ─── Discovery / Search ────────────────────────────────────

@app.get("/api/v1/agents", response_model=AgentListResponse)
def list_agents(
    q: str = Query(default="", description="Natural language query (searches name + description + skills)"),
    skill: str = Query(default="", description="Filter by skill ID or name"),
    verified: bool | None = Query(default=None, description="Filter by verification status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Search and list agents. GB/Z 185 Part 5: 智能体发现服务."""
    query = db.query(AgentRecord).filter(AgentRecord.active == True)

    # Full-text-like search across name, description, skills
    if q:
        like = f"%{q}%"
        query = query.filter(
            (AgentRecord.name.ilike(like)) |
            (AgentRecord.description.ilike(like)) |
            (AgentRecord.skills_json.ilike(like))
        )

    # Filter by skill
    if skill:
        query = query.filter(AgentRecord.skills_json.ilike(f"%{skill}%"))

    # Filter by verification
    if verified is not None:
        query = query.filter(AgentRecord.verified == verified)

    total = query.count()
    offset = (page - 1) * page_size
    records = query.order_by(AgentRecord.trust_score.desc()).offset(offset).limit(page_size).all()

    agents = [
        AgentSummary(
            node_id=r.node_id,
            name=r.name,
            master_name=r.master_name,
            description=r.description,
            skills=json.loads(r.skills_json),
            verified=r.verified,
            trust_score=r.trust_score,
            interactions_count=r.interactions_count,
            registered_at=r.registered_at.isoformat() if r.registered_at else "",
            last_seen_at=r.last_seen_at.isoformat() if r.last_seen_at else "",
        )
        for r in records
    ]

    return AgentListResponse(
        agents=agents,
        total=total,
        page=page,
        page_size=page_size,
        has_more=offset + page_size < total,
    )


# ─── Statistics ────────────────────────────────────────────

@app.get("/api/v1/stats", response_model=StatsResponse)
def get_stats(db: Session = Depends(get_db)):
    """Get registry statistics."""
    total = db.query(AgentRecord).count()
    active = db.query(AgentRecord).filter(AgentRecord.active == True).count()
    verified_count = db.query(AgentRecord).filter(AgentRecord.active == True, AgentRecord.verified == True).count()

    # Aggregate skills
    all_records = db.query(AgentRecord.skills_json).filter(AgentRecord.active == True).all()
    skill_counts: dict[str, int] = {}
    for (skills_json,) in all_records:
        try:
            for s in json.loads(skills_json):
                skill_counts[s] = skill_counts.get(s, 0) + 1
        except Exception:
            pass

    top_skills = sorted(
        [{"name": k, "count": v} for k, v in skill_counts.items()],
        key=lambda x: x["count"], reverse=True
    )[:20]

    return StatsResponse(
        total_agents=total,
        active_agents=active,
        verified_agents=verified_count,
        total_skills=len(skill_counts),
        top_skills=top_skills,
    )


# ─── Helpers ────────────────────────────────────────────────

def _parse_card(card_json: str) -> dict:
    """Parse and validate Agent Card JSON."""
    try:
        card = json.loads(card_json)
    except json.JSONDecodeError:
        raise ValueError("Invalid JSON")

    if "node_id" not in card:
        raise ValueError("Missing 'node_id' field")

    return card


def _verify_card_signature(card_json: str) -> bool:
    """Verify Master signature on Agent Card."""
    try:
        from qinetwork import AgentCard
        card = AgentCard.from_json(card_json)
        raw = json.loads(card_json)
        sig_data = raw.get("signatures", {}).get("master", {})
        if not sig_data:
            return False
        return card.verify(sig_data.get("signature", ""))
    except Exception:
        return False


def _extract_skills(card_json: str) -> list[str]:
    """Extract skill names from Agent Card for search indexing."""
    try:
        raw = json.loads(card_json)
        skills = raw.get("capabilities", {}).get("skills", [])
        return [s.get("name", s.get("id", "")) for s in skills if s.get("name") or s.get("id")]
    except Exception:
        return []


# ─── Run ───────────────────────────────────────────────────

def main():
    import uvicorn
    uvicorn.run("qi_registry.main:app", host=HOST, port=PORT, reload=False)


if __name__ == "__main__":
    main()
