# LiteLLM-Synchronisierung

Die optionale FastAPI-Webseite **f24-sales**, ohne Virtualenv, ist in
[WEB.md](WEB.md) dokumentiert (Katalog, Modellrecherche, Historie, Fire-Demo und HTTPS).

Ein Python-3.11-Script synchronisiert kostenlose Chatmodelle von OpenRouter, Groq,
Kilo Gateway, Nous Portal, OpenCode Zen und NVIDIA Build mit einem LiteLLM-Proxy. Dieses Synchronisierungsprogramm verwaltet Modelle ausschließlich über HTTP.
Der separate, optionale SQL-Importer ist in [IMPORT.md](IMPORT.md) beschrieben. LiteLLM benötigt PostgreSQL und
`STORE_MODEL_IN_DB=True`.

## Start

```sh
python3.11 -m venv .venv
.venv/bin/pip install httpx
cp .env.example .env
chmod 600 .env
# .env bearbeiten: URL, Port, LITELLM_ADMIN_KEY und Provider-Tokens eintragen
.venv/bin/python free_sync.py --dry-run
.venv/bin/python free_sync.py
```

`.env` und `config.yaml` werden standardmäßig neben dem Script gesucht. Relative
Dateipfade beziehen sich auf das Arbeitsverzeichnis. Ein expliziter Port in
`LITELLM_BASE_URL` hat Vorrang vor `LITELLM_PORT`. Nichtleere Prozessvariablen
haben Vorrang vor `.env`. Die Konfiguration verwendet **JSON-Syntax, eine gültige
YAML-1.2-Teilmenge**; deshalb ist kein YAML-Paket erforderlich.

`.env` bleibt die lokale Soll-Quelle für Tokens. Provider-Tokens werden zusätzlich
über `/credentials` verschlüsselt durch LiteLLM persistiert, als
`free-sync-openrouter`, `free-sync-groq`, `free-sync-kilo`, `free-sync-nous`,
`free-sync-opencode` und `free-sync-nvidia`.
Deployments referenzieren nur den Credential-Namen; sie enthalten keine eigenen
`api_key`/`api_base`-Werte. Leere oder fehlende Eingaben löschen niemals Credentials.
Der Admin-Key muss bei jedem Lauf lokal verfügbar sein.

### Separater Verwaltungsschlüssel

`LITELLM_ADMIN_KEY` muss nicht der globale Master-Key sein. Ein separater
LiteLLM-Virtual-Key genügt, wenn er als `proxy_admin`/Administrator ausgestellt
ist und die Management-Routen dieses Scripts verwenden darf: `GET /openapi.json`,
`/credentials`, `/model/*`, `/key/list`, `/key/info`, `/key/generate`,
`/key/update`, `/key/delete` sowie `/v1/models` und die kurze Nous-Prüfung.
Ein normaler Modell-Client-Key oder ein Key, der nur `models=[litellm-free]`
und Chat/Models-Routen besitzt, reicht dafür nicht. Der Verwaltungsschlüssel
bleibt ausschließlich in `.env` und wird nie in `free_models.json`, `news.md`
oder `runs.log` geschrieben.

## Virtual Key

`CLIENT_KEY` ist ausschließlich der LiteLLM-Bearer für die geprüften grünen
Routen der Access Group `litellm-free`, kein Provider-Token. Nach einem Fullscan
werden beide Virtual Keys auf die grünen Modell-Aliase begrenzt. Ohne lokalen Bearer erzeugt das Script ihn über
`/key/generate`, prüft seine Beschränkungen und schreibt ihn atomar mit `0600` in
`.env`. Ein erzeugter Bearer wird außerdem genau einmal auf stdout ausgegeben:
den ersten Lauf deshalb interaktiv ausführen und dessen Ausgabe vertraulich halten.

Ein vorhandener `CLIENT_KEY` wird wiederverwendet und über `/key/info` geprüft.
Sein Alias muss `CLIENT_KEY_ALIAS` entsprechen. Ein neuer, selbst gewählter
`sk-…`-Wert kann ebenfalls registriert werden. Der Key erlaubt nur die konfigurierte
grüne Modellliste und die Routen `/v1/models` und `/v1/chat/completions`.

LiteLLM gibt vorhandene Bearer nicht im Klartext zurück. Ist der lokale Bearer
verloren, wird ein neuer erzeugt, lokal gespeichert und mit `/v1/models` geprüft;
erst danach werden alte Keys desselben Alias gelöscht. `--no-delete` verhindert
diese Rotation. Alias daher ausschließlich für dieses Script verwenden.

## Lokale Soll-Liste und Verlauf

- **`model_probe_results.json`** ist die stabile Availability-SOT für f24-sales:
  letzter Fullscan, Statusfarben, sichere Katalogdaten und chronologische
  Discovery-Zeitstempel. Dated `model_probe_results-*.json` bleiben als einzelne
  Laufdateien erhalten. Der Bearer steht nie darin, sondern ausschließlich als
  `CLIENT_KEY` in `.env`.
- **`free_models.json`** bleibt dauerhaft im Repo-Verzeichnis. Sie enthält die
  LiteLLM-Soll-Deployments für die Access Group, Aggregator, gemeldete Provider,
  Modellautor, unterstützte Parameter, Fähigkeiten, Kontext-/Output-Limits,
  Routing-Parameter und UTC-Zeitstempel `created_at`/`updated_at`. Keine Tokens.
  Der erste erfolgreiche Provider-Abruf legt die Datei an. Erfolgreiche Abrufe
  aktualisieren nur den betreffenden Provider. Das Script persistiert die
  Soll-Liste **vor** dem Abgleich und liest daraus die anzuwendenden Deployments.
  Ein fehlgeschlagener API-Schreibzugriff kann deshalb vorübergehend zu einer
  Abweichung zwischen Soll-Liste und Proxy führen; der nächste Lauf gleicht nach.
- **`runs.log`** erhält pro abgeschlossenem Aufruf eine zusätzliche JSON-Zeile
  mit Start/Ende in UTC, Status, Exit-Code und Zählern pro Provider. Auch Dry-Runs
  und fehlgeschlagene Läufe werden erfasst; ein hart beendeter Prozess kann
  keinen Abschluss schreiben.
- **`news.md`** wird ausschließlich um Änderungen der Soll-Liste ergänzt:
  aufgenommen, geändert oder entfernt, jeweils mit vollständigen Parametern
  und bei Änderungen dem vorherigen Stand. Beim ersten Lauf werden sämtliche
  aufgenommenen Modelle aufgeführt. Unveränderte Läufe erzeugen keine News.
  News beschreiben die Soll-Liste; den Erfolg ihrer Anwendung zeigt `runs.log`.
- `free_sync_state.json` enthält lediglich Deployment-IDs und Hashes als Cache.
  Ownership wird immer anhand von `model_info.managed_by=free-sync` aus LiteLLM
  ermittelt. Ein fehlender State-Cache verhindert den Abgleich nicht.

Fehlgeschlagene Provider-Abrufe lassen deren lokale Soll-Liste und Deployments
unverändert. Bei einem API-Fehler während der Anwendung können bereits erfolgreiche
Änderungen bestehen bleiben; weitere Löschungen dieses Providers unterbleiben.
`--no-delete` behält auch verschwundene Modelle in der Soll-Liste und im Proxy.
Zeitstempel bestehender, unveränderter Einträge bleiben erhalten.

`aggregator` bezeichnet den abgefragten Dienst. Der tatsächlich ausführende Provider
ist bei dynamischem Routing nicht immer bekannt: `provider`/`providers` bleiben
in diesem Fall `null`. `model_author` wird separat aus dem ID-Präfix ausgewiesen;
es wird kein Inferenzanbieter daraus erfunden. Unbekannte Parameter/Limits bleiben
leer beziehungsweise `null`.

Tokens, Soll-Datei, State, Backups und Logs bleiben per `.gitignore` aus dem
öffentlichen Repository. `free_models.json` bleibt dabei lokal die aktuelle
Soll-Datei und enthält keine Geheimnisse. Die Beispiele enthalten ausschließlich
Platzhalter.

## Täglich um 04:00

Crontab; Pfad anpassen, 04:00 bezieht sich auf die Zeitzone des Cron-Dienstes:

```cron
0 4 * * * cd /opt/litellm-free && .venv/bin/python free_sync.py >/dev/null 2>&1
```

Das Script schreibt `runs.log` und `news.md` selbst mit Append-Semantik (`>>`).
Ein Dateilock verhindert parallele Läufe mit demselben `STATE_FILE`. Exit-Codes:
`0` erfolgreich, `1` teilweise Fehler, `2` fataler Start-/Konfigurationsfehler.
Fehlende Provider-Tokens ohne gespeichertes Credential führen zu einer Warnung
und zum Überspringen des Providers. Fehlender Admin-Key führt zu Exit-Code `2`.

## Optionen

```sh
.venv/bin/python free_sync.py --provider openrouter --provider groq
.venv/bin/python free_sync.py --no-delete
.venv/bin/python free_sync.py --verbose
.venv/bin/python free_sync.py --forget nous
.venv/bin/python free_sync.py --forget nous --forget-cascade
.venv/bin/python free_sync.py --config config.local.yaml --env-file .env
```

`--forget` arbeitet getrennt vom normalen Sync, damit Credentials nicht sofort
neu angelegt werden. Referenzierende Deployments verhindern die Löschung, sofern
`--forget-cascade` fehlt. Fremde Referenzen verhindern sie immer. Zum dauerhaften
Abschalten zusätzlich `enabled: false` setzen oder den lokalen Token entfernen.

`--dry-run` führt lesende HTTP-Aufrufe und Schema-Prüfungen durch, schreibt aber
keine Credentials, Deployments, Keys, Soll-Liste oder News. Lock und Laufprotokoll
werden auch dann geschrieben.

`--scrub-env` ist optional und nicht für den normalen Betrieb mit `.env` als
Soll-Quelle gedacht. Nach erfolgreicher Übernahme entfernt es geeignete Provider-
und explizit übernommene Client-Key-Zeilen, mit Backup `.env.bak` (`0600`). Den
Admin-Key entfernt es nie. Ein entfernter Client-Bearer führt beim nächsten Lauf
zur beschriebenen Rotation. Ein gerade automatisch erzeugter Client-Key bleibt
lokal erhalten. Ohne diesen Schalter werden nur automatisch erzeugte Client-Keys
in `.env` geschrieben; bestehende Eingaben werden nicht verändert.

## Provider-Regeln

| Dienst | Auswahl und Einschränkungen |
| --- | --- |
| OpenRouter | ID endet auf `:free` oder Prompt- und Completion-Preis sind exakt null. Unterstützte Parameter, Kontext und `top_provider.max_completion_tokens` werden übernommen. |
| Groq | Chatmodelle des Free Tiers; Whisper, TTS, Guard und verwandte Nicht-Chatmodelle werden ausgeschlossen. Keine Garantie kostenloser Nutzung eines kostenpflichtigen Kontos. Limits gelten organisations- und modellbezogen, nicht pro Key. |
| Kilo | `https://api.kilo.ai/api/gateway`, ohne `/v1`. Prompt, Completion und weitere gemeldete Preise müssen strikt null sein. Negative Platzhalterpreise werden abgelehnt. |
| Nous | `:free`-Suffix gemäß Nutzerangabe. Fehlen solche IDs, ist eine ausdrücklich geprüfte `free_allowlist` nötig; andernfalls kein Abgleich und keine Löschung. |
| OpenCode Zen | Katalog über `https://opencode.ai/zen/v1/models`. Die Antwort enthält kostenpflichtige und kostenlose Modelle ohne Preisfelder; standardmäßig werden nur IDs mit `-free` oder `:free` übernommen. Die Suffixe sind über `free_suffixes` konfigurierbar. |
| NVIDIA Build | OpenAI-kompatibler Katalog über `https://integrate.api.nvidia.com/v1/models`. Die API liefert keine verlässliche Free-Markierung; deshalb werden nur IDs aus der expliziten `free_allowlist` übernommen. |

Kilos Modellkatalog ist öffentlich; kostenlose Inferenz ist laut
[Gateway-Dokumentation](https://kilo.ai/docs/gateway/authentication) auch anonym
möglich. Dieses Script überspringt dennoch gemäß Credential-Regel Provider ohne
lokalen Token **und** ohne gespeichertes Credential.

Der OpenCode-Zen-Katalog ist öffentlich abrufbar. Für Inferenz wird ein
`OPENCODE_API_KEY` als `free-sync-opencode`-Credential hinterlegt; ein HTTP-200
bei `/models` beweist allein nicht, dass dieser Token für Chat-Anfragen berechtigt
ist. Das Script markiert deshalb auch hier nur die Katalogprüfung als erfolgreich
und lässt die Inferenzberechtigung offen. Wenn der Key-Katalog weniger Modelle
liefert als ein anonymer Abruf, gilt ausschließlich die Key-Sicht; öffentlich
gelistete, mit dem Key nicht erreichbare Modelle werden nicht registriert.

HTTP 200 auf öffentlichen Katalogen beweist nicht die Gültigkeit eines Tokens.
OpenRouter wird zusätzlich über `/key` geprüft. **TODO/verifizieren:** Kilo- und
Nous-Tokenautorisierung mittels Inferenz; `/models` allein genügt dort nicht als
Authentifizierungsnachweis. Fehlgeschlagene Validierung ersetzt keine gespeicherten
Credentials und führt zu Exit-Code `1`.

Nous `tags_mode=auto` prüft neue Deployments einmal mit einer kurzen Inferenz.
Nur bei einem HTTP-400-Fehler mit Hinweis auf fehlende Tags folgt ein Versuch mit
`extra_body={"tags":["user=free-sync"]}`. Erfolgreiche Ergebnisse werden gespeichert.
Andere Fehler werden nicht als Tags-Problem ausgelegt. `always` setzt Tags fest,
`off` deaktiviert den Test. **TODO/verifizieren:** tatsächliche Anforderung für
aktuelle `:free`-Modelle; sie wird nicht pauschal unterstellt.

Groqs Katalog verlangt einen Token. Gespeicherte LiteLLM-Credentials werden von
GET `/credentials` nur maskiert zurückgegeben. Ohne lokalen Token benötigt Groq
daher einen vom Betreiber eingerichteten `proxy_catalog_path`. Dieser muss über
den Proxy den authentifizierten Upstream-Katalog liefern. Das Script richtet
keine solche Route ein und kopiert keine Geheimnisse in Passthrough-Konfigurationen.
`/v1/models` ist die Client-Sicht auf bereits registrierte Modelle und ersetzt
keinen Provider-Katalog mit Preisen und neu hinzugekommenen IDs. Bei den anderen
Providern kann der öffentliche Katalog ohne lokalen Token verwendet werden.

NVIDIA Build verwendet `NVIDIA_API_KEY` aus `.env`. Der öffentliche Modellkatalog
unter `https://integrate.api.nvidia.com/v1/models` wird mit diesem Key gelesen;
die vier zunächst freigegebenen Modelle stehen in `config.yaml` unter
`providers.nvidia.free_allowlist`.

## Konfiguration und Varianten

Pro Provider: `enabled`, `allowlist`/`denylist` als Regex-Listen, `rpm`, `tpm`,
`reasoning_variants` und optional `proxy_catalog_path`. Standardmäßig werden keine
unverifizierten Rate-Limits erfunden. Für Nous zusätzlich `free_allowlist` und
`tags_mode`. `api_base` darf nur HTTPS verwenden; Konfiguration ist vertrauenswürdig
zu behandeln, da Provider-Tokens an die konfigurierte Adresse gesendet werden.

Reasoning-Varianten entstehen nur bei entsprechender Katalog-Metadatenangabe.
OpenRouter erhält `-think`/`-fast` mit `reasoning.enabled=true/false`; Modelle mit
zwingendem Reasoning behalten die Basisvariante. Groq verwendet nur bekannte,
modellbezogene Werte aus der [API-Referenz](https://console.groq.com/docs/api-reference)
und [Reasoning-Dokumentation](https://console.groq.com/docs/reasoning). `fast`
bedeutet bei GPT-OSS niedrigen Reasoning-Aufwand, nicht abgeschaltetes Reasoning.
Bei fehlenden Fähigkeitsdaten werden keine Varianten geraten.

Modellnamen tragen standardmäßig immer den Aggregator-Präfix. Explizites
Zusammenlegen benötigt eine `merge_rules`-Regel, beispielsweise:

```json
{"name":"shared/my-model", "members":["openrouter/vendor/my-model:free", "nous/vendor/my-model:free"]}
```

Die IDs sind hier Platzhalter. Verschiedene Anbieter bleiben getrennte Deployments;
nur der Routingname wird vereinheitlicht. Namen fremder Deployments werden nicht
übernommen, um fremde Routen nicht unbeabsichtigt freizugeben.

## Verifikation und Grenzen

Vor der Implementierung wurden `/openapi.json` und der installierte Quellcode
von LiteLLM **1.101.0** gelesen, einschließlich Credentials, Model-Updates,
Key-Erzeugung mit benutzerdefiniertem `key`, `credential_info` und erweiterbarem
`model_info`. Das Script prüft diese Schemas erneut bei jedem Lauf. Die veröffentlichte Webseite verwendet die Ergebnisse lokaler Synchronisierungen
und Modellprüfungen. Für eine eigene Installation sind eigene Zugangsdaten und
ein eigener Scan erforderlich.

Credential-Änderungen aktualisieren in dieser Version den CredentialAccessor des
bearbeitenden Workers; neue Requests lösen benannte Credentials zur Laufzeit auf.
Zusätzlich aktualisiert das Script betroffene Deployments, damit Router-Clients
neu aufgebaut werden. **TODO/verifizieren:** Cache-Übernahme über mehrere getrennte
Proxy-Worker/Instanzen; die HTTP-Antwort eines Workers garantiert das nicht.

GETs und wiederholbare Updates verwenden Timeouts und Backoff bei 429/5xx.
Erzeugungs-Requests werden bei unklarer Antwort nicht blind wiederholt; der nächste
Lauf gleicht über feste Deployment-IDs und Key-Alias ab. API-Fehlerantworten werden
nicht geloggt, um enthaltene Tokens auszuschließen. Modelländerungen und Provider-
Refreshs sind keine gemeinsame Datenbanktransaktion. Logs werden nicht automatisch
rotiert; sie wachsen absichtlich fortlaufend.
