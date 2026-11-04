import asyncio
import html
import unittest

from fakes import FakeSonosServer
from soap import (
    _dechunk,
    fault_envelope,
    http_request,
    parse_envelope,
    parse_fault,
    parse_xml,
    request_envelope,
    response_envelope,
)
from sonos import (
    AVTRANSPORT,
    SonosClient,
    SonosError,
    SonosRoom,
    coordinator_from_state,
    didl_metadata,
    parse_sonos_search_response,
    rooms_from_state,
)


class SoapTests(unittest.TestCase):
    def test_request_and_response_use_the_correct_element_names(self):
        request = request_envelope("Play", AVTRANSPORT, {"InstanceID": "0", "Speed": "1"})
        response = response_envelope("Play", AVTRANSPORT)
        self.assertEqual(parse_envelope(request), ("Play", {"InstanceID": "0", "Speed": "1"}))
        self.assertEqual(parse_envelope(response)[0], "PlayResponse")
        self.assertNotIn(b"PlayResponse", request)

    def test_values_are_escaped_and_round_trip(self):
        value = '<DIDL-Lite a="1">&amp; Büro</DIDL-Lite>'
        _name, values = parse_envelope(request_envelope("SetAVTransportURI", AVTRANSPORT, {"CurrentURIMetaData": value}))
        self.assertEqual(values["CurrentURIMetaData"], value)

    def test_fault_round_trip(self):
        self.assertEqual(parse_fault(fault_envelope(714, "Illegal MIME-type")), (714, "Illegal MIME-type"))

    def test_rejects_entity_declarations(self):
        with self.assertRaises(ValueError):
            parse_xml(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>')

    def test_dechunk(self):
        self.assertEqual(_dechunk(b"4\r\nWiki\r\n5\r\npedia\r\n0\r\n\r\n"), b"Wikipedia")


class HttpRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_chunked_response(self):
        async def handler(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n0\r\n\r\n")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        status, _headers, body = await http_request("127.0.0.1", server.sockets[0].getsockname()[1], "GET", "/")
        self.assertEqual((status, body), (200, b"abc"))


def zone_group_state(groups, names=None, hidden=()):
    """Build a ZoneGroupState document like Sonos returns it."""
    names = names or {}
    parts = ["<ZoneGroupState><ZoneGroups>"]
    for coordinator, members in groups:
        parts.append(f'<ZoneGroup Coordinator="{coordinator}" ID="{coordinator}:1">')
        for uuid, port in members:
            extra = ' Invisible="1"' if uuid in hidden else ""
            parts.append(
                f'<ZoneGroupMember UUID="{uuid}" ZoneName="{names.get(uuid, uuid)}"{extra} '
                f'Location="http://127.0.0.1:{port}/xml/device_description.xml"/>'
            )
        parts.append("</ZoneGroup>")
    parts.append("</ZoneGroups></ZoneGroupState>")
    return "".join(parts)


class CoordinatorTests(unittest.TestCase):
    def test_finds_the_coordinator_of_the_members_group(self):
        state = zone_group_state([("RINCON_A", [("RINCON_A", 1401), ("RINCON_B", 1402)]), ("RINCON_C", [("RINCON_C", 1403)])])
        self.assertEqual(coordinator_from_state(state, "127.0.0.1", 1402), ("127.0.0.1", 1401))
        self.assertEqual(coordinator_from_state(state, "127.0.0.1", 1403), ("127.0.0.1", 1403))
        self.assertIsNone(coordinator_from_state(state, "127.0.0.1", 1999))

    def test_rooms_skip_hidden_stereo_pair_speakers(self):
        state = zone_group_state(
            [("RINCON_L", [("RINCON_L", 1401), ("RINCON_R", 1402)]), ("RINCON_S", [("RINCON_S", 1403)])],
            names={"RINCON_L": "Büro", "RINCON_R": "Büro", "RINCON_S": "Studio-Ton"},
            hidden={"RINCON_R"},
        )
        self.assertEqual(rooms_from_state(state), {"Büro": ("127.0.0.1", 1401), "Studio-Ton": ("127.0.0.1", 1403)})

    def test_recognises_sonos_search_responses(self):
        response = (
            b"HTTP/1.1 200 OK\r\nLOCATION: http://192.168.1.52:1400/xml/device_description.xml\r\n"
            b"ST: urn:schemas-upnp-org:device:ZonePlayer:1\r\n\r\n"
        )
        self.assertEqual(parse_sonos_search_response(response), "192.168.1.52")
        self.assertIsNone(parse_sonos_search_response(response.replace(b"ZonePlayer", b"MediaServer")))

    def test_metadata_declares_flac(self):
        self.assertIn('protocolInfo="http-get:*:audio/flac:*"', didl_metadata("http://x/stream.flac", "T2 W"))


class SonosClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.coordinator = await FakeSonosServer().start()
        self.member = await FakeSonosServer().start()
        state = zone_group_state(
            [("RINCON_COORD", [("RINCON_COORD", self.coordinator.port), ("RINCON_MEMBER", self.member.port)])]
        )
        self.member.responses["GetZoneGroupState"] = {"ZoneGroupState": state}
        self.member.responses["GetVolume"] = {"CurrentVolume": "37"}
        self.coordinator.responses["GetTransportInfo"] = {"CurrentTransportState": "STOPPED"}
        self.coordinator.responses["GetMediaInfo"] = {"CurrentURI": "http://relay/stream.flac"}
        self.client = SonosClient("127.0.0.1", self.member.port)

    async def asyncTearDown(self):
        await self.coordinator.close()
        await self.member.close()

    async def test_play_goes_to_the_group_coordinator_with_flac_metadata(self):
        await self.client.play_uri("http://192.168.1.116:8186/stream.flac", "T2 W Relay – Büro")

        self.assertEqual(self.coordinator.actions(), ["SetAVTransportURI", "Play"])
        _path, _action, values = self.coordinator.calls[0]
        self.assertEqual(values["CurrentURI"], "http://192.168.1.116:8186/stream.flac")
        self.assertIn("audio/flac", values["CurrentURIMetaData"])
        self.assertIn("Büro", html.unescape(values["CurrentURIMetaData"]))

    async def test_transport_reads_state_and_uri_from_the_coordinator(self):
        self.assertEqual(await self.client.transport(), ("STOPPED", "http://relay/stream.flac"))

    async def test_volume_is_the_rooms_own(self):
        self.assertEqual(await self.client.get_volume(), 37)
        await self.client.set_volume(40)
        self.assertEqual(self.member.calls[-1][1:], ("SetVolume", {"InstanceID": "0", "Channel": "Master", "DesiredVolume": "40"}))

    async def test_upnp_errors_carry_the_code(self):
        self.coordinator.fail["Play"] = 701
        with self.assertRaises(SonosError) as raised:
            await self.client.play_uri("http://x/stream.flac", "T2 W")
        self.assertEqual(raised.exception.code, 701)

    async def test_falls_back_to_the_room_when_group_lookup_fails(self):
        self.member.fail["GetZoneGroupState"] = 401
        await self.client.stop()
        self.assertEqual(self.member.actions()[-1], "Stop")


class SonosRoomTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.seed = await FakeSonosServer().start()
        self.studio = await FakeSonosServer().start()
        self.addAsyncCleanup(self.seed.close)
        self.addAsyncCleanup(self.studio.close)
        self.set_state(self.studio.port)

    def set_state(self, studio_port):
        state = zone_group_state(
            [("RINCON_B", [("RINCON_B", self.seed.port)]), ("RINCON_S", [("RINCON_S", studio_port)])],
            names={"RINCON_B": "Büro", "RINCON_S": "Studio-Ton"},
        )
        for server in (self.seed, self.studio):
            server.responses["GetZoneGroupState"] = {"ZoneGroupState": state}

    def room(self, name, discover=None):
        async def nothing():
            return []

        return SonosRoom(name, ["127.0.0.1"], timeout=2, discover=discover or nothing, seed_port=self.seed.port)

    async def test_finds_a_room_by_name(self):
        room = self.room("studio-ton")
        await room.stop()
        self.assertEqual((room.host, room.port), ("127.0.0.1", self.studio.port))
        self.assertEqual(self.studio.actions()[-1], "Stop")
        self.assertEqual(room.stream_hosts(), {"127.0.0.1"})

    async def test_unknown_room_lists_the_known_ones(self):
        with self.assertRaisesRegex(SonosError, "Büro, Studio-Ton"):
            await self.room("Keller").resolve()

    async def test_uses_discovery_when_seeds_are_unreachable(self):
        async def discover():
            return ["127.0.0.1"]

        room = SonosRoom("Büro", ["10.255.255.1"], timeout=1, discover=discover, seed_port=self.seed.port)
        await room.resolve()
        self.assertEqual(room.port, self.seed.port)

    async def test_follows_the_room_to_a_new_address(self):
        room = self.room("Studio-Ton")
        await room.get_volume()
        moved = await FakeSonosServer().start()
        self.addAsyncCleanup(moved.close)
        moved.responses["GetVolume"] = {"CurrentVolume": "12"}
        await self.studio.close()
        self.set_state(moved.port)
        room._seed_hosts = ["127.0.0.1"]

        self.assertEqual(await room.get_volume(), 12)
        self.assertEqual(room.port, moved.port)


if __name__ == "__main__":
    unittest.main()
