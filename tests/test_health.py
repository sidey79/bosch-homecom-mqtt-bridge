import asyncio
import unittest

from bosch_homecom_mqtt_bridge.health import HealthServer


async def request(port: int, raw: bytes) -> tuple[int, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(raw)
    await writer.drain()
    data = await reader.read()
    writer.close()
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split()[1]), body


class HealthTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.state = "starting"
        self.server = HealthServer(0, lambda: self.state, host="127.0.0.1")
        await self.server.start()

    async def asyncTearDown(self) -> None:
        await self.server.stop()

    async def get(self, path: str, method: str = "GET") -> tuple[int, bytes]:
        return await request(self.server.port, f"{method} {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())

    async def test_healthz_is_always_200(self) -> None:
        for state in ("starting", "ready", "auth_required", "error", "disconnected"):
            with self.subTest(state=state):
                self.state = state
                self.assertEqual((await self.get("/healthz"))[0], 200)

    async def test_readyz_is_200_only_in_ready(self) -> None:
        for state, code in (
            ("starting", 503),
            ("auth_required", 503),
            ("disconnected", 503),
            ("error", 503),
            ("ready", 200),
        ):
            with self.subTest(state=state):
                self.state = state
                status, body = await self.get("/readyz")
                self.assertEqual(status, code)
                self.assertIn(state.encode(), body)

    async def test_other_requests(self) -> None:
        self.assertEqual((await self.get("/other"))[0], 404)
        self.assertEqual((await self.get("/healthz", "POST"))[0], 405)
        status, body = await self.get("/healthz", "HEAD")
        self.assertEqual((status, body), (200, b""))
        self.assertEqual((await self.get("/readyz?x=1"))[0], 503)

    async def test_broken_clients_do_not_disturb_the_server(self) -> None:
        self.assertEqual((await request(self.server.port, b"\x00\xff garbage\r\n\r\n"))[0], 405)
        reader, writer = await asyncio.open_connection("127.0.0.1", self.server.port)
        writer.write(b"GET /healthz HTTP/1.1\r\n" + b"X: " + b"a" * 20000 + b"\r\n\r\n")
        await writer.drain()
        await reader.read()
        writer.close()
        self.assertEqual((await self.get("/healthz"))[0], 200)


if __name__ == "__main__":
    unittest.main()
