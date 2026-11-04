"""Test doubles: a fake Sonos SOAP server and a fake Sonos controller."""

from __future__ import annotations

import asyncio

from soap import fault_envelope, parse_envelope, response_envelope


class FakeSonosServer:
    """Speaks just enough Sonos UPnP SOAP to test SonosClient."""

    def __init__(self, responses: dict[str, dict[str, str]] | None = None, fail: dict[str, int] | None = None) -> None:
        self.responses = responses or {}
        self.fail = fail or {}
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.server: asyncio.base_events.Server | None = None
        self.port = 0

    async def start(self) -> "FakeSonosServer":
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def close(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        path = lines[0].split(" ")[1]
        headers = {k.strip().lower(): v.strip() for k, v in (line.split(":", 1) for line in lines[1:] if ":" in line)}
        body = await reader.readexactly(int(headers.get("content-length", "0")))
        action, values = parse_envelope(body)
        service = headers.get("soapaction", "").strip('"').split("#")[0]
        self.calls.append((path, action, values))
        if action in self.fail:
            payload, status = fault_envelope(self.fail[action], "fake failure"), "500 Internal Server Error"
        else:
            payload, status = response_envelope(action, service, self.responses.get(action, {})), "200 OK"
        writer.write(
            f"HTTP/1.1 {status}\r\nContent-Type: text/xml\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
            + payload
        )
        await writer.drain()
        writer.close()

    def actions(self) -> list[str]:
        return [action for _path, action, _values in self.calls]


class FakeSonos:
    """Records renderer → Sonos commands; behaviour can be scripted."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.state = "PLAYING"
        self.uri = ""
        self.volume = 25
        self.mute = False
        self.play_error: Exception | None = None
        self.play_delay = 0.0
        self.on_play = None

    def stream_hosts(self) -> set[str]:
        return {"127.0.0.1"}

    async def play_uri(self, uri: str, title: str) -> None:
        self.calls.append(("play_uri", uri))
        if self.play_delay:
            await asyncio.sleep(self.play_delay)
        if self.play_error:
            raise self.play_error
        self.state, self.uri = "PLAYING", uri
        if self.on_play:
            await self.on_play(uri)

    async def stop(self) -> None:
        self.calls.append(("stop",))
        self.state = "STOPPED"

    async def pause(self) -> None:
        self.calls.append(("pause",))
        self.state = "PAUSED_PLAYBACK"

    async def transport(self) -> tuple[str, str]:
        self.calls.append(("transport",))
        return self.state, self.uri

    async def get_volume(self) -> int:
        return self.volume

    async def set_volume(self, volume: int) -> None:
        self.calls.append(("set_volume", volume))
        self.volume = volume

    async def get_mute(self) -> bool:
        return self.mute

    async def set_mute(self, mute: bool) -> None:
        self.calls.append(("set_mute", mute))
        self.mute = mute

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]

    def commands(self) -> list[tuple]:
        """Calls that change something (status queries left out)."""
        return [call for call in self.calls if call[0] != "transport"]

    def command_names(self) -> list[str]:
        return [call[0] for call in self.commands()]
