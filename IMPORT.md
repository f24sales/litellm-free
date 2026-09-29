# LiteLLM-Konfiguration importieren

Öffentliche, feste Quelle: **https://www.f24-sales.com/litellm-config.json**.
Die aktuelle Vorlage für Zugangsdaten liegt unter
**https://www.f24-sales.com/litellm.env.example** und als
[import.env.example](import.env.example) im Repository.

Der Export enthält nur erfolgreiche, recherchierte Chat-Routen. Musik,
Guardrails, Entscheidungsmodelle, Embeddings, Sprache und Router sind ausgeschlossen.
Eine Route kann eine bestätigte `fast`-/`think`-Voreinstellung enthalten;
`litellm_params.model` behält die originale Upstream-ID. Die lokale `model_name`
enthält den Aggregator und gegebenenfalls das Preset.

## Einrichtung und .env-Dateien

```sh
git clone https://github.com/f24sales/litellm-free.git
cd litellm-free
python3 -m pip install --user -r requirements-import.txt
cp import.env.example .env
chmod 600 .env
```

In `.env` nur eigene Schlüssel für die verwendeten Aggregatoren eintragen:
`OPENROUTER_API_KEY`, `GROQ_API_KEY`, `KILO_API_KEY`, `NOUS_API_KEY`,
`OPENCODE_API_KEY`, `NVIDIA_API_KEY`. Anbieter mit leeren Schlüsseln werden beim
Import ausgelassen. Die Datei enthält keine F24-Zugangsdaten.

`--env-file` darf mehrfach vorkommen; spätere nichtleere Werte gewinnen.
Nichtleere Prozessvariablen haben Vorrang. Ohne Angabe wird `.env` im aktuellen
Arbeitsverzeichnis verwendet, falls vorhanden. Werte werden nicht als Shellcode
ausgeführt und nicht durch `${...}`-Interpolation verändert.

```sh
python3 import_litellm.py file --env-file /etc/litellm/providers.env \
  --env-file /etc/litellm/site.env --output ./litellm-free.json
```

Der Download sendet keine Provider-Schlüssel an F24 SALES. Er erfolgt über HTTPS
von der fest im Skript hinterlegten Quelle. Vor dem Einsatz eigener Schlüssel
prüft der Importer API-Adressen, Variablenreferenzen, Presets und Modellidentitäten
gegen seine erlaubten Provider. Damit kann der Download keine fremde Zieladresse
für einen lokalen Schlüssel vorgeben.

## 1. Konfigurationsdatei

```sh
python3 import_litellm.py file --env-file .env --output litellm-free.json
```

Die erzeugte JSON-Datei enthält `model_list` und `os.environ/…`-Referenzen,
keine aufgelösten Schlüssel. JSON ist gültiges YAML und kann von LiteLLM als
Startkonfiguration geladen werden. TOML ist hierfür nicht das LiteLLM-Format.

```sh
# Die Provider-Variablen müssen auch im LiteLLM-Prozess verfügbar sein.
# Bei Containern dessen --env-file verwenden, bei systemd EnvironmentFile.
litellm --config /path/to/litellm-free.json
```

Für einen bestehenden Konfigurationsbestand nur `model_list` gezielt ergänzen;
das Skript überschreibt keine bestehenden Dateien ohne `--force`. Mit
`--all-providers` entsteht eine Vorlage für alle exportierten Provider, auch wenn
lokal noch Schlüssel fehlen. `--dry-run` zeigt den Umfang ohne Dateiänderung.

Das Laden einer Konfigurationsdatei ist keine Datenbankmigration. Um dieselbe
heruntergeladene Datei in die LiteLLM-Datenbank zu übernehmen, Weg 2 oder 3 nutzen.

## 2. Datei oder Download über die Verwaltungs-API in die Datenbank

Zusätzlich in `.env` setzen:

```env
LITELLM_BASE_URL=https://your-litellm.example
LITELLM_ADMIN_KEY=your-existing-admin-key
```

Ein separater `LITELLM_PORT` wird berücksichtigt, wenn die URL keinen Port enthält.
Remote-Endpunkte brauchen HTTPS; HTTP ist für Loopback erlaubt. LiteLLM benötigt
eine PostgreSQL-Anbindung und `STORE_MODEL_IN_DB=True`.

```sh
python3 import_litellm.py api --env-file .env --dry-run
python3 import_litellm.py api --env-file .env
# Alternativ aus einer bereits geladenen Datei:
python3 import_litellm.py api --env-file .env --input litellm-free.json
```

Neue Modelle gehen an `/model/new`, eigene bereits importierte Modelle an
`/model/{id}/update`. LiteLLM verschlüsselt die Daten und aktualisiert seine
Verwaltung. Mehrere HTTP-Aufrufe bilden keine gemeinsame Transaktion: Bei einem
Fehler können vorherige Modelle bereits übernommen sein. Ein erneuter Lauf nutzt
dieselben IDs und erzeugt keine zusätzlichen Kopien.

## 3. Direktes SQL, etwa bei litellm-database

Für einen vorhandenen Podman-Container:

```sh
python3 import_litellm.py sql --env-file .env \
  --container litellm-database --dry-run
python3 import_litellm.py sql --env-file .env \
  --container litellm-database
```

Für Docker `--engine docker` ergänzen. Mit `--input litellm-free.json` wird eine
lokale Datei statt des Downloads verwendet. Das Skript übergibt die ausgewählten
Modelle per stdin an [sql_import.py](sql_import.py) im Container. Zugangsdaten
stehen weder im Prozessargument noch in erzeugten SQL-Dateien.

Im LiteLLM-Container müssen `psycopg` (Version 3), das installierte LiteLLM-Paket,
`DATABASE_URL` und die **bereits verwendete** `LITELLM_SALT_KEY` beziehungsweise
`LITELLM_MASTER_KEY` vorhanden sein. Beim Container-Modus gelten dessen Datenbank-
und Verschlüsselungseinstellungen; entsprechende Werte aus der Host-`.env`
überschreiben sie nicht. Provider-Schlüssel stammen aus den ausgewählten
Host-`.env`-Dateien. Den bestehenden Saltschlüssel niemals für den Import ersetzen.

Ohne Container läuft derselbe Worker in der aktuellen Python-Umgebung. Dafür
müssen LiteLLM und `psycopg[binary]` installiert sein; die `.env` benötigt dann
zusätzlich die existierende `DATABASE_URL` und den bestehenden Verschlüsselungsschlüssel.

Der Worker:

- prüft die Spalten von `public."LiteLLM_ProxyModelTable"` vor dem Schreiben;
- nutzt LiteLLMs installierte `encrypt_value_helper` für String-Parameter;
- schreibt parametrisierte SQL-Anweisungen in einer gemeinsamen Transaktion;
- aktualisiert nur Modelle mit `managed_by=f24-sales-import` und stabiler Import-ID;
- überspringt gleichnamige fremde Modelle und bricht bei fremder ID-Eigentümerschaft ab;
- erhält vorhandene Sperrflags und löscht keine Modelle;
- verwendet beim Dry-Run eine tatsächlich schreibgeschützte Transaktion.

Geprüft wird das in LiteLLM 1.101.0 vorhandene PostgreSQL-Schema. Zusätzliche
Spalten sind möglich; inkompatible Pflichtspalten/Datentypen führen zum Abbruch.
Ein SQL-Fehler rollt die gesamte Importtransaktion zurück. Fehlerausgaben enthalten
keine SQL-Parameter, Datenbank-Passwörter oder Provider-Schlüssel.

Direkte SQL-Änderungen umgehen die Verwaltungs-API und deren Cache-Aktualisierung.
Nach einem echten SQL-Import die LiteLLM-Worker neu laden beziehungsweise neu
starten, z. B. `systemctl --user restart litellm-database.service` bei einem
entsprechenden Quadlet. Das Skript startet keine Dienste selbstständig neu.
Es ändert auch keine vorhandenen Virtual-Key-Freigaben; bei festen Modelllisten
müssen neue Aliase dort separat freigegeben werden.

## Wiederholungen und Grenzen

Datei, API und SQL sind alternative Wege für denselben Modellbestand. Nicht
zusätzlich denselben Bestand aus einer Startdatei und der Datenbank laden.
Bestehende durch `free_sync.py` verwaltete Modelle werden beim Datenbankimport
als fremder Bestand erkannt und übersprungen. Der Import ist keine automatische
Löschung später entfallener Modelle. Die laufende Provider-Synchronisierung ist
separat in [SYNC.md](SYNC.md) beschrieben.

Ein Free-Tier hängt weiterhin von Anbieter, Konto, Kontingent und Zeitpunkt ab.
Die JSON-Datei enthält den letzten geprüften Stand, keine dauerhafte Zusage.

## Verifikation

Am 29. September 2026 gegen das Schema der installierten LiteLLM-Version 1.101.0
geprüft. Ein echter Importlauf in einer separaten, kurzlebigen PostgreSQL-18-
Testdatenbank bestätigte: schreibgeschützten Dry-Run, Insert, Verschlüsselung und
Entschlüsselung, unveränderten Wiederholungslauf, Update, Erhalt von Sperrflags,
Überspringen fremder Aliase und vollständigen Rollback bei einer ID-Kollision.
Der produktive Bestand wurde dafür nur mit einem Read-only-Dry-Run geprüft.

Der reproduzierbare Integrationstest liegt unter
[tests/sql_integration_check.py](tests/sql_integration_check.py). Er verlangt eine
separate Datenbank namens `f24_import_test`, eine passende LiteLLM-Python-Umgebung
und mindestens drei Export-Einträge mit Dummy-Schlüsseln als JSON-Liste auf stdin.
Die regulären Tests unter `tests/test_import_and_review.py` prüfen Downloads,
Umgebungsvariablen, manipulierte Konfigurationen, API-Import, SQL-Transport und
die Agent-Validierung ohne Live-Inferenz.
