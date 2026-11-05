"""T2 W → Sonos in one process: lossless gap-tolerant relay + virtual UPnP speakers.

    Pro-Ject app ──selects──▶ "T2 W – <room>" (one virtual speaker per Sonos room, this process)
    T2 W ──Play──▶ that speaker ──▶ the Sonos room plays http://<this host>:8186/stream.flac
    relay ──reads──▶ T2 W FLAC (port 8146) ──buffer──▶ one continuous FLAC stream to Sonos

Nothing has to be configured: the T2 W is found over mDNS, the Sonos rooms
over UPnP, and both are followed when addresses or room names change.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import socket
from dataclasses import dataclass
from typing import Awaitable, Callable, Final, Iterable, Mapping
from urllib.parse import urlsplit

from discovery import find_t2w
from rooms import DEFAULT_UUID, RoomSync, SonosRooms, SpeakerSettings, parse_room_list, room_key, room_uuid
from stream import MAX_REQUEST_BYTES, VERSION, RelayConfig, RelayService
from t2w import T2WControl
from upnp import (
    Renderer,
    RendererConfig,
    RendererServer,
    SonosControl,
    Speakers,
    StreamTrigger,
    Subscriptions,
    announce,
    start_ssdp_listener,
)

__all__ = ["room_key", "room_uuid", "run", "settings_from_environment"]

LOG: Final = logging.getLogger("t2w")
OFF_VALUES: Final = ("0", "false", "no", "off")
T2W_FLAC_PORT: Final = 8146
T2W_SEARCH_SECONDS: Final = 300.0

MakeRenderer = Callable[[RendererConfig, SonosControl], Renderer]


def detect_local_ip(target: str) -> str:
    """The local address used to reach ``target`` (no packet is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((target, 9))
        return sock.getsockname()[0]


def default_local_ip(t2w_host: str) -> str:
    for target in (t2w_host, "239.255.255.250", "192.0.2.1"):
        if not target:
            continue
        with contextlib.suppress(OSError):
            address = detect_local_ip(target)
            if not address.startswith(("0.", "127.")):
                return address
    raise SystemExit("cannot find this host's LAN address; set ADVERTISED_HOST")


def flac_url(host: str) -> str:
    return f"http://{host}:{T2W_FLAC_PORT}/stream.flac"


@dataclass(frozen=True)
class Settings:
    relay: RelayConfig
    speakers: SpeakerSettings
    seed_hosts: list[str]
    watchdog: bool
    # Empty: find the T2 W on the network.
    t2w_host: str
    # Restart the T2 W stream through its API when no audio arrives after Play.
    source_recovery: bool


def settings_from_environment(env: Mapping[str, str] = os.environ) -> Settings:
    t2w_host = env.get("T2W_HOST", "").strip()
    flac_override = env.get("T2W_FLAC_URL", "").strip()
    if flac_override and not t2w_host:
        # A fixed stream URL also names the turntable (for the stream restart).
        t2w_host = urlsplit(flac_override).hostname or ""
    include = parse_room_list(env.get("SONOS_ROOMS", ""))
    exclude = parse_room_list(env.get("SONOS_EXCLUDE", ""))
    advertised_host = env.get("ADVERTISED_HOST", "").strip() or default_local_ip(t2w_host)
    relay_port = int(env.get("RELAY_PORT", "8186"))
    renderer_port = int(env.get("RENDERER_PORT", "8090"))
    upstream = flac_override or (flac_url(t2w_host) if t2w_host else "")
    relay = RelayConfig(
        upstream_url=upstream,
        port=relay_port,
        sample_rate=int(env.get("SAMPLE_RATE", "48000")),
        channels=int(env.get("CHANNELS", "2")),
        buffer_seconds=float(env.get("BUFFER_SECONDS", "5")),
        buffer_start_seconds=float(env.get("BUFFER_START_SECONDS", "1")),
        idle_grace_seconds=float(env.get("IDLE_GRACE_SECONDS", "15")),
        ffmpeg=env.get("FFMPEG", "ffmpeg"),
    )
    speakers = SpeakerSettings(
        advertised_host=advertised_host,
        http_port=renderer_port,
        relay_url=f"http://{advertised_host}:{relay_port}/stream.flac",
        name_format=env.get("FRIENDLY_NAME_FORMAT", "T2 W – {room}"),
        base_uuid=env.get("RENDERER_UUID", DEFAULT_UUID),
        manufacturer=env.get("RENDERER_MANUFACTURER", "Pro-Ject Audio Systems"),
        source_recovery_seconds=float(env.get("SOURCE_RECOVERY_SECONDS", "20")),
        include=tuple(include),
        exclude=tuple(exclude),
    )
    seeds = parse_room_list(env.get("SONOS_SEED_HOSTS", ""))
    watchdog = env.get("WATCHDOG", "1").strip().lower() not in OFF_VALUES
    source_recovery = env.get("SOURCE_RECOVERY", "1").strip().lower() not in OFF_VALUES
    return Settings(relay, speakers, seeds, watchdog, t2w_host, source_recovery)


async def keep_t2w_found(
    relay: RelayService,
    t2w: T2WControl | None,
    local_ip: str,
    search: Callable[[str], Awaitable[list[str]]] = find_t2w,
    interval_seconds: float = T2W_SEARCH_SECONDS,
    retry_seconds: float = 15.0,
) -> None:
    """Find the T2 W and follow it if it gets a new IP address."""
    current = ""
    reported_missing = reported_several = False
    while True:
        try:
            hosts = await search(local_ip)
        except OSError as exc:
            LOG.warning("T2 W search failed: %s", exc)
            hosts = []
        if hosts:
            host = current if current in hosts else hosts[0]
            if len(hosts) > 1 and not reported_several:
                reported_several = True
                LOG.warning("several T2 W found (%s); using %s. Set T2W_HOST to choose.", ", ".join(hosts), host)
            if host != current:
                LOG.info("T2 W found at %s", host)
                current = host
                relay.set_upstream(flac_url(host))
                if t2w is not None:
                    t2w.host = host
            reported_missing = False
        elif not reported_missing:
            reported_missing = True
            LOG.warning(
                "T2 W not found%s; is it switched on and in the same network? Searching again.",
                f" (last seen at {current})" if current else "",
            )
        await asyncio.sleep(interval_seconds if current else retry_seconds)


async def run(
    relay_config: RelayConfig,
    rooms: Iterable[tuple[RendererConfig, SonosControl]] = (),
    watchdog: bool = True,
    stop: asyncio.Event | None = None,
    ssdp: bool = True,
    t2w: StreamTrigger | None = None,
    room_sync: Callable[[Speakers, MakeRenderer], RoomSync] | None = None,
    renderer_port: int | None = None,
    local_ip: str | None = None,
    background: Iterable[Callable[[RelayService], Awaitable[None]]] = (),
) -> None:
    """Run everything. Speakers are either the fixed ``rooms`` or kept in step by ``room_sync``."""
    stop = stop or asyncio.Event()
    rooms = list(rooms)
    subscriptions = Subscriptions()
    speakers = Speakers()
    relay = RelayService(relay_config, extra_health=lambda: {"rooms": [r.health() for r in speakers.renderers()]})

    def make_renderer(config: RendererConfig, sonos: SonosControl) -> Renderer:
        hosts = getattr(sonos, "stream_hosts", None)

        async def clients() -> int:
            return relay.clients_from(hosts()) if hosts else await relay.fanout.count()

        source_audio_at = (lambda: relay.last_source_audio_at) if t2w is not None else None
        return Renderer(config, sonos, clients, subscriptions, t2w, source_audio_at)

    watchdogs: dict[str, asyncio.Task[None]] = {}

    def start_watchdog(renderer: Renderer) -> None:
        if watchdog or t2w is not None:
            watchdogs[renderer.config.key] = asyncio.create_task(
                renderer.run_watchdog(restart_sonos=watchdog), name=f"watchdog-{renderer.config.key}"
            )

    def stop_watchdog(renderer: Renderer) -> None:
        task = watchdogs.pop(renderer.config.key, None)
        if task is not None:
            task.cancel()

    speakers.on_add.append(start_watchdog)
    speakers.on_remove.append(stop_watchdog)
    for config, sonos in rooms:
        speakers.add(make_renderer(config, sonos))
        LOG.info("speaker %r ready at %s", config.friendly_name, config.location)

    port = renderer_port or (rooms[0][0].http_port if rooms else 8090)
    lan_ip = local_ip or (rooms[0][0].advertised_host if rooms else "")
    relay_server = await asyncio.start_server(relay.handle, relay_config.host, relay_config.port, limit=MAX_REQUEST_BYTES)
    renderer_server = await asyncio.start_server(RendererServer(speakers, subscriptions).handle, "0.0.0.0", port)
    tasks: list[asyncio.Task[None]] = []
    ssdp_transport = None
    try:
        if room_sync is not None:
            sync = room_sync(speakers, make_renderer)
            await sync.sync_once()  # speakers exist before the first announcement
            tasks.append(asyncio.create_task(sync.run(), name="room-sync"))
        if ssdp and lan_ip:
            try:
                ssdp_transport = await start_ssdp_listener(speakers, lan_ip)
            except OSError as exc:
                LOG.error("cannot listen for SSDP on UDP 1900 (%s); the app may not find the speakers", exc)
            tasks.append(asyncio.create_task(announce(speakers, lan_ip), name="ssdp-announce"))
        tasks.extend(asyncio.create_task(job(relay), name="background") for job in background)
        LOG.info(
            "stream at http://%s:%d/stream.flac, watchdog %s, T2 W stream restart %s",
            lan_ip or relay_config.host,
            relay_config.port,
            "on" if watchdog else "off",
            "on" if t2w is not None else "off",
        )
        await stop.wait()
    finally:
        LOG.info("shutting down")
        tasks.extend(watchdogs.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task
        for renderer in speakers.renderers():
            renderer.close()
        relay_server.close()
        renderer_server.close()
        if ssdp_transport is not None:
            ssdp_transport.close()
        await subscriptions.close()
        for task in list(relay._engine_tasks):
            task.cancel()
            with contextlib.suppress(BaseException):
                await task


async def main() -> None:
    LOG.info("t2w-lossless-bridge %s", VERSION)
    settings = settings_from_environment()
    if shutil.which(settings.relay.ffmpeg) is None:
        raise SystemExit(f"ffmpeg not found ({settings.relay.ffmpeg!r})")
    speaker_settings = settings.speakers
    local_ip = speaker_settings.advertised_host
    t2w = T2WControl(settings.t2w_host) if settings.source_recovery else None
    background: list[Callable[[RelayService], Awaitable[None]]] = []
    if not settings.relay.upstream_url:
        LOG.info("looking for the T2 W on the network")
        background.append(lambda relay: keep_t2w_found(relay, t2w, local_ip))
    sonos_rooms = SonosRooms(settings.seed_hosts)
    if speaker_settings.include:
        LOG.info("rooms: %s", ", ".join(speaker_settings.include))
    else:
        excluded = f" except {', '.join(speaker_settings.exclude)}" if speaker_settings.exclude else ""
        LOG.info("rooms: all Sonos rooms%s", excluded)

    def room_sync(speakers: Speakers, make_renderer: MakeRenderer) -> RoomSync:
        return RoomSync(speakers, speaker_settings, sonos_rooms.fetch, sonos_rooms.controller, make_renderer)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(signum, stop.set)
    await run(
        settings.relay,
        watchdog=settings.watchdog,
        stop=stop,
        t2w=t2w,
        room_sync=room_sync,
        renderer_port=speaker_settings.http_port,
        local_ip=local_ip,
        background=background,
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
