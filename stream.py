"""Gap-tolerant, lossless FLAC stream from the Pro-Ject T2 W to Sonos.

Signal path, all lossless (24-bit PCM in between):

    T2 W FLAC --ffmpeg decode--> PCM jitter buffer --paced in real time-->
    ffmpeg FLAC encode --> one continuous stream for Sonos

The T2 W's own FLAC server ends the HTTP stream on any hiccup, and Sonos then
stops playing. Here the renderer keeps one continuous FLAC stream: if the T2 W
stalls or drops, the buffer covers it, and if the buffer runs dry the relay
plays digital silence while it reconnects to the T2 W. The process never
writes to the T2 W.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass
from itertools import count
from typing import Callable, Final

from flac import (
    FLAC_MARKER,
    FlacWindow,
    build_http_response,
    first_frame_offset,
    frame_format,
    metadata_length,
    parse_http_url,
    streaminfo_header,
)

VERSION: Final = "1.0.0"
LOG: Final = logging.getLogger("t2w.stream")
MAX_REQUEST_BYTES: Final = 8 * 1024
CLIENT_DRAIN_TIMEOUT_SECONDS: Final = 20.0
ENCODER_OUTPUT_TIMEOUT_SECONDS: Final = 10.0
BYTES_PER_SAMPLE: Final = 4  # PCM travels as s32le: 24-bit audio, left-justified
PACE_PERIOD_SECONDS: Final = 0.02
# How much of the T2 W stream may be searched for its FLAC header or first frame.
MAX_SYNC_BYTES: Final = 1024 * 1024


def parse_request_line(line: bytes) -> str:
    try:
        method, path, version = line.decode("ascii").split(" ")
    except ValueError as exc:
        raise ValueError("malformed request line") from exc
    if method != "GET" or version not in {"HTTP/1.0", "HTTP/1.1"}:
        raise ValueError("only HTTP GET is supported")
    if path not in {"/stream.flac", "/healthz"}:
        raise ValueError("unknown path")
    return path


class Fanout:
    """Bounded queues: a slow client is disconnected, never allowed to grow RAM."""

    def __init__(self, queue_size: int = 256) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self._queue_size = queue_size
        self._ids = count(1)
        self._subscribers: dict[int, asyncio.Queue[bytes | None]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self) -> tuple[int, asyncio.Queue[bytes | None]]:
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=self._queue_size)
        async with self._lock:
            subscriber_id = next(self._ids)
            self._subscribers[subscriber_id] = queue
        return subscriber_id, queue

    async def unsubscribe(self, subscriber_id: int) -> None:
        async with self._lock:
            self._subscribers.pop(subscriber_id, None)

    async def count(self) -> int:
        async with self._lock:
            return len(self._subscribers)

    async def publish(self, payload: bytes) -> list[int]:
        dropped: list[int] = []
        async with self._lock:
            for subscriber_id, queue in list(self._subscribers.items()):
                if queue.full():
                    while not queue.empty():
                        queue.get_nowait()
                    queue.put_nowait(None)
                    self._subscribers.pop(subscriber_id, None)
                    dropped.append(subscriber_id)
                else:
                    queue.put_nowait(payload)
        return dropped

    async def close_all(self) -> int:
        async with self._lock:
            queues = list(self._subscribers.values())
            self._subscribers.clear()
            for queue in queues:
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(None)
            return len(queues)


class JitterBuffer:
    """PCM buffer between the T2 W (its clock) and the paced encoder (ours).

    Playback starts once ``start_seconds`` are buffered, so music begins
    quickly. A live source can't fill a buffer faster than real time, so the
    reserve can only grow during a pause: each time the buffer runs dry, the
    caller plays silence until a larger target is reached (``grow_seconds``
    more, up to ``target_seconds``). A turntable that never drops out keeps
    the short delay; one that does gets the full protection after the first
    dropout. If the source clock runs faster than ours, the oldest audio
    beyond ``max_seconds`` is dropped. All removals are whole sample frames,
    so channels stay aligned.
    """

    def __init__(
        self,
        bytes_per_second: int,
        frame_bytes: int,
        target_seconds: float,
        max_seconds: float,
        start_seconds: float | None = None,
        grow_seconds: float = 2.0,
    ) -> None:
        start_seconds = target_seconds if start_seconds is None else min(start_seconds, target_seconds)
        if start_seconds <= 0 or max_seconds <= target_seconds:
            raise ValueError("need 0 < start_seconds <= target_seconds < max_seconds")
        self._bytes_per_second = bytes_per_second
        self._frame_bytes = frame_bytes
        self._full_target = self._align(int(target_seconds * bytes_per_second))
        self._target = self._align(int(start_seconds * bytes_per_second))
        self._grow = self._align(int(grow_seconds * bytes_per_second))
        self._max = self._align(int(max_seconds * bytes_per_second))
        self._buffer = bytearray()
        self.playing = False
        self.underruns = 0
        self.overflow_drops = 0

    def _align(self, size: int) -> int:
        return size - size % self._frame_bytes

    def write(self, data: bytes) -> None:
        self._buffer.extend(data)
        if len(self._buffer) > self._max:
            excess = self._align(len(self._buffer) - self._target)
            del self._buffer[:excess]
            self.overflow_drops += 1

    def trim_partial(self) -> None:
        """Drop a trailing partial sample frame, e.g. after a source reconnect."""
        partial = len(self._buffer) % self._frame_bytes
        if partial:
            del self._buffer[-partial:]

    def read(self, size: int) -> bytes | None:
        """Return ``size`` bytes of audio, or None when silence must be played."""
        if not self.playing:
            if len(self._buffer) < self._target:
                return None
            self.playing = True
        if len(self._buffer) < size:
            self.playing = False
            self.underruns += 1
            self._target = min(self._full_target, self._target + self._grow)
            return None
        chunk = bytes(self._buffer[:size])
        del self._buffer[:size]
        return chunk

    @property
    def seconds(self) -> float:
        return len(self._buffer) / self._bytes_per_second

    @property
    def target_seconds(self) -> float:
        """Audio needed before playback (re)starts; grows after dropouts."""
        return self._target / self._bytes_per_second


@dataclass(frozen=True)
class RelayConfig:
    # The T2 W FLAC stream; empty until the turntable has been found.
    upstream_url: str
    host: str = "0.0.0.0"
    port: int = 8186
    sample_rate: int = 48_000
    channels: int = 2
    # Audio held in reserve; this is how long a T2 W dropout can last before
    # silence is heard. It is also the added latency.
    buffer_seconds: float = 5.0
    # Playback starts with this much; the reserve grows towards buffer_seconds
    # after each dropout (by buffer_grow_seconds).
    buffer_start_seconds: float = 1.0
    buffer_grow_seconds: float = 2.0
    max_extra_buffer_seconds: float = 3.0
    # Recent FLAC frames sent at once to a (re)connecting client.
    join_burst_bytes: int = 262_144
    queue_size: int = 256
    client_start_timeout_seconds: float = 15.0
    # Keep the stream running briefly after the last client left, so a quick
    # renderer reconnect continues the same stream.
    idle_grace_seconds: float = 15.0
    source_read_timeout_seconds: float = 5.0
    source_retry_seconds: float = 0.3
    ffmpeg: str = "ffmpeg"

    def __post_init__(self) -> None:
        if self.upstream_url:
            parse_http_url(self.upstream_url)
        if self.sample_rate <= 0 or not 1 <= self.channels <= 8:
            raise ValueError("invalid sample_rate or channels")
        if self.buffer_seconds <= 0 or self.max_extra_buffer_seconds <= 0 or self.buffer_start_seconds <= 0:
            raise ValueError("buffer sizes must be positive")

    @property
    def frame_bytes(self) -> int:
        return self.channels * BYTES_PER_SAMPLE

    @property
    def bytes_per_second(self) -> int:
        return self.sample_rate * self.frame_bytes


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(process.wait(), timeout=5)


async def _keep_last_line(stream: asyncio.StreamReader | None, last: list[str]) -> None:
    """Read a process's stderr continuously, keeping only the last line.

    An unread pipe fills up during a long session (e.g. warnings about
    damaged frames after T2 W dropouts) and would then block the process.
    """
    if stream is None:
        return
    with contextlib.suppress(Exception):
        while line := await stream.readline():
            text = line.decode("utf-8", "replace").strip()
            if text:
                last[:] = [text[:300]]


class RelayService:
    def __init__(self, config: RelayConfig, extra_health: Callable[[], dict[str, object]] | None = None) -> None:
        self.config = config
        self.upstream_url = config.upstream_url
        self._extra_health = extra_health
        self.window = FlacWindow(config.join_burst_bytes)
        self.fanout = Fanout(config.queue_size)
        self._state_lock = asyncio.Lock()
        self._engine_active = False
        self._engine_tasks: set[asyncio.Task[None]] = set()
        self._ready = asyncio.Event()
        self._started_at = time.monotonic()
        self._jitter: JitterBuffer | None = None
        self._idle_since: float | None = None
        self._source_connected = False
        self._source_sessions = 0
        self._last_source_error: str | None = None
        # time.monotonic() of the last audio received from the T2 W.
        self.last_source_audio_at: float | None = None
        # The T2 W sends its FLAC header only to clients that connect before
        # its encoder starts; the last one seen is reused for later connects.
        self._flac_header: bytes | None = None
        self.headers_added = 0
        self._last_engine_error: str | None = None
        self._client_hosts: dict[int, str] = {}

    def set_upstream(self, url: str) -> None:
        """Use another T2 W address from the next (re)connect on."""
        parse_http_url(url)
        if url != self.upstream_url:
            LOG.info("T2 W stream: %s", url)
            self.upstream_url = url

    def clients_from(self, hosts: set[str]) -> int:
        """Number of stream clients connected from any of ``hosts``."""
        return sum(1 for host in self._client_hosts.values() if host in hosts)

    def _ffmpeg(self, *arguments: str) -> list[str]:
        return [self.config.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", *arguments]

    async def start_engine_if_needed(self) -> None:
        async with self._state_lock:
            if not self._engine_active:
                self._engine_active = True
                self._idle_since = None
                task = asyncio.create_task(self._run_engine(), name="t2w-engine")
                self._engine_tasks.add(task)
                task.add_done_callback(self._engine_tasks.discard)

    def _reset_engine_state(self) -> None:
        """Forget the ended stream. The caller must hold _state_lock."""
        self.window = FlacWindow(self.config.join_burst_bytes)
        self._ready.clear()
        self._engine_active = False
        self._jitter = None
        self._idle_since = None
        self._source_connected = False

    async def _feed(self, payload: bytes) -> bool:
        """Publish encoder output; return False once the engine should stop.

        Checking and resetting under _state_lock keeps this atomic with
        _snapshot_and_subscribe: a joining client either keeps this stream
        alive or starts a clean new one.
        """
        async with self._state_lock:
            if await self.fanout.count() == 0:
                now = time.monotonic()
                if self._idle_since is None:
                    self._idle_since = now
                elif now - self._idle_since >= self.config.idle_grace_seconds:
                    self._reset_engine_state()
                    return False
            else:
                self._idle_since = None
            self.window.feed(payload)
            if self.window.ready:
                self._ready.set()
            dropped = await self.fanout.publish(payload)
        if dropped:
            LOG.warning("dropped %d slow downstream client(s)", len(dropped))
        return True

    async def _run_engine(self) -> None:
        config = self.config
        jitter = JitterBuffer(
            config.bytes_per_second,
            config.frame_bytes,
            config.buffer_seconds,
            config.buffer_seconds + config.max_extra_buffer_seconds,
            start_seconds=config.buffer_start_seconds,
            grow_seconds=config.buffer_grow_seconds,
        )
        self._jitter = jitter
        encoder: asyncio.subprocess.Process | None = None
        helpers: list[asyncio.Task[None]] = []
        started_at = time.monotonic()
        try:
            encoder = await asyncio.create_subprocess_exec(
                *self._ffmpeg(
                    "-f", "s32le", "-ar", str(config.sample_rate), "-ac", str(config.channels), "-i", "pipe:0",
                    "-c:a", "flac", "-sample_fmt", "s32", "-bits_per_raw_sample", "24",
                    "-flush_packets", "1", "-f", "flac", "pipe:1",
                ),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            assert encoder.stdin is not None and encoder.stdout is not None
            LOG.info(
                "stream started: %d Hz, %d channels, 24-bit FLAC, buffer %.1fs growing to %.1fs after dropouts",
                config.sample_rate,
                config.channels,
                jitter.target_seconds,
                config.buffer_seconds,
            )
            helpers = [
                asyncio.create_task(self._source_loop(jitter), name="t2w-source"),
                asyncio.create_task(self._pace(jitter, encoder.stdin), name="t2w-pacer"),
            ]
            while True:
                payload = await asyncio.wait_for(encoder.stdout.read(64 * 1024), timeout=ENCODER_OUTPUT_TIMEOUT_SECONDS)
                if not payload:
                    raise RuntimeError("FLAC encoder stopped")
                for helper in helpers:
                    if helper.done() and not helper.cancelled() and helper.exception() is not None:
                        raise RuntimeError(f"{helper.get_name()} failed: {helper.exception()!r}")
                if not await self._feed(payload):
                    LOG.info(
                        "stream stopped: no clients for %.0fs (ran %.0fs, %d source session(s), %d buffer underrun(s))",
                        config.idle_grace_seconds,
                        time.monotonic() - started_at,
                        self._source_sessions,
                        jitter.underruns,
                    )
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_engine_error = f"{type(exc).__name__}: {exc}"
            LOG.error("stream failed after %.0fs: %s", time.monotonic() - started_at, self._last_engine_error)
            async with self._state_lock:
                await self.fanout.close_all()
                self._reset_engine_state()
        finally:
            for helper in helpers:
                helper.cancel()
            for helper in helpers:
                with contextlib.suppress(BaseException):
                    await helper
            if encoder is not None:
                await _terminate(encoder)

    async def _source_loop(self, jitter: JitterBuffer) -> None:
        """Pull the T2 W FLAC stream, decode it to PCM, reconnect forever."""
        config = self.config
        failures = 0
        while True:
            upstream = self.upstream_url
            if not upstream:
                failures += 1
                self._last_source_error = "T2 W not found yet"
                if failures == 1 or failures % 30 == 0:
                    LOG.info("source not available yet (attempt %d): %s", failures, self._last_source_error)
                await asyncio.sleep(max(1.0, config.source_retry_seconds))
                continue
            started_at = time.monotonic()
            received, upstream_bytes, reason, detail = await self._source_session(upstream, jitter)
            if received:
                failures = 0
                self._last_source_error = f"{reason}{': ' + detail if detail else ''}"
                LOG.warning(
                    "source stream ended after %.0fs (%d bytes): %s; buffer holds %.1fs, reconnecting",
                    time.monotonic() - started_at,
                    received,
                    self._last_source_error,
                    jitter.seconds,
                )
            else:
                failures += 1
                if upstream_bytes:
                    reason = f"T2 W sent {upstream_bytes} bytes but no audio could be decoded"
                self._last_source_error = f"{reason}{': ' + detail if detail else ''}"
                if failures == 1 or failures % 30 == 0:
                    LOG.info("source not available yet (attempt %d): %s", failures, self._last_source_error)
            await asyncio.sleep(config.source_retry_seconds)

    async def _source_session(self, upstream: str, jitter: JitterBuffer) -> tuple[int, int, str, str]:
        """One connection to the T2 W: read its FLAC, decode it into ``jitter``.

        The stream is read here rather than by ffmpeg, because the T2 W sends
        its FLAC header only at the start of its encoder: a client that joins
        later gets bare frames, and ffmpeg then needs several seconds just to
        recognise the format. Such a stream gets a header put in front (the
        last real one, or one built from the frame headers), so decoding
        starts at once. The audio itself is passed on untouched.

        Returns PCM bytes decoded, bytes received from the T2 W, the reason
        the session ended and ffmpeg's last error line.
        """
        config = self.config
        timeout = config.source_read_timeout_seconds
        host, port, path = parse_http_url(upstream)
        upstream_bytes = 0
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        except (OSError, asyncio.TimeoutError) as exc:
            return 0, 0, f"cannot connect to the T2 W ({type(exc).__name__})", ""
        decoder: asyncio.subprocess.Process | None = None
        stderr_reader: asyncio.Task[None] | None = None
        last_error: list[str] = []
        received = 0
        reason = "source closed the stream"
        try:
            writer.write(
                f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUser-Agent: t2w-sonos\r\n"
                "Accept: */*\r\nConnection: close\r\n\r\n".encode("latin-1")
            )
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
            status_line = head.split(b"\r\n", 1)[0].decode("latin-1")
            if status_line.split(" ")[1:2] != ["200"]:
                return 0, 0, f"T2 W answered {status_line[:60]!r}", ""

            # Find the start: a FLAC header, or else the first frame.
            buffer = bytearray()
            prefix = b""
            while True:
                try:
                    chunk = await asyncio.wait_for(reader.read(65536), timeout)
                except asyncio.TimeoutError:
                    return 0, upstream_bytes, f"T2 W sent no audio for {timeout:.0f}s", ""
                if not chunk:
                    return 0, upstream_bytes, "source closed the stream", ""
                upstream_bytes += len(chunk)
                buffer += chunk
                if len(buffer) < 4:
                    continue
                if buffer[:4] == FLAC_MARKER:
                    length = metadata_length(buffer)
                    if length is not None:
                        self._flac_header = bytes(buffer[:length])
                        break
                else:
                    offset = first_frame_offset(buffer)
                    if offset is not None:
                        prefix = self._header_for(buffer, offset)
                        del buffer[:offset]
                        self.headers_added += 1
                        break
                if len(buffer) > MAX_SYNC_BYTES:
                    return 0, upstream_bytes, "no FLAC data found in the T2 W stream", ""

            decoder = await asyncio.create_subprocess_exec(
                *self._ffmpeg(
                    "-probesize", "32768", "-analyzeduration", "0",
                    "-f", "flac", "-i", "pipe:0", "-vn",
                    "-f", "s32le", "-ac", str(config.channels), "-ar", str(config.sample_rate), "pipe:1",
                ),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert decoder.stdin is not None and decoder.stdout is not None
            decoder_input, decoder_output = decoder.stdin, decoder.stdout
            stderr_reader = asyncio.create_task(_keep_last_line(decoder.stderr, last_error))

            async def copy_to_decoder() -> str:
                nonlocal upstream_bytes
                try:
                    decoder_input.write(prefix + bytes(buffer))
                    await asyncio.wait_for(decoder_input.drain(), timeout)
                    while True:
                        try:
                            chunk = await asyncio.wait_for(reader.read(65536), timeout)
                        except asyncio.TimeoutError:
                            return f"no audio from source for {timeout:.0f}s"
                        if not chunk:
                            return "source closed the stream"
                        upstream_bytes += len(chunk)
                        decoder_input.write(chunk)
                        await asyncio.wait_for(decoder_input.drain(), timeout)
                finally:
                    # The decoder then finishes what it holds and ends its output.
                    with contextlib.suppress(Exception):
                        decoder_input.close()

            feeder = asyncio.create_task(copy_to_decoder(), name="t2w-source-feed")
            try:
                while True:
                    try:
                        data = await asyncio.wait_for(decoder_output.read(64 * 1024), timeout)
                    except asyncio.TimeoutError:
                        reason = f"no audio from source for {timeout:.0f}s"
                        break
                    if not data:
                        break
                    if received == 0:
                        self._source_connected = True
                        self._source_sessions += 1
                        self._last_source_error = None
                        LOG.info(
                            "source connected: %s%s",
                            upstream,
                            "" if not prefix else " (joined a running stream; FLAC header added)",
                        )
                    received += len(data)
                    self.last_source_audio_at = time.monotonic()
                    jitter.write(data)
                if feeder.done() and not feeder.cancelled():
                    with contextlib.suppress(Exception):
                        reason = feeder.result()
            finally:
                feeder.cancel()
                with contextlib.suppress(BaseException):
                    await feeder
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError) as exc:
            reason = f"connection to the T2 W failed ({type(exc).__name__})"
        finally:
            self._source_connected = False
            jitter.trim_partial()
            writer.close()
            if decoder is not None:
                with contextlib.suppress(Exception):
                    decoder.stdin.close()  # type: ignore[union-attr]
                await _terminate(decoder)
            if stderr_reader is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(stderr_reader, 1)
        return received, upstream_bytes, reason, last_error[0] if last_error else ""

    def _header_for(self, data: bytes | bytearray, frame: int) -> bytes:
        """FLAC header for a stream that starts at a bare frame."""
        rate, channels, bits = frame_format(data, frame)
        cached = self._flac_header
        if cached is not None and (rate is None or bits is None):
            return cached
        try:
            return streaminfo_header(rate or self.config.sample_rate, channels, bits or 24)
        except ValueError:
            return cached or streaminfo_header(self.config.sample_rate, self.config.channels, 24)

    async def _pace(self, jitter: JitterBuffer, encoder_input: asyncio.StreamWriter) -> None:
        """Feed the encoder exactly in real time: audio if buffered, else silence."""
        config = self.config
        samples = max(1, round(config.sample_rate * PACE_PERIOD_SECONDS))
        period = samples / config.sample_rate
        size = samples * config.frame_bytes
        silence = bytes(size)
        next_tick = time.monotonic()
        playing = False
        silent_since = time.monotonic()
        while True:
            chunk = jitter.read(size)
            if chunk is None:
                if playing:
                    playing = False
                    silent_since = time.monotonic()
                    LOG.warning(
                        "buffer ran dry: playing silence until %.1fs of audio are buffered again",
                        jitter.target_seconds,
                    )
                chunk = silence
            elif not playing:
                playing = True
                LOG.info("playing source audio after %.1fs of silence", time.monotonic() - silent_since)
            encoder_input.write(chunk)
            await asyncio.wait_for(encoder_input.drain(), timeout=ENCODER_OUTPUT_TIMEOUT_SECONDS)
            next_tick += period
            delay = next_tick - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -0.5:
                next_tick = time.monotonic()

    async def _snapshot_and_subscribe(self) -> tuple[bytes, int, asyncio.Queue[bytes | None]]:
        async with self._state_lock:
            snapshot = self.window.snapshot()
            subscriber_id, queue = await self.fanout.subscribe()
        return snapshot, subscriber_id, queue

    async def _initial_snapshot_and_clear(self, queue: asyncio.Queue[bytes | None]) -> bytes:
        """Return the initial snapshot and discard queue bytes already in it."""
        async with self._state_lock:
            snapshot = self.window.snapshot()
            while not queue.empty():
                queue.get_nowait()
            return snapshot

    async def health(self) -> dict[str, object]:
        jitter = self._jitter
        return {
            "version": VERSION,
            "status": "streaming" if self._engine_active else "idle",
            "upstream": self.upstream_url or None,
            "clients": await self.fanout.count(),
            "source_connected": self._source_connected,
            "playing_source_audio": bool(jitter and jitter.playing),
            "buffer_seconds": round(jitter.seconds, 2) if jitter else 0.0,
            "buffer_target_seconds": round(jitter.target_seconds, 2) if jitter else self.config.buffer_start_seconds,
            "buffer_max_seconds": self.config.buffer_seconds,
            "buffer_underruns": jitter.underruns if jitter else 0,
            "flac_headers_added": self.headers_added,
            "source_sessions": self._source_sessions,
            "last_source_error": self._last_source_error,
            "seconds_since_source_audio": (
                round(time.monotonic() - self.last_source_audio_at, 1) if self.last_source_audio_at else None
            ),
            "last_stream_error": self._last_engine_error,
            "uptime_seconds": int(time.monotonic() - self._started_at),
            **(self._extra_health() if self._extra_health else {}),
        }

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        subscriber_id: int | None = None
        response_started = False
        peer = writer.get_extra_info("peername")
        started_at = time.monotonic()
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            if len(request) > MAX_REQUEST_BYTES:
                raise ValueError("request too large")
            path = parse_request_line(request.split(b"\r\n", 1)[0])
            if path == "/healthz":
                payload = json.dumps(await self.health(), separators=(",", ":")).encode("utf-8")
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode("ascii")
                    + payload
                )
                await writer.drain()
                return

            snapshot, subscriber_id, queue = await self._snapshot_and_subscribe()
            if isinstance(peer, tuple) and peer:
                self._client_hosts[subscriber_id] = peer[0]
            await self.start_engine_if_needed()
            if not snapshot:
                await asyncio.wait_for(self._ready.wait(), timeout=self.config.client_start_timeout_seconds)
                snapshot = await self._initial_snapshot_and_clear(queue)
            if not snapshot:
                raise RuntimeError("encoder did not deliver a FLAC header")
            LOG.info("client %s started, join burst %d bytes", peer, len(snapshot))
            response_started = True
            writer.write(build_http_response("t2w-sonos/1.0"))
            writer.write(snapshot)
            await asyncio.wait_for(writer.drain(), timeout=CLIENT_DRAIN_TIMEOUT_SECONDS)
            while True:
                payload = await queue.get()
                if payload is None:
                    LOG.info("client %s closed by relay after %.0fs", peer, time.monotonic() - started_at)
                    return
                writer.write(payload)
                await asyncio.wait_for(writer.drain(), timeout=CLIENT_DRAIN_TIMEOUT_SECONDS)
        except (ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            with contextlib.suppress(Exception):
                await writer.drain()
        except Exception as exc:
            LOG.info(
                "client %s ended after %.0fs: %s: %s",
                peer,
                time.monotonic() - started_at,
                type(exc).__name__,
                exc,
            )
            # Once audio has started, HTTP error text would corrupt the FLAC body.
            if not response_started and not writer.is_closing():
                writer.write(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n\r\n")
                with contextlib.suppress(Exception):
                    await writer.drain()
        finally:
            if subscriber_id is not None:
                self._client_hosts.pop(subscriber_id, None)
                await self.fanout.unsubscribe(subscriber_id)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
