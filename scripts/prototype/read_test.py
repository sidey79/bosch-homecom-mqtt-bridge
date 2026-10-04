"""Read all data of the Tronic (wddw2) via the HomeCom Easy cloud, using the saved refresh token."""
import asyncio
import dataclasses
import json
import sys
from pathlib import Path

from aiohttp import ClientSession
from homecom_alt import ConnectionOptions, HomeComWddw2

TOKEN_FILE = Path(__file__).resolve().parents[2] / ".tokens.json"
DEVICE_ID = sys.argv[1] if len(sys.argv) == 2 else sys.exit("usage: read_test.py <deviceId>")


async def main() -> None:
    opts = ConnectionOptions(refresh_token=json.loads(TOKEN_FILE.read_text())["refresh_token"], auth_provider=True)
    async with ClientSession() as session:
        api = HomeComWddw2(session, opts, DEVICE_ID, True)
        try:
            data = await api.async_update(DEVICE_ID)
        finally:
            # refresh tokens are single-use: always persist the rotated one
            TOKEN_FILE.write_text(json.dumps({"refresh_token": api.refresh_token}))
        print(json.dumps(dataclasses.asdict(data), indent=2, default=str))


asyncio.run(main())
