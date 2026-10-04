# Implementierungsplan: bosch-homecom-mqtt-bridge

## Ziel

Ein Container liest Geräte aus der Bosch-HomeCom-Easy-Cloud aus und veröffentlicht die Werte auf MQTT, vorrangig
für FHEM. Das erste unterstützte Gerät ist ein Bosch Tronic 7000 (Durchlauferhitzer, Gerätetyp `wddw2`). Weitere
Geräte desselben Kontos sind vorgesehen. Struktur, Pipelines und Betrieb folgen dem Schwesterprojekt
`whatsmeow-mqtt-bridge`.

Dieser Schritt bereitet nur das Repository vor. Die Bridge selbst folgt im nächsten Schritt.

## Abgrenzung und Name

Das Repository heißt `bosch-homecom-mqtt-bridge`. Der Name bindet die Bridge an die Cloud, nicht an ein Gerät. Bosch
betreibt weitere, getrennte Clouds mit eigener Anmeldung (Home Connect für Hausgeräte, Bosch Smart Home mit lokaler
API). Sie gehören nicht hierher und bekommen bei Bedarf eigene Repositories.

## Mehrere Geräte

- Der Dienst fragt standardmäßig alle Gateways des Kontos ab. `BOSCH_DEVICE_ID` schränkt auf ein Gerät ein.
- Pro Gerätetyp gibt es einen Adapter, der die passende `homecom_alt`-Klasse nutzt (`wddw2`, `rac`, `k40`, `icom`,
  `commodule` und weitere). Ein Adapter liefert die Abfrage, die Abbildung auf MQTT und optional die Schreibbefehle.
- Der Tronic 7000 (`wddw2`) ist der erste Adapter. Andere Typen starten deaktiviert und werden einzeln ergänzt.
- Unbekannte oder noch nicht unterstützte Typen werden einmal geloggt und übersprungen. Sie stören die übrigen nicht.
- Die Topics tragen die Geräte-ID (`<base>/<deviceId>/…`), damit mehrere Geräte nebeneinander funktionieren.

## Faktenlage (am 2026-10-04 mit eigenem Gerät geprüft)

- Es gibt keine offizielle öffentliche Bosch-API für den Tronic 7000. Der Zugriff läuft über die
  inoffizielle Cloud-Schnittstelle der HomeCom-Easy-App (`pointt-api.bosch-thermotechnology.com`),
  Anmeldung per SingleKey ID (OAuth2 Authorization Code mit PKCE).
- Die Python-Bibliothek [`homecom_alt`](https://pypi.org/project/homecom-alt/) (MIT) kapselt Login,
  Token-Refresh und die `wddw2`-Abfragen (Klasse `HomeComWddw2`).
- Gelesen wurden: Betriebsmodus, Sollwert, Zu-/Auslauftemperatur, Durchfluss, Leistung, Stromverbrauch,
  Wasserverbrauch, Betriebsstunden, Starts, Sicherheitstemperatur, Urlaubsmodus, Meldungen.
- Schreibbar laut Antwort: Betriebsmodus, manueller Sollwert, Sicherheitstemperatur, Urlaubsmodus.
- Die Daten kommen aus der Cloud. Polling statt Echtzeit; die Antwortzeit liegt im Sekundenbereich.

## Technologie

| Thema | whatsmeow-mqtt-bridge | bosch-homecom-mqtt-bridge |
| --- | --- | --- |
| Sprache | Go | Python 3.13 (`homecom_alt` gibt es nur in Python) |
| Abhängigkeiten | `go.mod` | `requirements.txt`, exakt gepinnt, von Renovate gepflegt |
| Sitzungsspeicher | SQLite oder PostgreSQL | Token-Datei im Volume `/data` (kein Datenbankbedarf) |
| Erstanmeldung | QR-Code in den Container-Logs | SingleKey-Login per Browser, siehe unten |
| Image | distroless, nonroot, Multi-Arch | distroless Python, nonroot, Multi-Arch |
| Tests | `go test`, `go vet` | `unittest`, `compileall` |

## Repository-Struktur

```text
.devcontainer/        Entwicklungscontainer (Python, mosquitto-clients)
.github/workflows/    docker-image.yml (Test, Multi-Arch-Build, Publish), linter.yml (dclint)
deploy/mosquitto.conf optionaler lokaler Broker
docs/                 Betriebsdokumente (folgen mit der Implementierung)
scripts/prototype/    Login- und Lese-Skripte aus der Machbarkeitsprüfung
src/bosch-homecom-mqtt-bridge/       Anwendung
tests/                Unit-Tests
Dockerfile, docker-compose*.yml, .env.example, VERSION, RELEASE_POLICY.md, renovate.json
```

Compose-Dateien als Overlays wie im Vorbild: `docker-compose.yml` (nur Bridge),
`docker-compose.host.yml`, `docker-compose.mqtt.yml`. Das lokale, ignorierte
`docker-compose.override.yml` bindet das FHEM-Docker-Netzwerk an. Ein PostgreSQL-Overlay entfällt bewusst.

## Pipelines

Unverändert vom Vorbild übernommen, nur die Go- durch Python-Schritte ersetzt:

- `docker-image.yml`: `prepare` (VERSION prüfen, `compileall`, `unittest`), `build` (nativ je Architektur,
  `linux/amd64` auf `ubuntu-latest`, `linux/arm64` auf `ubuntu-24.04-arm`, Push nur auf `main` per Digest),
  `merge` (Multi-Arch-Manifest mit `sha-<commit>`-Tag; Versions-Tags nur, wenn sich `VERSION` ändert).
- `linter.yml`: `dclint` auf alle Compose-Dateien bei Pull Requests.
- `renovate.json`: wie im Vorbild, mit angepinnten Digests.

Release-Ablauf: siehe `RELEASE_POLICY.md`.

## Login im Container

### Randbedingungen

1. Der Login passiert im Browser der Nutzerin oder des Nutzers, inklusive CAPTCHA. Ein Login mit
   Benutzername und Passwort aus dem Container ist nicht möglich.
2. Das Ziel der Weiterleitung ist ein App-Schema (`com.bosch.tt.dashtt.pointt://app/login?code=…`).
   Der Browser kann es nicht öffnen; der Code muss aus der Adresszeile oder der Entwicklerkonsole kopiert werden.
3. Der Code ist kurzlebig und nur einmal gültig.
4. Der Refresh-Token rotiert und ist ebenfalls nur einmal gültig. Jeder Refresh liefert einen neuen,
   der alte ist danach wertlos. Ein verlorener neuer Token erzwingt einen neuen Browser-Login.

### Ablauf

```text
docker compose run --rm -it bridge login
  1. erzeugt code_verifier (secrets.token_urlsafe(64)) und state, beide nur im Speicher
  2. druckt die Login-URL (PKCE-Challenge S256, Bosch-App-Client, Marke bosch oder buderus)
  3. wartet auf Eingabe über stdin: nur der Code oder die komplette Weiterleitungs-URL
     (Code wird extrahiert; bei kompletter URL wird state geprüft)
  4. ruft validate_auth(code, code_verifier) direkt auf und prüft die Tokens mit der Gateway-Liste
  5. schreibt /data/auth.json und zeigt Geräte-ID und -Typ der gefundenen Gateways
```

Die Bibliothek nutzt im Pfad `get_token()` mit `ConnectionOptions(code=…)` fest die öffentliche Konstante
`OAUTH_BROWSER_VERIFIER`; ein eigener Verifier geht nur über `validate_auth` (`base.py`). Login-URL und Austausch
müssen dieselbe Marke verwenden (`OAUTH_LOGIN_PARAMS` bzw. `_BUDERUS`, ebenso `options.brand`). Der Code wird vor dem
Einsetzen URL-kodiert, weil die Bibliothek ihn roh in den Body schreibt.

Fehlerbehandlung des Austauschs: Bei HTTP 400 liefert die Bibliothek `None`, `validate_auth` löst dann einen
`AttributeError` statt `AuthFailedError` aus. `login` fängt beides ab und meldet „Code ungültig oder abgelaufen,
bitte erneut einloggen“.

Der Verifier liegt im interaktiven Ablauf nur im Speicher. Nicht interaktive Variante, nachrangig: `login --print-url`
legt Verifier und state mit zehn Minuten Gültigkeit in `/data/login-pending.json` (Rechte 600) ab, `login --code -`
liest den Code von stdin (nicht als Argument, wegen Prozessliste und Shell-History) und löscht die Datei.

### Zustand und Persistenz

- `/data/auth.json` (Rechte 600, per `os.open` mit Modus 600 angelegt) enthält den Refresh-Token und Metadaten
  (Marke, Zeitpunkt). Zu prüfen bleibt, ob zusätzlich das Access-Token samt `exp` gespeichert werden soll,
  damit ein Neustart keinen Refresh-Token verbraucht (Crash-Loops, Ratenlimit). Standard: nicht speichern.
- Schreiben atomar: temporäre Datei, `fsync`, `rename`.
- Annahme, im Test mit dem echten Konto zu belegen: der Server entwertet den alten Refresh-Token bei jedem Refresh.
  Die Bibliothek geht davon aus (Docstring zu `get_token`), der Code beweist es nicht.

### Lock-Protokoll (Dienst und `login` teilen das Volume)

Der Dienst hält den Token im Speicher. Ein Lock allein genügt deshalb nicht; er muss die ganze Folge umfassen:

1. `flock` auf `/data/auth.lock` nehmen.
2. `auth.json` neu lesen. Unterscheidet sich der Token (oder eine Generationsnummer in der Datei) vom Speicher,
   die Datei übernehmen. So überschreibt der Dienst keinen frischen Login.
3. Erst dann refreshen, atomar schreiben, Lock freigeben.
4. `login` nimmt denselben Lock zum Schreiben und erhöht die Generationsnummer.

`flock` funktioniert über Container hinweg auf lokalen Named Volumes, nicht zuverlässig auf NFS oder CIFS.
Das Volume muss lokal sein.

### Refresh vor dem Abruf

`homecom_alt` ruft `get_token()` intern an vielen Stellen auf (`async_update`, `async_request_bulk` und andere) und
bietet keinen Hook zum Persistieren. Deshalb:

1. Die Bridge ruft vor jedem Poll selbst `await api.get_token()` unter dem Lock auf.
2. Hat sich `api.refresh_token` geändert, wird sofort geschrieben, vor jeder weiteren Anfrage.
3. Danach läuft `async_update`. Die internen `get_token()`-Aufrufe sind No-ops, solange das Access-Token mehr als
   fünf Minuten gültig ist (`check_jwt`). Das setzt ein Polling-Intervall deutlich unter der Token-Laufzeit voraus
   und wird im Test mit dem echten Konto bestätigt.
4. Nach jedem Bibliotheksaufruf wird `api.refresh_token` mit dem gespeicherten Wert verglichen und bei Abweichung
   sofort geschrieben.

### Statusmodell und Fehlerabbildung

`starting` → `auth_required` → `connecting` → `ready`; außerdem `disconnected` und `error`. Der Status ist
retained, die LWT setzt `disconnected`.

| Ereignis | Zustand |
| --- | --- |
| Keine `auth.json` | `auth_required` |
| `AuthFailedError` aus explizitem `get_token()` | `auth_required` |
| `AuthFailedError` (401) bei einem Datenabruf | einmal `get_token(force=True)`, danach `auth_required` |
| `NotRespondingError`, `ApiError`, `AttributeError` (die Bibliothek liefert bei 502/504 `{}`) | Backoff, `disconnected` |

In `auth_required` stellt der Dienst keine Anfragen. Er prüft alle zehn Sekunden, ob `login` eine neue
`auth.json` geschrieben hat, und verbindet sich dann selbst. Ein Neustart ist nicht nötig. Der Fehlercode ist
`AUTH_REQUIRED`, analog zu `SESSION_INVALID` im Vorbild.

Restrisiko: Der Refresh-Token geht verloren, wenn die Antwort nach der Rotation beim Server nicht ankommt
(Timeout, Verbindungsabbruch, Absturz vor dem Schreiben). Der nächste Versuch liefert dann `invalid_grant`.
Den Refresh deshalb nie blind wiederholen. Bei der nächsten Ablehnung geht der Dienst auf `auth_required` und
verweist im Fehler-Event auf `login`. Die Bibliothek verwirft den Fehler-Body bei HTTP 400, `invalid_grant` ist
also nicht von anderen Ablehnungen zu unterscheiden.

### Sicherheit

- Code, Verifier, state und Tokens erscheinen weder in MQTT-Nachrichten noch in Logs. Die Login-URL darf in die Logs.
- Image läuft als `nonroot` (UID 65532), das Volume gehört dieser UID.
- Der Prototyp-Token `.tokens.json` ist per `.gitignore` und `.dockerignore` ausgeschlossen. Die Prototypen nutzen den
  öffentlichen festen Verifier der Bibliothek und dienen nur dem Machbarkeitsnachweis. Nach der Umstellung auf
  `auth.json` den Prototyp-Token löschen oder durch einen neuen Login entwerten.

### Verworfene Alternativen

- Benutzername und Passwort im Container: scheitert am CAPTCHA der SingleKey ID.
- Browser-Automatisierung im Container: schwer, fragil, vergrößert das Image erheblich.
- Code über einen MQTT-Befehl: ein Geheimnis auf dem Broker, widerspricht dem Vorbild (keine Pairing-Daten über MQTT).
- HTTP-Endpunkt `/login` im Dienst: zusätzliche Angriffsfläche. Später prüfbar, wenn der Health-Server steht.

### Risiken

- Die Schnittstelle ist inoffiziell. Bosch oder SingleKey können Client, Weiterleitung oder CAPTCHA ändern.
- Die Bibliothek hat bereits Anpassungen an Login-Änderungen gebraucht (Issue zu CAPTCHA im Home-Assistant-Forum).
  Die Version bleibt gepinnt, Renovate schlägt Updates vor, die Pipeline testet sie.
- Mögliche Ratenlimits der Cloud. Polling-Intervall konfigurierbar, Standard 60 Sekunden.

## MQTT-Vertrag (Entwurf, im nächsten Schritt festzulegen)

Alle Publishes mit QoS 1, Status retained, Basis-Topic `MQTT_BASE_TOPIC` (Standard `bosch-homecom`).

```text
<base>/event/status          retained, {"state":"ready","connected":true,"message":"optional"}
<base>/event/error           {"code":"AUTH_REQUIRED","message":"..."}
<base>/<deviceId>/state      retained JSON mit allen gelesenen Werten
<base>/<deviceId>/cmd/...    Schreibbefehle, nur falls gewünscht (offen)
```

## Offene Entscheidungen

1. **Lizenz**: vorläufig MIT (wie `midea-mqtt`, vereinbar mit `homecom_alt`). Das Vorbild nutzt GPL-3.
2. **FHEM oder Home Assistant**: ein Beispiel für `MQTT2_DEVICE` oder MQTT-Discovery, oder beides.
3. **Nur lesen oder auch schreiben**: Schreibzugriff auf Sollwert und Modus über MQTT.
4. **Release-Automatik**: manuelle Versionserhöhung wie im Vorbild, oder Auto-Bump mit `CHANGELOG.md` wie in `midea-mqtt`.
5. **Broker und Zugangsdaten** sowie Name des FHEM-Docker-Netzwerks.
6. **Geräteauswahl**: Standard sind alle Gateways des Kontos; offen ist, ob Adapter für weitere Gerätetypen
   gleich mit aufgenommen werden.

## Nächste Schritte

1. Lizenz und offene Entscheidungen festlegen.
2. `auth`-Modul: Login-Befehl, Token-Datei, Lock-Protokoll, Refresh vor dem Abruf; Tests gegen einen Fake-Tokenendpunkt
   (Rotation, atomares Schreiben, Wettlauf Dienst gegen `login`, Fehlerabbildung).
3. Cloud-Client mit Adapter-Schnittstelle, `wddw2`-Adapter (`HomeComWddw2`), Polling-Schleife, Fehlerbehandlung.
4. MQTT-Client, Status, LWT, Topics; Tests wie im Vorbild (`topics`, `queue`, `protocol`).
5. Health-Server (`/healthz`, `/readyz`), Compose-Healthcheck, Port-Mapping.
6. FHEM-Beispiel und Betriebsdokumentation; Smoke-Test mit echtem Gerät.
