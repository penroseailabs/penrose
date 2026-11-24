"""The models a task can run on (agents/catalog.py), for the composer's picker."""

from fastapi import APIRouter

from ..agents.catalog import listing

router = APIRouter(prefix="/api/models")


@router.get("")
def models():
    """The picks on offer for the agent and the subagent, each model's efforts, and every provider's default picks."""
    return listing()
