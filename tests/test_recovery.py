"""Source recovery: the T2 W sent Play but its FLAC never arrived."""

import asyncio
import json
import time
import unittest
from urllib.parse import parse_qs, urlsplit

from fakes import FakeSonos
from t2w import T2WControl, T2WError, stream_request_path
from upnp import Renderer, Subscriptions
from test_upnp import T2W_URI, UUID, make_config


class FakeT2W:
    """Records pst:stream calls; can answer with a new Play like the real T2 W."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.renderer: Renderer | None = None
        self.error: Exception | None = None
        self.replay = True

    async def stream_to(self, renderer_udn: str) -> None:
        self.calls.append(renderer_udn)
        if self.error:
            raise self.error
        if self.replay and self.renderer is not None:
            await self.renderer.set_uri(T2W_URI, "")
            await self.renderer.play()


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sonos = FakeSonos()
        self.clients = 1
        self.audio_at: float | None = None
        self.subscriptions = Subscriptions()
        self.t2w = FakeT2W()

        async def relay_clients():
            return self.clients

        config = make_config(recovery_settle_seconds=0.01, recovery_play_wait_seconds=0.3, max_source_recoveries=2)
        self.renderer = Renderer(config, self.sonos, relay_clients, self.subscriptions, self.t2w, lambda: self.audio_at)
        self.t2w.renderer = self.renderer
        await self.renderer.set_uri(T2W_URI, "")
        await self.renderer.play()
        await self.renderer._start_task
        self.sonos.calls.clear()

    async def asyncTearDown(self):
        task = self.renderer._recovery_task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.subscriptions.close()

    async def check(self, after: float) -> None:
        await self.renderer.source_check(self.renderer._play_at + after)
        if self.renderer._recovery_task is not None:
            await self.renderer._recovery_task

    async def test_waits_while_the_t2w_is_still_starting(self):
        await self.check(15)
        self.assertEqual(self.t2w.calls, [])
        self.assertEqual(self.renderer.transport_state, "PLAYING")

    async def test_nothing_happens_when_audio_arrived(self):
        self.audio_at = time.monotonic()
        await self.check(60)
        self.assertEqual(self.t2w.calls, [])

    async def test_audio_from_before_the_play_does_not_count(self):
        self.audio_at = self.renderer._play_at - 5
        await self.check(21)
        self.assertEqual(self.t2w.calls, [UUID])

    async def test_restarts_the_t2w_stream_and_continues_with_its_new_play(self):
        first_play = self.renderer._play_at
        states = []
        original = self.renderer._set_transport

        def record(state, status="OK"):
            states.append(state)
            original(state, status)

        self.renderer._set_transport = record
        await self.check(21)
        self.assertEqual(self.t2w.calls, [UUID])
        # STOPPED first, so the T2 W releases its encoder; then its new Play.
        self.assertEqual(states[0], "STOPPED")
        self.assertGreater(self.renderer._play_at, first_play)
        self.assertTrue(self.renderer.session_active)
        await self.renderer._start_task
        self.assertEqual(self.renderer.transport_state, "PLAYING")
        self.assertEqual(self.renderer.source_recoveries, 1)
        self.assertIsNone(self.renderer.last_error)

    async def test_not_while_sonos_is_not_listening(self):
        self.clients = 0
        await self.check(30)
        self.assertEqual(self.t2w.calls, [])

    async def test_not_after_stop_or_pause(self):
        await self.renderer.pause()
        await self.check(30)
        await self.renderer.stop()
        await self.check(30)
        self.assertEqual(self.t2w.calls, [])

    async def test_gives_up_after_two_tries(self):
        for _ in range(3):
            await self.renderer._start_task
            await self.check(21)
        self.assertEqual(len(self.t2w.calls), 2)
        self.assertIn("select the speaker again", self.renderer.last_error)

    async def test_t2w_error_ends_the_session_cleanly(self):
        self.t2w.error = T2WError("nope")
        await self.check(21)
        self.assertFalse(self.renderer.session_active)
        self.assertEqual(self.renderer.transport_state, "STOPPED")
        self.assertIn("stop", self.sonos.names())
        self.assertIn("nope", self.renderer.last_error)

    async def test_no_new_play_ends_the_session_cleanly(self):
        self.t2w.replay = False
        await self.check(21)
        self.assertFalse(self.renderer.session_active)
        self.assertEqual(self.renderer.transport_state, "STOPPED")
        self.assertIn("did not start", self.renderer.last_error)

    async def test_disabled_without_t2w_client(self):
        renderer = Renderer(make_config(), FakeSonos(), self._one, self.subscriptions)
        await renderer.set_uri(T2W_URI, "")
        await renderer.play()
        await renderer._start_task
        await renderer.source_check(renderer._play_at + 60)
        self.assertIsNone(renderer._recovery_task)
        self.assertEqual(renderer.health()["t2w_stream_restart"], "off")

    @staticmethod
    async def _one():
        return 1


class T2WApiTests(unittest.IsolatedAsyncioTestCase):
    def test_request_is_the_apps_own_pst_stream_action(self):
        url = urlsplit(stream_request_path("uuid:6f1c2a4e-0b7d-5e93-a8c1-2d4f6e8a0b13"))
        query = parse_qs(url.query)
        self.assertEqual(url.path, "/api/setData")
        self.assertEqual(query["path"], ["pst:stream"])
        self.assertEqual(query["role"], ["activate"])
        self.assertEqual(
            json.loads(query["value"][0]),
            {"type": "string_", "string_": "uuid:6f1c2a4e-0b7d-5e93-a8c1-2d4f6e8a0b13"},
        )

    async def serve(self, status: str, body: bytes):
        requests = []

        async def handle(reader, writer):
            head = await reader.readuntil(b"\r\n\r\n")
            requests.append(head.decode().split("\r\n")[0])
            writer.write(f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.addAsyncCleanup(self._close, server)
        return T2WControl("127.0.0.1", server.sockets[0].getsockname()[1], timeout=2), requests

    @staticmethod
    async def _close(server):
        server.close()
        await server.wait_closed()

    async def test_success(self):
        client, requests = await self.serve("200 OK", b"[]")
        await client.stream_to(UUID)
        self.assertTrue(requests[0].startswith("GET /api/setData?path=pst:stream&role=activate&value="))

    async def test_error_in_body(self):
        client, _ = await self.serve("200 OK", b'{"error":{"name":"Error","message":"Device doesn\'t exist"}}')
        with self.assertRaises(T2WError):
            await client.stream_to(UUID)

    async def test_http_error(self):
        client, _ = await self.serve("500 Internal Server Error", b"")
        with self.assertRaises(T2WError):
            await client.stream_to(UUID)


if __name__ == "__main__":
    unittest.main()
