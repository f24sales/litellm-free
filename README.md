# litellm-free · F24 SALES

**Webseite: [www.f24-sales.com](https://www.f24-sales.com/)**

Kostenlos zugängliche KI-Chatmodelle, ihre Aggregatoren und tatsächlich geprüfte
API-Routen an einem Ort. Dieses Repository enthält die Webseite, den
LiteLLM-Synchronisierer, die Modellprüfungen, eine Agent-Anbindung zur Recherche
neuer Modelle und den Import für eigene LiteLLM-Installationen.

Unterstützte Aggregatoren: **OpenRouter, Groq, Kilo, Nous Portal, OpenCode Zen
und NVIDIA Build**. Modellhersteller und Aggregator werden getrennt ausgewiesen.

## Webseite

- Ein gemeinsamer Eintrag je Modell mit seinen verfügbaren Aggregatoren.
- Kontext, Output-Limits, Modalitäten, offene Gewichte und Thinking je API-Zugang.
- Info-Popups mit Anmeldung, API-Basis-URL, Dokumentation und Quellen.
- Originale Modell-IDs, offizielle Modellseiten und lokal eingebundene Logos.
- Filter nach Aggregator und Hersteller sowie eine Fire-Demo mit festen Fragen.
- Downloads für LiteLLM und ein GitHub-Button mit dem originalen GitHub-Symbol.

Musik, Guardrails, Entscheidungsmodelle, Embeddings, Sprache und reine Router
werden nicht als normale Chatmodelle angeboten. Die Karten verwenden beim
Scan erfolgreiche Routen; historische Hinweise können frühere Zugänge erwähnen.
Ein erfolgreicher Scan ist eine Momentaufnahme und keine Verfügbarkeitsgarantie.

**🏆**: zuerst in unseren Scans gelistet. **🥈**: nächster unterschiedlicher
Entdeckungstag. Spätere Aggregatoren teilen sich Platz drei. Diese Reihenfolge
bezeichnet weder Qualität noch exklusiven Zugang. Bei Modellen aus dem ersten
Bestand ist eine frühere Reihenfolge nicht bekannt.

## Import

**[LiteLLM-Konfiguration herunterladen](https://www.f24-sales.com/litellm-config.json)**
· **[.env-Vorlage herunterladen](https://www.f24-sales.com/litellm.env.example)**

Der Importer lädt standardmäßig direkt von der oben verlinkten Adresse auf
**www.f24-sales.com**. Die JSON-Datei ist auch gültiges YAML und lässt sich als
LiteLLM-Startkonfiguration verwenden. Sie enthält ausschließlich Verweise auf
Umgebungsvariablen für API-Schlüssel.

```sh
git clone https://github.com/f24sales/litellm-free.git
cd litellm-free
python3 -m pip install --user -r requirements-import.txt
cp import.env.example .env
chmod 600 .env
# .env bearbeiten: eigene Provider-Schlüssel eintragen
```

| Weg | Befehl | Ergebnis |
| --- | --- | --- |
| Konfigurationsdatei | `python3 import_litellm.py file --env-file .env` | `litellm-free.json` für `litellm --config` |
| HTTP-API → Datenbank | `python3 import_litellm.py api --env-file .env` | Modelle über die LiteLLM-Verwaltungs-API speichern |
| Direktes SQL | `python3 import_litellm.py sql --env-file .env --container litellm-database` | Transaktion in der PostgreSQL-Datenbank des laufenden LiteLLM-Containers |

Alle Wege unterstützen `--dry-run`, wiederholtes `--env-file` und `--input`
für eine bereits heruntergeladene JSON-Datei. Provider ohne lokalen Schlüssel
werden übersprungen. Fremd verwaltete Modelle werden nicht überschrieben;
der Importer löscht keine Modelle. SQL verwendet die Verschlüsselung der
installierten LiteLLM-Version. Bestehende Datenbank-/Saltschlüssel bleiben erhalten.

**Vollständige Beispiele, Datenbankvoraussetzungen, Docker/Podman und Reload:
[IMPORT.md](IMPORT.md).**

## Scan und Recherche durch einen Agenten

```text
Provider-Kataloge → LiteLLM-Sync → API-Scan → neue IDs recherchieren
                                               ↓
                                   validierte Modell-Metadaten
                                               ↓
                                Webseite und LiteLLM-Download
```

Der API-Scan prüft Erreichbarkeit. Ein LLM-Agent recherchiert ausschließlich neue
oder noch ungeprüfte IDs: Hersteller, Modelltyp, Dubletten, Fähigkeiten,
Thinking beim jeweiligen Aggregator und belastbare Quellen. Die Zuordnungen
werden vor der Übernahme strukturell geprüft. Bestehende recherchierte Einträge
werden nicht bei jedem Scan erneut an ein LLM geschickt.

Für den eingebauten Codex-CLI-Adapter in der lokalen `.env`:

```env
MODEL_REVIEW_AGENT=codex
MODEL_REVIEW_TIMEOUT=900
```

Codex CLI muss installiert und angemeldet sein. Andere Agenten wie Claude Code,
Hermes oder OpenClaw können über `MODEL_REVIEW_COMMAND` angebunden werden.
Ohne Agent wird ein prüfbarer Rechercheauftrag abgelegt. Vollständiger Ablauf,
Ausgabeformat und Fehlerbehandlung: **[AGENT_REVIEW.md](AGENT_REVIEW.md)**.

## Lokal ansehen

Python 3.11 oder neuer; Node.js für die UI-Tests. Für die Vorschau sind weder
API-Schlüssel noch ein LiteLLM-Server nötig.

```sh
python3 -m pip install --user -r requirements-web.txt
cp examples/model_probe_results.json model_probe_results.json
python3 -m uvicorn web:app --host 127.0.0.1 --port 8080 --no-access-log
```

Danach **http://127.0.0.1:8080/** öffnen. Der bereinigte Beispielkatalog stammt
vom 29. September 2026. Fire benötigt eine eigene Backend-Konfiguration.

## Eigene Synchronisierung betreiben

Ein vorhandener LiteLLM-Proxy mit PostgreSQL und `STORE_MODEL_IN_DB=True` wird
vorausgesetzt. In `.env` die Werte aus [.env.example](.env.example) ergänzen.

```sh
python3 free_sync.py --dry-run
python3 free_sync.py
python3 model_probe.py
```

`free_sync.py` aktualisiert die verwalteten Deployments. `model_probe.py` führt
echte Testanfragen aus, startet bei Bedarf die Recherche und beschränkt vorhandene
Client-/Web-Schlüssel auf grüne Routen. Provider-Limits gelten weiterhin.
`python3 setup_web_key.py` richtet den separaten Schlüssel und Vor-/Nachfilter
für die optionale Fire-Demo ein. Schlüssel bleiben im Backend; Antworten werden
von der Webanwendung nicht gespeichert.

## Modell-IDs

Direkte Aggregator-Aufrufe verwenden dessen Basis-URL und originale Modell-ID,
etwa `https://api.groq.com/openai/v1` mit `openai/gpt-oss-120b`.
`groq/openai/gpt-oss-120b` wäre dagegen ein lokaler LiteLLM-Routingname.
Zusätzliche `-fast`-/`-think`-Aliase sind lokale Voreinstellungen. Echte
Hersteller-Präfixe und dokumentierte Suffixe wie `:free` bleiben erhalten.
Ein Katalogeintrag allein beweist keine funktionierende Inferenz. Einige
OpenCode-Free-Routen verlangen App oder CLI; das gilt nicht für alle Modelle.

## Dokumentation und Tests

| Datei | Inhalt |
| --- | --- |
| [IMPORT.md](IMPORT.md) | JSON-Download, `.env`, Datei-, API- und SQL-Import |
| [AGENT_REVIEW.md](AGENT_REVIEW.md) | Recherche-Agent nach dem Scan, Adapter und Validierung |
| [MODEL_REVIEW_TASK.md](MODEL_REVIEW_TASK.md) | Vollständiger Rechercheauftrag für den Agenten |
| [WEB.md](WEB.md) | Darstellung, Datenmodell, Branding, Fire und Deployment |
| [SYNC.md](SYNC.md) | Synchronisierer, Credentials, Virtual Keys und Provider-Regeln |
| [model_metadata.json](model_metadata.json) | Recherchierte Zuordnungen und Quellen |

```sh
python3 -m pip install --user -r requirements-web.txt pytest
python3 -m pytest -q tests
node --test tests/catalog-ui.test.cjs
```

Lokale Zugangsdaten, aktuelle Scan-Dateien, Rechercheaufträge und Laufprotokolle
bleiben außerhalb von Git. Quellen und Lizenzen eingebundener Logos, Schrift
und HTMX stehen unter [static/logos](static/logos/SOURCES.md),
[static/fonts](static/fonts/Adwaita-LICENSE.txt) und
[static/vendor](static/vendor/htmx-LICENSE.txt). F24-SALES- und GitHub-Vektoren
behalten ihre ursprünglichen Proportionen.
