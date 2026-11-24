"""The API keys the agents run on, for the window and for a linked phone.

The keys live encrypted in the desktop app (the OS keychain, which only the app can open). So this API never holds
a key it can show: it reports each key's last four characters, and hands a new one to the app's control server
(desktop/src/main.js), which checks it with its provider, keeps it and restarts the services that read it."""

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..config import settings

router = APIRouter(prefix="/api/keys")

PROVIDERS = ("deepseek", "openai", "anthropic")


def masked() -> dict[str, str]:
    """Each provider's key as its last four characters ("…abcd"), or "" when there is none."""
    keys = {
        "deepseek": settings.deepseek_api_key,
        "openai": settings.openai_api_key,
        "anthropic": settings.anthropic_api_key,
    }
    return {provider: f"…{key[-4:]}" if key else "" for provider, key in keys.items()}


@router.get("")
def keys():
    return masked()


class Key(BaseModel):
    key: str = Field(default="", max_length=1024)  # empty removes the key


@router.put("/{provider}")
async def save(provider: str, body: Key):
    """Check the key with its provider, then keep it. The services restart a moment after the answer is sent, so a
    phone that asked sees the answer first and then reconnects."""
    if provider not in PROVIDERS:
        raise HTTPException(404, "Unknown provider")
    if not settings.desktop_control_url:
        raise HTTPException(501, "Keys are set in the penrose app on your computer")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{settings.desktop_control_url}/keys",
                headers={"Authorization": f"Bearer {settings.desktop_control_token}"},
                json={"provider": provider, "key": body.key},
            )
        result = response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, "penrose on your computer could not be reached") from None
    if not response.is_success or not result.get("ok"):
        raise HTTPException(400, result.get("error") or "The key could not be saved.")
    return {"ok": True, "keys": result.get("keys") or masked()}
