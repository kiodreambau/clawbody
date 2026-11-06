# Kiyo auf dem Reachy Mini

Kiyo verbindet das Mikrofon und den Lautsprecher des Reachy Mini mit einer lokalen
Python-CLI. Der Name, die Stimme, das Gespräch und die Wahl des Agenten gehören
zur Brücke. Kiyo nutzt Hermes, OpenClaw oder Claude Code für die Antworten.
Die bestehende ClawBody-App und ihre Kommandozeile sind weiter im Repository.

## Bedienung

Sag „Kiyo, guten Abend“. Nach einer Antwort hört Kiyo 45 Sekunden auf
Folgefragen. Mit „Bis später“ beendet er das Gespräch am Mikrofon.
„Neues Gespräch“ leert den geteilten Kontext. Das Mikrofon pausiert während
der Verarbeitung und der Sprachausgabe.

- „Kiyo, wechsle zu Hermes“ → Kiyo fragt nach der Schnittstelle. Antworte „API“.
- „Kiyo, nimm OpenClaw über lokal“ → wählt Agent und Schnittstelle direkt.
- „Kiyo, wechsle zu Claude über CLI“ → nutzt Claude Code mit seiner lokalen Anmeldung.
- „Kiyo, was steht morgen im Kalender?“ → liest den ausgewählten Google-Kalender.
- „Kiyo, welche Rechner sind verbunden?“ → meldet verbundene OpenClaw-Knoten.
- „Kiyo, gib OpenClaw den Auftrag: …“ → merkt den Auftrag für die Freigabe im Terminal vor.

Die gleichen Befehle funktionieren vom Terminal.

```sh
kiyo doctor
kiyo wake
kiyo ask "Woran waren wir gerade?"
kiyo use hermes --via api
kiyo calendar morgen
kiyo computers
kiyo sessions openclaw
kiyo remote-sessions codex --node "Dreambau M4"
kiyo remote-sessions claude --node "Dreambau M4"
kiyo tasks
kiyo approve ID
kiyo delegate "Fasse die folgenden Punkte zusammen …"
kiyo delegate --session agent:main:BEKANNTES-ZIEL "Was ist der aktuelle Stand?"
```

Ein bestätigter Auftrag erhält eine eigene OpenClaw-Sitzung. Mit `--session`
kannst du einen vorhandenen Sitzungsschlüssel gezielt adressieren. Das
Kommando `remote-sessions` listet Sitzungen über die vorhandenen OpenClaw-Plugins
des gekoppelten Rechners. Das Gateway kontrolliert, welche Rechner und
Schnittstellen erreichbar sind. Kiyo wiederholt einen fehlgeschlagenen Auftrag
nicht automatisch.

## Installation auf einem Apple-Silicon-Mac

Im eigenen Checkout mit Python 3.12 und `uv`:

```sh
uv venv --python 3.12
uv pip install -e '.[kiyo-voice,kiyo-calendar]'
mkdir -p ~/.config/kiyo
cp -n kiyo.example.toml ~/.config/kiyo/config.toml
HF_HUB_DISABLE_XET=1 .venv/bin/python -c 'from huggingface_hub import snapshot_download; snapshot_download("mlx-community/whisper-base-mlx")'
```

Nutze `.venv/bin/kiyo` oder lege einen Symlink auf das Skript in einen Ordner
in deinem PATH. `kiyo audio-test` spielt einen Satz auf dem USB-Lautsprecher ab.
`kiyo microphone-test` transkribiert einen Testsatz lokal. `kiyo robot` startet
den Sprachmodus im Vordergrund.

### Agenten

- **Hermes:** Die lokale API muss erreichbar sein. Für Sprache ein eigenes
  Hermes-Profil mit eigener API-Berechtigung und
  `platform_toolsets.api_server: []` anlegen. Das Profil muss einen angemeldeten
  Modellanbieter haben. In `[voice]` den Profilnamen eintragen. Kiyo prüft vor
  jeder gesprochenen Anfrage, ob das Profil Werkzeuge meldet.
- **OpenClaw:** Kiyo übernimmt Gateway, Plugins und Anmeldung der installierten
  CLI. Für Sprache unter `agents.entries` einen eigenen Agenten mit eigenem
  Workspace und `tools.deny: ["*"]` anlegen. Die ID kommt in `[voice]`.
  Weitere Schnittstellen sind Namen bereits eingerichteter OpenClaw-CLI-Profile
  unter `[openclaw_profiles]`. Diese Schnittstellen sind vom Terminal aus nutzbar.
- **Claude:** Die lokale Claude-Code-CLI benötigt eine gültige Anmeldung.
  Kiyo startet sie im Print-Modus mit gesperrten eingebauten Werkzeugen und ohne
  MCP-Konfiguration aus dem Projekt.

### Reachy beim Login starten

Wenn Reachy Mini Control den Daemon bereits verwaltet, reicht der Sprachdienst.
Ansonsten kannst du einen separaten lokalen Daemon auf dem USB-Seriellport
registrieren. Verwende immer einen Daemon pro Roboter. Ein anderer
Reachy-Startdienst oder eine automatisch startende Körper-App kann USB oder
Audio belegen.

```sh
.venv/bin/python scripts/install-kiyo-mac.py
# Oder mit separatem Daemon: scripts/install-kiyo-mac.py --serial /dev/cu.usbmodem...
# Die angezeigten launchctl-bootstrap-Befehle ausführen, zuerst com.kiyo.reachy.
kiyo doctor
kiyo wake
```

Der separate Daemon bindet an Loopback und lässt USB-Audio bei Kiyo. Er
startet mit ausgeschalteten Motoren. `kiyo wake` schaltet sie ein und führt
die Reachy-Aufwachbewegung aus. Mit `launchctl bootout gui/$(id -u)/com.kiyo.voice`
beendest du den Sprachdienst. Logs liegen unter `~/Library/Logs/Kiyo`.

### Google Kalender

In einem eigenen Google-Cloud-Projekt die Calendar API aktivieren und einen
OAuth-Client vom Typ **Desktop-App** erstellen. Dann einmalig verbinden.

```sh
kiyo calendar-connect /pfad/zur/client_secret.json
kiyo calendar-check
```

Kiyo fordert `calendar.events.readonly` an, verwendet standardmäßig den
primären Kalender und die Zeitzone Europe/Berlin. `woche` liest die nächsten
sieben Tage. Der Refresh-Token liegt in Kiyos privatem lokalen Zustandsordner.
Die direkte Kalenderabfrage geht an Google. Die formatierten Termindaten
werden lokal ausgegeben und bleiben außerhalb des Agentengesprächs.

## In Codex oder einem Editor weiterbauen

- [SOUL.md](src/kiyo/SOUL.md) enthält Kiyos Persönlichkeit.
- [kiyo.example.toml](kiyo.example.toml) zeigt lokale Einstellungen für
  Schnittstellen, Stimme und Kalender.
- [assistant.py](src/kiyo/assistant.py) steuert Kontext, Agentenwechsel und Aufträge.
- [backends.py](src/kiyo/backends.py) verbindet die installierten Agenten.
- [robot.py](src/kiyo/robot.py) verarbeitet USB-Audio. Whisper und die
  macOS-Sprachausgabe laufen lokal. Die ausgewählte Agenten-Schnittstelle
  erhält den Text einer aktiven Unterhaltung.
- `~/.local/state/kiyo` enthält den kurzen gemeinsamen Gesprächskontext, die
  vorgemerkten Aufträge und die lokale Google-Autorisierung.

```sh
.venv/bin/python -m unittest discover -s tests -v
```
