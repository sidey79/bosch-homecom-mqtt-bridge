# MQTT-Vertrag

Verbindlicher Vertrag zwischen der Bridge und MQTT-Konsumenten (FHEM, Home Assistant, eigene Skripte).
Entscheidung und Begründung: [ADR 0002](adr/0002-mqtt-contract.md); dort sind auch die Festlegungen
aufgeführt, die aus D10 abgeleitet und noch vom Nutzer zu bestätigen sind. Umsetzung: `src/bosch_homecom_mqtt_bridge/`
(`topics.py`, `protocol.py`, `publish_queue.py`, `publisher.py`). Die Tabellen dieses Dokuments werden von
`tests/test_protocol.py` gelesen und gegen den Code geprüft; eine Änderung am Vertrag ändert Dokument und Code
gemeinsam.

## Grundregeln

- Alle Publishes mit **QoS 1**.
- Basis-Topic `<base>` = `MQTT_BASE_TOPIC`, Default `bosch-homecom`. Es darf mehrere Ebenen haben (`haus/bosch`).
- Die Bridge **liest nur** bei der Cloud; sie schreibt nichts an Geräte. Einzige Ausnahme vom reinen Publizieren ist
  der optionale [Login über MQTT](#login-über-mqtt-optional): Nur mit `MQTT_LOGIN_ENABLED=true` abonniert sie genau
  ein Topic, `<base>/bridge/cmd/login`. Ohne den Schalter gibt es keine Subscriptions.
- Payloads sind UTF-8. JSON ist kompakt (ohne Leerzeichen); die Feldreihenfolge ist nicht Teil des Vertrags.

## Topics

Beispiele mit `<base>` = `bosch-homecom` und `<deviceId>` = `101506113`.

| Nachricht | Topic | Retained | QoS | Payload-Beispiel |
| --- | --- | --- | --- | --- |
| Status | `bosch-homecom/bridge/status` | ja | 1 | `{"state":"ready","connected":true}` |
| Status mit Text | `bosch-homecom/bridge/status` | ja | 1 | `{"state":"auth_required","connected":true,"message":"login required"}` |
| Status (LWT) | `bosch-homecom/bridge/status` | ja | 1 | `{"state":"disconnected","connected":false}` |
| Fehler | `bosch-homecom/bridge/error` | nein | 1 | `{"code":"AUTH_REQUIRED","message":"refresh token rejected"}` |
| Zustand | `bosch-homecom/101506113/state` | ja | 1 | `{"temperature":52.5,"heating":true,"mode":"eco","updated_at":"2026-10-04T12:00:00Z"}` |
| Verfügbarkeit online | `bosch-homecom/101506113/availability` | ja | 1 | `online` |
| Verfügbarkeit offline | `bosch-homecom/101506113/availability` | ja | 1 | `offline` |
| Login ausstehend | `bosch-homecom/bridge/login` | ja | 1 | `{"state":"pending","url":"https://singlekey-id.com/auth/connect/authorize?…","expires_at":"2026-10-04T12:10:00Z"}` |
| Login erledigt | `bosch-homecom/bridge/login` | ja | 1 | `{"state":"done"}` |

### `<base>/bridge/status`

Retained, `{"state": "<Statuswert>", "connected": <bool>, "message": "<optional>"}`. `message` fehlt, wenn es
nichts zu sagen gibt. `connected` heißt: Die Bridge ist mit dem Broker verbunden; es ist nur bei `disconnected`
`false`.

Die Bridge setzt beim Verbinden einen **Last Will** auf dieses Topic (retained, QoS 1) mit
`{"state":"disconnected","connected":false}`. Fällt sie ohne sauberes Trennen aus, veröffentlicht der Broker ihn.
Beim geordneten Stopp veröffentlicht die Bridge denselben Status selbst, wartet bis zu 5 s auf dessen PUBACK
und trennt erst dann sauber. Bleibt das PUBACK aus, schließt sie die Verbindung ohne DISCONNECT, sodass der
Broker den Last Will veröffentlicht. Nach jedem (Wieder-)Verbinden
veröffentlicht sie den zuletzt gesetzten Status erneut und überschreibt so den Last Will.

#### Statuswerte

| Wert | Bedeutung |
| --- | --- |
| `starting` | Prozess läuft, erster Abruf steht noch aus |
| `ready` | Cloud erreichbar, Werte werden abgerufen |
| `auth_required` | Kein gültiger Refresh-Token; `login` ist nötig |
| `error` | Anderer anhaltender Fehler (beim Abruf: ab dem 3. Fehlschlag in Folge); Einzelheiten in `message` und `<base>/bridge/error` |
| `disconnected` | Bridge nicht mit dem Broker verbunden (Last Will oder geordneter Stopp) |

Weitere Werte kommen nur mit einer Änderung dieses Vertrags hinzu. Konsumenten behandeln unbekannte Werte wie `error`.

### `<base>/bridge/error`

Nicht retained, `{"code": "<UPPER_SNAKE_CASE>", "message": "<Text>"}`. Bekannte Codes: `AUTH_REQUIRED` (Refresh-Token fehlt oder abgelehnt), `LOGIN_FAILED` (ein Schritt des [Logins über MQTT](#login-über-mqtt-optional) scheiterte; die Nachricht nennt den Schritt) und `POLL_FAILED` (Abruf bei der Cloud
3-mal in Folge fehlgeschlagen; einmal je Übergang in `error`, die Nachricht nennt nur den Ausnahmetyp). Einzelne
Fehlschläge davor werden nur geloggt und ändern weder Status noch Verfügbarkeit. Als Fehlschlag zählt auch ein
Abruf, der keinen einzigen Wert liefert (`NoDataError`): Erreicht die Cloud das Gerät nicht, antwortet sie je
Ressource mit 502/504, die Bibliothek macht daraus leere Werte, und ohne diese Regel erschiene das Gerät `online`
mit lauter `null` und `notifications` 0. Ebenso zählt eine leere Gateway-Liste bei der Erkennung
(`NoGatewaysError`): Die Bibliothek liefert sie auch bei 403/404/502/504, die Erkennung wird deshalb beim nächsten
Abruf wiederholt, statt nach einem kurzen Aussetzer beim Start dauerhaft ohne Geräte `ready` zu melden. Ein Konto ganz ohne Gateways bleibt so dauerhaft in `error`. Weitere
Codes folgen mit den Features, die sie auslösen, und werden hier ergänzt. Fehler-Events sind Hinweise, kein Zustand.

### `<base>/<deviceId>/state`

Retained, flaches JSON-Objekt der gelesenen Werte: Schlüssel sind Strings, Werte sind Zahl, Bool, String oder
`null`, keine verschachtelten Objekte oder Listen. Nicht endliche Zahlen (NaN, ±Inf) erscheinen als `null`. Zahlen stehen **ohne** Einheitentext; ob die Cloud-Werte umgerechnet werden, legt der Adapter je Gerätetyp fest
(`wddw2`: Werte wie geliefert, Temperaturen in °C, `water_total_consumption` in Litern (gegen die App geprüft), `hs_electricity_total_consumption` in kWh (gegen die App geprüft), `hs_operation_hours` in Stunden und `hs_starts` als Anzahl Betriebszyklen (gegen die App geprüft), `water_flow` in l/min und `hs_actual_power` in kW laut App (Wert im Leerlauf 0, Skalierung bei Last noch zu prüfen)). `updated_at` ist reserviert: Zeitpunkt des Abrufs, ISO-8601 in UTC,
sekundengenau mit `Z`, etwa `2026-10-04T12:00:00Z`. Die Namen der Werte je Gerätetyp legt der Adapter fest
(PR `feat/wddw2-poller-health`).

**Gerätetyp `wddw2`** (Schlüssel ohne Einheitentext, Werte wie von der Cloud geliefert, nicht umgerechnet; fehlende
Werte `null`): je Warmwasserkreis `dhw<n>_` + `operation_mode`, `air_box_temperature`, `fan_speed`,
`inlet_temperature`, `outlet_temperature`, `water_flow`, `safety_temperature`, `temp_level_<Stufe>`; dazu
`hs_starts`, `hs_actual_power`, `hs_power_percentage`, `hs_operation_hours`, `hs_electricity_total_consumption`,
`water_total_consumption`, `holiday_mode` und `notifications` (Anzahl).

### `<base>/<deviceId>/availability`

Retained, `online` oder `offline` als Klartext. Gilt nur, solange `<base>/bridge/status` `connected: true` meldet;
bei `disconnected` sind alle Geräte als nicht verfügbar zu betrachten. `offline` folgt erst, wenn der Abruf eines Geräts
3-mal in Folge fehlschlägt; ein erfolgreicher Abruf setzt den Zähler zurück.

## Login über MQTT (optional)

Nur mit `MQTT_LOGIN_ENABLED=true` (Default `false`). Ersetzt `login` im Terminal; beide schreiben denselben neuen
Login in `auth.json`, den der laufende Dienst innerhalb von etwa 10 s übernimmt.

### `<base>/bridge/cmd/login` (Befehl an die Bridge)

QoS 1, **nicht retained** senden. Retained-Befehle ignoriert die Bridge (mit WARNING), weil der Broker einen Code
sonst bei jedem Verbinden erneut zustellen würde.

| Payload | Wirkung |
| --- | --- |
| `start` | Neuer Login: PKCE-Verifier und `state` nur im Speicher, Login-URL auf `<base>/bridge/login`. Ersetzt einen ausstehenden Login. |
| alles andere | Code-Eingabe: Weiterleitungs-URL (`…://app/login?code=…&state=…`), nur der Code oder eine ganze Zeile aus der Browser-Konsole, die diese URL enthält (`Failed to launch '…'`). |

Eine Eingabe ohne Code für den ausstehenden Login (fremder `state`, kein `code`) beendet den Login nicht. Jede
Eingabe mit Code beendet ihn, ob erfolgreich oder nicht, weil der Code dann verbraucht sein kann. Ohne Code-Eingabe
läuft ein Login nach 10 Minuten ab.

### `<base>/bridge/login`

Retained, `{"state": "<Wert>"}`, bei `pending` zusätzlich `url` und `expires_at` (Format wie `updated_at`).

| Wert | Bedeutung |
| --- | --- |
| `idle` | Kein Login ausstehend (nach dem Start, nach Ablauf und nach einem Fehlschlag) |
| `pending` | Login gestartet; `url` im Browser öffnen, danach die Code-Eingabe senden |
| `done` | Code eingelöst und gespeichert; `bridge/status` wechselt kurz darauf nach `ready` |

Ein Fehlschlag folgt als `LOGIN_FAILED` auf `<base>/bridge/error`. Über MQTT gehen nur Login-URL und Code. Der
Code ist einmal verwendbar und ohne den Verifier, den nur die Bridge kennt, wertlos; Tokens verlassen die Bridge
nie. Wer auf das Befehls-Topic schreiben darf, kann allerdings einen Login anstoßen und die Bridge so an ein
anderes Konto binden; der Schalter bleibt deshalb aus, wenn der Broker nicht abgesichert ist.

## Gültige Topic-Segmente

`<deviceId>` und jede Ebene von `<base>` werden abgelehnt, wenn sie

- leer sind,
- `/`, `+`, `#` oder das Zeichen NUL (`\x00`) enthalten,
- mit `$` beginnen (Systemtopics des Brokers).

Zusätzlich ist `bridge` als `<deviceId>` verboten, weil es mit `<base>/bridge/...` kollidiert. Die Fehlermeldung
nennt die verletzte Regel, nicht den Wert.

## Verbindung

- `mqtt://host[:port]` (Default-Port 1883) oder `mqtts://host[:port]` (Default-Port 8883). Bei `mqtts://` prüft
  die Bridge das Zertifikat gegen die System-CAs inklusive Hostname.
- Zugangsdaten nur aus `MQTT_USERNAME` und `MQTT_PASSWORD`; Zugangsdaten in `MQTT_URL` werden abgelehnt. Das
  Passwort wird nie geloggt.
- Client-ID aus `MQTT_CLIENT_ID`, MQTT 3.1.1 mit Clean Session, Keepalive 60 s.
- Reconnect automatisch mit exponentiellem Backoff von 1 s bis 60 s.

## Puffer bei Verbindungsverlust

Nachrichten laufen durch eine begrenzte Queue, Größe `MQTT_QUEUE_SIZE` (Default 1000, erlaubt 10–100000).
An die MQTT-Bibliothek gehen höchstens 100 unbestätigte Nachrichten; erst danach rückt die Queue nach.
Ist die Queue voll, gilt beim Einreihen:

1. Zuerst wird die **älteste Zustandsnachricht** (`state`) verworfen.
2. Gibt es keine, wird das **älteste Fehler-Event** verworfen.
3. **Status und Verfügbarkeit werden nie verworfen.** Bleiben nur sie übrig, wächst die Queue über die Grenze.

Status und Verfügbarkeit sind retained; deshalb wird je Topic nur die neueste Nachricht gepuffert: Eine neue
ersetzt die ältere desselben Topics (sie rückt ans Ende) und zählt nicht als Verlust. Die Queue überschreitet
ihre Grenze so um höchstens eine Nachricht je Status- bzw. Verfügbarkeits-Topic.

Die neue Nachricht zählt dabei als neueste ihrer Art: Eine neue Zustandsnachricht wird selbst verworfen, wenn
die Queue keine ältere Zustandsnachricht mehr enthält. Jeder Verlust wird mit Art und Topic geloggt.

## Home-Assistant-Discovery (vorgesehen, nicht umgesetzt)

Discovery-Konfigurationen würden unter dem Discovery-Präfix von Home Assistant (`homeassistant/...`) liegen, also
außerhalb von `<base>`, und auf die obigen Topics verweisen (`state_topic` = `<base>/<deviceId>/state`,
`availability` = `<base>/<deviceId>/availability` und `<base>/bridge/status`). Umfang und Präfix entscheidet D2;
bis dahin veröffentlicht die Bridge keine Discovery-Nachrichten.
