# T2 W → Sonos: verlustfrei und stabil

🇬🇧 [English](README.md) · 🇩🇪 Deutsch

> **Beta:** getestet mit T2-W-Firmware 0.7.31 und Sonos S2. Rückmeldungen und Logs gerne als [Issue](https://github.com/blaraz/t2w-lossless-bridge/issues).

Ein Docker-Container, z. B. für einen Raspberry Pi. Er bringt den Pro-Ject T2 W verlustfrei (FLAC, 24 Bit/48 kHz) auf deine Sonos-Räume und hält die Wiedergabe stabil. Einstellen musst du nichts: Plattenspieler und Sonos-Räume findet er selbst.

## Warum es dieses Projekt gibt

Mit eingeschaltetem „Lossless“ schickt der T2 W FLAC direkt an einen Sonos-Lautsprecher. In der Praxis ist das wacklig: Die Musik setzt aus oder bricht ab, der Lautsprecher bleibt nach „Play“ stumm, oder die Pro-Ject-App gibt mit „Lossless stream failed to start. Consider disabling lossless“ auf. Ein Blick in die Firmware 0.7.31 und auf den Stream im Netzwerk zeigt, warum:

- **Keine Reserve.** Der T2 W sendet FLAC als nackten Live-Stream, genau so schnell, wie sich die Platte dreht, ohne Flusskontrolle und ohne Vorrat. Jeder Hänger des T2 W oder des Netzwerks kommt direkt beim Lautsprecher an. Reißt die Verbindung ab, hört Sonos auf.
- **Der FLAC-Kopf kommt nur einmal.** Den Kopf, der das Audioformat beschreibt, sendet der T2 W nur beim Start seines Encoders. Ein Lautsprecher, der sich in einen laufenden Stream einklinkt (nach einem Neuverbinden, bei Autoplay oder als zweiter Raum), bekommt Audio ohne Kopf. Sonos braucht dann lange, um den Stream zu erkennen, oder spielt gar nicht.
- **Eine strenge 9-Sekunden-Prüfung.** 9 Sekunden nach „Play“ prüft der T2 W, ob der Lautsprecher seinen Stream spielt. Ist er noch nicht so weit, z. B. weil er langsam antwortet, stellt der T2 W ihn auf MP3 um und zeigt die Meldung „Lossless stream failed“.
- **Autoplay ohne Ton.** Nach dem Aufwachen aus dem Standby schickt der T2 W manchmal „Play“, aber kein Audio, bis man den Lautsprecher in der App neu auswählt.

So löst der Container das:

- **Sonos verbindet sich nie mit dem T2 W.** Sonos bekommt einen einzigen, durchgehenden Stream vom Container, der beim Spielen nie abreißt. Der Container liest den T2 W, hält eine Reserve vor und verbindet sich im Hintergrund neu, wenn der T2 W aussetzt. Kurze Aussetzer überbrückt die Reserve, längere werden zu kurzer Stille, aber Sonos bleibt verbunden und spielt weiter.
- **Der fehlende Kopf wird ergänzt**, sodass jeder Start, jedes Neuverbinden und jeder weitere Raum innerhalb von Sekunden läuft.
- **Die 9-Sekunden-Prüfung wird bestanden:** Der Container antwortet dem T2 W sofort und meldet die Wiedergabe rechtzeitig, auch wenn ein Sonos-Lautsprecher langsam ist.
- **Stumme Starts werden repariert:** Schickt der T2 W „Play“, aber kein Audio, startet der Container dessen Stream neu, genau wie das erneute Auswählen in der App. Ein Watchdog startet Sonos-Räume neu, die von selbst stoppen.
- **Es bleibt verlustfrei:** Das Audio kommt Bit für Bit bei Sonos an, und am T2 W selbst wird nichts verändert.

## Was er macht

```text
Pro-Ject-App ──wählt──▶ „T2 W – <Raum>“ (ein virtueller Lautsprecher pro Sonos-Raum in diesem Container)
T2 W ──Play──▶ Container ──▶ der gewählte Sonos-Raum spielt http://<Rechner>:8186/stream.flac
Container ──liest──▶ T2-W-FLAC ──▶ Puffer (1–5 s) ──▶ ein durchgehender FLAC-Stream an Sonos
```

- **Ein Lautsprecher pro Sonos-Raum, automatisch:** Jeder Raum deiner Sonos-Anlage erscheint in der Pro-Ject-App als „T2 W – <Raum>“.
  - Neue Räume kommen innerhalb einer Minute dazu.
  - Benennst du einen Raum um, behält er seinen Lautsprecher unter neuem Namen.
  - Ein Raum, der länger als 5 Minuten verschwunden ist (z. B. ein Move im Bluetooth-Betrieb), wird entfernt. Das passiert nie, während er spielt.
- **Mehrere Räume gleichzeitig:** Wähle einen Raum in der Pro-Ject-App und gruppiere in der Sonos-App weitere Räume dazu. Alle spielen dann synchron mit.
- **Verlustfrei:** Das Audio kommt bit-genau bei Sonos an.
- **Startet zuverlässig:** Der T2 W schickt seinen FLAC-Kopf nur, wenn sein Encoder startet. Wer sich in einen laufenden Stream einklinkt, bekommt nur die Audio-Frames, und Decoder erkennen das Format dann nur schwer. Der Container ergänzt den passenden Kopf, damit die Wiedergabe jedes Mal nach wenigen Sekunden startet. Das Audio selbst bleibt unverändert.
- **Schneller Start, wachsender Schutz:** Die Musik startet, sobald 1 Sekunde gepuffert ist. Setzt der T2 W länger aus, als die Reserve reicht, hört man kurz Stille. Der Container nutzt diese Pause, um die Reserve um 2 Sekunden zu vergrößern, bis auf 5. Ein stabiler Plattenspieler behält also die kurze Verzögerung, ein wackliger bekommt nach dem ersten Aussetzer den vollen Schutz. Sonos bleibt die ganze Zeit verbunden.
- **Watchdog pro Raum:** Stoppt ein Sonos-Raum von sich aus, etwa durch einen WLAN-Aussetzer, startet der Container ihn automatisch neu. Pausierst du bewusst, oder spielst du in der Sonos-App etwas anderes, greift er nicht ein.
- **Stream-Neustart beim T2 W:** Manchmal schickt der T2 W nach dem Aufwachen „Play“, liefert aber kein Audio, z. B. beim automatischen Start, wenn sich der Teller dreht.
  - Kommt 20 Sekunden lang nichts an, startet der Container den Stream des T2 W neu.
  - Das ist derselbe Befehl, den die Pro-Ject-App beim Auswählen eines Lautsprechers schickt. Es passiert also dasselbe wie beim Ab- und Anwählen von Hand.
  - Höchstens 2 Versuche in 10 Minuten.
- **Findet alles selbst:**
  - Den T2 W über mDNS/Bonjour, so wie es die Pro-Ject-App tut.
  - Die Sonos-Räume über UPnP.
  - Ändert sich eine IP-Adresse, folgt der Container ihr.
  - Stereopaare, Sub und Rear-Lautsprecher steuert er automatisch richtig an.
- **Steuerung aus der Pro-Ject-App:** Play, Stop, Pause und Lautstärke gehen an den jeweiligen Raum.
- **Kein Eingriff am T2 W:** Es werden weder Einstellungen geändert noch die Firmware angefasst. Der Container nutzt nur die Schnittstelle, die auch die App verwendet.

## Voraussetzungen

- Ein Rechner, auf dem Docker läuft und der ständig eingeschaltet ist, z. B. ein Raspberry Pi 3/4/5 oder ein NAS. Er muss mit Netzwerkmodus `host` laufen können.
- T2 W, Sonos und dieser Rechner hängen **im selben Netz**. Ein Gäste-WLAN oder getrennte VLANs funktionieren nicht ohne Weiteres.
- In der Pro-Ject-App ist beim T2 W **„Lossless“** eingeschaltet, sonst schickt der T2 W nur MP3.

## Installation

1. Lade das Projekt herunter und starte den Container:

   ```bash
   git clone https://github.com/blaraz/t2w-lossless-bridge.git
   cd t2w-lossless-bridge
   docker compose up -d --build
   docker compose logs --tail=30
   ```

   Im Log sollten nach einigen Sekunden diese Zeilen stehen:
   - `T2 W found at …`
   - für jeden Raum: `speaker 'T2 W – <Raum>' ready at …`

2. Wähle in der Pro-Ject-App den gewünschten **„T2 W – <Raum>“** aus und lege eine Platte auf.

Nach einem Neustart des Rechners startet der Container von selbst (`restart: unless-stopped`). Docker selbst muss dafür beim Systemstart mitstarten: `sudo systemctl enable docker`.

## Prüfen

```bash
curl http://<IP des Rechners>:8186/healthz
```

Allgemein:

| Feld | Bedeutung |
|---|---|
| `upstream` | Die Adresse des T2-W-Streams. `null` bedeutet, der T2 W wurde noch nicht gefunden. |
| `source_connected` | Der Container empfängt gerade FLAC vom T2 W |
| `buffer_seconds` | Audio, das gerade in Reserve ist |
| `buffer_target_seconds` | Aktuelle Größe der Reserve (1 am Anfang, wächst nach Aussetzern) |
| `seconds_since_source_audio` | Sekunden seit dem letzten Audio vom T2 W |
| `buffer_underruns` | So oft war ein T2-W-Abbruch länger als der Puffer und es gab Stille |
| `flac_headers_added` | So oft hat sich der Container in einen laufenden T2-W-Stream eingeklinkt und den FLAC-Kopf ergänzt |
| `source_sessions` | Verbindungen zum T2 W. Steigt der Wert beim Hören, gab es Abbrüche, die überbrückt wurden. |
| `clients` | Wie viele Sonos-Räume gerade zuhören |

Pro Raum, unter `rooms`:

| Feld | Bedeutung |
|---|---|
| `renderer_state` | `PLAYING`, wenn dieser Raum über den Container spielt |
| `sonos_restarts_by_watchdog` | So oft hat der Watchdog den Raum neu gestartet |
| `t2w_stream_restarts` | So oft musste der Stream des T2 W neu gestartet werden, weil nach „Play“ kein Audio kam |
| `t2w_stream_uri` | Die Adresse, die der T2 W dem Lautsprecher geschickt hat (nur zur Diagnose) |
| `last_renderer_error` | Letzter Fehler, z. B. Sonos nicht erreichbar |

### Log-Meldungen (`docker compose logs -f`)

| Meldung | Bedeutung |
|---|---|
| `T2 W found at …` | Der Plattenspieler wurde gefunden |
| `T2 W not found …` | Der T2 W antwortet nicht. Ist er an und im selben Netz? Notfalls `T2W_HOST` setzen. |
| `speaker 'T2 W – X' ready` / `removed` | Ein Raum ist dazugekommen oder weggefallen |
| `T2 W: Play` / `Sonos is playing …` | Die Wiedergabe wurde gestartet |
| `source connected … (joined a running stream; FLAC header added)` | Normal: Der Encoder des T2 W lief schon, der Kopf wurde ergänzt |
| `playing source audio after …s of silence` | Die Musik kommt an. Die ersten Sekunden nach Play füllt sich der Puffer. |
| `source stream ended … reconnecting` | Der T2 W hat abgebrochen und wird neu verbunden. Folgt kein `buffer ran dry`, war es unhörbar. |
| `buffer ran dry: playing silence until …s` | Der Aussetzer war länger als die Reserve, man hat Stille gehört. Die Reserve ist jetzt größer. |
| `source not available yet` | Der T2 W liefert gerade kein FLAC |
| `watchdog: Sonos stopped on its own … restarting` | Ein Raum ist ausgestiegen und wurde neu gestartet |
| `no audio from the T2 W 20s after its Play: asking it to restart the stream` | Der T2 W hat nach „Play“ nichts geliefert, sein Stream wird neu gestartet |
| `source recovery: the T2 W started its stream again` | Der Neustart hat funktioniert, kurz danach folgt `source connected` |
| `source recovery: … select the speaker again` | Der Neustart hat nicht geholfen. Wähle den Lautsprecher in der App neu aus. |
| `the T2 W offers a compressed stream` | Der T2 W hat MP3 angeboten (siehe „Bekannte Eigenheiten“). Der Container liest trotzdem den FLAC-Ausgang. Die Wiedergabe bleibt verlustfrei, solange danach `source connected` folgt. |
| `none of the rooms in SONOS_ROOMS exists` | Ein Name in `SONOS_ROOMS` passt nicht. Die Meldung listet die richtigen Namen auf. |

## Einstellungen (`docker-compose.yml`, alle optional)

| Variable | Standard | Wirkung |
|---|---|---|
| `SONOS_ROOMS` | leer = alle | Nur diese Räume anbieten. Namen wie in der Sonos-App, durch Komma getrennt, Groß-/Kleinschreibung egal. |
| `SONOS_EXCLUDE` | leer | Diese Räume weglassen, z. B. `Bad,Garage` |
| `T2W_HOST` | leer = suchen | IP des T2 W. Nur nötig, wenn er nicht gefunden wird oder mehrere im Netz sind. |
| `FRIENDLY_NAME_FORMAT` | `T2 W – {room}` | Name in der Pro-Ject-App |
| `BUFFER_START_SECONDS` | `1` | Reserve zu Beginn der Wiedergabe: die Zeit von „Play“ bis zur Musik und die Verzögerung zwischen Nadel und Lautsprecher |
| `BUFFER_SECONDS` | `5` | Größte Reserve, die nach Aussetzern erreicht wird. Bei häufigem `buffer ran dry` erhöhen, z. B. auf `8`. Beide gleich setzen ergibt einen festen Puffer. |
| `WATCHDOG` | `1` | `0` schaltet den automatischen Neustart von Sonos ab |
| `SOURCE_RECOVERY` | `1` | `0` schaltet den Stream-Neustart beim T2 W ab |
| `SOURCE_RECOVERY_SECONDS` | `20` | So lange wartet der Container nach „Play“ auf Audio. Ein normaler Start dauert 10 bis 11 Sekunden. |
| `ADVERTISED_HOST` | leer = erkennen | IP dieses Rechners. Nur nötig, wenn er mehrere Netzwerkanschlüsse hat. |
| `SONOS_SEED_HOSTS` | leer | IP eines beliebigen Sonos-Lautsprechers, falls die automatische Suche im Netz blockiert ist |

Nach einer Änderung: `docker compose up -d`.

## Bekannte Eigenheiten

- **„Lossless stream failed to start. Consider disabling lossless“ in der Pro-Ject-App:** Der T2 W prüft 9 Sekunden nach „Play“, ob der Lautsprecher seinen Stream spielt, und bricht diese Prüfung bei „Stop“ nicht ab. Wechselst du innerhalb dieser 9 Sekunden zu einem anderen Lautsprecher, meldet er den verlassenen als gescheitert. Das passiert genauso mit einer echten Sonos-Box. **Lossless nicht abschalten.** Meldung wegklicken. Um sie zu vermeiden, bleib mindestens 10 Sekunden auf einem Lautsprecher, bevor du wechselst.
- **Alte Lautsprecher nach einem Update:** Nach dem Update von einer Version, die Lautsprecher anders benannt hat, kann die Pro-Ject-App einen Lautsprecher eine Weile doppelt zeigen. Nimm den anderen Eintrag oder starte die App neu. Veraltete Einträge verschwinden nach etwa 30 Minuten von selbst. Nutzt du Autoplay, wähle den Lautsprecher einmal neu aus, damit der T2 W sich den neuen merkt.
- **Eine Live-Quelle lässt sich nicht vorpuffern:** Streamingdienste füllen ihren Puffer schneller als in Echtzeit, weil das ganze Lied auf ihrem Server liegt. Der T2 W sendet genau so schnell, wie sich die Platte dreht, eine Reserve lässt sich also nur in einer Pause füllen. Deshalb startet sie klein und wächst nach einem Aussetzer. Die Musik beginnt etwa 2 bis 3 Sekunden nach „Play“ (1 s Reserve plus die eigene Pufferung von Sonos). Wer lieber von Anfang an vollen Schutz will, setzt `BUFFER_START_SECONDS` gleich `BUFFER_SECONDS`.

## Ports

Der Container nutzt diese Ports, alle nur im LAN:
- TCP 8186 für den Stream
- TCP 8090 für die UPnP-Steuerung
- UDP 1900 für die UPnP-Suche

Steuerbefehle nimmt er ausschließlich aus privaten Netzen an. Nichts davon gehört ins Internet freigegeben.

## Tests (auf einem Rechner mit Python 3.12+ und ffmpeg)

```bash
python -m unittest discover -s tests -v
```

105 Tests, darunter:
- ein bit-genauer Audio-Vergleich
- Abbrüche des T2 W
- ein Ende-zu-Ende-Durchlauf vom Steuerbefehl bis zum Audio
- Räume, die dazukommen, umbenannt werden oder verschwinden
- die Raumsuche einschließlich IP-Wechsel
- die Suche nach dem T2 W
- alle UPnP-Aktionen, Statusmeldungen und die SSDP-Suche
- alle Watchdog-Fälle
- das Einklinken in einen laufenden T2-W-Stream ohne FLAC-Kopf
- der Stream-Neustart beim T2 W

## Hintergrund

Getestet mit T2-W-Firmware 0.7.31. Ein Firmware-Update von Pro-Ject oder Sonos kann das Verhalten ändern.

Der T2 W schickt FLAC nur an Lautsprecher bestimmter Hersteller. Deshalb meldet sich der virtuelle Lautsprecher als „Pro-Ject Audio Systems“ (`RENDERER_MANUFACTURER`). Das dient allein der Kompatibilität. Dies ist ein inoffizielles Projekt, es steht in keiner Verbindung zu Pro-Ject oder Sonos.

## Unterstützen

Wenn dir das Projekt die Aussetzer erspart, freue ich mich über einen Kaffee:

[![Buy me a coffee](https://img.shields.io/badge/Buy%20me%20a%20coffee-FFDD00?style=for-the-badge&logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/blaraz)

## Lizenz

MIT, siehe [LICENSE](LICENSE).
