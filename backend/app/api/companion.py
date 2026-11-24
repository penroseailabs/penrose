"""The window's side of linking phones (app/companion): a QR code to show, the phones waiting for the user's
go-ahead, and the phones already linked. Only the window may call these, never a phone."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..companion import tunnel as companion_tunnel

router = APIRouter(prefix="/api/companion")


def require():
    if companion_tunnel.current is None:
        raise HTTPException(503, "The companion is not running")
    return companion_tunnel.current


class Origin(BaseModel):
    origin: str


@router.put("/origin")
async def set_origin(body: Origin):
    """Where the coordinator is deployed (the desktop app deploys it, then tells the API here)."""
    tunnel = require()
    origin = body.origin.strip().rstrip("/")
    if origin and not origin.startswith(("https://", "http://")):
        raise HTTPException(400, "The coordinator address must start with https://")
    tunnel.identity.set_origin(origin)
    await tunnel.restart()
    tunnel.changed()
    return tunnel.status()


@router.get("")
def status():
    tunnel = require()
    return {**tunnel.status(), "phones": tunnel.identity.phones()}


@router.post("/pairings", status_code=201)
def new_pairing():
    tunnel = require()
    if not tunnel.identity.origin:
        raise HTTPException(503, "The companion is not set up on this computer")
    if not tunnel.connected:
        raise HTTPException(503, "penrose is not connected to its coordinator yet")
    return tunnel.identity.new_pairing()


class Decision(BaseModel):
    allow: bool


@router.post("/pending/{approval_id}")
async def decide(approval_id: str, body: Decision):
    tunnel = require()
    if not await tunnel.decide(approval_id, body.allow):
        raise HTTPException(404, "That phone is no longer waiting")
    return {"ok": True}


@router.delete("/phones/{phone_id}")
async def forget(phone_id: str):
    tunnel = require()
    if not tunnel.identity.forget(phone_id):
        raise HTTPException(404, "Phone not found")
    for peer in list(tunnel.peers.values()):
        if peer.phone_id == phone_id:
            await tunnel.drop(peer)
    tunnel.changed()
    return {"ok": True}
