"""Tiny client for the T2 W's own web API (the one the Pro-Ject app uses).

Only one action is used: ``pst:stream``, which is exactly what the app sends
when a speaker is selected. Firmware 0.7.31 (libnsdk_pst.so) handles it by
starting its encoder pipeline (``adchls:start``) and sending
SetAVTransportURI + Play to the renderer whose UDN is given. No setting of
the turntable is changed.
"""

from __future__ import annotations

import json
import logging
from typing import Final
from urllib.parse import quote

from soap import http_request

LOG: Final = logging.getLogger("t2w.api")


class T2WError(RuntimeError):
    pass


def stream_request_path(renderer_udn: str) -> str:
    value = json.dumps({"type": "string_", "string_": renderer_udn}, separators=(",", ":"))
    return f"/api/setData?path={quote('pst:stream', safe=':')}&role=activate&value={quote(value, safe='')}"


class T2WControl:
    def __init__(self, host: str, port: int = 80, timeout: float = 8.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout

    async def stream_to(self, renderer_udn: str) -> None:
        """Ask the T2 W to (re)start streaming to the renderer ``renderer_udn``."""
        if not self.host:
            raise T2WError("T2 W not found on the network")
        status, _headers, body = await http_request(
            self.host, self.port, "GET", stream_request_path(renderer_udn), timeout=self.timeout
        )
        text = body.decode("utf-8", "replace").strip()
        if status != 200:
            raise T2WError(f"T2 W answered HTTP {status}: {text[:200]}")
        # Errors come back as HTTP 200 with an error object in the JSON body.
        try:
            answer = json.loads(text) if text else None
        except ValueError:
            answer = None
        if isinstance(answer, dict) and answer.get("error"):
            raise T2WError(f"T2 W refused pst:stream: {json.dumps(answer['error'])[:200]}")
        LOG.debug("pst:stream answer: %s", text[:200])
