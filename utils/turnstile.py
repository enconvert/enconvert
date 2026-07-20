"""
Cloudflare Turnstile verification utility.
"""
import os
import httpx
from fastapi import HTTPException

# Always-passes test key for local dev: https://developers.cloudflare.com/turnstile/troubleshooting/testing/
TURNSTILE_SECRET_KEY = os.getenv("TURNSTILE_SECRET_KEY", "1x0000000000000000000000000000000AA")

VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


async def verify_turnstile(token: str, remote_ip: str | None = None) -> bool:
    payload = {"secret": TURNSTILE_SECRET_KEY, "response": token}
    if remote_ip:
        payload["remoteip"] = remote_ip

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(VERIFY_URL, data=payload)
            result = resp.json()
        except (httpx.RequestError, ValueError):
            raise HTTPException(status_code=503, detail="Turnstile verification unavailable")

    if not result.get("success"):
        raise HTTPException(status_code=400, detail="Turnstile verification failed")

    return True
