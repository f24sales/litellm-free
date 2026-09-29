# Modellrecherche nach dem Scan

Ein API-Scan kann Erreichbarkeit und Katalogfelder prüfen. Er kann nicht allein
zuverlässig feststellen, ob zwei neue Namen dasselbe Modell meinen oder ob ein
Eintrag ein Chatmodell, ein Router, Musik, ein Guardrail oder ein Entscheidungsmodell
ist. Dafür gibt es [review_models.py](review_models.py) und den vollständigen
Auftrag in [MODEL_REVIEW_TASK.md](MODEL_REVIEW_TASK.md).

## Ablauf

1. `free_sync.py` synchronisiert die Provider-Kataloge nach LiteLLM.
2. `model_probe.py` prüft die Routen und speichert den vollständigen Scan.
3. Bei einem vollständigen Scan sucht `review_models.py` nach neuen Kombinationen
   aus Aggregator und exakter Upstream-ID sowie Einträgen ohne `research_status=reviewed`.
4. Nur wenn solche Einträge vorhanden sind, wird ein Rechercheauftrag angelegt
   und ein konfigurierter Agent gestartet. Bestehende geprüfte Zuordnungen dienen
   als Referenz für Dubletten.
5. Ein erfolgreicher Agent liefert JSON. Der Validator prüft die erlaubten IDs,
   Modelltypen, Feldtypen, HTTPS-Quellen, Belege für offene Gewichte und die
   Begründung für zusammengeführte Identitäten.
6. Die Metadaten werden atomar ergänzt. Der vorherige Stand bleibt im Rechercheauftrag
   gesichert. Die Webseite liest die aktualisierten Metadaten beim nächsten Abruf.

Ein Provider-Teilscan startet die automatische Recherche nicht. Sie kann danach
mit `python3 review_models.py` separat ausgeführt werden. Der JSON-Export verlangt
zusätzlich zur erfolgreichen API-Prüfung einen recherchierten Chat-Eintrag.
Ungeprüfte Metadaten werden dadurch nicht automatisch zu importierbaren Chatmodellen.

## Codex CLI

Codex CLI installieren und unter demselben Betriebssystembenutzer anmelden,
unter dem der Scan läuft. In der lokalen `.env`:

```env
MODEL_REVIEW_AGENT=codex
MODEL_REVIEW_TIMEOUT=900
# Optional: ein Modell, das in der eigenen Codex-CLI verfügbar ist
MODEL_REVIEW_MODEL=
```

Der Adapter nutzt `codex exec`, den Read-only-Sandboxmodus, Live-Websuche,
`--ephemeral` und `--output-last-message`. Das Arbeitsverzeichnis enthält den
bereinigten Rechercheauftrag. Die Zugangsdaten des Katalogs werden nicht an den
Agent-Prozess vererbt; der Agent verwendet die eigene gespeicherte CLI-Anmeldung.
Der Adapter schaltet die Sandbox nicht ab.

```sh
python3 review_models.py --prepare-only
python3 review_models.py
```

Der erste Befehl legt nur den Auftrag ab. Der zweite startet den konfigurierten
Agenten und übernimmt eine gültige Antwort. Ohne offene Einträge erfolgt kein
Agent-Aufruf. Das verwendete Agent-Angebot kann eigene Kosten und Limits haben.

## Claude Code, Hermes, OpenClaw und andere Agenten

Es gibt einen nativen Codex-Adapter und eine allgemeine Befehlsschnittstelle.
Für andere CLIs wird ein eigener kleiner Wrapper passend zur lokal installierten
CLI-Version angegeben; das Repository behauptet keine ungeprüften CLI-Schalter.

```env
MODEL_REVIEW_COMMAND=/absolute/path/to/my-review-agent --input {input} --output {output}
MODEL_REVIEW_TIMEOUT=900
```

Der Befehl wird ohne Shell gestartet. Er erhält den vollständigen Auftrag auch
auf stdin. Unterstützte Platzhalter:

| Platzhalter | Inhalt |
| --- | --- |
| `{input}` | Absoluter Pfad zu `input.json`: neue IDs und vorhandene Zuordnungen |
| `{task}` | Absoluter Pfad zu `TASK.md`: Auftrag einschließlich Input |
| `{output}` | Absoluter Pfad für `proposal.json` |

Der Wrapper muss Webrecherche mit dem gewählten Agenten ermöglichen und genau
das JSON-Objekt aus `MODEL_REVIEW_TASK.md` liefern: entweder in `{output}` oder
als alleinigen stdout-Inhalt. Diagnosemeldungen gehören auf stderr. Exit-Code
`0` bedeutet Erfolg. Eine konfigurierte `MODEL_REVIEW_COMMAND` hat Vorrang vor
`MODEL_REVIEW_AGENT`. Der Prozess erhält grundlegende Pfad-/Benutzervariablen,
keine Provider-, Datenbank- oder LiteLLM-Schlüssel aus der Scan-`.env`.

## Manuelle Prüfung und fehlende Agenten

Ohne Agent-Konfiguration werden Aufträge unter `model_reviews/<id>/` abgelegt:

- `input.json`: bereinigte Katalogfelder und vorhandene Metadaten;
- `TASK.md`: vollständiger Auftrag;
- `proposal.json`: Antwort des Agenten;
- `metadata.before.json`: Sicherung vor einer erfolgreichen Übernahme.

Ein anderweitig erstellter Vorschlag lässt sich gezielt übernehmen:

```sh
python3 review_models.py --apply model_reviews/JOB_ID
```

Der aktuelle Metadaten-Hash muss dabei noch dem Ausgangsstand entsprechen.
Parallele manuelle Änderungen werden nicht überschrieben; bei geändertem Stand
ist ein neuer Auftrag nötig. Aufträge sind lokale Betriebsdaten und werden nicht
nach GitHub gepusht.

## Was geprüft wird und was nicht

Der Agent muss Hersteller und Aggregator unterscheiden, Original-IDs erhalten,
Versionen getrennt halten und Thinking pro Gateway recherchieren. Primärquellen
haben Vorrang. Stealth-Identitäten, unbekannte Fähigkeiten, Länder und Logos dürfen
nicht geraten werden. Ein unbekannter Hersteller bleibt `null`.

Die strukturelle Prüfung ersetzt keine inhaltliche Beweisprüfung der Quellen.
`reviewed` bedeutet, dass die Recherche stattgefunden hat; es behauptet keine
vollständig bekannte Modellidentität. Unsicherheit muss im Text stehen. Logos und
Länderangaben werden durch diese automatische Schnittstelle nicht verändert;
solche Assets werden separat aus offiziellen Quellen kuratiert.

Agent-Fehler, Timeout, ungültiges JSON oder ein geänderter Metadatenbestand lassen
den vorhandenen Bestand bestehen. Der Scan erhält einen `model_review`-Status
mit dem Fehlertyp; der eigentliche API-Scan bleibt erhalten. Der Agent kann weder
Scan-Farben noch Zugangsdaten, LiteLLM-Deployments oder Virtual Keys ändern.

## Zeitplanung

Beide Schritte unter dem Benutzer mit den passenden CLI- und Provider-Zugängen
aufrufen, beispielsweise über einen systemd-Timer oder Cron:

```sh
cd /path/to/litellm-free
python3 free_sync.py && python3 model_probe.py
```

Die Recherche ist bereits in den vollständigen Probe-Lauf integriert und muss
nicht zusätzlich eingeplant werden. Bei Agent-Timeouts den abgelegten Auftrag
prüfen und `review_models.py` später erneut aufrufen. Der Synchronisierer ist
weiterhin auch ohne Webanwendung oder Agent nutzbar.
