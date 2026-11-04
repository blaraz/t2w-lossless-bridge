import asyncio
import time
import unittest
from xml.etree import ElementTree as ET

from fakes import FakeSonos
from soap import parse_envelope, parse_fault, request_envelope
from upnp import (
    SERVICES,
    Renderer,
    RendererConfig,
    RendererServer,
    Subscriptions,
    build_device_description,
    build_notify,
    build_scpd,
    build_search_response,
    is_lan_address,
    parse_callbacks,
    parse_search,
    parse_timeout,
    search_targets,
    ssdp_targets,
)

UUID = "uuid:11111111-2222-3333-4444-555555555555"
RELAY_URL = "http://192.168.1.116:8186/stream.flac"
T2W_URI = "http://192.168.1.228:8146/stream.flac"
NS = {"d": "urn:schemas-upnp-org:device-1-0", "s": "urn:schemas-upnp-org:service-1-0"}


def make_config(**overrides):
    settings = dict(
        friendly_name="T2 W Relay – Büro",
        uuid=UUID,
        advertised_host="192.168.1.116",
        http_port=8090,
        relay_url=RELAY_URL,
        watchdog_grace_seconds=10,
        max_restarts=3,
        restart_window_seconds=600,
        key="buero",
    )
    settings.update(overrides)
    return RendererConfig(**settings)


class DescriptionTests(unittest.TestCase):
    def test_device_description_lists_all_three_services(self):
        root = ET.fromstring(build_device_description(make_config()))
        types = [node.text for node in root.iter(f"{{{NS['d']}}}serviceType")]
        self.assertEqual(types, [spec.service_type for spec in SERVICES.values()])
        self.assertEqual(root.find(".//d:UDN", NS).text, UUID)
        self.assertEqual(root.find(".//d:friendlyName", NS).text, "T2 W Relay – Büro")
        self.assertEqual(root.find(".//d:SCPDURL", NS).text, "/dev/buero/upnp/avtransport.xml")
        # Required by the T2 W firmware to hand out its lossless FLAC stream.
        self.assertIn("Pro-Ject Audio Systems", root.find(".//d:manufacturer", NS).text)

    def test_every_scpd_is_complete_and_consistent(self):
        for spec in SERVICES.values():
            root = ET.fromstring(build_scpd(spec))
            variables = {node.text for node in root.iterfind(".//s:stateVariable/s:name", NS)}
            self.assertEqual(variables, set(spec.variables), spec.key)
            for related in root.iterfind(".//s:relatedStateVariable", NS):
                self.assertIn(related.text, variables, spec.key)
            for action in spec.actions:
                self.assertTrue(hasattr(Renderer, f"_{spec.key}_{action}"), f"no handler for {spec.key}.{action}")


class SsdpTests(unittest.TestCase):
    def test_ssdp_all_answers_once_per_device_and_service(self):
        targets = search_targets("ssdp:all", UUID)
        self.assertEqual(len(targets), 6)
        self.assertEqual(targets, ssdp_targets(UUID))

    def test_responses_echo_the_search_target_with_correct_usn(self):
        config = make_config()
        rootdevice = build_search_response("upnp:rootdevice", config).decode()
        self.assertIn("ST: upnp:rootdevice\r\n", rootdevice)
        self.assertIn(f"USN: {UUID}::upnp:rootdevice\r\n", rootdevice)
        by_uuid = build_search_response(UUID, config).decode()
        self.assertIn(f"USN: {UUID}\r\n", by_uuid)
        self.assertIn("LOCATION: http://192.168.1.116:8090/dev/buero/description.xml", by_uuid)
        self.assertEqual(search_targets("urn:schemas-upnp-org:device:Printer:1", UUID), [])

    def test_parse_search(self):
        request = b'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\nMX: 3\r\nST: ssdp:all\r\n\r\n'
        self.assertEqual(parse_search(request), ("ssdp:all", 3))
        self.assertIsNone(parse_search(request.replace(b"ssdp:discover", b"other")))
        self.assertIsNone(parse_search(b"NOTIFY * HTTP/1.1\r\n\r\n"))

    def test_byebye_has_no_location(self):
        config = make_config()
        self.assertIn(b"LOCATION:", build_notify("upnp:rootdevice", config, "ssdp:alive"))
        self.assertNotIn(b"LOCATION:", build_notify("upnp:rootdevice", config, "ssdp:byebye"))


class SsdpNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_answers_an_m_search_over_udp(self):
        from upnp import SSDPProtocol

        loop = asyncio.get_running_loop()
        studio = make_config(key="studio-ton", uuid="uuid:99999999-2222-3333-4444-555555555555")
        responder, _ = await loop.create_datagram_endpoint(
            lambda: SSDPProtocol([make_config(), studio]), local_addr=("127.0.0.1", 0)
        )
        self.addCleanup(responder.close)
        answers: asyncio.Queue[bytes] = asyncio.Queue()

        class Collector(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                answers.put_nowait(data)

        searcher, _ = await loop.create_datagram_endpoint(Collector, local_addr=("127.0.0.1", 0))
        self.addCleanup(searcher.close)
        searcher.sendto(
            b'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\nMX: 1\r\nST: ssdp:all\r\n\r\n',
            responder.get_extra_info("sockname"),
        )
        received = [await asyncio.wait_for(answers.get(), 3) for _ in range(12)]
        usns = {line.split(":", 1)[1].strip() for data in received for line in data.decode().split("\r\n") if line.startswith("USN:")}
        self.assertIn(UUID, usns)
        self.assertIn(f"{UUID}::urn:schemas-upnp-org:device:MediaRenderer:1", usns)
        self.assertIn(studio.uuid, usns)
        self.assertEqual(len(usns), 12)
        locations = {line.split(":", 1)[1].strip() for data in received for line in data.decode().split("\r\n") if line.startswith("LOCATION:")}
        self.assertEqual(locations, {make_config().location, studio.location})


class GenaParsingTests(unittest.TestCase):
    def test_callbacks_must_be_http_on_the_lan(self):
        self.assertEqual(
            parse_callbacks("<http://192.168.1.228:49152/evt><https://192.168.1.2/x><http://8.8.8.8/x>"),
            ["http://192.168.1.228:49152/evt"],
        )
        self.assertTrue(is_lan_address("192.168.1.5"))
        self.assertFalse(is_lan_address("8.8.8.8"))

    def test_timeout_is_clamped(self):
        self.assertEqual(parse_timeout("Second-1800"), 1800)
        self.assertEqual(parse_timeout("Second-5"), 60)
        self.assertEqual(parse_timeout("infinite"), 1800)


class RendererServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sonos = FakeSonos()
        self.clients = 0
        self.subscriptions = Subscriptions()

        async def relay_clients():
            return self.clients

        self.renderer = Renderer(make_config(), self.sonos, relay_clients, self.subscriptions)
        self.studio_sonos = FakeSonos()
        self.studio = Renderer(
            make_config(key="studio-ton", friendly_name="T2 W – Studio-Ton", uuid="uuid:99999999-2222-3333-4444-555555555555"),
            self.studio_sonos,
            relay_clients,
            self.subscriptions,
        )
        self.server = await asyncio.start_server(
            RendererServer([self.renderer, self.studio], self.subscriptions).handle, "127.0.0.1", 0
        )
        self.port = self.server.sockets[0].getsockname()[1]
        self.events: list[tuple[dict, bytes]] = []
        self.event_arrived = asyncio.Event()
        self.callback_server = await asyncio.start_server(self._callback, "127.0.0.1", 0)
        self.callback_url = f"http://127.0.0.1:{self.callback_server.sockets[0].getsockname()[1]}/evt"

    async def asyncTearDown(self):
        await self.subscriptions.close()
        for server in (self.server, self.callback_server):
            server.close()
            await server.wait_closed()

    async def _callback(self, reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        headers = {k.strip().upper(): v.strip() for k, v in (line.split(":", 1) for line in lines[1:] if ":" in line)}
        body = await reader.readexactly(int(headers.get("CONTENT-LENGTH", "0")))
        self.events.append(({"METHOD": lines[0].split(" ")[0], **headers}, body))
        self.event_arrived.set()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    async def request(self, method, path, headers=None, body=b"", device="buero"):
        path = f"/dev/{device}{path}" if device else path
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        head = f"{method} {path} HTTP/1.1\r\nHost: x\r\nContent-Length: {len(body)}\r\n"
        head += "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
        writer.write(head.encode() + b"\r\n" + body)
        await writer.drain()
        data = await reader.read()
        writer.close()
        status_line, _, rest = data.partition(b"\r\n")
        response_headers, _, payload = rest.partition(b"\r\n\r\n")
        parsed = {k.strip().upper(): v.strip() for k, v in (line.split(":", 1) for line in response_headers.decode().split("\r\n") if ":" in line)}
        return int(status_line.split()[1]), parsed, payload

    async def soap(self, service, action, values=None, device="buero"):
        service_type = SERVICES[service].service_type
        status, _headers, payload = await self.request(
            "POST",
            f"/upnp/control/{service}",
            {"SOAPACTION": f'"{service_type}#{action}"', "Content-Type": "text/xml"},
            request_envelope(action, service_type, values or {}),
            device=device,
        )
        if status != 200:
            return status, parse_fault(payload)[0]
        name, result = parse_envelope(payload)
        self.assertEqual(name, f"{action}Response")
        return status, result

    async def wait_for(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                self.fail("condition not reached")
            await asyncio.sleep(0.01)

    async def test_description_and_scpd_are_served(self):
        status, _headers, body = await self.request("GET", "/description.xml")
        self.assertEqual(status, 200)
        self.assertIn(b"MediaRenderer:1", body)
        for key in SERVICES:
            status, _headers, body = await self.request("GET", f"/upnp/{key}.xml")
            self.assertEqual(status, 200)
            ET.fromstring(body)
        self.assertEqual((await self.request("GET", "/nothing"))[0], 404)
        self.assertEqual((await self.request("GET", "/description.xml", device="keller"))[0], 404)
        self.assertEqual((await self.request("GET", "/description.xml", device=None))[0], 404)

    async def test_each_room_is_its_own_speaker(self):
        _status, _headers, body = await self.request("GET", "/description.xml", device="studio-ton")
        self.assertIn("T2 W – Studio-Ton".encode(), body)
        await self.soap("avtransport", "SetAVTransportURI", {"InstanceID": "0", "CurrentURI": T2W_URI, "CurrentURIMetaData": ""}, device="studio-ton")
        await self.soap("avtransport", "Play", {"InstanceID": "0", "Speed": "1"}, device="studio-ton")
        await self.wait_for(lambda: self.studio.transport_state == "PLAYING")
        self.assertEqual(self.studio_sonos.command_names(), ["play_uri"])
        self.assertEqual(self.sonos.calls, [])
        self.assertEqual(self.renderer.transport_state, "NO_MEDIA_PRESENT")

    async def test_play_answers_at_once_and_then_starts_sonos(self):
        self.sonos.play_delay = 0.5
        await self.soap("avtransport", "SetAVTransportURI", {"InstanceID": "0", "CurrentURI": T2W_URI, "CurrentURIMetaData": ""})

        started = time.monotonic()
        status, _ = await self.soap("avtransport", "Play", {"InstanceID": "0", "Speed": "1"})

        self.assertEqual(status, 200)
        self.assertLess(time.monotonic() - started, 0.3)
        _, info = await self.soap("avtransport", "GetTransportInfo", {"InstanceID": "0"})
        self.assertEqual(info["CurrentTransportState"], "TRANSITIONING")
        await self.wait_for(lambda: self.renderer.transport_state == "PLAYING")
        self.assertEqual(self.sonos.commands()[0], ("play_uri", RELAY_URL))
        _, media = await self.soap("avtransport", "GetMediaInfo", {"InstanceID": "0"})
        self.assertEqual(media["CurrentURI"], T2W_URI)

    async def test_any_uri_from_the_t2w_is_accepted_but_never_used_as_source(self):
        await self.soap("avtransport", "SetAVTransportURI", {"InstanceID": "0", "CurrentURI": "x-project://stream/42", "CurrentURIMetaData": ""})
        await self.soap("avtransport", "Play", {"InstanceID": "0", "Speed": "1"})
        await self.wait_for(lambda: self.renderer.transport_state == "PLAYING")
        self.assertEqual(self.sonos.commands()[0], ("play_uri", RELAY_URL))

    async def test_repeated_play_does_not_restart_a_playing_sonos(self):
        await self.renderer.set_uri(T2W_URI, "")
        await self.renderer.play()
        await self.renderer._start_task
        self.assertEqual(self.sonos.command_names(), ["play_uri"])
        for _ in range(3):  # the T2 W sends Play again, e.g. after re-selection
            await self.renderer.play()
            await self.renderer._start_task
        self.assertEqual(self.sonos.command_names(), ["play_uri"])
        self.assertEqual(self.renderer.transport_state, "PLAYING")

    async def test_slow_sonos_is_reported_playing_before_the_t2w_gives_up_on_lossless(self):
        renderer = Renderer(make_config(report_playing_after_seconds=0.1), self.sonos, self._no_clients, self.subscriptions)
        self.sonos.play_delay = 0.6
        await renderer.set_uri(T2W_URI, "")
        await renderer.play()
        await asyncio.sleep(0.3)
        self.assertEqual(renderer.transport_state, "PLAYING")  # Sonos still busy
        await renderer._start_task
        self.assertEqual(renderer.transport_state, "PLAYING")

    async def test_slow_sonos_that_fails_is_reported_stopped(self):
        renderer = Renderer(make_config(report_playing_after_seconds=0.1), self.sonos, self._no_clients, self.subscriptions)
        self.sonos.play_delay = 0.3
        self.sonos.play_error = RuntimeError("busy")
        self.sonos.state = "STOPPED"
        await renderer.set_uri(T2W_URI, "")
        await renderer.play()
        await renderer._start_task
        self.assertEqual(renderer.transport_state, "STOPPED")
        self.assertFalse(renderer.session_active)

    @staticmethod
    async def _no_clients():
        return 0

    async def test_protocol_errors(self):
        self.assertEqual(await self.soap("avtransport", "Play", {"InstanceID": "0", "Speed": "1"}), (500, 701))
        self.assertEqual(await self.soap("avtransport", "GetTransportInfo", {"InstanceID": "1"}), (500, 718))
        self.assertEqual(await self.soap("avtransport", "Seek", {"InstanceID": "0"}), (500, 401))
        self.assertEqual(await self.soap("avtransport", "Play", {"InstanceID": "0"}), (500, 402))
        self.assertEqual(self.sonos.calls, [])

    async def test_stop_and_pause_are_forwarded_only_for_our_session(self):
        await self.soap("avtransport", "Stop", {"InstanceID": "0"})
        self.assertEqual(self.sonos.calls, [])

        await self.soap("avtransport", "SetAVTransportURI", {"InstanceID": "0", "CurrentURI": T2W_URI, "CurrentURIMetaData": ""})
        await self.soap("avtransport", "Play", {"InstanceID": "0", "Speed": "1"})
        await self.wait_for(lambda: self.renderer.transport_state == "PLAYING")
        await self.soap("avtransport", "Pause", {"InstanceID": "0"})
        self.assertEqual(self.renderer.transport_state, "PAUSED_PLAYBACK")
        await self.soap("avtransport", "Stop", {"InstanceID": "0"})

        self.assertEqual(self.sonos.command_names(), ["play_uri", "pause", "stop"])
        self.assertEqual(self.renderer.transport_state, "STOPPED")
        self.assertFalse(self.renderer.session_active)

    async def test_failed_sonos_start_stops_but_keeps_the_speaker_usable(self):
        self.sonos.play_error = RuntimeError("Sonos unreachable")
        self.sonos.state = "STOPPED"
        await self.soap("avtransport", "SetAVTransportURI", {"InstanceID": "0", "CurrentURI": T2W_URI, "CurrentURIMetaData": ""})
        await self.soap("avtransport", "Play", {"InstanceID": "0", "Speed": "1"})
        await self.wait_for(lambda: self.renderer.last_error is not None)
        self.assertEqual((self.renderer.transport_state, self.renderer.transport_status), ("STOPPED", "OK"))
        self.assertIn("RuntimeError: Sonos unreachable", self.renderer.last_error)
        self.assertFalse(self.renderer.session_active)

    async def test_slow_sonos_answer_is_not_a_failure_when_sonos_plays(self):
        self.sonos.play_error = TimeoutError()
        self.sonos.state, self.sonos.uri = "PLAYING", RELAY_URL
        await self.soap("avtransport", "SetAVTransportURI", {"InstanceID": "0", "CurrentURI": T2W_URI, "CurrentURIMetaData": ""})
        await self.soap("avtransport", "Play", {"InstanceID": "0", "Speed": "1"})
        await self.wait_for(lambda: self.renderer.transport_state == "PLAYING")
        self.assertIsNone(self.renderer.last_error)

    async def test_volume_and_mute_are_forwarded(self):
        self.assertEqual(await self.soap("renderingcontrol", "SetVolume", {"InstanceID": "0", "Channel": "Master", "DesiredVolume": "150"}), (500, 601))
        await self.soap("renderingcontrol", "SetVolume", {"InstanceID": "0", "Channel": "Master", "DesiredVolume": "30"})
        await self.soap("renderingcontrol", "SetMute", {"InstanceID": "0", "Channel": "Master", "DesiredMute": "1"})
        _, volume = await self.soap("renderingcontrol", "GetVolume", {"InstanceID": "0", "Channel": "Master"})
        _, mute = await self.soap("renderingcontrol", "GetMute", {"InstanceID": "0", "Channel": "Master"})
        self.assertEqual((volume["CurrentVolume"], mute["CurrentMute"]), ("30", "1"))
        self.assertEqual((self.sonos.volume, self.sonos.mute), (30, True))
        _, protocols = await self.soap("connectionmanager", "GetProtocolInfo")
        self.assertIn("audio/flac", protocols["Sink"])

    async def test_event_subscription_lifecycle(self):
        status, headers, _ = await self.request(
            "SUBSCRIBE", "/upnp/event/avtransport", {"CALLBACK": f"<{self.callback_url}>", "NT": "upnp:event", "TIMEOUT": "Second-300"}
        )
        self.assertEqual(status, 200)
        sid = headers["SID"]
        self.assertEqual(headers["TIMEOUT"], "Second-300")
        await asyncio.wait_for(self.event_arrived.wait(), 2)
        initial_headers, initial_body = self.events[0]
        self.assertEqual((initial_headers["METHOD"], initial_headers["SID"], initial_headers["SEQ"]), ("NOTIFY", sid, "0"))
        self.assertIn(b"TransportState val=&quot;NO_MEDIA_PRESENT&quot;", initial_body)

        await self.soap("avtransport", "SetAVTransportURI", {"InstanceID": "0", "CurrentURI": T2W_URI, "CurrentURIMetaData": ""})
        await self.soap("avtransport", "Play", {"InstanceID": "0", "Speed": "1"})
        await self.wait_for(lambda: any(b"val=&quot;PLAYING&quot;" in body for _h, body in self.events))
        sequences = [int(h["SEQ"]) for h, _b in self.events]
        self.assertEqual(sequences, list(range(len(sequences))))

        renew = await self.request("SUBSCRIBE", "/upnp/event/avtransport", {"SID": sid, "TIMEOUT": "Second-600"})
        self.assertEqual((renew[0], renew[1]["TIMEOUT"]), (200, "Second-600"))
        self.assertEqual((await self.request("UNSUBSCRIBE", "/upnp/event/avtransport", {"SID": sid}))[0], 200)
        self.assertEqual((await self.request("UNSUBSCRIBE", "/upnp/event/avtransport", {"SID": sid}))[0], 412)

    async def test_subscription_to_an_internet_callback_is_refused(self):
        status, _headers, _ = await self.request(
            "SUBSCRIBE", "/upnp/event/avtransport", {"CALLBACK": "<http://8.8.8.8/evt>", "NT": "upnp:event"}
        )
        self.assertEqual(status, 412)


class WatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sonos = FakeSonos()
        self.clients = 1
        self.subscriptions = Subscriptions()

        async def relay_clients():
            return self.clients

        self.renderer = Renderer(make_config(), self.sonos, relay_clients, self.subscriptions)
        await self.renderer.set_uri(T2W_URI, "")
        await self.renderer.play()
        await self.renderer._start_task
        self.sonos.calls.clear()

    async def asyncTearDown(self):
        await self.subscriptions.close()

    async def run_steps(self, *times):
        for moment in times:
            await self.renderer.watchdog_step(moment)

    async def test_does_nothing_while_sonos_is_connected_to_the_relay(self):
        await self.run_steps(0, 20, 40)
        self.assertEqual(self.sonos.calls, [])

    async def test_restarts_sonos_that_stopped_on_its_own(self):
        self.clients = 0
        self.sonos.state = "STOPPED"
        await self.run_steps(0, 5)
        self.assertEqual(self.sonos.calls, [])  # still within the grace period
        await self.run_steps(11)
        self.assertEqual(self.sonos.names(), ["transport", "play_uri"])
        self.assertEqual(self.renderer.sonos_restarts, 1)

    async def test_ends_the_session_when_sonos_plays_something_else(self):
        self.clients = 0
        self.sonos.uri = "x-sonos-spotify:track"
        await self.run_steps(0, 11)
        self.assertEqual(self.sonos.names(), ["transport"])
        self.assertFalse(self.renderer.session_active)
        self.assertEqual(self.renderer.transport_state, "STOPPED")

    async def test_respects_a_pause_from_the_sonos_app_and_notices_the_resume(self):
        self.clients = 0
        self.sonos.state = "PAUSED_PLAYBACK"
        await self.run_steps(0, 11)
        self.assertEqual(self.renderer.transport_state, "PAUSED_PLAYBACK")
        await self.run_steps(30, 60)
        self.assertNotIn("play_uri", self.sonos.names())
        self.clients = 1
        await self.run_steps(61)
        self.assertEqual(self.renderer.transport_state, "PLAYING")

    async def test_never_restarts_a_session_paused_by_the_t2w(self):
        await self.renderer.pause()
        self.clients = 0
        self.sonos.state = "STOPPED"
        await self.run_steps(0, 11, 30)
        self.assertNotIn("play_uri", self.sonos.names())

    async def test_gives_up_after_repeated_failures(self):
        self.clients = 0
        self.sonos.play_error = RuntimeError("no")
        self.sonos.state = "STOPPED"
        moment = 0
        for _ in range(4):
            await self.run_steps(moment, moment + 11)
            moment += 30
        self.assertEqual(self.sonos.names().count("play_uri"), 3)
        self.assertFalse(self.renderer.session_active)
        self.assertEqual(self.renderer.transport_state, "STOPPED")
        self.assertIn("giving up", self.renderer.last_error)


if __name__ == "__main__":
    unittest.main()
