FROM python:3.12-alpine

# ffmpeg decodes the T2 W FLAC and re-encodes one continuous stream (lossless).
RUN apk add --no-cache ffmpeg

WORKDIR /app
COPY flac.py stream.py soap.py sonos.py upnp.py t2w.py discovery.py rooms.py main.py ./

RUN addgroup -S t2w && adduser -S t2w -G t2w
USER t2w

ENV PYTHONUNBUFFERED=1 \
    RELAY_PORT=8186 \
    RENDERER_PORT=8090 \
    LOG_LEVEL=INFO

EXPOSE 8186 8090 1900/udp
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
  CMD wget -q -O - "http://127.0.0.1:${RELAY_PORT}/healthz" >/dev/null || exit 1

CMD ["python", "-u", "main.py"]
