# T2 W → Sonos: lossless and stable

🇬🇧 English · 🇩🇪 [Deutsch](README.de.md)

> **Version 1.0.0** · tested with T2 W firmware 0.7.31 and Sonos S2. Problems or questions: please open an [issue](https://github.com/blaraz/t2w-lossless-bridge/issues) with the log.

A Docker container, e.g. for a Raspberry Pi. It streams the Pro-Ject T2 W turntable losslessly (FLAC, 24-bit/48 kHz) to your Sonos rooms and keeps playback stable. Nothing needs to be configured: it finds the turntable and the Sonos rooms by itself.

## Why this exists

With "Lossless" switched on, the T2 W streams FLAC straight to a Sonos speaker. In practice that is fragile: the music drops out or stops, the speaker stays silent after "Play", or the Pro-Ject app gives up with "Lossless stream failed to start. Consider disabling lossless". Looking at firmware 0.7.31 and at the stream on the network shows why:

- **No reserve.** The T2 W sends FLAC as a bare live stream, exactly as fast as the record turns, with no flow control and nothing held back. Every hiccup of the T2 W or the network reaches the speaker directly. If the connection ends, Sonos stops.
- **The FLAC header is sent only once.** The T2 W sends the header that describes the audio format only when its encoder starts. A speaker that connects to a running stream (after a reconnect, on autoplay, or a second room) gets audio without it. Sonos then takes a long time to recognise the stream or doesn't play at all.
- **A strict 9-second check.** 9 seconds after "Play", the T2 W checks whether the speaker is playing its stream. If it isn't yet, e.g. because the speaker answers slowly, the T2 W switches it to MP3 and shows the "Lossless stream failed" message.
- **Autoplay without audio.** After waking from standby the T2 W sometimes sends "Play" but no audio, until the speaker is selected again in the app.

How the container solves this:

- **Sonos never connects to the T2 W.** It gets one continuous stream from the container, which never breaks off while playing. The container reads the T2 W, holds a reserve, and reconnects in the background if the T2 W drops out. Short dropouts are bridged by the reserve; longer ones become a short silence, but Sonos stays connected and plays on.
- **The missing header is added**, so every start, reconnect and extra room begins within seconds.
- **The 9-second check is passed:** the container answers the T2 W at once and confirms playback in time, even if a Sonos speaker is slow.
- **Silent starts are repaired:** if the T2 W sends "Play" but no audio, the container restarts its stream, the same way reselecting the speaker in the app does. A watchdog restarts Sonos rooms that stop on their own.
- **It stays lossless:** the audio reaches Sonos bit for bit, and the T2 W itself is not changed.

## What it does

```text
Pro-Ject app ──selects──▶ "T2 W – <room>" (one virtual speaker per Sonos room, in this container)
T2 W ──Play──▶ container ──▶ the chosen Sonos room plays http://<computer>:8186/stream.flac
container ──reads──▶ T2 W FLAC ──▶ buffer (1–5 s) ──▶ one continuous FLAC stream to Sonos
```

- **One speaker per Sonos room, automatically:** every room of your Sonos system appears in the Pro-Ject app as "T2 W – <room>".
  - New rooms show up within a minute.
  - A renamed room keeps its speaker under the new name.
  - A room that has been gone for more than 5 minutes (e.g. a Move in Bluetooth mode) is removed. This never happens while it is playing.
- **Several rooms at once:** choose one room in the Pro-Ject app and group more rooms with it in the Sonos app. All of them play in sync.
- **Lossless:** the audio reaches Sonos bit for bit.
- **Starts reliably:** the T2 W sends its FLAC header only when its encoder starts. A listener that joins a running stream gets bare audio frames, which decoders struggle to recognise. The container adds a matching header, so playback starts within seconds every time. The audio itself is passed on untouched.
- **Quick start, growing protection:** music starts as soon as 1 second is buffered. If the T2 W drops out for longer than the reserve, you hear a short silence, and the container uses that pause to enlarge the reserve by 2 seconds, up to 5. So a stable turntable keeps the short delay, an unstable one gets full protection after its first dropout. Sonos stays connected throughout.
- **Watchdog per room:** if a Sonos room stops on its own, e.g. after a Wi-Fi hiccup, the container restarts it. If you pause on purpose, or play something else in the Sonos app, it leaves the room alone.
- **T2 W stream restart:** sometimes the T2 W sends "Play" after waking up but delivers no audio, e.g. on autoplay when the platter starts.
  - If no audio arrives for 20 seconds, the container restarts the T2 W stream.
  - It sends the same command the Pro-Ject app sends when you select a speaker. So the effect is the same as deselecting and reselecting the speaker by hand.
  - At most 2 attempts in 10 minutes.
- **Finds everything by itself:**
  - The T2 W via mDNS/Bonjour, the same way the Pro-Ject app does.
  - The Sonos rooms via UPnP.
  - If an IP address changes, the container follows it.
  - Stereo pairs, subs and surround speakers are handled correctly.
- **Control from the Pro-Ject app:** play, stop, pause and volume go to the chosen room.
- **The T2 W stays untouched:** no settings are changed and the firmware is not modified. The container only uses the interface the app uses.

## Requirements

- A computer that runs Docker and is always on, e.g. a Raspberry Pi 3/4/5 or a NAS. It must support network mode `host`.
- The T2 W, Sonos and this computer are **on the same network**. A guest Wi-Fi or separate VLANs won't work without extra setup.
- **"Lossless"** is switched on for the T2 W in the Pro-Ject app. Otherwise the T2 W only sends MP3.

## Installation

1. Download the project and start the container:

   ```bash
   git clone https://github.com/blaraz/t2w-lossless-bridge.git
   cd t2w-lossless-bridge
   docker compose up -d --build
   docker compose logs --tail=30
   ```

   After a few seconds the log should contain:
   - `T2 W found at …`
   - for each room: `speaker 'T2 W – <room>' ready at …`

2. In the Pro-Ject app, choose the **"T2 W – <room>"** you want and put on a record.

The container starts by itself after a reboot (`restart: unless-stopped`). For that, Docker itself must start at boot: `sudo systemctl enable docker`.

## Checking

```bash
curl http://<IP of the computer>:8186/healthz
```

General:

| Field | Meaning |
|---|---|
| `version` | Version of the container |
| `upstream` | Address of the T2 W stream. `null` means the T2 W hasn't been found yet. |
| `source_connected` | The container is currently receiving FLAC from the T2 W |
| `buffer_seconds` | Audio in reserve right now |
| `buffer_target_seconds` | Current size of the reserve (1 at the start, grows after dropouts) |
| `seconds_since_source_audio` | Seconds since the last audio from the T2 W |
| `buffer_underruns` | How often a T2 W dropout was longer than the buffer, causing silence |
| `flac_headers_added` | How often the container joined a running T2 W stream and added the FLAC header |
| `source_sessions` | Connections to the T2 W. If it rises while listening, there were dropouts that were bridged. |
| `clients` | How many Sonos rooms are listening right now |

Per room, under `rooms`:

| Field | Meaning |
|---|---|
| `renderer_state` | `PLAYING` when this room plays through the container |
| `sonos_restarts_by_watchdog` | How often the watchdog restarted the room |
| `t2w_stream_restarts` | How often the T2 W stream had to be restarted because no audio came after "Play" |
| `t2w_stream_uri` | The address the T2 W sent to the speaker (for diagnosis only) |
| `last_renderer_error` | Last error, e.g. Sonos not reachable |

### Log messages (`docker compose logs -f`)

| Message | Meaning |
|---|---|
| `T2 W found at …` | The turntable was found |
| `T2 W not found …` | The T2 W doesn't answer. Is it on and in the same network? If needed, set `T2W_HOST`. |
| `speaker 'T2 W – X' ready` / `removed` | A room was added or removed |
| `T2 W: Play` / `Sonos is playing …` | Playback has started |
| `source connected … (joined a running stream; FLAC header added)` | Normal: the T2 W encoder was already running, the header was added |
| `playing source audio after …s of silence` | Music is coming through. The first seconds after Play fill the buffer. |
| `source stream ended … reconnecting` | The T2 W dropped the stream and is reconnected. If no `buffer ran dry` follows, it was inaudible. |
| `buffer ran dry: playing silence until …s` | The dropout was longer than the reserve; there was silence. The reserve is now larger. |
| `source not available yet` | The T2 W isn't delivering FLAC right now |
| `watchdog: Sonos stopped on its own … restarting` | A room dropped out and was restarted |
| `no audio from the T2 W 20s after its Play: asking it to restart the stream` | The T2 W sent nothing after "Play"; its stream is being restarted |
| `source recovery: the T2 W started its stream again` | The restart worked; `source connected` follows shortly |
| `source recovery: … select the speaker again` | The restart didn't help. Select the speaker again in the app. |
| `the T2 W offers a compressed stream` | The T2 W offered MP3 (see "Known quirks"). The container still reads the FLAC output, so playback stays lossless as long as `source connected` follows. |
| `none of the rooms in SONOS_ROOMS exists` | A name in `SONOS_ROOMS` doesn't match. The message lists the correct names. |

## Settings (`docker-compose.yml`, all optional)

| Variable | Default | Effect |
|---|---|---|
| `SONOS_ROOMS` | empty = all | Offer only these rooms. Names as in the Sonos app, comma-separated, case doesn't matter. |
| `SONOS_EXCLUDE` | empty | Leave these rooms out, e.g. `Bathroom,Garage` |
| `T2W_HOST` | empty = search | IP of the T2 W. Only needed if it isn't found or there are several on the network. |
| `FRIENDLY_NAME_FORMAT` | `T2 W – {room}` | Name in the Pro-Ject app |
| `BUFFER_START_SECONDS` | `1` | Reserve at the start of playback: the time from "Play" to music, and the delay between needle and speaker |
| `BUFFER_SECONDS` | `5` | Largest reserve, reached after dropouts. Raise it, e.g. to `8`, if `buffer ran dry` happens often. Set both to the same value for a fixed buffer. |
| `WATCHDOG` | `1` | `0` turns off the automatic Sonos restart |
| `SOURCE_RECOVERY` | `1` | `0` turns off the T2 W stream restart |
| `SOURCE_RECOVERY_SECONDS` | `20` | How long the container waits for audio after "Play". A normal start takes 10 to 11 seconds. |
| `ADVERTISED_HOST` | empty = detect | IP of this computer. Only needed if it has several network connections. |
| `SONOS_SEED_HOSTS` | empty | IP of any Sonos speaker, in case automatic discovery is blocked on the network |

After a change: `docker compose up -d`.

## Known quirks

- **"Lossless stream failed to start. Consider disabling lossless" in the Pro-Ject app:** the T2 W checks 9 seconds after "Play" whether the speaker is playing its stream, and doesn't cancel this check on "Stop". If you switch to another speaker within those 9 seconds, it reports the one you left as failed. The same happens with a real Sonos speaker. **Don't switch lossless off.** Dismiss the message; to avoid it, stay on a speaker for at least 10 seconds before switching.
- **Old speakers after an update:** after updating from a version that named speakers differently, the Pro-Ject app may list a speaker twice for a while. Choose the other entry or restart the app; stale entries disappear by themselves after about 30 minutes. If you use autoplay, select the speaker once more so the T2 W remembers the new one.
- **A live source can't be buffered ahead:** streaming services fill their buffer faster than real time because the whole song is on their server. The T2 W sends exactly as fast as the record turns, so a reserve can only be filled during a pause. That is why it starts small and grows after a dropout. Music starts about 2 to 3 seconds after "Play" (1 s reserve plus Sonos' own buffering). Set `BUFFER_START_SECONDS` equal to `BUFFER_SECONDS` if you prefer full protection from the start.

## Ports

The container uses these ports, all on the LAN only:
- TCP 8186 for the stream
- TCP 8090 for UPnP control
- UDP 1900 for UPnP discovery

It accepts control commands only from private networks. None of these ports should be exposed to the internet.

## Tests (on a computer with Python 3.12+ and ffmpeg)

```bash
python -m unittest discover -s tests -v
```

105 tests, including:
- a bit-exact audio comparison
- T2 W dropouts
- an end-to-end run from control command to audio
- rooms being added, renamed or removed
- room discovery including IP changes
- finding the T2 W
- all UPnP actions, state events and SSDP discovery
- all watchdog cases
- joining a running T2 W stream without FLAC header
- the T2 W stream restart

## Background

Tested with T2 W firmware 0.7.31. A firmware update from Pro-Ject or Sonos may change the behaviour.

The T2 W sends FLAC only to speakers from certain manufacturers. That is why the virtual speaker reports itself as "Pro-Ject Audio Systems" (`RENDERER_MANUFACTURER`). This is done solely for compatibility. This is an unofficial project and is not affiliated with Pro-Ject or Sonos.

## Support

If this project saves your records from dropouts, I'd be happy about a coffee:

[![Buy me a coffee](https://img.shields.io/badge/Buy%20me%20a%20coffee-FFDD00?style=for-the-badge&logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/blaraz)

## License

MIT, see [LICENSE](LICENSE).
