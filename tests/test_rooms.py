"""Dynamic rooms: one virtual speaker per Sonos room, kept in step."""

import unittest

from discovery import answers_service, mdns_query
from fakes import FakeSonos
from rooms import RoomSync, SpeakerSettings, room_key
from sonos import RoomInfo, SonosRoom, room_infos_from_state
from test_soap_sonos import zone_group_state
from upnp import Renderer, RendererServer, Speakers, SSDPProtocol, Subscriptions, search_targets

BUERO = RoomInfo("RINCON_B01400", "Büro", "192.168.1.53")
STUDIO = RoomInfo("RINCON_S01400", "Studio-Ton", "192.168.1.52")
MOVE = RoomInfo("RINCON_M01400", "Move", "192.168.1.85")


def settings(**overrides):
    values = dict(advertised_host="192.168.1.116", http_port=8090, relay_url="http://192.168.1.116:8186/stream.flac")
    values.update(overrides)
    return SpeakerSettings(**values)


class RoomListTests(unittest.TestCase):
    def test_rooms_have_fixed_ids_and_skip_hidden_speakers_and_bridges(self):
        state = zone_group_state(
            [("RINCON_L", [("RINCON_L", 1401), ("RINCON_R", 1402)]), ("RINCON_S", [("RINCON_S", 1403)])],
            names={"RINCON_L": "Büro", "RINCON_R": "Büro", "RINCON_S": "Studio-Ton"},
            hidden={"RINCON_R"},
        )
        state = state.replace("</ZoneGroups>", '<ZoneGroup Coordinator="RINCON_X"><ZoneGroupMember UUID="RINCON_X" '
                              'ZoneName="Boost" Location="http://127.0.0.1:1404/xml/device_description.xml" '
                              'IsZoneBridge="1"/></ZoneGroup></ZoneGroups>')
        rooms = room_infos_from_state(state)
        self.assertEqual([(room.room_id, room.name, room.port) for room in rooms],
                         [("RINCON_L", "Büro", 1401), ("RINCON_S", "Studio-Ton", 1403)])

    def test_filters(self):
        self.assertTrue(settings().wanted("Bad"))
        self.assertTrue(settings(include=("büro",)).wanted("Büro"))
        self.assertFalse(settings(include=("Büro",)).wanted("Studio-Ton"))
        self.assertFalse(settings(exclude=("bad",)).wanted("Bad"))

    def test_speaker_identity_comes_from_the_sonos_id_not_the_name(self):
        renamed = RoomInfo(BUERO.room_id, "Arbeitszimmer", BUERO.host)
        first, second = settings().renderer_config(BUERO), settings().renderer_config(renamed)
        self.assertEqual((first.uuid, first.key), (second.uuid, second.key))
        self.assertEqual(first.key, "rincon-b01400")
        self.assertEqual(second.friendly_name, "T2 W – Arbeitszimmer")


class RoomSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.rooms: list[RoomInfo] | None = [BUERO, STUDIO]
        self.subscriptions = Subscriptions()
        self.speakers = Speakers()
        self.added, self.removed = [], []
        self.speakers.on_add.append(lambda r: self.added.append(r.config.friendly_name))
        self.speakers.on_remove.append(lambda r: self.removed.append(r.config.friendly_name))
        self.sync = self.make_sync(settings())

    def make_sync(self, speaker_settings):
        async def fetch():
            return self.rooms

        def controller(room):
            async def nothing():
                return []

            sonos = SonosRoom(room.name, [], discover=nothing, room_id=room.room_id)
            sonos.set_address(room.host, room.port)
            return sonos

        async def one():
            return 1

        def make(config, sonos):
            return Renderer(config, sonos, one, self.subscriptions)

        return RoomSync(self.speakers, speaker_settings, fetch, controller, make, remove_after_seconds=300)

    async def asyncTearDown(self):
        await self.subscriptions.close()

    def names(self):
        return sorted(config.friendly_name for config in self.speakers.configs())

    async def test_every_room_becomes_a_speaker(self):
        await self.sync.sync_once(0)
        self.assertEqual(self.names(), ["T2 W – Büro", "T2 W – Studio-Ton"])

    async def test_new_room_appears(self):
        await self.sync.sync_once(0)
        self.rooms.append(MOVE)
        await self.sync.sync_once(60)
        self.assertIn("T2 W – Move", self.names())

    async def test_renamed_room_keeps_its_speaker(self):
        await self.sync.sync_once(0)
        renderer = self.speakers.get(room_key(BUERO.room_id))
        self.speakers.changed.clear()
        self.rooms = [RoomInfo(BUERO.room_id, "Arbeitszimmer", "192.168.1.60"), STUDIO]
        await self.sync.sync_once(60)
        self.assertIs(self.speakers.get(room_key(BUERO.room_id)), renderer)
        self.assertEqual(renderer.config.friendly_name, "T2 W – Arbeitszimmer")
        self.assertEqual(renderer.sonos.host, "192.168.1.60")
        self.assertEqual(renderer.sonos.name, "Arbeitszimmer")
        self.assertTrue(self.speakers.changed.is_set())
        self.assertEqual(self.removed, [])

    async def test_vanished_room_is_removed_after_a_while(self):
        self.rooms.append(MOVE)
        await self.sync.sync_once(0)
        self.rooms.remove(MOVE)
        await self.sync.sync_once(60)
        self.assertIn("T2 W – Move", self.names())
        await self.sync.sync_once(400)
        self.assertNotIn("T2 W – Move", self.names())
        self.assertEqual(self.removed, ["T2 W – Move"])

    async def test_short_dropout_does_not_remove_a_room(self):
        self.rooms.append(MOVE)
        await self.sync.sync_once(0)
        self.rooms.remove(MOVE)
        await self.sync.sync_once(60)
        self.rooms.append(MOVE)
        await self.sync.sync_once(120)
        self.rooms.remove(MOVE)
        await self.sync.sync_once(400)  # missing again only since 400
        self.assertIn("T2 W – Move", self.names())

    async def test_playing_speaker_is_never_removed(self):
        self.rooms.append(MOVE)
        await self.sync.sync_once(0)
        self.speakers.get(room_key(MOVE.room_id)).session_active = True
        self.rooms.remove(MOVE)
        await self.sync.sync_once(1000)
        self.assertIn("T2 W – Move", self.names())

    async def test_unreachable_sonos_changes_nothing(self):
        await self.sync.sync_once(0)
        self.rooms = None
        await self.sync.sync_once(1000)
        self.assertEqual(len(self.speakers), 2)
        self.assertFalse(self.sync.last_sync_ok)

    async def test_include_and_exclude(self):
        self.sync = self.make_sync(settings(exclude=("Studio-Ton",)))
        await self.sync.sync_once(0)
        self.assertEqual(self.names(), ["T2 W – Büro"])
        # A room excluded later goes at once.
        self.sync.settings = settings(include=("Studio-Ton",))
        await self.sync.sync_once(10)
        self.assertEqual(self.names(), ["T2 W – Studio-Ton"])


class LiveSpeakerListTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_and_ssdp_follow_the_list(self):
        subscriptions = Subscriptions()
        self.addAsyncCleanup(subscriptions.close)
        speakers = Speakers()

        async def one():
            return 1

        server = RendererServer(speakers, subscriptions)
        ssdp = SSDPProtocol(speakers)
        config = settings().renderer_config(MOVE)
        self.assertEqual(ssdp._configs(), [])
        self.assertIsNone(server.speakers.get(config.key))
        speakers.add(Renderer(config, FakeSonos(), one, subscriptions))
        self.assertEqual(ssdp._configs(), [config])
        self.assertIsNotNone(server.speakers.get(config.key))
        self.assertTrue(search_targets("ssdp:all", config.uuid))
        speakers.remove(config.key)
        self.assertEqual(ssdp._configs(), [])


class MdnsTests(unittest.TestCase):
    def test_query_and_answer_recognition(self):
        query = mdns_query("_sues800device._tcp.local")
        self.assertIn(b"\x0e_sues800device\x04_tcp\x05local\x00", query)
        self.assertFalse(answers_service(query))  # a question, not an answer
        answer = b"\x00\x00\x84\x00\x00\x00\x00\x01\x00\x00\x00\x00" + query[12:]
        self.assertTrue(answers_service(answer))
        self.assertFalse(answers_service(answer.replace(b"_sues800device", b"_other00device")))


if __name__ == "__main__":
    unittest.main()
