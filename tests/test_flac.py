import unittest

from flac import (
    INVALID,
    FlacWindow,
    build_http_response,
    crc8,
    first_frame_offset,
    frame_header_length,
    metadata_length,
    parse_http_url,
)

# "fLaC" + one STREAMINFO block flagged as the last metadata block.
METADATA = b"fLaC" + bytes([0x80, 0x00, 0x00, 34]) + bytes(34)


def flac_frame(number: int, payload: bytes = b"x" * 40) -> bytes:
    """A frame with a valid header: 4096-sample blocks, 44.1 kHz, stereo, 24-bit."""
    header = bytearray(b"\xff\xf8\xc9\x1c") + bytes([number & 0x7F])
    header.append(crc8(header))
    return bytes(header) + payload


class FrameHeaderTests(unittest.TestCase):
    def test_accepts_a_valid_header_and_rejects_a_bad_crc(self):
        frame = flac_frame(3)
        self.assertEqual(frame_header_length(frame, 0), 6)
        broken = bytearray(frame)
        broken[5] ^= 0x01
        self.assertEqual(frame_header_length(broken, 0), INVALID)

    def test_skips_chance_sync_patterns_inside_audio_data(self):
        payload = b"ab\xff\xf8\xc9\x1c\x05\x00cd"  # sync-like bytes with a wrong CRC
        data = flac_frame(1, payload) + flac_frame(2)
        self.assertEqual(first_frame_offset(data, 1), len(flac_frame(1, payload)))

    def test_metadata_length_waits_for_complete_blocks(self):
        self.assertIsNone(metadata_length(METADATA[:20]))
        self.assertEqual(metadata_length(METADATA + b"rest"), len(METADATA))


class FlacWindowTests(unittest.TestCase):
    def test_late_client_receives_metadata_and_a_frame_aligned_window(self):
        window = FlacWindow(max_bytes=150)
        window.feed(METADATA + b"".join(flac_frame(n) for n in range(10)))

        snapshot = window.snapshot()

        self.assertTrue(snapshot.startswith(METADATA))
        self.assertGreater(frame_header_length(snapshot, len(METADATA)), 0)
        self.assertLessEqual(len(snapshot) - len(METADATA), 150)

    def test_window_does_not_start_at_a_chance_sync_pattern(self):
        payload = b"\xff\xf8\xc9\x1c\x05\x00" * 4  # looks like sync, fails CRC
        window = FlacWindow(max_bytes=60)
        window.feed(METADATA + b"".join(flac_frame(n, payload) for n in range(8)))

        frames = window.snapshot()[len(METADATA):]

        # Each frame is 30 bytes, so a 60-byte window holds exactly frames 6 and 7.
        self.assertGreater(frame_header_length(frames, 0), 0)
        self.assertEqual(frames, flac_frame(6, payload) + flac_frame(7, payload))

    def test_snapshot_is_empty_until_metadata_and_a_frame_exist(self):
        window = FlacWindow(max_bytes=64)
        window.feed(METADATA[:10])
        self.assertEqual(window.snapshot(), b"")
        window.feed(METADATA[10:])
        self.assertEqual(window.snapshot(), b"")
        window.feed(flac_frame(0))
        self.assertTrue(window.snapshot().startswith(METADATA))


class ProtocolTests(unittest.TestCase):
    def test_builds_t2_compatible_http10_flac_response(self):
        self.assertEqual(
            build_http_response("ignored"),
            b"HTTP/1.0 200 OK\r\nContent-Type: audio/flac\r\nicy-name: Audio Stream\r\n\r\n",
        )

    def test_parses_only_plain_http_upstream(self):
        self.assertEqual(
            parse_http_url("http://192.168.1.228:8146/stream.flac"), ("192.168.1.228", 8146, "/stream.flac")
        )
        with self.assertRaises(ValueError):
            parse_http_url("https://192.168.1.228/stream.flac")


if __name__ == "__main__":
    unittest.main()
