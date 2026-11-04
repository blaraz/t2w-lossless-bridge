"""One virtual speaker per Sonos room, kept in step with the Sonos system.

The room list is read from the Sonos system every minute. A new room gets
its own speaker in the Pro-Ject app, a renamed room keeps its speaker under
the new name, and a room that is gone (e.g. a Move on battery or Bluetooth)
is removed after a few minutes, never while it is playing the turntable.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
import uuid as uuid_module
from dataclasses import dataclass
from typing import Awaitable, Callable, Final

from sonos import RoomInfo, SonosRoom, discover_sonos_hosts, fetch_rooms, normalize_room_name
from upnp import Renderer, RendererConfig, SonosControl, Speakers

LOG: Final = logging.getLogger("t2w.rooms")
DEFAULT_UUID: Final = "uuid:0dfbc94a-1931-4451-9a64-25a9269ad458"


def room_key(text: str) -> str:
    """URL-safe id, e.g. 'RINCON_000E58A0B1C201400' → 'rincon-000e58a0b1c201400', 'Büro' → 'buero'."""
    text = text.casefold().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-") or "room"


def room_uuid(base_uuid: str, room_id: str) -> str:
    """A stable UPnP UDN per room, so the Pro-Ject app (and autoplay) keeps recognising it."""
    base = uuid_module.UUID(base_uuid.removeprefix("uuid:"))
    return f"uuid:{uuid_module.uuid5(base, room_id.casefold())}"


def parse_room_list(value: str) -> list[str]:
    return [name.strip() for name in value.split(",") if name.strip()]


@dataclass(frozen=True)
class SpeakerSettings:
    """How a Sonos room becomes a virtual speaker."""

    advertised_host: str
    http_port: int
    relay_url: str
    name_format: str = "T2 W – {room}"
    base_uuid: str = DEFAULT_UUID
    manufacturer: str = "Pro-Ject Audio Systems"
    source_recovery_seconds: float = 20.0
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    def wanted(self, name: str) -> bool:
        normalized = normalize_room_name(name)
        if self.include and normalized not in {normalize_room_name(room) for room in self.include}:
            return False
        return normalized not in {normalize_room_name(room) for room in self.exclude}

    def friendly_name(self, room: RoomInfo) -> str:
        return self.name_format.replace("{room}", room.name)

    def renderer_config(self, room: RoomInfo) -> RendererConfig:
        return RendererConfig(
            friendly_name=self.friendly_name(room),
            uuid=room_uuid(self.base_uuid, room.room_id),
            advertised_host=self.advertised_host,
            http_port=self.http_port,
            relay_url=self.relay_url,
            key=room_key(room.room_id),
            manufacturer=self.manufacturer,
            source_recovery_seconds=self.source_recovery_seconds,
        )


class SonosRooms:
    """Reads the room list from the Sonos system, wherever its speakers are."""

    def __init__(
        self,
        seed_hosts: list[str],
        discover: Callable[[], Awaitable[list[str]]] = discover_sonos_hosts,
        port: int = 1400,
    ) -> None:
        self._seed_hosts = seed_hosts
        self._discover = discover
        self._port = port
        self._known: list[str] = []

    async def fetch(self) -> list[RoomInfo] | None:
        candidates = [(host, self._port) for host in [*self._known, *self._seed_hosts]]
        rooms = await fetch_rooms(candidates) if candidates else None
        if rooms is None:
            rooms = await fetch_rooms([(host, self._port) for host in await self._discover()])
        if rooms is not None:
            self._known = [room.host for room in rooms][:8]
        return rooms

    def controller(self, room: RoomInfo) -> SonosRoom:
        sonos = SonosRoom(room.name, self._seed_hosts, discover=self._discover, seed_port=self._port, room_id=room.room_id)
        sonos.set_address(room.host, room.port)
        return sonos


class RoomSync:
    """Adds, renames and removes virtual speakers to match the Sonos rooms."""

    def __init__(
        self,
        speakers: Speakers,
        settings: SpeakerSettings,
        fetch: Callable[[], Awaitable[list[RoomInfo] | None]],
        controller: Callable[[RoomInfo], SonosControl],
        make_renderer: Callable[[RendererConfig, SonosControl], Renderer],
        interval_seconds: float = 60.0,
        remove_after_seconds: float = 300.0,
    ) -> None:
        self.speakers = speakers
        self.settings = settings
        self._fetch = fetch
        self._controller = controller
        self._make_renderer = make_renderer
        self.interval_seconds = interval_seconds
        self.remove_after_seconds = remove_after_seconds
        self._missing_since: dict[str, float] = {}
        self._warned_empty = False
        self.last_sync_ok = False

    async def sync_once(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        rooms = await self._fetch()
        self.last_sync_ok = rooms is not None
        if rooms is None:
            if not self._warned_empty:
                LOG.warning("no Sonos speaker answered; trying again in %.0fs", self.interval_seconds)
                self._warned_empty = True
            return  # keep everything as it is
        self._warned_empty = False
        wanted = {}
        for room in rooms:
            if self.settings.wanted(room.name):
                wanted[room_key(room.room_id)] = room
        if not wanted and self.settings.include:
            LOG.warning(
                "none of the rooms in SONOS_ROOMS exists; Sonos rooms are: %s", ", ".join(room.name for room in rooms)
            )
        for key, room in wanted.items():
            self._missing_since.pop(key, None)
            renderer = self.speakers.get(key)
            if renderer is None:
                renderer = self._make_renderer(self.settings.renderer_config(room), self._controller(room))
                self.speakers.add(renderer)
                LOG.info("speaker %r ready at %s", renderer.config.friendly_name, renderer.config.location)
                continue
            friendly = self.settings.friendly_name(room)
            sonos = renderer.sonos
            if isinstance(sonos, SonosRoom):
                sonos.name = room.name
                sonos.set_address(room.host, room.port)
            if friendly != renderer.config.friendly_name:
                renderer.rename(friendly)
                self.speakers.changed.set()  # announce the new name
        for renderer in self.speakers.renderers():
            key = renderer.config.key
            if key in wanted:
                continue
            if renderer.session_active:
                continue  # never pull a playing speaker away
            since = self._missing_since.setdefault(key, now)
            # A room that was filtered out goes at once; a vanished one only
            # after a while, as speakers drop out briefly (updates, Wi-Fi).
            vanished = key not in {room_key(room.room_id) for room in rooms}
            if not vanished or now - since >= self.remove_after_seconds:
                self.speakers.remove(key)
                self._missing_since.pop(key, None)
                LOG.info(
                    "speaker %r removed (%s)",
                    renderer.config.friendly_name,
                    "Sonos room gone" if vanished else "excluded by SONOS_ROOMS/SONOS_EXCLUDE",
                )

    async def run(self) -> None:
        """Keep in step; the first sync is expected to have been done already."""
        while True:
            await asyncio.sleep(self.interval_seconds)
            try:
                await self.sync_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("room list update failed")
