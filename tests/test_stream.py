import asyncio
import contextlib
import os
import shutil
import subprocess
import time
import unittest

from flac import first_frame_offset, frame_format, metadata_length, streaminfo_header
from stream import Fanout, JitterBuffer, RelayConfig, RelayService, parse_request_line


def find_ffmpeg():
    path = os.environ.get("FFMPEG") or shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


FFMPEG = find_ffmpeg()
RATE, CHANNELS = 48_000, 2
FRAME = CHANNELS * 4


def random_pcm(seconds: float) -> bytes:
    """Random 24-bit stereo audio as s32le (low byte zero): unique, uncompressible."""
    samples = int(RATE * seconds) * CHANNELS
    pcm = bytearray(os.urandom(samples * 4))
    pcm[0::4] = bytes(samples)
    return bytes(pcm)


def ffmpeg_run(arguments, data: bytes) -> bytes:
    result = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", *arguments], input=data, capture_output=True)
    return result.stdout


def encode_flac(pcm: bytes) -> bytes:
    return ffmpeg_run(
        ["-f", "s32le", "-ar", str(RATE), "-ac", str(CHANNELS), "-i", "pipe:0",
         "-c:a", "flac", "-sample_fmt", "s32", "-bits_per_raw_sample", "24", "-f", "flac", "pipe:1"],
        pcm,
    )


def decode_flac(flac: bytes) -> bytes:
    return ffmpeg_run(["-i", "pipe:0", "-f", "s32le", "-ac", str(CHANNELS), "-ar", str(RATE), "pipe:1"], flac)


def streaminfo(flac: bytes) -> tuple[int, int, int]:
    value = int.from_bytes(flac[18:22], "big")
    return value >> 12, ((value >> 9) & 7) + 1, ((value >> 4) & 31) + 1


class FanoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_publish_delivers_to_fast_subscriber(self):
        fanout = Fanout(queue_size=1)
        subscriber_id, queue = await fanout.subscribe()
        self.assertEqual(await fanout.publish(b"frame"), [])
        self.assertEqual(await queue.get(), b"frame")
        await fanout.unsubscribe(subscriber_id)

    async def test_publish_drops_slow_subscriber_instead_of_buffering_unboundedly(self):
        fanout = Fanout(queue_size=1)
        subscriber_id, _queue = await fanout.subscribe()
        await fanout.publish(b"first")
        self.assertEqual(await fanout.publish(b"second"), [subscriber_id])
        self.assertEqual(await fanout.count(), 0)


class JitterBufferTests(unittest.TestCase):
    def make(self):
        # 100 frames of 8 bytes per "second" keeps the arithmetic readable.
        return JitterBuffer(bytes_per_second=800, frame_bytes=8, target_seconds=0.5, max_seconds=1.0)

    def test_waits_for_target_then_plays(self):
        buffer = self.make()
        buffer.write(bytes(392))
        self.assertIsNone(buffer.read(80))
        buffer.write(bytes(8))
        self.assertEqual(len(buffer.read(80)), 80)
        self.assertTrue(buffer.playing)

    def test_underrun_returns_silence_until_target_is_reached_again(self):
        buffer = self.make()
        buffer.write(bytes(400))
        for _ in range(5):
            buffer.read(80)
        self.assertIsNone(buffer.read(80))
        self.assertEqual(buffer.underruns, 1)
        buffer.write(bytes(200))
        self.assertIsNone(buffer.read(80))

    def test_starts_early_and_grows_the_reserve_after_each_dropout(self):
        # Starts after 1 s, grows by 2 s per dropout up to 5 s ("seconds" = 100 frames).
        buffer = JitterBuffer(800, 8, target_seconds=5.0, max_seconds=8.0, start_seconds=1.0, grow_seconds=2.0)
        targets = []
        for _ in range(4):
            targets.append(buffer.target_seconds)
            buffer.write(bytes(int(buffer.target_seconds * 800)))
            self.assertIsNotNone(buffer.read(8))  # plays as soon as the current target is reached
            while buffer.read(80) is not None:
                pass  # play until it runs dry (a dropout)
        self.assertEqual(targets, [1.0, 3.0, 5.0, 5.0])
        self.assertEqual(buffer.underruns, 4)

    def test_fixed_buffer_when_start_equals_target(self):
        buffer = JitterBuffer(800, 8, target_seconds=0.5, max_seconds=1.0, start_seconds=0.5)
        buffer.write(bytes(400))
        while buffer.read(80) is not None:
            pass
        self.assertEqual(buffer.target_seconds, 0.5)

    def test_overflow_drops_oldest_whole_frames(self):
        buffer = self.make()
        buffer.write(bytes(range(8)) * 130)
        self.assertEqual(buffer.overflow_drops, 1)
        self.assertEqual(buffer.seconds, 0.5)
        self.assertEqual(buffer.read(8), bytes(range(8)))

    def test_trim_partial_keeps_channels_aligned_after_reconnect(self):
        buffer = self.make()
        buffer.write(bytes(range(8)) * 50 + b"\x01\x02\x03")
        buffer.trim_partial()
        buffer.write(bytes(range(8)) * 10)
        while (chunk := buffer.read(8)) is not None:
            self.assertEqual(chunk, bytes(range(8)))


class HttpRequestTests(unittest.TestCase):
    def test_allows_only_stream_and_health_gets(self):
        self.assertEqual(parse_request_line(b"GET /stream.flac HTTP/1.1"), "/stream.flac")
        self.assertEqual(parse_request_line(b"GET /healthz HTTP/1.0"), "/healthz")
        for line in (b"POST /stream.flac HTTP/1.1", b"GET /other HTTP/1.1"):
            with self.assertRaises(ValueError):
                parse_request_line(line)


@unittest.skipUnless(FFMPEG, "ffmpeg not available")
class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def start(self, upstream_handler, **overrides):
        upstream = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
        port = upstream.sockets[0].getsockname()[1]
        settings = dict(
            upstream_url=f"http://127.0.0.1:{port}/stream.flac",
            buffer_seconds=0.4,
            idle_grace_seconds=0.5,
            source_retry_seconds=0.2,
            source_read_timeout_seconds=2.0,
            ffmpeg=FFMPEG,
        )
        settings.update(overrides)
        service = RelayService(RelayConfig(**settings))
        relay = await asyncio.start_server(service.handle, "127.0.0.1", 0)
        self.addCleanup(upstream.close)
        self.addCleanup(relay.close)

        async def stop_engine():
            for task in list(service._engine_tasks):
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task

        self.addAsyncCleanup(stop_engine)
        return service, relay.sockets[0].getsockname()[1]

    @staticmethod
    async def listen(port, seconds):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /stream.flac HTTP/1.1\r\nHost: relay\r\n\r\n")
        await writer.drain()
        data = b""
        ended_early = False
        deadline = time.monotonic() + seconds
        try:
            while (remaining := deadline - time.monotonic()) > 0:
                try:
                    chunk = await asyncio.wait_for(reader.read(65536), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                if not chunk:
                    ended_early = True
                    break
                data += chunk
        finally:
            writer.close()
        head, _, body = data.partition(b"\r\n\r\n")
        return head, body, ended_early

    @staticmethod
    def serve_flac(flac_for_session):
        sessions = []

        async def handler(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            sessions.append(time.monotonic())
            body, hold = flac_for_session(len(sessions))
            with contextlib.suppress(ConnectionError):
                if body is not None:
                    writer.write(b"HTTP/1.0 200 OK\r\nContent-Type: audio/flac\r\n\r\n" + body)
                    await writer.drain()
                    await asyncio.sleep(hold)
            writer.close()

        return handler, sessions

    async def test_stream_is_bit_perfect_24_bit_flac(self):
        source_pcm = random_pcm(1.5)
        source_flac = encode_flac(source_pcm)
        handler, _sessions = self.serve_flac(lambda n: (source_flac, 0.5) if n == 1 else (None, 0))
        service, port = await self.start(handler)

        head, body, ended_early = await self.listen(port, seconds=3.0)

        self.assertIn(b"200 OK", head)
        self.assertFalse(ended_early)
        self.assertEqual(streaminfo(body), (RATE, CHANNELS, 24))
        received_pcm = decode_flac(body)
        self.assertGreater(len(received_pcm), len(source_pcm))
        self.assertIn(source_pcm, received_pcm, "relay output does not contain the source audio bit-for-bit")

    async def test_client_keeps_one_stream_through_source_dropouts(self):
        clip = encode_flac(random_pcm(0.6))
        # Each source session delivers 0.6 s of audio, then hangs for 1 s and drops.
        handler, sessions = self.serve_flac(lambda n: (clip, 1.0))
        service, port = await self.start(handler)

        head, body, ended_early = await self.listen(port, seconds=4.5)
        health = await service.health()

        self.assertIn(b"200 OK", head)
        self.assertFalse(ended_early, "relay ended the client stream when the source dropped")
        self.assertGreaterEqual(len(sessions), 2)
        self.assertGreaterEqual(health["source_sessions"], 2)
        self.assertGreaterEqual(health["buffer_underruns"], 1)
        self.assertEqual(body.count(b"fLaC"), 1)
        # Decodable as one continuous stream of ~4.5 s.
        self.assertGreater(len(decode_flac(body)), 3.5 * RATE * FRAME)

    async def test_joining_a_running_t2w_stream_without_header_is_fast_and_bit_perfect(self):
        # The T2 W sends its FLAC header only when its encoder starts; a relay
        # that connects later gets bare frames, delivered in real time.
        source_pcm = random_pcm(4.0)
        flac = encode_flac(source_pcm)
        joined_at = first_frame_offset(flac, len(flac) // 4)
        bare = flac[joined_at:]

        async def handler(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            with contextlib.suppress(ConnectionError):
                writer.write(b"HTTP/1.0 200 OK\r\nContent-Type: audio/flac\r\n\r\n")
                step = len(bare) // 30  # a little faster than real time
                for index in range(0, len(bare), step):
                    writer.write(bare[index:index + step])
                    await writer.drain()
                    await asyncio.sleep(0.07)
                await asyncio.sleep(1)
            writer.close()

        # A roomy buffer keeps a slow test machine from inserting silence.
        service, port = await self.start(handler, buffer_seconds=1.0)
        head, body, _ended = await self.listen(port, seconds=6.0)
        health = await service.health()

        self.assertIn(b"200 OK", head)
        self.assertGreaterEqual(health["flac_headers_added"], 1)  # every reconnect joins mid-stream
        # The audio after the join point arrives untouched (1.5 s of it, bit for bit) and
        # soon: a decoder without header would need seconds just to detect the format.
        joined_pcm = source_pcm[len(source_pcm) - len(decode_flac(streaminfo_header(RATE, CHANNELS, 24) + bare)):]
        received_pcm = decode_flac(body)
        position = received_pcm.find(joined_pcm[int(0.2 * RATE) * FRAME:int(1.7 * RATE) * FRAME])
        self.assertGreaterEqual(position, 0, "joined audio missing or altered")
        self.assertLess(position / (RATE * FRAME), 3.0, "joined audio arrived too late")

    async def test_decoder_messages_are_read_continuously(self):
        # A pipe nobody reads fills up after ~64 KB and would block ffmpeg.
        from stream import _keep_last_line

        stream = asyncio.StreamReader()
        for number in range(5000):
            stream.feed_data(f"[flac] invalid frame {number}\n".encode())
        stream.feed_eof()
        last: list[str] = []
        await asyncio.wait_for(_keep_last_line(stream, last), 5)
        self.assertEqual(last, ["[flac] invalid frame 4999"])

    def test_header_for_bare_frames_matches_the_stream(self):
        flac = encode_flac(random_pcm(0.5))
        frame = first_frame_offset(flac, metadata_length(flac))
        header = streaminfo_header(*[value for value in frame_format(flac, frame)])
        self.assertEqual(streaminfo(header + flac[frame:]), (RATE, CHANNELS, 24))
        self.assertEqual(decode_flac(header + flac[frame:]), decode_flac(flac))

    async def test_counts_clients_per_address(self):
        handler, _sessions = self.serve_flac(lambda n: (None, 0))
        service, port = await self.start(handler)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /stream.flac HTTP/1.1\r\nHost: relay\r\n\r\n")
        await writer.drain()
        await asyncio.wait_for(reader.readuntil(b"fLaC"), 5)

        self.assertEqual(service.clients_from({"127.0.0.1"}), 1)
        self.assertEqual(service.clients_from({"192.168.1.52"}), 0)
        writer.close()
        for _ in range(100):
            if service.clients_from({"127.0.0.1"}) == 0:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(service.clients_from({"127.0.0.1"}), 0)

    async def test_stream_stops_after_idle_grace(self):
        handler, _sessions = self.serve_flac(lambda n: (None, 0))
        service, port = await self.start(handler, idle_grace_seconds=0.3)

        head, body, _ = await self.listen(port, seconds=0.8)
        self.assertIn(b"200 OK", head)
        self.assertTrue(body.startswith(b"fLaC"))  # silence is streamed while the source is missing

        for _ in range(60):
            if not service._engine_active and not service._engine_tasks:
                break
            await asyncio.sleep(0.05)
        self.assertFalse(service._engine_active)
        self.assertFalse(service._engine_tasks)


if __name__ == "__main__":
    unittest.main()
