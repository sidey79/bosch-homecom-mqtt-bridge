# bosch-homecom-mqtt-bridge

Bridge zwischen der Bosch-HomeCom-Easy-Cloud und MQTT, gebaut für Geräte aus der HomeCom-Easy-App, zuerst ein Bosch Tronic 7000 (Gerätetyp `wddw2`), und den Einsatz mit FHEM. Aufbau, Pipelines und Betrieb folgen dem Schwesterprojekt `whatsmeow-mqtt-bridge`.

**Status:** Das Repository ist vorbereitet, die Bridge ist noch nicht implementiert. Der Plan, einschließlich des Logins über den Container, steht in [`PLAN.md`](PLAN.md). Der Container gibt derzeit nur Hilfe und Version aus und beendet sich danach.

## Schnellstart im Devcontainer

Das Repository in VS Code öffnen und **Dev Containers: Reopen in Container** wählen. Die Definition liegt unter `.devcontainer/`. Alternativ:

```sh
cp .env.example .env
docker build -t bosch-homecom-mqtt-bridge .
docker run --rm bosch-homecom-mqtt-bridge --version
```

Bis zur Implementierung der Bridge beendet sich der Container sofort, deshalb startet `docker compose up` ihn nicht dauerhaft.

### Login (mit der Bridge geplant)

Die Erstanmeldung läuft über den Browser, weil die Bosch-SingleKey-ID ein CAPTCHA verlangt:

```sh
docker compose run --rm -it bridge login
```

Der Befehl zeigt eine Login-URL, nimmt den Code aus der Weiterleitung entgegen und legt den Refresh-Token im Volume `bosch-data` ab. Details und Sicherheitsüberlegungen stehen in [`PLAN.md`](PLAN.md#login-im-container). Das Volume muss erhalten bleiben.

Die Prototyp-Skripte aus der Machbarkeitsprüfung liegen unter [`scripts/prototype/`](scripts/prototype/).

### Container-Image

Das Multi-Arch-Image für `linux/amd64` und `linux/arm64` wird in der GitHub Container Registry veröffentlicht, sobald eine Version auf `main` erscheint:

```text
ghcr.io/sidey79/bosch-homecom-mqtt-bridge:0.1.0
```

Der Tag `0.1.0` entsteht erst, wenn sich `VERSION` nach dem ersten Commit auf `main` ändert; der erste Push veröffentlicht nur `sha-<commit>`.

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
