# bosch-homecom-mqtt-bridge

Bridge zwischen der Bosch-HomeCom-Easy-Cloud und MQTT, gebaut für Geräte aus der HomeCom-Easy-App, zuerst ein Bosch Tronic 7000 (Gerätetyp `wddw2`), und den Einsatz mit FHEM. Aufbau, Pipelines und Betrieb folgen dem Schwesterprojekt `whatsmeow-mqtt-bridge`.

**Status:** Die Bridge liest den Bosch Tronic 7000 (`wddw2`) nur lesend aus der Cloud und veröffentlicht die Werte per MQTT ([Vertrag](docs/mqtt-contract.md)). Gegen die echte Cloud und einen echten Broker ist sie noch nicht über längere Zeit gelaufen. Plan und Hintergründe stehen in [`PLAN.md`](PLAN.md), der Betrieb in [`docs/operations.md`](docs/operations.md).

## Schnellstart im Devcontainer

Das Repository in VS Code öffnen und **Dev Containers: Reopen in Container** wählen. Die Definition liegt unter `.devcontainer/`. Alternativ:

```sh
cp .env.example .env
docker build -t bosch-homecom-mqtt-bridge .
docker run --rm bosch-homecom-mqtt-bridge --version
```

`docker compose up` startet die Bridge (Befehl `run`, der Standard). Vorher ist einmal der Login nötig (nächster Abschnitt).

### Login

Die Erstanmeldung läuft über den Browser, weil die Bosch-SingleKey-ID ein CAPTCHA verlangt:

```sh
docker compose run --rm -it bridge login
```

Der Befehl zeigt eine Login-URL, nimmt den Code aus der Weiterleitung entgegen und legt den Refresh-Token im Volume `bosch-data` ab. Details und Sicherheitsüberlegungen stehen in [`PLAN.md`](PLAN.md#login-im-container). Das Volume muss erhalten bleiben.

Die Prototyp-Skripte aus der Machbarkeitsprüfung liegen unter [`scripts/prototype/`](scripts/prototype/).

### Container-Image

Das Multi-Arch-Image für `linux/amd64` und `linux/arm64` wird in der GitHub Container Registry veröffentlicht, sobald eine Version auf `main` erscheint:

```text
ghcr.io/sidey79/bosch-homecom-mqtt-bridge:0.6.1
```

Der Tag `0.6.1` entsteht, wenn sich `VERSION` auf `main` ändert; Builds ohne Änderung von `VERSION` veröffentlichen nur `sha-<commit>`.

Für reproduzierbare Deployments sollte die vollständige Version verwendet werden. Jeder Build von `main` erhält zusätzlich einen unveränderlichen `sha-<commit>`-Tag. Der Ablauf steht in [`RELEASE_POLICY.md`](RELEASE_POLICY.md).

### Compose-Varianten

Die Basisdatei startet ausschließlich die Bridge. Weitere Dateien ergänzen sie:

- `docker-compose.host.yml`: Host-Netzwerk.
- `docker-compose.mqtt.yml`: lokaler Mosquitto zum Testen ohne FHEM.
- `docker-compose.override.yml` (lokal, von Git ignoriert): Anbindung an das FHEM-Docker-Netzwerk, wie im Schwesterprojekt.

```sh
docker compose -f docker-compose.yml -f docker-compose.mqtt.yml up -d --build
```

## Konfiguration

Alle Konfiguration kommt aus der Umgebung, siehe [`.env.example`](.env.example). Die Variablen werden mit der Bridge verbindlich.

## Entwicklung

```sh
make test      # Unit-Tests
make verify    # compileall, Tests, Compose-Validierung
make image     # Docker-Image bauen
```

Die Pipeline in `.github/workflows/docker-image.yml` testet bei Pull Requests, baut das Image für beide Architekturen und veröffentlicht es nur auf `main`.
