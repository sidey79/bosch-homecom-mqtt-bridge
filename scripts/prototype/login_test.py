"""Feasibility prototype only (uses the library's public fixed PKCE verifier; the bridge will not).

Two-step login test against the Bosch HomeCom Easy cloud (SingleKey ID).

  python scripts/prototype/login_test.py url            -> prints the login URL to open in a browser
  python scripts/prototype/login_test.py code <CODE>    -> exchanges the code, lists gateways, saves tokens
"""
import asyncio
import base64
import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlencode

from aiohttp import ClientSession
from homecom_alt import ConnectionOptions, HomeComAlt
from homecom_alt.const import (
    OAUTH_BROWSER_VERIFIER,
    OAUTH_DOMAIN,
    OAUTH_LOGIN,
    OAUTH_LOGIN_PARAMS,
)

TOKEN_FILE = Path(__file__).resolve().parents[2] / ".tokens.json"


def login_url() -> str:
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(OAUTH_BROWSER_VERIFIER.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    params = {**OAUTH_LOGIN_PARAMS, "code_challenge": challenge}
    return OAUTH_DOMAIN + OAUTH_LOGIN + "?" + urlencode(params)


async def exchange(code: str) -> None:
    async with ClientSession() as session:
        opts = ConnectionOptions(code=code, auth_provider=True)
        api = HomeComAlt(session, opts, True)
        ok = await api.get_token()
        print("Token erhalten:", ok)
        fd = os.open(TOKEN_FILE, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"refresh_token": api.refresh_token}, fh)
        devices = await api.async_get_devices()
        print(json.dumps(await devices if asyncio.iscoroutine(devices) else devices, indent=2))


if __name__ == "__main__":
    if sys.argv[1:2] == ["url"]:
        print(login_url())
    elif sys.argv[1:2] == ["code"] and len(sys.argv) == 3:
        asyncio.run(exchange(sys.argv[2]))
    else:
        sys.exit(__doc__)
