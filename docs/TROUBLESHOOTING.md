# Troubleshooting

## Logs

```sh
tail -f ~/Library/Logs/VoiceBridge.log
```

Das Log enthält Zustandswechsel, STT/TTS-Fehler, Sitzungs-IDs,
Fortschrittsansagen und die Barge-in-Diagnose. Das Cockpit zeigt nur das
Transkript, nicht das Roh-Log.

## Mikrofon

Beim ersten Start fragt macOS nach der Freigabe. Wenn die App stumm bleibt:

1. Systemeinstellungen → Datenschutz & Sicherheit → Mikrofon → VoiceBridge
   aktivieren.
2. Freigabe zurücksetzen und neu starten:

```sh
tccutil reset Microphone local.voicebridge
open ~/Applications/VoiceBridge.app
```

Das Terminal braucht beim Start über `python -m voicebridge` eine eigene
Freigabe.

## „VoiceBridge läuft bereits"

Es darf nur eine Instanz geben. Die Sperren liegen unter
`~/.local/state/voice-bridge/` (`widget.lock`, `loop.lock`). Sie werden beim
Prozessende automatisch freigegeben. Laufende Instanzen anzeigen:

```sh
pgrep -fl "python.*-m voicebridge"
```

Hängt nach einem Neustart noch ein alter Prozess, beende ihn über das Menü
(„Beenden") oder gezielt per PID. Starte danach nur **eine** Instanz:

```sh
open -n ~/Applications/VoiceBridge.app
```

## Cockpit über dem Vollbild-Terminal

macOS erlaubt fremden Fenstern keinen Overlay über den Vollbild-Space einer
anderen App. Das Cockpit bleibt auf dem Desktop-Space. Lösungen:

- Menüleisten-Symbol → **„Cockpit anzeigen"**
- Space wechseln (Ctrl + Pfeiltasten)
- OpenCode im Fenster statt im Vollbild laufen lassen

## Menüleiste zeigt „Python"

Bekannte Grenze des Python-Helfers: Der Prozessname in der Menüleiste bleibt
`Python`. App, Fenster und Bundle heißen `VoiceBridge` (Bundle-ID
`local.voicebridge`). Ein eigener Menüname bräuchte eine vollständige
py2app-/Swift-Einbettung.

## STT/TTS

- **Erster Request dauert ~2 Minuten:** Der STT-Container wärmt das Modell auf.
  `voicebridge --warmup` vorab ausführen.
- **401/403:** `VOICE_API_KEY` prüfen.
- **400:** `VOICE_STT_MODEL` muss zum Dienst passen (Standard
  `whisper-turbo`).
- **Kein Ton:** `afplay` ist auf macOS vorhanden; alternative Ausgabe mit
  `VOICE_PLAYER=portaudio` testen.
- **Zeitüberschreitung (120 s):** `VOICE_REQUEST_TIMEOUT_S` erhöhen oder die
  Aufgabe kleiner schneiden.

## Barge-in

Bricht Rauschen die Antwort ab, `VOICE_BARGE_FACTOR` und
`VOICE_BARGE_MIN_RMS` erhöhen. Lässt sich die Antwort nicht unterbrechen,
`VOICE_BARGE_FACTOR` senken. Nach jeder Wiedergabe steht eine Diagnosezeile im
Log:

```
Barge-in: Boden 600 · Gain 0.04 · Spitze 1118 · Schwelle max 600 · 1900 ms
```

`Spitze` ist der lauteste Mikrofonpegel während der Wiedergabe, `Schwelle max`
die höchste angewendete Schwelle. Liegt `Spitze` unter `Schwelle`, war die
Sprache zu leise für die aktuelle Einstellung.

## Cockpit-Theme

Das Dropdown **Retro/Modern/Amber** oben rechts wechselt zwischen dem
Retro-Look („Voice Link // 2086": Neon-Grid, Scanlines, Cyan/Magenta), dem
modernen Look und dem bernsteinfarbenen Amber-Terminal. Die Wahl wird lokal
gespeichert. Bei aktivierter macOS-Einstellung „Bewegung reduzieren" steht das
Grid still.

## Modell fehlt in der Auswahl

Die Auswahl zeigt Provider aus `~/.config/opencode/opencode.json` unter
`providers`, verbundene Integrationen (z. B. `opencode` Zen) und Provider aus
OpenCodes `auth.json` (API-Key). Mit `VOICE_MODEL_PROVIDERS` lässt sich die
Auswahl explizit begrenzen (z. B. `local-litellm,treno,opencode,ollama-cloud`).
Ein Provider, der nirgends auftaucht, erscheint nicht.

## Helfer-Leiste bleibt leer

Die Leiste zeigt nur Subagenten, die der Orchestrator **in dieser Voice-Sitzung**
startet. Arbeitet er stattdessen selbst über `execute` (Code-Mode), bleibt sie
leer. Sage dann zum Beispiel „Nimm den Scout" oder „Lass das den Coder machen".
Die Agenten-Prompts unter `agents/` definieren, wann delegiert werden soll.

## Verlauf im Cockpit

Beim Auswählen einer Sitzung werden die letzten 20 Züge geladen (Nutzerfrage +
letzte Assistenten-Antwort). Der Verlauf kommt live aus OpenCode; ältere
Nachrichten können dort verdichtet oder entfernt werden. Das ist kein
VoiceBridge-Speicher.

## PDFs

`anymd` als MCP einrichten (siehe [MCP.md](MCP.md)). Prüfen, ob der Chip im
Cockpit grün ist, und danach eine **neue Sitzung** starten. Ein CLI-Test zeigt,
ob die Extraktion selbst funktioniert:

```sh
npx -y @sylphx/anymd dokument.pdf | head -40
```

## Tests

```sh
python -m unittest discover -s tests -v
```
