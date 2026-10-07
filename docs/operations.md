# Betrieb

## Erster Start

1. `cp .env.example .env` und `MQTT_URL`, bei Bedarf `MQTT_USERNAME`/`MQTT_PASSWORD` setzen.
2. Login im Terminal (nicht ohne `-it`): `docker compose run --rm -it bridge login`. Der Refresh-Token landet in
   `/data/auth.json` im Volume `bosch-data`.
3. `docker compose up -d`. Die Bridge startet mit `run` (Standardbefehl), fragt die Gateways ab und pollt alle
   `BOSCH_POLL_INTERVAL` Sekunden (gerechnet ab dem Ende eines Abrufs).

Das Volume `bosch-data` muss erhalten bleiben: Refresh-Tokens sind einmal verwendbar, ein verlorenes Volume
bedeutet einen neuen Login. Ein neuer Login ersetzt einen laufenden Dienst ohne Neustart (die Bridge übernimmt die
Datei innerhalb von etwa 10 s).

## Stoppen: 20 Sekunden Pflicht

Beim Stoppen kann ein Refresh-POST laufen (bis 15 s). Die Bridge wartet auf ihn und das Schreiben von `auth.json`,
trennt dann den Broker (bis zu 9 s). Docker beendet nach 10 s per Voreinstellung mit SIGKILL, das kann den neuen
Refresh-Token vernichten. Deshalb:

- Compose: `stop_grace_period: 20s` ist gesetzt.
- `docker run`: immer `--stop-timeout 20` angeben.

## Health

`HEALTH_PORT` (Standard 8080), kein Port-Mapping nötig.

| Pfad | Antwort |
| --- | --- |
| `/healthz` | immer 200, solange der Prozess antwortet |
| `/readyz` | 200 nur im Zustand `ready`; 503 in `starting`, `auth_required`, `error` und `disconnected` (Broker nicht verbunden) |

Der Compose-Healthcheck fragt `/readyz`. `restart: unless-stopped` startet nur bei Prozessende neu, nicht bei
`unhealthy`: Bei `auth_required` hilft kein Neustart, nur ein Login. Der Container bleibt dann `unhealthy`, und
`<base>/bridge/status` meldet `auth_required` (Fehler-Event `AUTH_REQUIRED`).

## Broker und Netzwerk

`MQTT_URL` muss vom Container aus erreichbar sein. Im Compose-Overlay `docker-compose.mqtt.yml` ist es `mqtt://mqtt:1883`
(mitgelieferter Mosquitto, anonym, nur für Tests), mit `docker-compose.host.yml` läuft die Bridge im Host-Netzwerk.
Für einen vorhandenen Broker (`mqtts://` prüft das Zertifikat gegen die System-CAs) `MQTT_URL`, `MQTT_USERNAME` und
`MQTT_PASSWORD` in `.env` eintragen.

**FHEM als Broker im eigenen Docker-Netzwerk:** Eine lokale `docker-compose.override.yml` (von Git ignoriert, wird von
`docker compose` automatisch geladen) hängt die Bridge an das bestehende Netzwerk und setzt `MQTT_URL`:

```yaml
services:
  bridge:
    environment:
      MQTT_URL: ${FHEM_MQTT_URL:-mqtt://fhem:1883}
    networks:
      - smarthome
networks:
  smarthome:
    external: true
    name: ${SMARTHOME_NETWORK:-smarthome}
```
In `.env` stehen `SMARTHOME_NETWORK` (Name des Netzwerks, `docker network ls`), `FHEM_MQTT_URL` und die
Zugangsdaten. Nicht zusammen mit `docker-compose.mqtt.yml` verwenden.

## FHEM (MQTT2_SERVER mit MQTT2_DEVICE)

Topics und Payloads stehen in [mqtt-contract.md](mqtt-contract.md). Die Bridge veröffentlicht zwei Arten von Topics:
ihren eigenen Zustand unter `<base>/bridge/…` und je Gerät `<base>/<deviceId>/…`. In FHEM sollen beide in getrennten
Geräten landen: ein von Hand angelegtes **Bridge-Gerät** und je Gerät ein per Autocreate angelegtes
`MQTT2_DEVICE`. Das Muster entspricht der zigbee2mqtt-Integration (`bridgeRegexp`); `<base>` ist hier
`bosch-homecom`.

**Ungetestet:** Die Konfiguration unten folgt der zigbee2mqtt-Integration. Geprüft mit echtem FHEM ist nur das
Autocreate ohne `bridgeRegexp` (Readings `availability`, `state_<Schlüssel>`, z. B. `state_dhw1_outlet_temperature`,
`state_hs_starts`, `state_updated_at`; Werte mit `null` im JSON erscheinen nicht als Reading). Ohne die folgenden
Schritte landen die Readings der Bridge (`status_state`, `status_connected`) im Gerät des Geräts.

**1. Bridge-Gerät von Hand anlegen** (vor dem Autocreate der Geräte):

```
defmod bosch_bridge MQTT2_DEVICE
attr bosch_bridge IODev MQTT2_FHEM_Server
attr bosch_bridge readingList bosch-homecom/bridge/status:.* { json2nameValue($EVENT, 'status_') } \
  bosch-homecom/bridge/error:.* { json2nameValue($EVENT, 'error_') }
```
Das Gerät zeigt `status_state` (`ready`, `starting`, `auth_required`, `error`, `disconnected`), `status_connected`,
`status_message` und beim letzten Fehler `error_code`/`error_message`.

**2. Geräte aus dem Autocreate ausnehmen, die nicht `bridge` sind:** am IO-Gerät (`MQTT2_SERVER` bzw. `MQTT2_CLIENT`)

```
attr MQTT2_FHEM_Server bridgeRegexp bosch-homecom/((?!bridge/)[A-Za-z0-9._-]+)/.*:.* "bosch_$1"
```
Nachrichten von `bosch-homecom/<deviceId>/…` gehören damit zum Gerät `bosch_<deviceId>` (z. B. `bosch_101468551`),
Nachrichten unter `bosch-homecom/bridge/…` nicht. Die ID `bridge` ist im Vertrag reserviert, deshalb reicht der
Ausschluss genau dieser Ebene (`(?!bridge/)`).

**3. Aufräumen nach einem Umstieg:** Ein früher per Autocreate angelegtes Gerät (z. B. `bosch-homecom_101468551`)
löschen, damit es nicht neben dem neuen Gerät weiter Readings hält. Retained Nachrichten unter `<base>/event/…` aus
Versionen vor 0.7.0 einmal im Broker löschen: `mosquitto_pub -r -n -t bosch-homecom/event/status` (und `…/event/error`).

Wer die Geräte lieber selbst anlegt, mit `<deviceId>` = `101506113` (nicht getestet):

```
defmod bosch_tronic MQTT2_DEVICE
attr bosch_tronic IODev MQTT2_FHEM_Server
attr bosch_tronic readingList bosch-homecom/101506113/state:.* { json2nameValue($EVENT, 'state_') } \
  bosch-homecom/101506113/availability:.* availability
attr bosch_tronic devStateIcon online:10px-kreis-gruen offline:10px-kreis-rot
```

Die Bridge liest nur; es gibt keine Befehle.

## Restrisiken (ADR 0001)

- **Hängender Verbindungsaufbau (Accepted risk A):** Bleibt der Aufbau zum Token-Endpunkt hängen, endet der Versuch
  nach dem Gesamt-Timeout der Bibliothek (15 s) als unklarer Fehler und führt zu `auth_required`, obwohl nichts
  gesendet wurde. Ein Login behebt das.
- **Absturz während des POST:** Es gibt keinen Marker vor dem POST. Stirbt der Prozess (SIGKILL, Stromausfall, OOM)
  zwischen Antwort und Schreiben, ist der alte Refresh-Token verbraucht und der neue verloren; es folgt
  `auth_required` und ein Login. Der geordnete Stopp ist durch `stop_grace_period` abgedeckt.
- **Backup-Restore mit Live-Secrets:** `auth.json` enthält Refresh- und Access-Token. Backups gehören geschützt. Ein
  zurückgespieltes Backup wird bewusst übernommen; sein Refresh-Token ist meist schon verbraucht, dann ist ein neuer
  Login nötig.
- **Healthcheck:** `unhealthy` bei `auth_required` löst keinen Neustart aus (siehe oben).
- **Lange Abrufe:** Der Poll-Timeout `BOSCH_POLL_TIMEOUT` (Standard 300 s) liegt weit über der gemessenen Abrufdauer
  (rund 65 s für Discovery und ersten Poll eines `wddw2`). Die Bridge rechnet keine Werte um; die Einheiten stehen im
  Vertrag.
