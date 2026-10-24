# MCP-Server

Das Cockpit zeigt alle MCP-Server, die OpenCode für die Sitzung sieht. Jeder
Chip hat einen Statuspunkt:

| Punkt | Bedeutung |
| --- | --- |
| grün | verbunden / bereit |
| grau | ausgeschaltet (`disabled`) |
| rot | Fehler |
| orange | unbekannt / startet |

## Ein- und Ausschalten

Ein Klick auf einen konfigurierbaren Chip ändert die **globale**
OpenCode-Konfiguration:

- **V1:** `mcp.<name>.enabled = true|false`
- **V2:** `mcp.servers.<name>.disabled = true|false`

Vor der ersten Änderung wird
`~/.config/opencode/opencode.json.bak-voicebridge` angelegt. Geschrieben wird
atomar über eine temporäre Datei.

**Wichtig:** Tool-Definitionen werden beim Start einer Sitzung geladen. Nach dem
Umschalten im Cockpit deshalb **„Neue Sitzung"** (＋) starten. Die laufende
Sitzung behält ihren Tool-Satz.

Server aus einer Integration (z. B. `figma`) stehen nicht in der Config. Ihr Chip
ist gesperrt und der Tooltip sagt „Integration, nicht schaltbar".

## Kontext schlank halten

Jeder aktive MCP-Server schickt seine Tool-Beschreibungen in den Kontext. Für
Sprachsitzungen ist das teuer und macht den Agenten unruhig. Empfehlung:

- Nur Dienste eingeschaltet lassen, die du in der Sitzung brauchst.
- Nach dem Umschalten eine neue Sitzung starten.
- Den Zustand im Cockpit im Blick behalten; die Liste aktualisiert sich alle 10 s.

## PDFs und Dokumente: anymd

`anymd` ist ein lokaler MCP-Server, der PDF, DOCX, XLSX, PPTX, EPUB, HTML und
Bilder in sauberes Markdown verwandelt. Kein API-Key, nichts wird hochgeladen.

Eintrag in `~/.config/opencode/opencode.json` (V1):

```json
{
  "mcp": {
    "anymd": {
      "type": "local",
      "command": ["npx", "-y", "@sylphx/anymd"],
      "enabled": true
    }
  }
}
```

Danach eine neue Sitzung starten. Der Agent kann dann PDFs direkt lesen,
durchsuchen und seitenweise zusammenfassen. CLI-Test:

```sh
npx -y @sylphx/anymd bericht.pdf | head -40
```

Der Server bietet u. a. `read` (Markdown), `outline` (Gliederung),
`search` (Volltext mit Seitenangaben) und `inspect` (Seiten, OCR, Struktur).

## Sicherheit

MCP-Server laufen mit deinen Benutzerrechten. Nur vertrauenswürdige Server
eintragen. Ein Datei-Leser wie `anymd` kann jede Datei lesen, die dein Benutzer
lesen darf – genauso wie ein Shell-Subagent. Der Orchestrator selbst darf weder
schreiben noch Shell ausführen; diese Rechte liegen bei `coder`/`scout`.
