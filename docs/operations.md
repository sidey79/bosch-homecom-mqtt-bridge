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

## FHEM (MQTT2_SERVER mit MQTT2_DEVICE, gegen ein echtes FHEM geprüft)

Topics und Payloads stehen in [mqtt-contract.md](mqtt-contract.md). Läuft FHEM selbst als Broker (`MQTT2_SERVER`),
legt Autocreate das Gerät `bosch-homecom_<deviceId>` als `MQTT2_DEVICE` an. Geprüft mit einem Bosch Tronic 7000
(`wddw2`): Die Readings heißen `availability`, `state_<Schlüssel>` (z. B. `state_dhw1_outlet_temperature`,
`state_hs_starts`, `state_updated_at`) und `status_state`/`status_connected` (Status der Bridge). Werte mit `null` im
JSON (z. B. `dhw1_air_box_temperature` beim Tronic 7000) erscheinen nicht als Reading.

Wer lieber eigene Reading-Namen und ein Gerät pro `<deviceId>` möchte, setzt das Gerät von Hand an, hier mit
`<deviceId>` = `101506113` und `<base>` = `bosch-homecom` (nicht getestet):

```
defmod bosch_tronic MQTT2_DEVICE
attr bosch_tronic readingList bosch-homecom/101506113/state:.* { json2nameValue($EVENT) } \
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
