# Entwicklung

## Lokale Prüfung

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
make test      # Unit-Tests
make verify    # compileall, Tests, Compose-Validierung inklusive beider Overlays
make image     # Docker-Image bauen
```

`make verify` prüft `docker-compose.yml` allein und zusammen mit `docker-compose.host.yml` bzw.
`docker-compose.mqtt.yml`. Ein kaputtes Overlay lässt das Target scheitern.

## Login lokal testen

`login` liest den Code ohne Echo über `getpass` und braucht deshalb ein Terminal. Im Container immer mit
`-it` starten (`docker compose run --rm -it bridge login`). Ohne TTY liest `getpass` mit Warnung von stdin (die
Eingabe ist dann womöglich sichtbar) oder bekommt sofort EOF, dann endet der Login mit „Login abgebrochen.“

## Abhängigkeiten

Alle Laufzeitabhängigkeiten stehen flach und exakt gepinnt in `requirements.txt`, ohne Hashes, auch die
transitiven. Die Pins gelten für Python 3.13, die Version des Images: Eine frische venv ergibt mit
`pip freeze` genau diese Liste. Renovate pflegt sie über den Manager `pip_requirements`. Feature-PRs fügen keine
Abhängigkeit ohne Vermerk in der PR-Beschreibung hinzu. Das Verfahren ist ein änderbarer Default; die Alternative
`pip-compile` mit Hashes ist offen.

## Pull Requests

- Die PR-Vorlage (`.github/PULL_REQUEST_TEMPLATE.md`) enthält die Checkliste; nicht Ausgeführtes bleibt offen.
- Merge per Squash nach Rebase auf `main`. Der PR-Titel ist ein Conventional Commit.
- `VERSION` ändert sich nur in einer Release-PR (siehe `RELEASE_POLICY.md`).

### Required Checks

Branch-Schutz für `main` mit „up to date“ und „linear history“. Erforderlich sind:

| Workflow | Job |
| --- | --- |
| `docker-image.yml` | `prepare` |
| `docker-image.yml` | `build` (beide Matrix-Einträge, `linux/amd64` und `linux/arm64`) |
| `linter.yml` | `runner / dclint` |

Die Namen werden nach dem ersten PR-Lauf mit den in GitHub angezeigten Job-Namen abgeglichen. Im PR-Pfad lädt
`build` das Image lokal (`bosch-homecom-mqtt-bridge:ci`) und führt je Architektur zwei Smoke-Tests aus: `--version`
und den Import von `aiohttp`, `homecom_alt` und `paho.mqtt`.

## Test-Fixtures und Geheimnisse

- Token-Literale in Tests beginnen immer mit `FAKE-`.
- JWTs (für `exp`) werden zur Laufzeit erzeugt (`jwt.encode` mit Testschlüssel), nie als Literal eingecheckt.
- `.tokens.json` und `auth.json` tauchen nie im Diff auf.
- Tests sprechen nie mit der echten Cloud; sie injizieren eine Fake-Session über den Konstruktor von `homecom_alt`.

Vor jedem PR:

```sh
# keine Platzhalter
git diff --diff-filter=d --name-only origin/main | xargs -r grep -nE "TODO|FIXME|skip\(|NotImplementedError"

# Geheimnis-Scan (JSON, Keyword-Argumente, Zuweisungen; FAKE-Fixtures erlaubt)
git diff origin/main | grep -nE "^\+" \
  | grep -niE "(refresh|access)_token[\"']?\s*[:=]\s*[\"'][^\"']{16,}[\"']" \
  | grep -v "[\"']FAKE-"
```

Beide Befehle müssen leer bleiben oder der Treffer wird in der PR begründet.

Grenzen des Geheimnis-Scans: Er sucht nur nach `refresh_token`/`access_token` mit einem Literal von mindestens
16 Zeichen in hinzugefügten Zeilen. Nicht erfasst werden u. a. andere Schlüsselnamen (`password`, `client_secret`,
camelCase wie `refreshToken`), kürzere Werte, über Zeilen verteilte oder zusammengesetzte Literale, JWTs ohne
Schlüssel, Binärdateien und Geheimnisse in bereits gelöschten Zeilen der Historie. Er ersetzt kein Review und kein
dediziertes Werkzeug (z. B. gitleaks).
