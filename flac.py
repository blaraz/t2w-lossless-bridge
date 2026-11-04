"""FLAC primitives: URL parsing, HTTP response, frame-aligned join window."""

from __future__ import annotations

from urllib.parse import urlsplit

FLAC_MARKER = b"fLaC"


def parse_http_url(url: str) -> tuple[str, int, str]:
    parsed = urlsplit(url)
    if parsed.scheme != "http" or not parsed.hostname:
        raise ValueError("only plain http:// upstream URLs are supported")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("upstream URL must not contain credentials, query, or fragment")
    return parsed.hostname, parsed.port or 80, (parsed.path or "/")


def build_http_response(_server_name: str) -> bytes:
    """Mirror the T2 W's minimal FLAC endpoint response shape for Sonos."""
    return (
        b"HTTP/1.0 200 OK\r\n"
        b"Content-Type: audio/flac\r\n"
        b"icy-name: Audio Stream\r\n"
        b"\r\n"
    )


def crc8(data: bytes | bytearray) -> int:
    """CRC-8 used by FLAC frame headers (polynomial 0x07, initial value 0)."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


NEED_MORE_DATA = 0
INVALID = -1


def frame_header_length(data: bytes | bytearray, index: int) -> int:
    """Validate a FLAC frame header at ``index``.

    Returns the header length when valid, NEED_MORE_DATA when the bytes end
    before the header could be checked, or INVALID. The CRC-8 check rejects
    the 0xFFF8 byte pairs that occur by chance inside compressed audio.
    """
    end = len(data)
    if index + 4 > end:
        return NEED_MORE_DATA
    if data[index] != 0xFF or data[index + 1] not in (0xF8, 0xF9):
        return INVALID
    block_size_code = data[index + 2] >> 4
    sample_rate_code = data[index + 2] & 0x0F
    if block_size_code == 0 or sample_rate_code == 0x0F:
        return INVALID
    channel_code = data[index + 3] >> 4
    sample_size_code = (data[index + 3] >> 1) & 0x07
    if channel_code > 10 or sample_size_code == 3 or data[index + 3] & 0x01:
        return INVALID
    position = index + 4
    if position >= end:
        return NEED_MORE_DATA
    first = data[position]
    if first < 0x80:
        coded_length = 1
    elif first & 0xE0 == 0xC0:
        coded_length = 2
    elif first & 0xF0 == 0xE0:
        coded_length = 3
    elif first & 0xF8 == 0xF0:
        coded_length = 4
    elif first & 0xFC == 0xF8:
        coded_length = 5
    elif first & 0xFE == 0xFC:
        coded_length = 6
    elif first == 0xFE:
        coded_length = 7
    else:
        return INVALID
    if position + coded_length > end:
        return NEED_MORE_DATA
    if any(data[position + k] & 0xC0 != 0x80 for k in range(1, coded_length)):
        return INVALID
    position += coded_length
    position += {6: 1, 7: 2}.get(block_size_code, 0)
    position += {12: 1, 13: 2, 14: 2}.get(sample_rate_code, 0)
    if position >= end:
        return NEED_MORE_DATA
    if crc8(data[index:position]) != data[position]:
        return INVALID
    return position + 1 - index


def first_frame_offset(data: bytes | bytearray, start: int = 0) -> int | None:
    """Offset of the first valid frame header, or None if none is known yet."""
    index = data.find(b"\xff", start)
    while index != -1:
        status = frame_header_length(data, index)
        if status > 0:
            return index
        if status == NEED_MORE_DATA:
            return None
        index = data.find(b"\xff", index + 1)
    return None


FRAME_SAMPLE_RATES = {1: 88200, 2: 176400, 3: 192000, 4: 8000, 5: 16000, 6: 22050, 7: 24000,
                      8: 32000, 9: 44100, 10: 48000, 11: 96000}
FRAME_SAMPLE_SIZES = {1: 8, 2: 12, 4: 16, 5: 20, 6: 24, 7: 32}


def frame_format(data: bytes | bytearray, index: int) -> tuple[int | None, int, int | None]:
    """Sample rate, channels and bits per sample stated in a valid frame header.

    Rate or bits are None when the frame refers to STREAMINFO instead (or
    codes the rate in the header tail, which this does not decode).
    """
    rate = FRAME_SAMPLE_RATES.get(data[index + 2] & 0x0F)
    channel_code = data[index + 3] >> 4
    channels = channel_code + 1 if channel_code < 8 else 2
    bits = FRAME_SAMPLE_SIZES.get((data[index + 3] >> 1) & 0x07)
    return rate, channels, bits


def streaminfo_header(sample_rate: int, channels: int, bits: int) -> bytes:
    """A minimal "fLaC" + STREAMINFO header for a stream that arrives without one.

    Block and frame sizes are left open (16…65535 samples, frame sizes and
    total length unknown, no MD5), which every decoder accepts.
    """
    if not (1 <= sample_rate < 1 << 20 and 1 <= channels <= 8 and 4 <= bits <= 32):
        raise ValueError("invalid FLAC stream format")
    info = (16).to_bytes(2, "big") + (65535).to_bytes(2, "big") + bytes(6)
    info += ((sample_rate << 44) | ((channels - 1) << 41) | ((bits - 1) << 36)).to_bytes(8, "big")
    info += bytes(16)
    return FLAC_MARKER + bytes([0x80]) + len(info).to_bytes(3, "big") + info


def metadata_length(data: bytes | bytearray) -> int | None:
    """Length of "fLaC" plus all metadata blocks, or None while incomplete."""
    if len(data) < 4:
        return None
    position = 4
    while True:
        if position + 4 > len(data):
            return None
        is_last = data[position] & 0x80
        block_length = int.from_bytes(data[position + 1 : position + 4], "big")
        position += 4 + block_length
        if position > len(data):
            return None
        if is_last:
            return position


class FlacWindow:
    """Keep the FLAC metadata and a frame-aligned window of recent frames.

    A client that (re)connects gets the metadata plus this window, so it
    starts on a valid frame boundary of the same continuous stream.
    """

    def __init__(self, max_bytes: int) -> None:
        if max_bytes < 4:
            raise ValueError("max_bytes must be at least 4")
        self._max_bytes = max_bytes
        self._header: bytes | None = None
        self._pending = bytearray()
        self._frames = bytearray()

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        if self._header is None:
            self._pending.extend(chunk)
            marker = self._pending.find(FLAC_MARKER)
            if marker < 0:
                # Keep only a possible partial marker at the end.
                del self._pending[: max(0, len(self._pending) - 3)]
                return
            del self._pending[:marker]
            header_length = metadata_length(self._pending)
            if header_length is None:
                return
            self._header = bytes(self._pending[:header_length])
            self._frames.extend(self._pending[header_length:])
            self._pending.clear()
        else:
            self._frames.extend(chunk)
        self._trim()

    def _trim(self) -> None:
        if len(self._frames) <= self._max_bytes:
            return
        cut = len(self._frames) - self._max_bytes
        offset = first_frame_offset(self._frames, cut)
        if offset is not None:
            del self._frames[:offset]
        elif len(self._frames) > 4 * self._max_bytes:
            # No frame boundary found in a very large window: should not happen
            # with real FLAC, but never let the window grow without bound.
            del self._frames[:cut]

    @property
    def ready(self) -> bool:
        return self._header is not None and first_frame_offset(self._frames) == 0

    def snapshot(self) -> bytes:
        if not self.ready:
            return b""
        return self._header + bytes(self._frames)
