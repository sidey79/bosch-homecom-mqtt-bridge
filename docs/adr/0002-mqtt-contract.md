# ADR 0002: MQTT-Vertrag

- **Status:** Accepted (für die Punkte unter „Entscheidung“, D10). Die Punkte unter
  „Implementierungsableitungen“ sind nicht Teil von D10 und vom Nutzer zu bestätigen.
- **Entscheidung:** D10, getroffen von der Nutzerin bzw. dem Nutzer (Repository-Inhaber) im Rahmen der Umsetzung
  von PR 5 (`feat/mqtt-publisher`).
- **Vertrag:** [`docs/mqtt-contract.md`](../mqtt-contract.md)

## Kontext

PLAN.md enthielt einen Entwurf des MQTT-Vertrags (Topics `event/status`, `event/error`, `<deviceId>/state`,
optional `cmd`; die Topics `event/…` heißen seit 0.7.0 `bridge/…`, siehe unten). Offen waren Retain, QoS, Last Will, Verfügbarkeit, Schreibbefehle, Pufferung bei
Verbindungsverlust und die Prüfung von Topic-Segmenten. Der Vertrag ist ein Merge-Gate für PR 5, weil Konsumenten
(FHEM, später Home Assistant) darauf aufbauen.

## Entscheidung

- QoS 1 für alle Publishes. Basis-Topic `MQTT_BASE_TOPIC`, Default `bosch-homecom`.
- Nur lesen: keine `cmd`-Topics. Home-Assistant-Discovery ist im Vertrag vorgesehen, wird aber erst nach D2
  umgesetzt.
- `<base>/bridge/status` retained, `{"state": …, "connected": bool, "message": optional}`. Last Will auf demselben
  Topic mit `state` `disconnected` und `connected` `false`.
- `<base>/bridge/error` nicht retained, `{"code": "AUTH_REQUIRED", "message": "…"}`.
- `<base>/<deviceId>/state` retained, flaches JSON der gelesenen Werte in SI-Einheiten ohne Einheitentext plus
  `updated_at` (ISO-8601, UTC).
- `<base>/<deviceId>/availability` retained, `online` oder `offline`.
- Device-IDs und Topic-Segmente mit `/`, `+`, `#`, führendem `$` oder NUL werden abgelehnt.
- Begrenzte Queue. Bei Überlauf werden zuerst die ältesten Zustandsnachrichten verworfen; Status wird nie
  verworfen.
- Reconnect mit Backoff. TLS bei `mqtts://` gegen die System-CAs. Zugangsdaten nur aus `MQTT_USERNAME` und
  `MQTT_PASSWORD`; das Passwort wird nie geloggt.
- Bibliothek paho-mqtt 2.1.0 (gepinnt) mit `CallbackAPIVersion.VERSION2`. Der Publisher blockiert die
  asyncio-Loop nicht.

## Implementierungsableitungen (vom Nutzer zu bestätigen)

Die folgenden Festlegungen hat die Umsetzung aus D10 abgeleitet; D10 hat sie nicht entschieden. Sie gelten, bis
die Nutzerin bzw. der Nutzer sie bestätigt oder ändert, und stehen so auch in `docs/mqtt-contract.md`.

- **Statuswerte `starting` und `error`** zusätzlich zu `ready`, `auth_required` und `disconnected`.
- **`bridge` als reservierte Geräte-ID**, weil `<base>/bridge/...` sonst mit `<base>/<deviceId>/...` kollidiert.
- **Fehler-Events als zweite Verwurfsstufe:** Bei vollem Puffer werden sie erst verworfen, wenn keine
  Zustandsnachricht mehr übrig ist.
- **Verfügbarkeit wird nie verworfen;** Status und Verfügbarkeit werden im Puffer **je Topic zusammengefasst**
  (eine neue Nachricht ersetzt die ältere desselben Topics). Der Puffer überschreitet seine Grenze dadurch um
  höchstens eine Nachricht je Status- bzw. Verfügbarkeits-Topic.
- **`MQTT_QUEUE_SIZE`** als Name der Puffergröße, Default 1000, erlaubt 10–100000.
- **paho-Fenster 100:** höchstens 100 unbestätigte QoS-1-Nachrichten liegen bei paho, erst danach rückt der
  Puffer nach.
- **Nicht endliche Zahlen** (NaN, ±Inf) im Zustand werden als `null` veröffentlicht, statt den ganzen Zustand
  abzulehnen.
- **Geordneter Stopp:** Die Bridge wartet bis zu 5 s auf das PUBACK des Status `disconnected` und trennt erst
  dann mit DISCONNECT. Kommt es nicht, schließt sie die Verbindung ohne DISCONNECT, damit der Broker den Last
  Will veröffentlicht.

## Änderung (0.7.0): Namensraum `bridge`

Die Topics der Bridge heißen `<base>/bridge/status` und `<base>/bridge/error` (vorher `event/…`, übernommen aus dem
Entwurf in PLAN.md). Der Name beschreibt, was dort liegt: Zustand und Fehler der Bridge selbst. Das ist ein Bruch der
Schnittstelle (Entscheidung des Repository-Inhabers). Retained Nachrichten unter `<base>/event/…` bleiben im Broker
stehen und müssen einmal gelöscht werden (`mosquitto_pub -r -n -t <base>/event/status`).

## Konsequenzen

- Retained Status mit Last Will gibt Konsumenten jederzeit einen gültigen Zustand der Bridge, auch nach deren
  Absturz.
- Zustandsnachrichten sind retained und tragen `updated_at`; ein Konsument erkennt veraltete Werte selbst.
  Verworfene Zwischenstände während eines Ausfalls kosten deshalb nur Historie, keinen aktuellen Zustand.
- Schreibzugriff (D3) und Discovery (D2) erfordern eine Änderung dieses ADR bzw. ein Folge-ADR.
- Zusätzliche Konfiguration: `MQTT_QUEUE_SIZE` (10–100000). Zugangsdaten in `MQTT_URL` lehnt die Konfiguration ab.

## Verworfene Alternativen

- **QoS 0 für Zustand:** weniger Overhead, aber verlorene Nachrichten bei kurzen Unterbrechungen; QoS 1 ist bei
  einer Nachricht pro Minute ohne Kosten.
- **Unbegrenzte Queue:** einfacher, aber unbegrenzter Speicher bei langem Broker-Ausfall.
- **Zustand nicht retained:** Konsumenten müssten bis zum nächsten Abruf warten.
