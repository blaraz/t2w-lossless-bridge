"""SOAP and HTTP helpers shared by the UPnP renderer and the Sonos client."""

from __future__ import annotations

import asyncio
import html
from typing import Final
from xml.etree import ElementTree as ET

SOAP_ENVELOPE_NS: Final = "http://schemas.xmlsoap.org/soap/envelope/"
SOAP_ENCODING: Final = "http://schemas.xmlsoap.org/soap/encoding/"
UPNP_CONTROL_NS: Final = "urn:schemas-upnp-org:control-1-0"
MAX_RESPONSE_BYTES: Final = 1024 * 1024


def xml_escape(value: str) -> str:
    return html.escape(value, quote=True)


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_xml(data: bytes | str) -> ET.Element:
    """Parse untrusted XML; documents with DTDs or entities are rejected."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    upper = raw.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValueError("XML with DTD or entity declarations is not accepted")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError(f"invalid XML: {exc}") from exc


def envelope(element: str, service: str, values: dict[str, str] | None = None) -> bytes:
    """A SOAP envelope whose body holds ``<u:element>`` with the given fields."""
    fields = "".join(f"<{key}>{xml_escape(str(value))}</{key}>" for key, value in (values or {}).items())
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<s:Envelope xmlns:s="{SOAP_ENVELOPE_NS}" s:encodingStyle="{SOAP_ENCODING}">'
        f'<s:Body><u:{element} xmlns:u="{service}">{fields}</u:{element}></s:Body></s:Envelope>'
    ).encode("utf-8")


def request_envelope(action: str, service: str, values: dict[str, str] | None = None) -> bytes:
    return envelope(action, service, values)


def response_envelope(action: str, service: str, values: dict[str, str] | None = None) -> bytes:
    return envelope(f"{action}Response", service, values)


def fault_envelope(code: int, description: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<s:Envelope xmlns:s="{SOAP_ENVELOPE_NS}" s:encodingStyle="{SOAP_ENCODING}"><s:Body><s:Fault>'
        "<faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring><detail>"
        f'<UPnPError xmlns="{UPNP_CONTROL_NS}"><errorCode>{code}</errorCode>'
        f"<errorDescription>{xml_escape(description)}</errorDescription></UPnPError>"
        "</detail></s:Fault></s:Body></s:Envelope>"
    ).encode("utf-8")


def parse_envelope(body: bytes) -> tuple[str, dict[str, str]]:
    """Return the body element name and its child values."""
    root = parse_xml(body)
    body_element = next((node for node in root.iter() if local_name(node.tag) == "Body"), None)
    if body_element is None or len(body_element) == 0:
        raise ValueError("SOAP body has no element")
    element = body_element[0]
    return local_name(element.tag), {local_name(child.tag): child.text or "" for child in element}


def parse_fault(body: bytes) -> tuple[int | None, str]:
    try:
        root = parse_xml(body)
    except ValueError:
        return None, ""
    code: int | None = None
    description = ""
    for node in root.iter():
        name = local_name(node.tag)
        if name == "errorCode" and node.text and node.text.strip().isdigit():
            code = int(node.text.strip())
        elif name == "errorDescription":
            description = node.text or ""
    return code, description


def _dechunk(data: bytes) -> bytes:
    output = bytearray()
    position = 0
    while True:
        line_end = data.find(b"\r\n", position)
        if line_end < 0:
            raise ValueError("truncated chunked body")
        size = int(data[position:line_end].split(b";", 1)[0].strip() or b"0", 16)
        position = line_end + 2
        if size == 0:
            return bytes(output)
        output.extend(data[position : position + size])
        position += size + 2


async def http_request(
    host: str,
    port: int,
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    timeout: float = 8.0,
) -> tuple[int, dict[str, str], bytes]:
    """One HTTP/1.1 request with ``Connection: close``; returns status, headers, body."""
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
    try:
        head = (
            f"{method} {path} HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n"
            f"Content-Length: {len(body)}\r\n"
            + "".join(f"{key}: {value}\r\n" for key, value in (headers or {}).items())
            + "\r\n"
        )
        writer.write(head.encode("latin-1") + body)
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        raw = bytearray()

        async def read_all() -> None:
            while chunk := await reader.read(65536):
                raw.extend(chunk)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ValueError("HTTP response too large")

        await asyncio.wait_for(read_all(), timeout=timeout)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    head_bytes, separator, payload = bytes(raw).partition(b"\r\n\r\n")
    if not separator:
        raise ValueError("malformed HTTP response")
    lines = head_bytes.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        raise ValueError(f"malformed status line: {lines[0]!r}")
    response_headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            response_headers[key.strip().lower()] = value.strip()
    if response_headers.get("transfer-encoding", "").lower() == "chunked":
        payload = _dechunk(payload)
    elif "content-length" in response_headers and response_headers["content-length"].isdigit():
        payload = payload[: int(response_headers["content-length"])]
    return int(parts[1]), response_headers, payload
