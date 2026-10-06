## Zusammenfassung

<!-- Was ändert sich und warum? Bezug auf Plan, Issue oder ADR. -->

## Entscheidungen und Abweichungen

<!-- Nutzerentscheidungen (ADR unter docs/adr/) und Abweichungen von PLAN.md; „keine“, wenn nicht zutreffend. -->

## Verifikation

Nicht Ausgeführtes bleibt offen und wird begründet, nicht abgehakt.

- [ ] `make verify` grün
- [ ] `make image`, danach:
  - [ ] `docker run --rm bosch-homecom-mqtt-bridge --version`
  - [ ] `docker run --rm --entrypoint python3 bosch-homecom-mqtt-bridge -c "import aiohttp, homecom_alt, paho.mqtt"`
- [ ] CI grün: `prepare`, `build` (amd64 und arm64, inkl. Smoke-Tests), `runner / dclint`
- [ ] Keine Platzhalter: `git diff --diff-filter=d --name-only origin/main | xargs -r grep -nE "TODO|FIXME|skip\(|NotImplementedError"` ist leer oder begründet
- [ ] Geheimnis-Scan (Befehl in `docs/development.md`) ist leer; `.tokens.json` und `auth.json` sind nicht im Diff
- [ ] Gate-PR: Das ADR ist `Accepted` und nennt die Entscheidung der Nutzerin oder des Nutzers (sonst „keine Gate-PR“)
- [ ] Review separat, nicht im Kontext des Autors

### PR-spezifische Verifikation

<!-- Akzeptanzkriterien dieser PR mit Ergebnis; manuelle Schritte mit Protokoll oder als „nicht ausgeführt“. -->
