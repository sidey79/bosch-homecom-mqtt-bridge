# ADR 0001 – Token-Persistenz und Refresh-Orchestrierung

- **Status:** Accepted
- **Datum:** 2026-10-04
- **Entscheidungen:** D7 und D11, getroffen von der Nutzerin bzw. dem Nutzer am 2026-10-04
- **Umsetzung:** Tabelle K in `auth/refresh_errors.py` (`feat/refresh-errors`); Persistenz und Orchestrierung in
  `auth/store.py` und `auth/token_manager.py` (`feat/token-manager`)

## Kontext

Der Refresh-Token von SingleKey ID ist einmal verwendbar. Er rotiert bei jedem Refresh. Geht die Antwort
eines Refresh verloren, nachdem der Server rotiert hat, ist die Sitzung weg und nur ein neuer `login` hilft.
`homecom_alt` 1.8.2 refresht intern, sobald die Restlaufzeit unter 5 min fällt, und das in jeder Anfrage eines
Polls. Die Zahl der Anfragen je Poll ist nicht begrenzt.

## Entscheidung

### D7: Access-Token und `exp` werden persistiert

`auth.json` enthält zusätzlich `access_token` und `exp`. Beide sind optional, damit Dateien ohne diese Felder
lesbar bleiben. Sie werden validiert: Der Access-Token ist nie leer, `exp` ist eine positive ganze Zahl, und
`exp` gibt es nur zusammen mit einem Access-Token. Es gelten dieselben Regeln wie für den Refresh-Token: Die
Datei hat die Rechte 600 und wird atomar geschrieben. Ein Neustart nutzt einen gültigen Access-Token aus der
Datei ohne Refresh. `login` schreibt beide Felder, setzt `last_refresh_at` aber nicht; dieses Feld setzt nur der
Dienst-Refresh.

### Orchestrierung R-b

- Datenabrufe und Discovery laufen über Instanzen mit `auth_provider=False`. Dort ist `get_token()` ein No-op.
  Den Refresh macht nur eine Refresh-Instanz mit `auth_provider=True`. Alle Instanzen teilen sich dieselben
  `ConnectionOptions`. Das Feld `ConnectionOptions.auth_provider` wird in 1.8.2 nicht ausgewertet; die Fabrik
  setzt es trotzdem ausdrücklich auf `False`. Ein Test prüft, dass die Refresh-Instanz dennoch refresht.
- `ensure_fresh()` läuft vor jeder Discovery und jedem Poll und liest die Datei bei jedem Aufruf unter dem
  Lock (`read_locked()`). Eine höhere Generation wird übernommen. Ein fehlender oder unlesbarer Access-Token
  gilt als abgelaufen.
- **Marge** = `min(BOSCH_POLL_TIMEOUT + 60 s, Laufzeit / 2)`, mit Laufzeit = `exp − iat`, ersatzweise
  `exp − Zeitpunkt des Erhalts`. Ist `BOSCH_POLL_TIMEOUT + 60 s` größer als die halbe Laufzeit, wird einmal
  gewarnt. `BOSCH_POLL_TIMEOUT` liegt zwischen 60 und 900 s, Default 300 s. Der Timeout umschließt nur
  Discovery und Poll, nie einen Refresh. Polls laufen nacheinander, das Intervall zählt ab dem Ende des
  vorherigen Polls; deshalb ist der Timeout nicht an das Intervall gekoppelt.
- `refresh_locked()` ist eine einzige Task je Prozess. Aufrufer warten per `asyncio.shield` auf sie. Unter dem
  Lock liest die Task zuerst die Datei. Hat ein Login eine höhere Generation mit frischem Access-Token
  geschrieben, geht kein POST raus. Andernfalls wird nur mit dem übernommenen Refresh-Token refresht, nie mit
  dem alten. Nach einem Erfolg wird sofort geschrieben (Generation + 1, `last_refresh_at`).
- **Phasen der Task:** `acquiring` und `backoff` sind abbrechbar, `sending` und `writing` werden abgewartet.
  Zwischen dem erfolgreichen `LOCK_NB` und `sending` liegt kein `await`, der Lock wird im `finally` freigegeben.
  Nach `aclose()` beginnt kein neuer Versuch. `auth_required` und `disconnected` setzt die Task selbst.
- **Gemeinsamer Lock im Prozess:** Läuft die Refresh-Task, liest `ensure_fresh()` nicht selbst, sondern wartet
  auf die Task. So sperrt sich der Prozess nicht selbst aus.
- **Schreibfehler nach erfolgreichem Refresh:** Der Dienst läuft mit den Tokens im Speicher weiter und loggt
  einen ERROR. Vor jedem Poll versucht er das Schreiben erneut. Solange nicht geschrieben ist, startet nur dann
  ein weiterer Refresh, wenn der Access-Token fällig wird oder ein 401 kommt; dafür nimmt er den neuesten
  Refresh-Token aus dem Speicher. Schreibt ein Login zwischendurch eine höhere Generation, gewinnt der Login.
- **401 bei einem Datenabruf:** Darauf folgen genau ein Refresh und genau eine Wiederholung des Polls. Ein
  zweiter 401 führt zu `auth_required`, wenn der verwendete Token laut `exp` noch gültig war. Sonst läuft
  `ensure_fresh()` einmal erneut.
- **`auth_required`:** Der Dienst stellt keine Cloud-Anfragen. Alle 10 s liest er die Datei unter dem Lock und
  übernimmt eine höhere Generation. Beim Eintritt geht das Fehler-Event `AUTH_REQUIRED` raus.
- **Crash-Loop-Schutz:** Liegt `last_refresh_at` weniger als 60 s zurück, wartet der erste Refresh nach dem
  Start, bis 60 s vergangen sind. Ein gültiger Access-Token aus der Datei macht den Refresh überflüssig.
- **NFS/CIFS:** Liegt das Verzeichnis von `auth.json` laut `/proc/mounts` auf NFS oder CIFS, wird gewarnt.

### D11: Option (b) – Retry nur bei K3 und K4

Fehler werden nur direkt um `get_token(force=True)` der Refresh-Instanz gefangen, und zwar `Exception`, nie
`BaseException`. Ein Abbruch ist also keine Fehlerklasse. Die Klassifikation folgt Tabelle K.

| K | Auslöser | Erscheint als | Rotationsstatus | Folge bei D11 (b) |
| --- | --- | --- | --- | --- |
| K0 | `get_token(force=True)` endet ohne Ausnahme, liefert aber nicht `True` oder lässt den Refresh-Token unverändert | – | unklar oder verarbeitet | `auth_required`, Typ wird geloggt |
| K1 | HTTP 400; leeres JSON ist davon nicht unterscheidbar | `AuthFailedError` ohne `__cause__` | endgültig ungültig | `auth_required` |
| K2 | HTTP 401 am Token-Endpunkt | `AuthFailedError`, `__cause__` 401 | endgültig abgelehnt | `auth_required` |
| K3 | HTTP 429 | `NotRespondingError`, `__cause__` 429 | nicht verarbeitet (Annahme, Q-K3) | Retry nach `Retry-After`, mindestens 60 s, höchstens 1 h; ≤ 3 POSTs/h, danach `disconnected`, bis das Fenster frei ist |
| K4 | Verbindungsaufbau gescheitert oder beim Aufbau abgelaufen (Nutzerentscheidung 2026-10-06); ebenso Lock-Timeout und Dateisystemfehler beim Lesen vor dem POST | `NotRespondingError`, `__cause__` `ClientConnectorError` oder `ConnectionTimeoutError`; beide auch roh | nachweislich nicht gesendet | Backoff 30 s → max. 15 min, unbegrenzt, `disconnected` |
| K5 | Timeout nach dem Aufbau oder Gesamt-Timeout: `SocketTimeoutError`, sonstiger `ServerTimeoutError`, roher `TimeoutError` beim Lesen der Antwort | `NotRespondingError` mit `TimeoutError` oder roh | unklar | `auth_required` |
| K6 | Abbruch nach dem Senden | roh: `ServerDisconnectedError`, `ClientOSError`, `ClientPayloadError` | unklar oder verarbeitet | `auth_required` |
| K7 | HTTP 500 und sonstige Statuscodes | `ApiError` | unklar | `auth_required` |
| K8 | HTTP 403/404/502/504 | `{}` → `AttributeError` | unklar | `auth_required` |
| K9 | 200 mit unbrauchbarem Body | `InvalidSensorDataError`, roh `ContentTypeError`, `KeyError` | verarbeitet, wahrscheinlich rotiert und verloren | `auth_required` |
| K10 | jede andere `Exception` | – | unklar oder verarbeitet | `auth_required`, Typ wird geloggt |

Fehlt `auth.json` oder ist sie ungültig, folgt `auth_required` ohne POST.

**Timeouts (aiohttp 3.14, `homecom_alt` 1.8.2, `base.py:305–311`):** Die Bibliothek fängt `TimeoutError` und
`ClientConnectorError` und wirft `NotRespondingError` mit der Ausnahme als `__cause__`. aiohttp wirft
`ConnectionTimeoutError` nur, solange der Connector die Verbindung aufbaut (`_connect_and_send_request`, vor
`req.send`); das ist K4. `ConnectionTimeoutError` ist selbst ein `TimeoutError` und wird deshalb vor K5 geprüft.
`SocketTimeoutError` (Lesen) bleibt K5. Der Gesamt-Timeout bricht die Anfrage dort ab, wo sie gerade steht, und
erscheint als einfacher `TimeoutError`, auch wenn er im Aufbau ablief; er bleibt K5. Weil 1.8.2 nur
`ClientTimeout(total=15)` setzt, endet ein hängender Aufbau mit dieser Bibliothek als K5. K4 per Timeout entsteht
erst, wenn ein `connect`- oder `sock_connect`-Timeout gesetzt ist. Tests mit echter `ClientSession` und hängendem
Connector belegen beides.

Scheitert ein Versuch, werden die Tokens im Speicher auf den Stand davor zurückgesetzt. Das betrifft auch einen
teilweise übernommenen Body, etwa bei K9 `KeyError`. Jeder neue Versuch nimmt den Lock neu, Wartezeiten liegen
außerhalb des Locks. Implementiert ist nur Option (b); es gibt weder eine Varianten-Konstante noch einen
Schalter.

## Abweichungen von PLAN.md

- **W7 (Q-W7):** Fehler, auch `AttributeError` und rohe aiohttp-Ausnahmen, werden nicht pauschal als
  Netzwerkfehler behandelt (PLAN.md:161). Sie werden nur direkt um `get_token(force=True)` gefangen und nach
  Tabelle K klassifiziert; im Login gilt das für `validate_auth`.
- **W9:** PLAN.md:143–147 nimmt an, ein Refresh vor dem Abruf genüge, weil interne `get_token()`-Aufrufe
  No-ops seien. Das trifft nicht zu. Ersetzt durch die Instanzen mit `auth_provider=False` (R-b).
- **W8 entfällt:** Option (b) wiederholt nur, wenn der Refresh nicht verarbeitet wurde (K3 als Annahme, K4
  nachweislich). Die Regel „Refresh nie blind wiederholen“ (PLAN.md:169) bleibt daher bestehen.

## Deployment-Annahme T2

Der Dienst muss beim Stoppen genug Zeit bekommen, damit ein laufender POST (bis 15 s) samt Schreiben fertig
wird. Angenommen sind `stop_grace_period: 20s` in Compose bzw. `docker run --stop-timeout 20`. Das reicht, weil
`aclose()` das Warten auf den Lock abbricht; vor dem POST ist das sicher. Signal-Handler und Compose-Eintrag
folgen in PR 6.

## Alternativen

- **R-a** (Zeitmarge, eine Instanz mit `auth_provider=True`): Die Sicherheit hinge an der unbegrenzten
  Poll-Dauer.
- **R-c** (R-a plus Timeout um den Poll): Ein Abbruch kann einen internen Refresh-POST treffen.
- **R-d** (Unterklasse überschreibt `get_token`): Der Refresh bliebe mitten im Poll und damit im Bereich des
  Timeouts; außerdem koppelt das eng an Interna der Bibliothek.
- **`asyncio.to_thread(flock)`:** Bricht ein Timeout das Warten ab, droht ein Deadlock. Stattdessen gibt es nur
  `LOCK_NB`-Polling.
- **D11 (a)** (ein Retry auch bei unklaren Klassen) und **D11 (c)** (Retry nur bei K4): nicht gewählt.

## Konsequenzen und offene Punkte

- Die Sicherheit stützt sich auf `auth_provider` in `homecom_alt` 1.8.2. Zwei Schutztests sichern das bei jedem
  Update ab: Die Abruf-Instanz stellt keine Token-Anfragen, die Refresh-Instanz refresht.
- **Die Laufzeit des Access-Tokens ist nicht gemessen.** Der Login in PR 3 hat sie nicht protokolliert. Die
  Tests nehmen 3600 s an und kennzeichnen den Wert als Annahme. PR 6 misst sie im 24-h-Lauf und trägt sie hier
  nach, zusammen mit Poll-Dauer, Rotation (ja/nein) und 429-Zähler.
- Q-K3 („429 = nicht verarbeitet“) und Q-ROT (Widerruf der Token-Familie) bleiben offen.
