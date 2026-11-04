"""Virtual UPnP/DLNA MediaRenderer that plays the T2 W through a Sonos room.

The Pro-Ject app selects this renderer. When the T2 W sends Play, the Sonos
room is told to play the local relay stream; Stop, Pause and volume are
forwarded. Subscribers (the T2 W, the app) get standard GENA state events.
A watchdog restarts Sonos if it stops on its own during a session.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import random
import re
import socket
import time
import uuid as uuid_module
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Awaitable, Callable, Final, Iterable, Protocol
from urllib.parse import urlsplit

from soap import fault_envelope, http_request, parse_envelope, response_envelope, xml_escape

LOG: Final = logging.getLogger("t2w.upnp")

DEVICE_TYPE: Final = "urn:schemas-upnp-org:device:MediaRenderer:1"
AVTRANSPORT: Final = "urn:schemas-upnp-org:service:AVTransport:1"
CONNECTION_MANAGER: Final = "urn:schemas-upnp-org:service:ConnectionManager:1"
RENDERING_CONTROL: Final = "urn:schemas-upnp-org:service:RenderingControl:1"
AVT_EVENT_NS: Final = "urn:schemas-upnp-org:metadata-1-0/AVT/"
RCS_EVENT_NS: Final = "urn:schemas-upnp-org:metadata-1-0/RCS/"
SERVER_HEADER: Final = "Linux UPnP/1.0 T2W-Sonos/1.0"
SSDP_ADDRESS: Final = "239.255.255.250"
SSDP_PORT: Final = 1900
SSDP_MAX_AGE: Final = 1800
HTTP_MAX_HEADER: Final = 16 * 1024
HTTP_MAX_BODY: Final = 64 * 1024
# What this renderer claims to accept. The T2 W chooses its stream URL from
# this list; the relay always reads the FLAC endpoint regardless.
SINK_PROTOCOL_INFO: Final = ",".join(
    f"http-get:*:{mime}:*"
    for mime in (
        "audio/flac",
        "audio/x-flac",
        "audio/mpeg",
        "audio/mp4",
        "audio/aac",
        "audio/wav",
        "audio/L16;rate=48000;channels=2",
        "application/vnd.apple.mpegurl",
        "application/x-mpegurl",
    )
)

IN, OUT = "in", "out"
INSTANCE: Final = ("InstanceID", IN, "A_ARG_TYPE_InstanceID")
CHANNEL: Final = ("Channel", IN, "A_ARG_TYPE_Channel")

Argument = tuple[str, str, str]
Allowed = list[str] | tuple[int, int, int] | None


@dataclass(frozen=True)
class ServiceSpec:
    key: str
    service_type: str
    service_id: str
    actions: dict[str, list[Argument]]
    variables: dict[str, tuple[str, Allowed]]
    evented: frozenset[str]


AVTRANSPORT_SPEC: Final = ServiceSpec(
    "avtransport",
    AVTRANSPORT,
    "urn:upnp-org:serviceId:AVTransport",
    {
        "SetAVTransportURI": [INSTANCE, ("CurrentURI", IN, "AVTransportURI"), ("CurrentURIMetaData", IN, "AVTransportURIMetaData")],
        "SetNextAVTransportURI": [INSTANCE, ("NextURI", IN, "NextAVTransportURI"), ("NextURIMetaData", IN, "NextAVTransportURIMetaData")],
        "GetMediaInfo": [
            INSTANCE,
            ("NrTracks", OUT, "NumberOfTracks"),
            ("MediaDuration", OUT, "CurrentMediaDuration"),
            ("CurrentURI", OUT, "AVTransportURI"),
            ("CurrentURIMetaData", OUT, "AVTransportURIMetaData"),
            ("NextURI", OUT, "NextAVTransportURI"),
            ("NextURIMetaData", OUT, "NextAVTransportURIMetaData"),
            ("PlayMedium", OUT, "PlaybackStorageMedium"),
            ("RecordMedium", OUT, "RecordStorageMedium"),
            ("WriteStatus", OUT, "RecordMediumWriteStatus"),
        ],
        "GetTransportInfo": [
            INSTANCE,
            ("CurrentTransportState", OUT, "TransportState"),
            ("CurrentTransportStatus", OUT, "TransportStatus"),
            ("CurrentSpeed", OUT, "TransportPlaySpeed"),
        ],
        "GetPositionInfo": [
            INSTANCE,
            ("Track", OUT, "CurrentTrack"),
            ("TrackDuration", OUT, "CurrentTrackDuration"),
            ("TrackMetaData", OUT, "CurrentTrackMetaData"),
            ("TrackURI", OUT, "CurrentTrackURI"),
            ("RelTime", OUT, "RelativeTimePosition"),
            ("AbsTime", OUT, "AbsoluteTimePosition"),
            ("RelCount", OUT, "RelativeCounterPosition"),
            ("AbsCount", OUT, "AbsoluteCounterPosition"),
        ],
        "GetDeviceCapabilities": [
            INSTANCE,
            ("PlayMedia", OUT, "PossiblePlaybackStorageMedia"),
            ("RecMedia", OUT, "PossibleRecordStorageMedia"),
            ("RecQualityModes", OUT, "PossibleRecordQualityModes"),
        ],
        "GetTransportSettings": [INSTANCE, ("PlayMode", OUT, "CurrentPlayMode"), ("RecQualityMode", OUT, "CurrentRecordQualityMode")],
        "GetCurrentTransportActions": [INSTANCE, ("Actions", OUT, "CurrentTransportActions")],
        "Play": [INSTANCE, ("Speed", IN, "TransportPlaySpeed")],
        "Stop": [INSTANCE],
        "Pause": [INSTANCE],
    },
    {
        "TransportState": ("string", ["STOPPED", "PLAYING", "PAUSED_PLAYBACK", "TRANSITIONING", "NO_MEDIA_PRESENT"]),
        "TransportStatus": ("string", ["OK", "ERROR_OCCURRED"]),
        "PlaybackStorageMedium": ("string", ["NETWORK", "NONE"]),
        "RecordStorageMedium": ("string", ["NOT_IMPLEMENTED"]),
        "PossiblePlaybackStorageMedia": ("string", None),
        "PossibleRecordStorageMedia": ("string", None),
        "CurrentPlayMode": ("string", ["NORMAL"]),
        "TransportPlaySpeed": ("string", ["1"]),
        "RecordMediumWriteStatus": ("string", ["NOT_IMPLEMENTED"]),
        "CurrentRecordQualityMode": ("string", ["NOT_IMPLEMENTED"]),
        "PossibleRecordQualityModes": ("string", None),
        "NumberOfTracks": ("ui4", (0, 1, 1)),
        "CurrentTrack": ("ui4", (0, 1, 1)),
        "CurrentTrackDuration": ("string", None),
        "CurrentMediaDuration": ("string", None),
        "CurrentTrackMetaData": ("string", None),
        "CurrentTrackURI": ("string", None),
        "AVTransportURI": ("string", None),
        "AVTransportURIMetaData": ("string", None),
        "NextAVTransportURI": ("string", None),
        "NextAVTransportURIMetaData": ("string", None),
        "RelativeTimePosition": ("string", None),
        "AbsoluteTimePosition": ("string", None),
        "RelativeCounterPosition": ("i4", None),
        "AbsoluteCounterPosition": ("i4", None),
        "CurrentTransportActions": ("string", None),
        "LastChange": ("string", None),
        "A_ARG_TYPE_InstanceID": ("ui4", None),
    },
    frozenset({"LastChange"}),
)

CONNECTION_MANAGER_SPEC: Final = ServiceSpec(
    "connectionmanager",
    CONNECTION_MANAGER,
    "urn:upnp-org:serviceId:ConnectionManager",
    {
        "GetProtocolInfo": [("Source", OUT, "SourceProtocolInfo"), ("Sink", OUT, "SinkProtocolInfo")],
        "GetCurrentConnectionIDs": [("ConnectionIDs", OUT, "CurrentConnectionIDs")],
        "GetCurrentConnectionInfo": [
            ("ConnectionID", IN, "A_ARG_TYPE_ConnectionID"),
            ("RcsID", OUT, "A_ARG_TYPE_RcsID"),
            ("AVTransportID", OUT, "A_ARG_TYPE_AVTransportID"),
            ("ProtocolInfo", OUT, "A_ARG_TYPE_ProtocolInfo"),
            ("PeerConnectionManager", OUT, "A_ARG_TYPE_ConnectionManager"),
            ("PeerConnectionID", OUT, "A_ARG_TYPE_ConnectionID"),
            ("Direction", OUT, "A_ARG_TYPE_Direction"),
            ("Status", OUT, "A_ARG_TYPE_ConnectionStatus"),
        ],
    },
    {
        "SourceProtocolInfo": ("string", None),
        "SinkProtocolInfo": ("string", None),
        "CurrentConnectionIDs": ("string", None),
        "A_ARG_TYPE_ConnectionStatus": (
            "string",
            ["OK", "ContentFormatMismatch", "InsufficientBandwidth", "UnreliableChannel", "Unknown"],
        ),
        "A_ARG_TYPE_ConnectionManager": ("string", None),
        "A_ARG_TYPE_Direction": ("string", ["Input", "Output"]),
        "A_ARG_TYPE_ProtocolInfo": ("string", None),
        "A_ARG_TYPE_ConnectionID": ("i4", None),
        "A_ARG_TYPE_AVTransportID": ("i4", None),
        "A_ARG_TYPE_RcsID": ("i4", None),
    },
    frozenset({"SourceProtocolInfo", "SinkProtocolInfo", "CurrentConnectionIDs"}),
)

RENDERING_CONTROL_SPEC: Final = ServiceSpec(
    "renderingcontrol",
    RENDERING_CONTROL,
    "urn:upnp-org:serviceId:RenderingControl",
    {
        "ListPresets": [INSTANCE, ("CurrentPresetNameList", OUT, "PresetNameList")],
        "SelectPreset": [INSTANCE, ("PresetName", IN, "A_ARG_TYPE_PresetName")],
        "GetMute": [INSTANCE, CHANNEL, ("CurrentMute", OUT, "Mute")],
        "SetMute": [INSTANCE, CHANNEL, ("DesiredMute", IN, "Mute")],
        "GetVolume": [INSTANCE, CHANNEL, ("CurrentVolume", OUT, "Volume")],
        "SetVolume": [INSTANCE, CHANNEL, ("DesiredVolume", IN, "Volume")],
    },
    {
        "PresetNameList": ("string", None),
        "LastChange": ("string", None),
        "Mute": ("boolean", None),
        "Volume": ("ui2", (0, 100, 1)),
        "A_ARG_TYPE_Channel": ("string", ["Master"]),
        "A_ARG_TYPE_InstanceID": ("ui4", None),
        "A_ARG_TYPE_PresetName": ("string", ["FactoryDefaults"]),
    },
    frozenset({"LastChange"}),
)

SERVICES: Final = {spec.key: spec for spec in (AVTRANSPORT_SPEC, CONNECTION_MANAGER_SPEC, RENDERING_CONTROL_SPEC)}


class UPnPError(Exception):
    def __init__(self, code: int, description: str) -> None:
        super().__init__(f"{code} {description}")
        self.code = code
        self.description = description


@dataclass(frozen=True)
class RendererConfig:
    friendly_name: str
    uuid: str
    advertised_host: str
    http_port: int
    relay_url: str
    # URL-safe id of the room; each virtual speaker lives under /dev/<key>/.
    key: str = "room"
    # The T2 W firmware (libnsdk_pst.so) only sends its lossless FLAC stream to
    # renderers whose manufacturer contains "Sonos, Inc.", "Pro-Ject Audio
    # Systems" or (for Rose devices) "CITECH"; everyone else gets MP3.
    manufacturer: str = "Pro-Ject Audio Systems"
    watchdog_interval_seconds: float = 5.0
    # Report PLAYING to the T2 W after this long even if Sonos hasn't answered
    # yet (the T2 W falls back to MP3 if a renderer isn't playing after 9 s).
    report_playing_after_seconds: float = 5.0
    # How long Sonos may be absent from the relay before the watchdog checks it.
    watchdog_grace_seconds: float = 10.0
    max_restarts: int = 5
    restart_window_seconds: float = 600.0
    # Source recovery: if no audio has come from the T2 W this long after its
    # Play, ask it to start its stream again (as re-selecting the speaker does).
    source_recovery_seconds: float = 20.0
    max_source_recoveries: int = 2
    source_recovery_window_seconds: float = 600.0
    # Time for the T2 W to see this speaker as stopped and release its encoder.
    recovery_settle_seconds: float = 3.0
    # How long to wait for the T2 W's new Play after asking it to restart.
    recovery_play_wait_seconds: float = 25.0

    def __post_init__(self) -> None:
        if not self.uuid.startswith("uuid:"):
            raise ValueError("uuid must begin with 'uuid:'")
        if not re.fullmatch(r"[a-z0-9-]+", self.key):
            raise ValueError("key must be lowercase letters, digits or '-'")
        ipaddress.IPv4Address(self.advertised_host)

    @property
    def base_url(self) -> str:
        return f"http://{self.advertised_host}:{self.http_port}"

    @property
    def path_prefix(self) -> str:
        return f"/dev/{self.key}"

    @property
    def location(self) -> str:
        return f"{self.base_url}{self.path_prefix}/description.xml"


class SonosControl(Protocol):
    async def play_uri(self, uri: str, title: str) -> None: ...
    async def stop(self) -> None: ...
    async def pause(self) -> None: ...
    async def transport(self) -> tuple[str, str]: ...
    async def get_volume(self) -> int: ...
    async def set_volume(self, volume: int) -> None: ...
    async def get_mute(self) -> bool: ...
    async def set_mute(self, mute: bool) -> None: ...


class StreamTrigger(Protocol):
    async def stream_to(self, renderer_udn: str) -> None: ...


# --------------------------------------------------------------------------- descriptions


def build_device_description(config: RendererConfig) -> bytes:
    services = "".join(
        "<service>"
        f"<serviceType>{spec.service_type}</serviceType><serviceId>{spec.service_id}</serviceId>"
        f"<SCPDURL>{config.path_prefix}/upnp/{spec.key}.xml</SCPDURL>"
        f"<controlURL>{config.path_prefix}/upnp/control/{spec.key}</controlURL>"
        f"<eventSubURL>{config.path_prefix}/upnp/event/{spec.key}</eventSubURL>"
        "</service>"
        for spec in SERVICES.values()
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<root xmlns="urn:schemas-upnp-org:device-1-0" xmlns:dlna="urn:schemas-dlna-org:device-1-0">'
        "<specVersion><major>1</major><minor>0</minor></specVersion>"
        "<device>"
        f"<deviceType>{DEVICE_TYPE}</deviceType>"
        f"<friendlyName>{xml_escape(config.friendly_name)}</friendlyName>"
        f"<manufacturer>{xml_escape(config.manufacturer)}</manufacturer>"
        "<modelDescription>Lossless T2 W to Sonos renderer</modelDescription>"
        "<modelName>T2W Sonos Relay</modelName><modelNumber>1.0</modelNumber>"
        f"<serialNumber>{xml_escape(config.uuid[5:])}</serialNumber>"
        f"<UDN>{xml_escape(config.uuid)}</UDN>"
        "<dlna:X_DLNADOC>DMR-1.50</dlna:X_DLNADOC>"
        f"<serviceList>{services}</serviceList>"
        f"<presentationURL>{xml_escape(config.base_url)}/healthz</presentationURL>"
        "</device></root>"
    ).encode("utf-8")


def build_scpd(spec: ServiceSpec) -> bytes:
    parts = [
        '<?xml version="1.0" encoding="utf-8"?><scpd xmlns="urn:schemas-upnp-org:service-1-0">'
        "<specVersion><major>1</major><minor>0</minor></specVersion><actionList>"
    ]
    for name, arguments in spec.actions.items():
        parts.append(f"<action><name>{name}</name><argumentList>")
        for argument, direction, variable in arguments:
            parts.append(
                f"<argument><name>{argument}</name><direction>{direction}</direction>"
                f"<relatedStateVariable>{variable}</relatedStateVariable></argument>"
            )
        parts.append("</argumentList></action>")
    parts.append("</actionList><serviceStateTable>")
    for name, (data_type, allowed) in spec.variables.items():
        events = "yes" if name in spec.evented else "no"
        parts.append(f'<stateVariable sendEvents="{events}"><name>{name}</name><dataType>{data_type}</dataType>')
        if isinstance(allowed, list):
            parts.append("<allowedValueList>")
            parts.extend(f"<allowedValue>{value}</allowedValue>" for value in allowed)
            parts.append("</allowedValueList>")
        elif isinstance(allowed, tuple):
            minimum, maximum, step = allowed
            parts.append(
                f"<allowedValueRange><minimum>{minimum}</minimum><maximum>{maximum}</maximum>"
                f"<step>{step}</step></allowedValueRange>"
            )
        parts.append("</stateVariable>")
    parts.append("</serviceStateTable></scpd>")
    return "".join(parts).encode("utf-8")


def last_change(namespace: str, values: dict[str, str], channel: bool = False) -> str:
    channel_attribute = ' channel="Master"' if channel else ""
    inner = "".join(f'<{key}{channel_attribute} val="{xml_escape(value)}"/>' for key, value in values.items())
    return f'<Event xmlns="{namespace}"><InstanceID val="0">{inner}</InstanceID></Event>'


def property_set(properties: dict[str, str]) -> bytes:
    inner = "".join(f"<e:property><{key}>{xml_escape(value)}</{key}></e:property>" for key, value in properties.items())
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">{inner}</e:propertyset>'
    ).encode("utf-8")


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


# --------------------------------------------------------------------------- GENA events


def is_lan_address(host: str | None) -> bool:
    try:
        address = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def parse_callbacks(header: str) -> list[str]:
    """Event callback URLs from a CALLBACK header, limited to http on the LAN."""
    callbacks = []
    for url in re.findall(r"<([^>]+)>", header):
        parsed = urlsplit(url)
        if parsed.scheme == "http" and is_lan_address(parsed.hostname) and parsed.port != 0:
            callbacks.append(url)
    return callbacks


def parse_timeout(header: str | None) -> int:
    match = re.fullmatch(r"\s*Second-(\d+)\s*", header or "", re.IGNORECASE)
    requested = int(match.group(1)) if match else SSDP_MAX_AGE
    return max(60, min(requested, 3600))


@dataclass
class Subscription:
    sid: str
    service: str
    callbacks: list[str]
    expires_at: float
    sequence: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Subscriptions:
    MAX_SUBSCRIPTIONS = 64

    def __init__(self) -> None:
        self._subscriptions: dict[str, Subscription] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def _expire(self) -> None:
        now = time.monotonic()
        for sid in [sid for sid, sub in self._subscriptions.items() if sub.expires_at <= now]:
            del self._subscriptions[sid]

    def subscribe(self, service: str, callbacks: list[str], timeout: int) -> str:
        self._expire()
        if len(self._subscriptions) >= self.MAX_SUBSCRIPTIONS:
            oldest = min(self._subscriptions.values(), key=lambda sub: sub.expires_at)
            del self._subscriptions[oldest.sid]
        sid = f"uuid:{uuid_module.uuid4()}"
        self._subscriptions[sid] = Subscription(sid, service, callbacks, time.monotonic() + timeout)
        return sid

    def renew(self, sid: str, service: str, timeout: int) -> bool:
        self._expire()
        subscription = self._subscriptions.get(sid)
        if subscription is None or subscription.service != service:
            return False
        subscription.expires_at = time.monotonic() + timeout
        return True

    def unsubscribe(self, sid: str, service: str) -> bool:
        subscription = self._subscriptions.get(sid)
        if subscription is None or subscription.service != service:
            return False
        del self._subscriptions[sid]
        return True

    def count(self) -> int:
        self._expire()
        return len(self._subscriptions)

    def publish(self, service: str, properties: dict[str, str], only_sid: str | None = None) -> None:
        """Queue an event to every subscriber of ``service`` (or just ``only_sid``)."""
        self._expire()
        body = property_set(properties)
        for subscription in list(self._subscriptions.values()):
            if subscription.service == service and (only_sid is None or subscription.sid == only_sid):
                task = asyncio.create_task(self._send(subscription, body))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    async def _send(self, subscription: Subscription, body: bytes) -> None:
        async with subscription.lock:
            sequence = subscription.sequence
            subscription.sequence = 1 if sequence >= 0xFFFFFFFF else sequence + 1
            for url in subscription.callbacks:
                parsed = urlsplit(url)
                path = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
                try:
                    status, _headers, _body = await http_request(
                        parsed.hostname or "",
                        parsed.port or 80,
                        "NOTIFY",
                        path,
                        {
                            "Content-Type": 'text/xml; charset="utf-8"',
                            "NT": "upnp:event",
                            "NTS": "upnp:propchange",
                            "SID": subscription.sid,
                            "SEQ": str(sequence),
                        },
                        body,
                        timeout=5,
                    )
                    if status == 200:
                        return
                    LOG.debug("event to %s answered HTTP %d", url, status)
                except (OSError, ValueError, TimeoutError) as exc:
                    LOG.debug("event to %s failed: %s", url, exc)

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(BaseException):
                await task


# --------------------------------------------------------------------------- renderer


class Renderer:
    """Renderer state, UPnP actions, Sonos control and the watchdog."""

    def __init__(
        self,
        config: RendererConfig,
        sonos: SonosControl,
        relay_clients: Callable[[], Awaitable[int]],
        subscriptions: Subscriptions,
        t2w: StreamTrigger | None = None,
        source_audio_at: Callable[[], float | None] | None = None,
    ) -> None:
        self.config = config
        self._sonos = sonos
        self._relay_clients = relay_clients
        self._events = subscriptions
        # Both set: restart the T2 W stream when its audio never arrives.
        self._t2w = t2w
        self._source_audio_at = source_audio_at
        self.transport_state = "NO_MEDIA_PRESENT"
        self.transport_status = "OK"
        self.uri = ""
        self.uri_metadata = ""
        self.volume = 0
        self.mute = False
        self.session_active = False
        self.last_error: str | None = None
        self.sonos_restarts = 0
        self.source_recoveries = 0
        self._play_at: float | None = None
        self._recovery_times: deque[float] = deque()
        self._recovery_task: asyncio.Task[None] | None = None
        self._recovery_given_up_for: float | None = None
        self._playing_since: float | None = None
        self._start_task: asyncio.Task[None] | None = None
        self._restart_times: deque[float] = deque()
        self._no_client_since: float | None = None
        self._volume_checked_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def sonos(self) -> SonosControl:
        return self._sonos

    def rename(self, friendly_name: str) -> None:
        """New name in the Pro-Ject app, e.g. after the Sonos room was renamed."""
        if friendly_name != self.config.friendly_name:
            LOG.info("speaker %r is now called %r", self.config.friendly_name, friendly_name)
            self.config = replace(self.config, friendly_name=friendly_name)

    def close(self) -> None:
        """Forget background work; the speaker is being removed."""
        for task in (self._start_task, self._recovery_task):
            if task is not None and not task.done():
                task.cancel()

    # ----- state and events

    def _transport_actions(self) -> str:
        if self.transport_state == "PLAYING":
            return "Pause,Stop"
        if self.transport_state in ("STOPPED", "PAUSED_PLAYBACK"):
            return "Play,Stop" if self.uri else ""
        if self.transport_state == "TRANSITIONING":
            return "Stop"
        return ""

    def evented_properties(self, service: str) -> dict[str, str]:
        if service == "avtransport":
            return {
                "LastChange": last_change(
                    AVT_EVENT_NS,
                    {
                        "TransportState": self.transport_state,
                        "TransportStatus": self.transport_status,
                        "TransportPlaySpeed": "1",
                        "CurrentPlayMode": "NORMAL",
                        "PlaybackStorageMedium": "NETWORK" if self.uri else "NONE",
                        "NumberOfTracks": "1" if self.uri else "0",
                        "CurrentTrack": "1" if self.uri else "0",
                        "AVTransportURI": self.uri,
                        "AVTransportURIMetaData": self.uri_metadata,
                        "CurrentTrackURI": self.uri,
                        "CurrentTrackMetaData": self.uri_metadata,
                        "CurrentTransportActions": self._transport_actions(),
                    },
                )
            }
        if service == "renderingcontrol":
            return {
                "LastChange": last_change(
                    RCS_EVENT_NS, {"Volume": str(self.volume), "Mute": "1" if self.mute else "0"}, channel=True
                )
            }
        return {"SourceProtocolInfo": "", "SinkProtocolInfo": SINK_PROTOCOL_INFO, "CurrentConnectionIDs": "0"}

    def _changed(self, service: str = "avtransport") -> None:
        self._events.publish(f"{self.config.key}/{service}", self.evented_properties(service))

    def _set_transport(self, state: str, status: str = "OK") -> None:
        if state == "PLAYING" and self.transport_state != "PLAYING":
            self._playing_since = time.monotonic()
        elif state != "PLAYING":
            self._playing_since = None
        changed = (state, status) != (self.transport_state, self.transport_status)
        self.transport_state = state
        self.transport_status = status
        if changed:
            LOG.info("renderer state: %s%s", state, "" if status == "OK" else f" ({status})")
            self._changed()

    # ----- transport

    async def set_uri(self, uri: str, metadata: str) -> None:
        async with self._lock:
            LOG.info("T2 W set stream URI: %s", uri or "(empty)")
            if uri and "flac" not in uri.lower():
                LOG.warning(
                    "the T2 W offers a compressed stream (%s), not FLAC: check that 'Lossless' is on in the T2 W "
                    "settings; the relay only plays the T2 W FLAC stream",
                    uri,
                )
            self.uri = uri
            self.uri_metadata = metadata
            if self.transport_state == "NO_MEDIA_PRESENT" and uri:
                self._set_transport("STOPPED")
            else:
                self._changed()

    async def play(self) -> None:
        async with self._lock:
            if not self.uri:
                raise UPnPError(701, "Transition not available")
            already_playing = self.session_active and self.transport_state == "PLAYING"
            self.session_active = True
            self._play_at = time.monotonic()
            self._no_client_since = None
            self._restart_times.clear()
            if not already_playing:
                # A repeated Play keeps reporting PLAYING: no needless state flapping.
                self._set_transport("TRANSITIONING")
            self._cancel_start()
            # Answer the T2 W at once; Sonos is started in the background.
            self._start_task = asyncio.create_task(self._start_sonos(), name="sonos-start")

    def _cancel_start(self) -> None:
        if self._start_task is not None and not self._start_task.done():
            self._start_task.cancel()
        self._start_task = None

    async def _sonos_is_playing_relay(self) -> bool:
        try:
            state, uri = await self._sonos.transport()
        except Exception:
            return False
        return uri == self.config.relay_url and state in ("PLAYING", "TRANSITIONING")

    def _report_playing_early(self) -> None:
        """Sonos is slow to answer: report PLAYING anyway.

        The T2 W checks 9 s after Play whether the renderer plays its stream and
        otherwise falls back to MP3 and shows "Lossless stream failed to start".
        A home theater set busy with its TV input can take far longer to answer.
        """
        if self.session_active and self.transport_state == "TRANSITIONING":
            LOG.info("Sonos is slow to answer; reporting PLAYING so the T2 W keeps its lossless stream")
            self._set_transport("PLAYING")

    async def _start_sonos(self) -> None:
        early = asyncio.get_running_loop().call_later(self.config.report_playing_after_seconds, self._report_playing_early)
        try:
            if await self._sonos_is_playing_relay():
                # A repeated Play (app re-selection, T2 W retry): restarting Sonos
                # would only cause an audible gap.
                LOG.info("Sonos already plays %s", self.config.relay_url)
            else:
                await self._sonos.play_uri(self.config.relay_url, self.config.friendly_name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A slow answer (e.g. a home theater set) can time out although
            # Sonos did start: check before calling it a failure.
            if not await self._sonos_is_playing_relay():
                async with self._lock:
                    self.last_error = f"Sonos play failed: {type(exc).__name__}: {exc}"
                    LOG.error("%s", self.last_error)
                    self.session_active = False
                    # Report STOPPED/OK so the app keeps the speaker selectable.
                    self._set_transport("STOPPED")
                return
            LOG.info("Sonos answered slowly (%s) but is playing", type(exc).__name__)
        finally:
            early.cancel()
        async with self._lock:
            if self.session_active:
                LOG.info("Sonos is playing %s", self.config.relay_url)
                self.last_error = None
                self._set_transport("PLAYING")

    async def stop(self) -> None:
        async with self._lock:
            self._cancel_start()
            if self.session_active:
                self.session_active = False
                try:
                    await self._sonos.stop()
                except Exception as exc:
                    LOG.warning("Sonos stop failed: %s", exc)
            if self.transport_state != "NO_MEDIA_PRESENT":
                self._set_transport("STOPPED")

    async def pause(self) -> None:
        async with self._lock:
            if self.transport_state not in ("PLAYING", "TRANSITIONING"):
                raise UPnPError(701, "Transition not available")
            self._cancel_start()
            try:
                await self._sonos.pause()
            except Exception as exc:
                LOG.warning("Sonos pause failed: %s", exc)
            self._set_transport("PAUSED_PLAYBACK")

    def elapsed(self) -> str:
        return format_duration(time.monotonic() - self._playing_since) if self._playing_since else "0:00:00"

    # ----- volume

    async def refresh_volume(self) -> None:
        if time.monotonic() - self._volume_checked_at < 5:
            return
        self._volume_checked_at = time.monotonic()
        try:
            volume, mute = await self._sonos.get_volume(), await self._sonos.get_mute()
        except Exception as exc:
            LOG.debug("volume query failed: %s", exc)
            return
        if (volume, mute) != (self.volume, self.mute):
            self.volume, self.mute = volume, mute
            self._changed("renderingcontrol")

    async def set_volume(self, volume: int) -> None:
        await self._sonos.set_volume(volume)
        self.volume = volume
        self._volume_checked_at = time.monotonic()
        self._changed("renderingcontrol")

    async def set_mute(self, mute: bool) -> None:
        await self._sonos.set_mute(mute)
        self.mute = mute
        self._volume_checked_at = time.monotonic()
        self._changed("renderingcontrol")

    # ----- watchdog

    async def watchdog_step(self, now: float) -> None:
        """One check: restart Sonos if it stopped on its own during a session."""
        if not self.session_active or self.transport_state not in ("PLAYING", "PAUSED_PLAYBACK"):
            self._no_client_since = None
            return
        if await self._relay_clients() > 0:
            self._no_client_since = None
            if self.transport_state == "PAUSED_PLAYBACK":
                async with self._lock:
                    self._set_transport("PLAYING")  # resumed from the Sonos app
            return
        if self.transport_state == "PAUSED_PLAYBACK":
            # Paused on purpose (T2 W or Sonos app): never restart a paused session.
            self._no_client_since = None
            return
        if self._no_client_since is None:
            self._no_client_since = now
            return
        if now - self._no_client_since < self.config.watchdog_grace_seconds:
            return
        try:
            sonos_state, sonos_uri = await self._sonos.transport()
        except Exception as exc:
            LOG.warning("watchdog: Sonos not reachable: %s", exc)
            return
        async with self._lock:
            if not self.session_active:
                return
            if sonos_uri and sonos_uri != self.config.relay_url:
                LOG.info("watchdog: Sonos now plays something else (%s); ending turntable session", sonos_uri)
                self.session_active = False
                self._set_transport("STOPPED")
                return
            if sonos_state == "PAUSED_PLAYBACK":
                self._set_transport("PAUSED_PLAYBACK")  # paused in the Sonos app: respect it
                return
            if sonos_state in ("PLAYING", "TRANSITIONING"):
                return  # Sonos is (re)connecting to the relay
            while self._restart_times and now - self._restart_times[0] > self.config.restart_window_seconds:
                self._restart_times.popleft()
            if len(self._restart_times) >= self.config.max_restarts:
                self.last_error = (
                    f"Sonos stopped {len(self._restart_times)} times within "
                    f"{self.config.restart_window_seconds / 60:.0f} minutes; giving up"
                )
                LOG.error("watchdog: %s", self.last_error)
                self.session_active = False
                self._set_transport("STOPPED")
                return
            self._restart_times.append(now)
            self.sonos_restarts += 1
            self._no_client_since = now
        LOG.warning("watchdog: Sonos stopped on its own (state %s); restarting playback", sonos_state or "unknown")
        try:
            await self._sonos.play_uri(self.config.relay_url, self.config.friendly_name)
        except Exception as exc:
            LOG.warning("watchdog: restart failed: %s", exc)

    # ----- source recovery

    @property
    def source_recovery_enabled(self) -> bool:
        return self._t2w is not None and self._source_audio_at is not None

    async def source_check(self, now: float) -> None:
        """Restart the T2 W stream if its audio never arrived after a Play.

        Seen after the T2 W wakes from standby (autoplay): it sends Play but its
        FLAC encoder delivers nothing. Asking it to stream again is ignored while
        it believes this speaker is playing, and its encoder start is a no-op
        while the old pipeline exists ("Pipeline already started"). Re-selecting
        the speaker fixes it: the T2 W sees the speaker stop, tears its encoder
        down and builds a fresh one. This does the same: report STOPPED, then
        send the app's own ``pst:stream`` for this speaker.
        """
        if not self.source_recovery_enabled or self._play_at is None:
            return
        if not self.session_active or self.transport_state != "PLAYING":
            return
        if self._recovery_task is not None and not self._recovery_task.done():
            return
        play_at = self._play_at
        if now - play_at < self.config.source_recovery_seconds:
            return
        last_audio = self._source_audio_at()  # type: ignore[misc]
        if last_audio is not None and last_audio >= play_at:
            return  # audio arrived after this Play
        if await self._relay_clients() == 0:
            return  # Sonos is not listening: that is the watchdog's business
        window = self.config.source_recovery_window_seconds
        while self._recovery_times and now - self._recovery_times[0] > window:
            self._recovery_times.popleft()
        if len(self._recovery_times) >= self.config.max_source_recoveries:
            if self._recovery_given_up_for != play_at:
                self._recovery_given_up_for = play_at
                self.last_error = (
                    f"no audio from the T2 W after {len(self._recovery_times)} stream restarts within "
                    f"{window / 60:.0f} minutes; select the speaker again in the Pro-Ject app"
                )
                LOG.error("source recovery: %s", self.last_error)
            return
        self._recovery_times.append(now)
        attempt = len(self._recovery_times)
        self._recovery_task = asyncio.create_task(
            self._recover_source(now - play_at, attempt), name=f"recover-{self.config.key}"
        )

    async def _recover_source(self, silent_for: float, attempt: int) -> None:
        config = self.config
        LOG.warning(
            "no audio from the T2 W %.0fs after its Play: asking it to restart the stream (attempt %d of %d)",
            silent_for,
            attempt,
            config.max_source_recoveries,
        )
        started = time.monotonic()
        async with self._lock:
            if not self.session_active:
                return
            self.source_recoveries += 1
            # The T2 W now releases this speaker and, if no other speaker plays,
            # stops its encoder. Sonos keeps the relay stream open meanwhile.
            self._set_transport("STOPPED")
        await asyncio.sleep(config.recovery_settle_seconds)
        try:
            await self._t2w.stream_to(config.uuid)  # type: ignore[union-attr]
        except Exception as exc:
            await self._end_failed_recovery(f"T2 W stream restart failed: {type(exc).__name__}: {exc}", started)
            return
        deadline = time.monotonic() + config.recovery_play_wait_seconds
        while time.monotonic() < deadline:
            if self._play_at is not None and self._play_at > started:
                LOG.info("source recovery: the T2 W started its stream again")
                return
            await asyncio.sleep(0.2)
        await self._end_failed_recovery("the T2 W did not start its stream again", started)

    async def _end_failed_recovery(self, reason: str, started: float) -> None:
        async with self._lock:
            if self._play_at is not None and self._play_at > started:
                return  # a new Play arrived after all
            self.last_error = f"{reason}; select the speaker again in the Pro-Ject app"
            LOG.error("source recovery: %s", self.last_error)
            if self.session_active:
                self.session_active = False
                try:
                    await self._sonos.stop()
                except Exception as exc:
                    LOG.warning("Sonos stop failed: %s", exc)
            self._set_transport("STOPPED")

    async def run_watchdog(self, restart_sonos: bool = True) -> None:
        while True:
            await asyncio.sleep(self.config.watchdog_interval_seconds)
            try:
                now = time.monotonic()
                await self.source_check(now)
                if restart_sonos:
                    await self.watchdog_step(now)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("watchdog step failed")

    # ----- SOAP actions

    async def handle_action(self, service: str, action: str, arguments: dict[str, str]) -> dict[str, str]:
        spec = SERVICES[service]
        if action not in spec.actions:
            raise UPnPError(401, "Invalid Action")
        for name, direction, _variable in spec.actions[action]:
            if direction == IN and name not in arguments:
                raise UPnPError(402, f"Invalid Args: missing {name}")
        if "InstanceID" in arguments and arguments["InstanceID"].strip() != "0":
            raise UPnPError(718, "Invalid InstanceID")
        result = await getattr(self, f"_{service}_{action}")(arguments)
        return {name: result[name] for name, direction, _variable in spec.actions[action] if direction == OUT}

    async def _avtransport_SetAVTransportURI(self, arguments: dict[str, str]) -> dict[str, str]:
        await self.set_uri(arguments["CurrentURI"].strip(), arguments.get("CurrentURIMetaData", ""))
        return {}

    async def _avtransport_SetNextAVTransportURI(self, _arguments: dict[str, str]) -> dict[str, str]:
        return {}  # a live turntable has no next track

    async def _avtransport_GetMediaInfo(self, _arguments: dict[str, str]) -> dict[str, str]:
        return {
            "NrTracks": "1" if self.uri else "0",
            "MediaDuration": "0:00:00",
            "CurrentURI": self.uri,
            "CurrentURIMetaData": self.uri_metadata,
            "NextURI": "",
            "NextURIMetaData": "",
            "PlayMedium": "NETWORK" if self.uri else "NONE",
            "RecordMedium": "NOT_IMPLEMENTED",
            "WriteStatus": "NOT_IMPLEMENTED",
        }

    async def _avtransport_GetTransportInfo(self, _arguments: dict[str, str]) -> dict[str, str]:
        return {
            "CurrentTransportState": self.transport_state,
            "CurrentTransportStatus": self.transport_status,
            "CurrentSpeed": "1",
        }

    async def _avtransport_GetPositionInfo(self, _arguments: dict[str, str]) -> dict[str, str]:
        elapsed = self.elapsed()
        return {
            "Track": "1" if self.uri else "0",
            "TrackDuration": "0:00:00",
            "TrackMetaData": self.uri_metadata,
            "TrackURI": self.uri,
            "RelTime": elapsed,
            "AbsTime": elapsed,
            "RelCount": "2147483647",
            "AbsCount": "2147483647",
        }

    async def _avtransport_GetDeviceCapabilities(self, _arguments: dict[str, str]) -> dict[str, str]:
        return {"PlayMedia": "NETWORK", "RecMedia": "NOT_IMPLEMENTED", "RecQualityModes": "NOT_IMPLEMENTED"}

    async def _avtransport_GetTransportSettings(self, _arguments: dict[str, str]) -> dict[str, str]:
        return {"PlayMode": "NORMAL", "RecQualityMode": "NOT_IMPLEMENTED"}

    async def _avtransport_GetCurrentTransportActions(self, _arguments: dict[str, str]) -> dict[str, str]:
        return {"Actions": self._transport_actions()}

    async def _avtransport_Play(self, _arguments: dict[str, str]) -> dict[str, str]:
        LOG.info("T2 W: Play")
        await self.play()
        return {}

    async def _avtransport_Stop(self, _arguments: dict[str, str]) -> dict[str, str]:
        LOG.info("T2 W: Stop")
        await self.stop()
        return {}

    async def _avtransport_Pause(self, _arguments: dict[str, str]) -> dict[str, str]:
        LOG.info("T2 W: Pause")
        await self.pause()
        return {}

    async def _connectionmanager_GetProtocolInfo(self, _arguments: dict[str, str]) -> dict[str, str]:
        return {"Source": "", "Sink": SINK_PROTOCOL_INFO}

    async def _connectionmanager_GetCurrentConnectionIDs(self, _arguments: dict[str, str]) -> dict[str, str]:
        return {"ConnectionIDs": "0"}

    async def _connectionmanager_GetCurrentConnectionInfo(self, arguments: dict[str, str]) -> dict[str, str]:
        if arguments["ConnectionID"].strip() != "0":
            raise UPnPError(706, "Invalid connection reference")
        return {
            "RcsID": "0",
            "AVTransportID": "0",
            "ProtocolInfo": "http-get:*:audio/flac:*",
            "PeerConnectionManager": "",
            "PeerConnectionID": "-1",
            "Direction": "Input",
            "Status": "OK",
        }

    @staticmethod
    def _check_channel(arguments: dict[str, str]) -> None:
        if arguments["Channel"].strip() != "Master":
            raise UPnPError(402, "Invalid Args: only the Master channel exists")

    async def _renderingcontrol_ListPresets(self, _arguments: dict[str, str]) -> dict[str, str]:
        return {"CurrentPresetNameList": "FactoryDefaults"}

    async def _renderingcontrol_SelectPreset(self, arguments: dict[str, str]) -> dict[str, str]:
        if arguments["PresetName"].strip() != "FactoryDefaults":
            raise UPnPError(701, "Invalid Name")
        return {}

    async def _renderingcontrol_GetVolume(self, arguments: dict[str, str]) -> dict[str, str]:
        self._check_channel(arguments)
        await self.refresh_volume()
        return {"CurrentVolume": str(self.volume)}

    async def _renderingcontrol_SetVolume(self, arguments: dict[str, str]) -> dict[str, str]:
        self._check_channel(arguments)
        try:
            volume = int(arguments["DesiredVolume"].strip())
        except ValueError as exc:
            raise UPnPError(402, "Invalid Args: volume is not a number") from exc
        if not 0 <= volume <= 100:
            raise UPnPError(601, "Argument Value Out of Range")
        try:
            await self.set_volume(volume)
        except Exception as exc:
            raise UPnPError(501, f"Action Failed: {exc}") from exc
        return {}

    async def _renderingcontrol_GetMute(self, arguments: dict[str, str]) -> dict[str, str]:
        self._check_channel(arguments)
        await self.refresh_volume()
        return {"CurrentMute": "1" if self.mute else "0"}

    async def _renderingcontrol_SetMute(self, arguments: dict[str, str]) -> dict[str, str]:
        self._check_channel(arguments)
        value = arguments["DesiredMute"].strip().lower()
        if value not in ("0", "1", "true", "false", "yes", "no"):
            raise UPnPError(402, "Invalid Args: mute must be boolean")
        try:
            await self.set_mute(value in ("1", "true", "yes"))
        except Exception as exc:
            raise UPnPError(501, f"Action Failed: {exc}") from exc
        return {}

    def health(self) -> dict[str, object]:
        return {
            "room": self.config.friendly_name,
            "renderer_state": self.transport_state,
            "turntable_session": self.session_active,
            "t2w_stream_uri": self.uri,
            "sonos_restarts_by_watchdog": self.sonos_restarts,
            "t2w_stream_restarts": self.source_recoveries,
            "t2w_stream_restart": "on" if self.source_recovery_enabled else "off",
            "event_subscriptions": self._events.count(),
            "last_renderer_error": self.last_error,
        }


# --------------------------------------------------------------------------- speaker registry


class Speakers:
    """The virtual speakers currently offered; rooms come and go at runtime."""

    def __init__(self, renderers: Iterable[Renderer] = ()) -> None:
        self._renderers: dict[str, Renderer] = {}
        self.changed = asyncio.Event()
        self.on_add: list[Callable[[Renderer], None]] = []
        self.on_remove: list[Callable[[Renderer], None]] = []
        for renderer in renderers:
            self.add(renderer)

    def add(self, renderer: Renderer) -> None:
        if renderer.config.key in self._renderers:
            raise ValueError(f"speaker {renderer.config.key!r} exists already")
        self._renderers[renderer.config.key] = renderer
        for hook in self.on_add:
            hook(renderer)
        self.changed.set()

    def remove(self, key: str) -> Renderer | None:
        renderer = self._renderers.pop(key, None)
        if renderer is not None:
            for hook in self.on_remove:
                hook(renderer)
            renderer.close()
            self.changed.set()
        return renderer

    def get(self, key: str) -> Renderer | None:
        return self._renderers.get(key)

    def renderers(self) -> list[Renderer]:
        return list(self._renderers.values())

    def configs(self) -> list[RendererConfig]:
        return [renderer.config for renderer in self._renderers.values()]

    def __len__(self) -> int:
        return len(self._renderers)


# --------------------------------------------------------------------------- HTTP server


def _http_response(status: str, headers: dict[str, str] | None = None, body: bytes = b"") -> bytes:
    lines = [f"HTTP/1.1 {status}", f"SERVER: {SERVER_HEADER}", f"Content-Length: {len(body)}", "Connection: close"]
    lines.extend(f"{key}: {value}" for key, value in (headers or {}).items())
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


async def _read_request(reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str], bytes]:
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=8)
    if len(head) > HTTP_MAX_HEADER:
        raise ValueError("request header too large")
    lines = head[:-4].decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or parts[2] not in ("HTTP/1.0", "HTTP/1.1"):
        raise ValueError("malformed request line")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    length_header = headers.get("content-length", "0")
    if not length_header.isdigit() or int(length_header) > HTTP_MAX_BODY:
        raise ValueError("invalid content length")
    length = int(length_header)
    body = await asyncio.wait_for(reader.readexactly(length), timeout=8) if length else b""
    return parts[0].upper(), parts[1].split("?", 1)[0], headers, body


class RendererServer:
    """One HTTP server for all virtual speakers; each lives under /dev/<key>/."""

    def __init__(self, speakers: Speakers | Iterable[Renderer], subscriptions: Subscriptions) -> None:
        self.speakers = speakers if isinstance(speakers, Speakers) else Speakers(speakers)
        self.subscriptions = subscriptions

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = (writer.get_extra_info("peername") or ("", 0))[0]
        try:
            if not is_lan_address(peer):
                writer.write(_http_response("403 Forbidden"))
                return
            try:
                method, path, headers, body = await _read_request(reader)
            except (ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
                writer.write(_http_response("400 Bad Request"))
                return
            device = re.fullmatch(r"/dev/([a-z0-9-]+)(/.*)", path)
            renderer = self.speakers.get(device.group(1)) if device else None
            if device is None or renderer is None:
                writer.write(_http_response("404 Not Found"))
                return
            await self._route(renderer, method, device.group(2), headers, body, writer, peer)
        except Exception:
            LOG.exception("renderer request from %s failed", peer)
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.drain(), timeout=8)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _route(
        self,
        renderer: Renderer,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
        writer: asyncio.StreamWriter,
        peer: str,
    ) -> None:
        xml = {"Content-Type": 'text/xml; charset="utf-8"'}
        if method in ("GET", "HEAD") and path == "/description.xml":
            payload = build_device_description(renderer.config)
            writer.write(_http_response("200 OK", xml, payload if method == "GET" else b""))
            return
        match = re.fullmatch(r"/upnp/(?:(control|event)/)?([a-z]+)(\.xml)?", path)
        service = match.group(2) if match else ""
        if not match or service not in SERVICES:
            writer.write(_http_response("404 Not Found"))
            return
        kind, is_scpd = match.group(1), bool(match.group(3))
        if method in ("GET", "HEAD") and kind is None and is_scpd:
            payload = build_scpd(SERVICES[service])
            writer.write(_http_response("200 OK", xml, payload if method == "GET" else b""))
        elif method == "POST" and kind == "control" and not is_scpd:
            writer.write(await self._control(renderer, service, body, peer))
        elif method == "SUBSCRIBE" and kind == "event" and not is_scpd:
            await self._subscribe(renderer, service, headers, writer, peer)
        elif method == "UNSUBSCRIBE" and kind == "event" and not is_scpd:
            ok = self.subscriptions.unsubscribe(headers.get("sid", ""), f"{renderer.config.key}/{service}")
            writer.write(_http_response("200 OK" if ok else "412 Precondition Failed"))
        else:
            writer.write(_http_response("405 Method Not Allowed"))

    async def _control(self, renderer: Renderer, service: str, body: bytes, peer: str) -> bytes:
        xml = {"Content-Type": 'text/xml; charset="utf-8"', "EXT": ""}
        try:
            action, arguments = parse_envelope(body)
        except ValueError:
            return _http_response("500 Internal Server Error", xml, fault_envelope(401, "Invalid Action"))
        log = LOG.debug if action.startswith(("Get", "List")) else LOG.info
        room = renderer.config.key
        log("[%s] %s %s from %s", room, service, action, peer)
        try:
            result = await renderer.handle_action(service, action, arguments)
        except UPnPError as exc:
            LOG.info("[%s] %s %s from %s rejected: %s", room, service, action, peer, exc)
            return _http_response("500 Internal Server Error", xml, fault_envelope(exc.code, exc.description))
        except Exception:
            LOG.exception("%s %s failed", service, action)
            return _http_response("500 Internal Server Error", xml, fault_envelope(501, "Action Failed"))
        return _http_response("200 OK", xml, response_envelope(action, SERVICES[service].service_type, result))

    async def _subscribe(
        self, renderer: Renderer, service: str, headers: dict[str, str], writer: asyncio.StreamWriter, peer: str
    ) -> None:
        topic = f"{renderer.config.key}/{service}"
        timeout = parse_timeout(headers.get("timeout"))
        sid = headers.get("sid")
        if sid:
            if "callback" in headers or "nt" in headers:
                writer.write(_http_response("400 Bad Request"))
            elif self.subscriptions.renew(sid, topic, timeout):
                writer.write(_http_response("200 OK", {"SID": sid, "TIMEOUT": f"Second-{timeout}"}))
            else:
                writer.write(_http_response("412 Precondition Failed"))
            return
        callbacks = parse_callbacks(headers.get("callback", ""))
        if headers.get("nt") != "upnp:event" or not callbacks:
            writer.write(_http_response("412 Precondition Failed"))
            return
        sid = self.subscriptions.subscribe(topic, callbacks, timeout)
        LOG.info("%s subscribed to %s events", peer, topic)
        writer.write(_http_response("200 OK", {"SID": sid, "TIMEOUT": f"Second-{timeout}"}))
        await asyncio.wait_for(writer.drain(), timeout=8)
        # The initial event follows the SUBSCRIBE response, as the spec requires.
        self.subscriptions.publish(topic, renderer.evented_properties(service), only_sid=sid)


# --------------------------------------------------------------------------- SSDP


def ssdp_targets(uuid: str) -> list[str]:
    return ["upnp:rootdevice", uuid, DEVICE_TYPE, *(spec.service_type for spec in SERVICES.values())]


def ssdp_usn(uuid: str, target: str) -> str:
    return uuid if target == uuid else f"{uuid}::{target}"


def search_targets(st: str, uuid: str) -> list[str]:
    targets = ssdp_targets(uuid)
    if st == "ssdp:all":
        return targets
    return [st] if st in targets else []


def build_search_response(target: str, config: RendererConfig) -> bytes:
    return (
        "HTTP/1.1 200 OK\r\n"
        f"CACHE-CONTROL: max-age={SSDP_MAX_AGE}\r\n"
        "EXT:\r\n"
        f"LOCATION: {config.location}\r\n"
        f"SERVER: {SERVER_HEADER}\r\n"
        f"ST: {target}\r\n"
        f"USN: {ssdp_usn(config.uuid, target)}\r\n\r\n"
    ).encode("ascii")


def build_notify(target: str, config: RendererConfig, nts: str) -> bytes:
    lines = [
        "NOTIFY * HTTP/1.1",
        f"HOST: {SSDP_ADDRESS}:{SSDP_PORT}",
        f"NT: {target}",
        f"NTS: {nts}",
        f"USN: {ssdp_usn(config.uuid, target)}",
    ]
    if nts == "ssdp:alive":
        lines += [f"CACHE-CONTROL: max-age={SSDP_MAX_AGE}", f"LOCATION: {config.location}", f"SERVER: {SERVER_HEADER}"]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


def parse_search(data: bytes) -> tuple[str, int] | None:
    """Return (ST, MX) for a valid M-SEARCH request, else None."""
    lines = data.decode("latin-1", "replace").split("\r\n")
    if lines[0].strip().upper() != "M-SEARCH * HTTP/1.1":
        return None
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().upper()] = value.strip()
    if headers.get("MAN", "").strip('"').lower() != "ssdp:discover" or not headers.get("ST"):
        return None
    mx = int(headers["MX"]) if headers.get("MX", "").isdigit() else 1
    return headers["ST"], max(1, min(mx, 5))


ConfigSource = Speakers | Iterable[RendererConfig]


def _config_getter(source: ConfigSource) -> Callable[[], list[RendererConfig]]:
    if isinstance(source, Speakers):
        return source.configs
    fixed = list(source)
    return lambda: fixed


class SSDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, configs: ConfigSource) -> None:
        self._configs = _config_getter(configs)
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        search = parse_search(data)
        if search is None or not is_lan_address(addr[0]):
            return
        st, mx = search
        answers = [(target, config) for config in self._configs() for target in search_targets(st, config.uuid)]
        if not answers:
            return
        loop = asyncio.get_running_loop()
        # Spread the answers over part of MX, as the spec asks.
        loop.call_later(random.uniform(0, min(mx, 2) / 2), self._respond, answers, addr)

    def _respond(self, answers: list[tuple[str, RendererConfig]], addr: tuple[str, int]) -> None:
        if self.transport is None:
            return
        for target, config in answers:
            self.transport.sendto(build_search_response(target, config), addr)


def _multicast_socket(local_ip: str) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
    return sock


async def start_ssdp_listener(configs: ConfigSource, local_ip: str) -> asyncio.DatagramTransport:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    with contextlib.suppress(AttributeError, OSError):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("", SSDP_PORT))
    sock.setsockopt(
        socket.IPPROTO_IP,
        socket.IP_ADD_MEMBERSHIP,
        socket.inet_aton(SSDP_ADDRESS) + socket.inet_aton(local_ip),
    )
    sock.setblocking(False)
    transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(lambda: SSDPProtocol(configs), sock=sock)
    return transport


async def announce(speakers: Speakers, local_ip: str) -> None:
    """Keep the network informed: ssdp:alive for new or renamed speakers (three
    times) and every half max-age for all, ssdp:byebye for removed ones and on exit."""
    sock = _multicast_socket(local_ip)
    announced: dict[str, RendererConfig] = {}

    def send(configs: Iterable[RendererConfig], nts: str) -> None:
        for config in configs:
            for target in ssdp_targets(config.uuid):
                with contextlib.suppress(OSError):
                    sock.sendto(build_notify(target, config, nts), (SSDP_ADDRESS, SSDP_PORT))

    try:
        refresh_at = time.monotonic() + SSDP_MAX_AGE / 2
        while True:
            speakers.changed.clear()
            current = {config.uuid: config for config in speakers.configs()}
            send([config for uuid, config in announced.items() if uuid not in current], "ssdp:byebye")
            fresh = [config for uuid, config in current.items() if announced.get(uuid) != config]
            announced = current
            for _ in range(3 if fresh else 0):
                send(fresh, "ssdp:alive")
                await asyncio.sleep(0.5)
            if time.monotonic() >= refresh_at:
                send(announced.values(), "ssdp:alive")
                refresh_at = time.monotonic() + SSDP_MAX_AGE / 2
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(speakers.changed.wait(), max(1.0, refresh_at - time.monotonic()))
    finally:
        send(announced.values(), "ssdp:byebye")
        sock.close()
