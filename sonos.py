"""Minimal async client for one Sonos room: play a URL, stop, pause, volume.

Transport commands go to the room's group coordinator, so the room may be
part of a group or a stereo pair. SonosRoom finds the room by name and
follows it when its IP address changes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, Awaitable, Final
from urllib.parse import urlsplit

from soap import http_request, parse_envelope, parse_fault, parse_xml, request_envelope, xml_escape

LOG: Final = logging.getLogger("t2w.sonos")
AVTRANSPORT: Final = "urn:schemas-upnp-org:service:AVTransport:1"
RENDERING_CONTROL: Final = "urn:schemas-upnp-org:service:RenderingControl:1"
ZONE_GROUP_TOPOLOGY: Final = "urn:schemas-upnp-org:service:ZoneGroupTopology:1"
CONTROL_PATHS: Final = {
    AVTRANSPORT: "/MediaRenderer/AVTransport/Control",
    RENDERING_CONTROL: "/MediaRenderer/RenderingControl/Control",
    ZONE_GROUP_TOPOLOGY: "/ZoneGroupTopology/Control",
}


class SonosError(RuntimeError):
    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def didl_metadata(uri: str, title: str) -> str:
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="t2w-relay" parentID="0" restricted="true">'
        f"<dc:title>{xml_escape(title)}</dc:title>"
        "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
        f'<res protocolInfo="http-get:*:audio/flac:*">{xml_escape(uri)}</res>'
        "</item></DIDL-Lite>"
    )


def coordinator_from_state(state_xml: str, member_host: str, member_port: int) -> tuple[str, int] | None:
    """Find the group coordinator of the member at host:port in a ZoneGroupState document."""
    root = parse_xml(state_xml)
    for group in root.iter("ZoneGroup"):
        locations = {member.get("UUID"): member.get("Location", "") for member in group.iter("ZoneGroupMember")}
        for location in locations.values():
            parsed = urlsplit(location)
            if parsed.hostname == member_host and (parsed.port or 1400) == member_port:
                coordinator = urlsplit(locations.get(group.get("Coordinator"), "") or "")
                if coordinator.hostname:
                    return coordinator.hostname, coordinator.port or 1400
                return None
    return None


def normalize_room_name(name: str) -> str:
    return " ".join(name.split()).casefold()


@dataclass(frozen=True)
class RoomInfo:
    """A Sonos room: its fixed id (RINCON_…), its name and its visible main speaker."""

    room_id: str
    name: str
    host: str
    port: int = 1400


def room_infos_from_state(state_xml: str) -> list[RoomInfo]:
    """All rooms in a ZoneGroupState document.

    Hidden members (second speaker of a stereo pair, subs, surrounds) and
    Sonos Boost/Bridge devices are skipped; the id stays the same when the
    room is renamed or gets a new IP address.
    """
    rooms: dict[str, RoomInfo] = {}
    for member in parse_xml(state_xml).iter("ZoneGroupMember"):
        if member.get("Invisible") == "1" or member.get("IsZoneBridge") == "1":
            continue
        location = urlsplit(member.get("Location", ""))
        room_id, name = member.get("UUID", ""), member.get("ZoneName", "")
        if room_id and name and location.hostname:
            rooms[room_id] = RoomInfo(room_id, name, location.hostname, location.port or 1400)
    return sorted(rooms.values(), key=lambda room: normalize_room_name(room.name))


def rooms_from_state(state_xml: str) -> dict[str, tuple[str, int]]:
    """Room name → address of its visible main speaker, from a ZoneGroupState document.

    Hidden members (second speaker of a stereo pair, subs, surrounds) are skipped.
    """
    rooms: dict[str, tuple[str, int]] = {}
    for member in parse_xml(state_xml).iter("ZoneGroupMember"):
        if member.get("Invisible") == "1":
            continue
        location = urlsplit(member.get("Location", ""))
        name = member.get("ZoneName", "")
        if name and location.hostname:
            rooms[name] = (location.hostname, location.port or 1400)
    return rooms


SONOS_SEARCH_TARGET: Final = "urn:schemas-upnp-org:device:ZonePlayer:1"


def parse_sonos_search_response(data: bytes) -> str | None:
    """Host of a Sonos speaker from an SSDP search response, if it is one."""
    headers = {}
    for line in data.decode("latin-1", "replace").split("\r\n")[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().upper()] = value.strip()
    if headers.get("ST") != SONOS_SEARCH_TARGET:
        return None
    return urlsplit(headers.get("LOCATION", "")).hostname


async def discover_sonos_hosts(timeout: float = 2.0) -> list[str]:
    """Find Sonos speakers on the LAN with an SSDP search."""
    hosts: list[str] = []

    class Collector(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
            host = parse_sonos_search_response(data)
            if host and host not in hosts:
                hosts.append(host)

    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(Collector, local_addr=("0.0.0.0", 0))
    try:
        message = (
            "M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
            f'MAN: "ssdp:discover"\r\nMX: 1\r\nST: {SONOS_SEARCH_TARGET}\r\n\r\n'
        ).encode("ascii")
        for _ in range(2):
            transport.sendto(message, ("239.255.255.250", 1900))
            await asyncio.sleep(timeout / 2)
    finally:
        transport.close()
    return hosts


class SonosClient:
    def __init__(self, host: str, port: int = 1400, timeout: float = 8.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.last_coordinator: tuple[str, int] | None = None

    def stream_hosts(self) -> set[str]:
        """Addresses from which this room fetches the relay stream."""
        hosts = {self.host}
        if self.last_coordinator:
            hosts.add(self.last_coordinator[0])
        return hosts

    async def call(
        self, service: str, action: str, values: dict[str, str], target: tuple[str, int] | None = None
    ) -> dict[str, str]:
        host, port = target or (self.host, self.port)
        status, _headers, body = await http_request(
            host,
            port,
            "POST",
            CONTROL_PATHS[service],
            {"Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": f'"{service}#{action}"'},
            request_envelope(action, service, values),
            self.timeout,
        )
        if status != 200:
            code, description = parse_fault(body)
            detail = f", UPnP error {code}" if code is not None else ""
            detail += f" ({description})" if description else ""
            raise SonosError(f"Sonos {action} at {host}:{port} returned HTTP {status}{detail}", code)
        _name, result = parse_envelope(body)
        return result

    async def coordinator(self) -> tuple[str, int]:
        target = (self.host, self.port)
        try:
            result = await self.call(ZONE_GROUP_TOPOLOGY, "GetZoneGroupState", {})
            target = coordinator_from_state(result.get("ZoneGroupState", ""), self.host, self.port) or target
        except (SonosError, OSError, ValueError, TimeoutError) as exc:
            LOG.debug("group lookup failed, using the room itself: %s", exc)
        self.last_coordinator = target
        return target

    async def play_uri(self, uri: str, title: str) -> None:
        # Starting a stream can take Sonos a while (it connects to the stream
        # first), so allow more time than for other commands.
        target = await self.coordinator()
        timeout, self.timeout = self.timeout, max(self.timeout, 20.0)
        try:
            await self._set_and_play(uri, title, target)
        finally:
            self.timeout = timeout

    async def _set_and_play(self, uri: str, title: str, target: tuple[str, int]) -> None:
        await self.call(
            AVTRANSPORT,
            "SetAVTransportURI",
            {"InstanceID": "0", "CurrentURI": uri, "CurrentURIMetaData": didl_metadata(uri, title)},
            target,
        )
        await self.call(AVTRANSPORT, "Play", {"InstanceID": "0", "Speed": "1"}, target)

    async def stop(self) -> None:
        await self.call(AVTRANSPORT, "Stop", {"InstanceID": "0"}, await self.coordinator())

    async def pause(self) -> None:
        await self.call(AVTRANSPORT, "Pause", {"InstanceID": "0"}, await self.coordinator())

    async def transport(self) -> tuple[str, str]:
        """Current transport state and URI of the room's group."""
        target = await self.coordinator()
        info = await self.call(AVTRANSPORT, "GetTransportInfo", {"InstanceID": "0"}, target)
        media = await self.call(AVTRANSPORT, "GetMediaInfo", {"InstanceID": "0"}, target)
        return info.get("CurrentTransportState", ""), media.get("CurrentURI", "")

    async def get_volume(self) -> int:
        result = await self.call(RENDERING_CONTROL, "GetVolume", {"InstanceID": "0", "Channel": "Master"})
        return int(result.get("CurrentVolume", "0"))

    async def set_volume(self, volume: int) -> None:
        await self.call(
            RENDERING_CONTROL, "SetVolume", {"InstanceID": "0", "Channel": "Master", "DesiredVolume": str(volume)}
        )

    async def get_mute(self) -> bool:
        result = await self.call(RENDERING_CONTROL, "GetMute", {"InstanceID": "0", "Channel": "Master"})
        return result.get("CurrentMute", "0") in ("1", "true")

    async def set_mute(self, mute: bool) -> None:
        await self.call(
            RENDERING_CONTROL, "SetMute", {"InstanceID": "0", "Channel": "Master", "DesiredMute": "1" if mute else "0"}
        )


async def fetch_rooms(candidates: list[tuple[str, int]], timeout: float = 4.0) -> list[RoomInfo] | None:
    """The rooms of the Sonos system, asked from the first speaker that answers."""
    for host, port in dict.fromkeys(candidates):
        try:
            result = await SonosClient(host, port, timeout=timeout).call(ZONE_GROUP_TOPOLOGY, "GetZoneGroupState", {})
            return room_infos_from_state(result.get("ZoneGroupState", ""))
        except (SonosError, OSError, ValueError, TimeoutError) as exc:
            LOG.debug("room list via %s failed: %s", host, exc)
    return None


class SonosRoom(SonosClient):
    """A Sonos room addressed by id (or name); its IP address is looked up and refreshed."""

    REFRESH_SECONDS = 300.0

    def __init__(
        self,
        name: str,
        seed_hosts: list[str],
        timeout: float = 8.0,
        discover: Callable[[], Awaitable[list[str]]] = discover_sonos_hosts,
        seed_port: int = 1400,
        room_id: str | None = None,
    ) -> None:
        super().__init__("", 1400, timeout)
        self.name = name
        self.room_id = room_id
        self._seed_hosts = seed_hosts
        self._seed_port = seed_port
        self._discover = discover
        self._resolved_at = 0.0
        self._resolve_lock = asyncio.Lock()

    def set_address(self, host: str, port: int = 1400) -> None:
        """Known-good address, e.g. from a fresh room list."""
        if (host, port) != (self.host, self.port):
            LOG.info("Sonos room %r is at %s:%d", self.name, host, port)
        self.host, self.port = host, port
        self._resolved_at = time.monotonic()

    async def _lookup(self, candidates: list[tuple[str, int]]) -> tuple[str, int] | None:
        wanted = normalize_room_name(self.name)
        for host, port in dict.fromkeys(candidates):
            try:
                result = await SonosClient(host, port, timeout=4).call(ZONE_GROUP_TOPOLOGY, "GetZoneGroupState", {})
                rooms = room_infos_from_state(result.get("ZoneGroupState", ""))
            except (SonosError, OSError, ValueError, TimeoutError) as exc:
                LOG.debug("room lookup via %s failed: %s", host, exc)
                continue
            for room in rooms:
                if room.room_id == self.room_id if self.room_id else normalize_room_name(room.name) == wanted:
                    self.name = room.name
                    return room.host, room.port
            names = ", ".join(room.name for room in rooms)
            raise SonosError(f"Sonos room {self.name!r} not found; rooms: {names}")
        return None

    async def resolve(self, force: bool = False) -> None:
        async with self._resolve_lock:
            if self.host and not force and time.monotonic() - self._resolved_at < self.REFRESH_SECONDS:
                return
            candidates = [(self.host, self.port)] if self.host else []
            candidates += [(host, self._seed_port) for host in self._seed_hosts]
            address = await self._lookup(candidates)
            if address is None:
                address = await self._lookup([(host, self._seed_port) for host in await self._discover()])
            if address is None:
                raise SonosError(f"no Sonos speaker reachable to find room {self.name!r}")
            if address != (self.host, self.port):
                LOG.info("Sonos room %r is at %s:%d", self.name, *address)
            self.host, self.port = address
            self._resolved_at = time.monotonic()

    async def call(
        self, service: str, action: str, values: dict[str, str], target: tuple[str, int] | None = None
    ) -> dict[str, str]:
        await self.resolve()
        try:
            return await super().call(service, action, values, target)
        except (OSError, TimeoutError):
            if target is not None:
                raise
            # The room may have a new IP address: look it up again and retry once.
            await self.resolve(force=True)
            return await super().call(service, action, values)
