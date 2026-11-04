"""Find the T2 W on the LAN.

The T2 W does not answer UPnP searches (it is a control point, not a
device). It announces itself over mDNS/Bonjour as ``_sues800device._tcp``,
the StreamUnlimited service the Pro-Ject app looks for. Every answer is
checked against the turntable's web API, so other StreamUnlimited-based
devices are not mistaken for it: only the T2 W has the ``adchls`` (analog
to digital, HTTP streaming) node.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import struct
from typing import Final

from soap import http_request

LOG: Final = logging.getLogger("t2w.discovery")
MDNS_ADDRESS: Final = "224.0.0.251"
MDNS_PORT: Final = 5353
T2W_SERVICE: Final = "_sues800device._tcp.local"


def mdns_query(name: str) -> bytes:
    """A PTR question asking for a unicast answer (QU bit)."""
    labels = b"".join(bytes([len(part)]) + part.encode("ascii") for part in name.split(".")) + b"\0"
    return struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0) + labels + struct.pack(">HH", 12, 0x8001)


def answers_service(data: bytes, service: str = T2W_SERVICE) -> bool:
    """True if an mDNS packet is an answer that mentions ``service``."""
    if len(data) < 12:
        return False
    flags, _questions, answers, _authority, additional = struct.unpack(">HHHHH", data[2:12])
    if not flags & 0x8000 or answers + additional == 0:
        return False
    return service.split(".")[0].encode("ascii") in data


async def mdns_responders(local_ip: str, timeout: float = 2.0, service: str = T2W_SERVICE) -> list[str]:
    """Addresses that answer an mDNS question for ``service``."""
    found: list[str] = []

    class Collector(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
            if answers_service(data, service) and addr[0] not in found:
                found.append(addr[0])

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
    sock.bind((local_ip, 0))
    sock.setblocking(False)
    transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(Collector, sock=sock)
    try:
        query = mdns_query(service)
        for _ in range(2):
            transport.sendto(query, (MDNS_ADDRESS, MDNS_PORT))
            await asyncio.sleep(timeout / 2)
    finally:
        transport.close()
    return found


async def is_t2w(host: str, port: int = 80, timeout: float = 4.0) -> bool:
    """True if ``host`` has the T2 W's streaming node in its web API."""
    try:
        status, _headers, body = await http_request(
            host, port, "GET", "/api/getData?path=adchls:serverUrl&roles=@all", timeout=timeout
        )
        return status == 200 and "value" in json.loads(body.decode("utf-8", "replace"))
    except (OSError, ValueError, TimeoutError, asyncio.TimeoutError):
        return False


async def find_t2w(local_ip: str, timeout: float = 2.0) -> list[str]:
    """IP addresses of all T2 W turntables on the LAN."""
    candidates = await mdns_responders(local_ip, timeout)
    checks = await asyncio.gather(*(is_t2w(host) for host in candidates))
    return sorted(host for host, ok in zip(candidates, checks) if ok)
