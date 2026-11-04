import asyncio
import contextlib
import json
import socket
import time
import unittest

from fakes import FakeSonos
from main import room_key, room_uuid, run, settings_from_environment
from soap import parse_envelope, request_envelope
from stream import RelayConfig, RelayService
from test_stream import FFMPEG, decode_flac, encode_flac, random_pcm, streaminfo
from upnp import AVTRANSPORT, RendererConfig


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def http(port, method, path, headers=None, body=b""):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    head = f"{method} {path} HTTP/1.1\r\nHost: x\r\nContent-Length: {len(body)}\r\n"
    head += "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
    writer.write(head.encode() + b"\r\n" + body)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    return data.partition(b"\r\n\r\n")[2]


class ConfigTests(unittest.TestCase):
    def test_nothing_has_to_be_configured(self):
        settings = settings_from_environment({"ADVERTISED_HOST": "192.168.1.116"})
        self.assertEqual(settings.relay.upstream_url, "")  # T2 W is searched for
        self.assertEqual(settings.t2w_host, "")
        self.assertEqual(settings.speakers.include, ())
        self.assertEqual(settings.speakers.relay_url, "http://192.168.1.116:8186/stream.flac")
        self.assertTrue(settings.watchdog)
        self.assertTrue(settings.source_recovery)

    def test_optional_settings(self):
        settings = settings_from_environment(
            {
                "T2W_HOST": "192.168.1.228",
                "SONOS_ROOMS": "Büro, Studio-Ton",
                "SONOS_EXCLUDE": "Bad",
                "ADVERTISED_HOST": "192.168.1.116",
                "SONOS_SEED_HOSTS": "192.168.1.53",
                "SOURCE_RECOVERY": "off",
            }
        )
        self.assertEqual(settings.relay.upstream_url, "http://192.168.1.228:8146/stream.flac")
        self.assertEqual(settings.speakers.include, ("Büro", "Studio-Ton"))
        self.assertEqual(settings.speakers.exclude, ("Bad",))
        self.assertEqual(settings.seed_hosts, ["192.168.1.53"])
        self.assertFalse(settings.source_recovery)

    def test_fixed_stream_url_also_names_the_turntable(self):
        settings = settings_from_environment(
            {"ADVERTISED_HOST": "192.168.1.116", "T2W_FLAC_URL": "http://192.168.1.240:8146/stream.flac"}
        )
        self.assertEqual(settings.t2w_host, "192.168.1.240")
        self.assertEqual(settings.relay.upstream_url, "http://192.168.1.240:8146/stream.flac")

    def test_room_ids_and_uuids_are_stable(self):
        self.assertEqual(room_key("Büro"), "buero")
        self.assertEqual(room_key("RINCON_000E58A0B1C201400"), "rincon-000e58a0b1c201400")
        self.assertEqual(room_uuid("uuid:0dfbc94a-1931-4451-9a64-25a9269ad458", "RINCON_A"),
                         room_uuid("uuid:0dfbc94a-1931-4451-9a64-25a9269ad458", "rincon_a"))


class T2WSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_finds_and_follows_the_t2w(self):
        from main import keep_t2w_found
        from t2w import T2WControl

        answers = [[], ["192.168.1.228"], ["192.168.1.230"]]

        async def search(_local_ip):
            return answers.pop(0) if answers else ["192.168.1.230"]

        relay = RelayService(RelayConfig(upstream_url="", ffmpeg="ffmpeg"))
        control = T2WControl("")
        task = asyncio.create_task(keep_t2w_found(relay, control, "127.0.0.1", search, 0.01, 0.01))
        try:
            deadline = time.monotonic() + 2
            while relay.upstream_url != "http://192.168.1.230:8146/stream.flac" and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
        self.assertEqual(relay.upstream_url, "http://192.168.1.230:8146/stream.flac")
        self.assertEqual(control.host, "192.168.1.230")


@unittest.skipUnless(FFMPEG, "ffmpeg not available")
class EndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_t2w_play_reaches_sonos_as_lossless_stream(self):
        source_pcm = random_pcm(1.0)
        source_flac = encode_flac(source_pcm)

        async def t2w(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            with contextlib.suppress(ConnectionError):
                writer.write(b"HTTP/1.0 200 OK\r\nContent-Type: audio/flac\r\n\r\n" + source_flac)
                await writer.drain()
                await asyncio.sleep(3)
            writer.close()

        t2w_server = await asyncio.start_server(t2w, "127.0.0.1", 0)
        self.addCleanup(t2w_server.close)
        relay_port, renderer_port = free_port(), free_port()
        relay_config = RelayConfig(
            upstream_url=f"http://127.0.0.1:{t2w_server.sockets[0].getsockname()[1]}/stream.flac",
            host="127.0.0.1",
            port=relay_port,
            buffer_seconds=0.3,
            idle_grace_seconds=0.5,
            ffmpeg=FFMPEG,
        )
        renderer_config = RendererConfig(
            friendly_name="T2 W Relay – Test",
            uuid="uuid:00000000-0000-0000-0000-000000000001",
            advertised_host="127.0.0.1",
            http_port=renderer_port,
            relay_url=f"http://127.0.0.1:{relay_port}/stream.flac",
            key="test",
        )

        received = bytearray()
        listener: list[asyncio.Task] = []

        async def sonos_fetches_stream(uri):
            async def fetch():
                reader, writer = await asyncio.open_connection("127.0.0.1", relay_port)
                writer.write(b"GET /stream.flac HTTP/1.1\r\nHost: sonos\r\n\r\n")
                await writer.drain()
                try:
                    while chunk := await reader.read(65536):
                        received.extend(chunk)
                finally:
                    writer.close()

            listener.append(asyncio.create_task(fetch()))

        sonos = FakeSonos()
        sonos.on_play = sonos_fetches_stream
        stop = asyncio.Event()
        app = asyncio.create_task(run(relay_config, [(renderer_config, sonos)], watchdog=True, stop=stop, ssdp=False))
        try:
            await asyncio.sleep(0.2)

            async def soap(action, values):
                body = await http(
                    renderer_port,
                    "POST",
                    "/dev/test/upnp/control/avtransport",
                    {"SOAPACTION": f'"{AVTRANSPORT}#{action}"'},
                    request_envelope(action, AVTRANSPORT, values),
                )
                return parse_envelope(body)

            await soap("SetAVTransportURI", {"InstanceID": "0", "CurrentURI": "http://192.168.1.228:8146/stream.flac", "CurrentURIMetaData": ""})
            self.assertEqual((await soap("Play", {"InstanceID": "0", "Speed": "1"}))[0], "PlayResponse")

            deadline = time.monotonic() + 6
            while time.monotonic() < deadline and source_pcm not in decode_flac(bytes(received).partition(b"\r\n\r\n")[2]):
                await asyncio.sleep(0.3)
            body = bytes(received).partition(b"\r\n\r\n")[2]
            self.assertEqual(streaminfo(body), (48000, 2, 24))
            self.assertIn(source_pcm, decode_flac(body), "Sonos did not receive the T2 W audio bit-for-bit")

            health = json.loads(await http(relay_port, "GET", "/healthz"))
            self.assertEqual(health["rooms"][0]["renderer_state"], "PLAYING")
            self.assertTrue(health["rooms"][0]["turntable_session"])
            self.assertEqual(health["clients"], 1)

            await soap("Stop", {"InstanceID": "0"})
            self.assertEqual(sonos.command_names(), ["play_uri", "stop"])
        finally:
            stop.set()
            for task in listener:
                task.cancel()
            await asyncio.wait_for(app, 10)


if __name__ == "__main__":
    unittest.main()
