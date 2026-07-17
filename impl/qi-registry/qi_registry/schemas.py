"""
Qi Registry — Pydantic schemas for API requests/responses.
"""

from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Any


class AgentCardRegister(BaseModel):
    """Request to register an Agent Card."""
    card_json: str = Field(..., description="Signed Agent Card JSON string")
    verify_signature: bool = Field(default=True, description="Verify Master signature before registering")


class AgentCardUpdate(BaseModel):
    """Request to update an Agent Card."""
    card_json: str = Field(..., description="Updated signed Agent Card JSON string")


class AgentSummary(BaseModel):
    """Public summary of a registered agent (safe for discovery)."""
    node_id: str
    name: str
    master_name: str
    description: str
    skills: list[str] = []
    verified: bool = False
    trust_score: float = 0.0
    interactions_count: float = 0
    registered_at: str = ""
    last_seen_at: str = ""

    class Config:
        from_attributes = True


class AgentDetail(AgentSummary):
    """Full agent detail including the complete Agent Card."""
    gb_identity_code: str = ""
    card_json: str = ""


class AgentListResponse(BaseModel):
    """Paginated agent list."""
    agents: list[AgentSummary]
    total: int
    page: int = 1
    page_size: int = 20
    has_more: bool = False


class StatsResponse(BaseModel):
    """Registry statistics."""
    total_agents: int
    active_agents: int
    verified_agents: int
    total_skills: int
    top_skills: list[dict[str, Any]] = []


class ErrorResponse(BaseModel):
    """Standard error response."""
    error: str
    detail: str = ""
