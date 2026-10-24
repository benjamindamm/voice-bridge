# OpenCode-Agenten

Diese vier Prompts gehören zum Sprachmodus:

| Datei | Modus | Rolle |
| --- | --- | --- |
| `voice-orchestrator.md` | primary | kurze, gesprochene Antworten; fragt vor Änderungen, delegiert |
| `coder.md` | subagent | setzt bestätigte Änderungen um und prüft sie |
| `scout.md` | subagent | recherchiert lesend in Dateien und im Web |
| `voice-reviewer.md` | subagent | prüft Änderungen lesend |

## Installation

```sh
mkdir -p ~/.config/opencode/agents
cp agents/*.md ~/.config/opencode/agents/
```

`VOICE_AGENT=voice-orchestrator` (Standard) wählt den primären Agenten. Die
Subagenten werden vom Orchestrator gestartet; er darf selbst weder schreiben
noch Shell-Befehle ausführen.

Die `.env`-Sperre in allen vier Prompts verhindert, dass im Sprachbetrieb eine
Berechtigungsabfrage die Sitzung blockiert.
