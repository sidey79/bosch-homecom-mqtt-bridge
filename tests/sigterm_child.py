"""Child process of tests/test_shutdown.py: the real ``run`` command with fakes instead of cloud and broker.

The token endpoint answers after 10 s; ``CHILD_MARKER`` is written when the refresh POST starts.
"""
import asyncio
import os
import sys
import time
from pathlib import Path

from bosch_homecom_mqtt_bridge import __main__ as entry
from bosch_homecom_mqtt_bridge import app
from bosch_homecom_mqtt_bridge.publisher import MqttPublisher

from tests.test_protocol import FakeClient
from tests.token_fakes import TOKEN_URL, FakeResponse, make_jwt

marker = Path(os.environ["CHILD_MARKER"])


class SlowTokenSession:
    async def request(self, method, url, **kwargs):
        if url == TOKEN_URL:
            marker.write_text("posting")
            await asyncio.sleep(10)
            now = time.time()
            return FakeResponse(
                200,
                {"access_token": make_jwt(now, 3600), "refresh_token": "FAKE-refresh-after-sigterm", "expires_in": 3600},
            )
        return FakeResponse(200, [])  # discovery: no devices

    async def close(self):
        pass


app.ClientSession = SlowTokenSession
app.MqttPublisher = lambda config: MqttPublisher(config, client_factory=FakeClient)
sys.exit(entry.main([]))
