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

`exp` und `iat` werden aus dem JWT nur übernommen, wenn sie plausibel sind: endliche Zahlen, kein `bool`,
`exp > 0`, `iat < exp` (sonst wird `iat` verworfen). Ein `exp` mehr als 30 Tage nach der aktuellen Zeit gilt als
unlesbar (zweites Review PR 4), damit ein manipulierter Token nicht monatelang frisch aussieht. Ein Helper
(`auth/claims.py`) gilt für Login und Dienst; ein `exp` zwischen 0 und 1 wird nicht gespeichert. Das gespeicherte `exp` dient als Rückfallwert, wenn genau dieser
Access-Token aus der Datei kein lesbares JWT ist (Review PR 4). Beide Schreiber leiten `exp` heute aus dem JWT ab;
der Rückfall greift also nur bei Dateien anderer Herkunft oder wenn PyJWT einen Token künftig nicht mehr liest.
Streichen hätte die Nutzerentscheidung D7 geändert.

**Datei-Identität und Buchführung (Review PR 4):** `login` schreibt zusätzlich eine zufällige `login_id`
(Refreshes übernehmen sie). Weitere optionale Felder gehören dem Dienst: `refresh_blocked` (Klasse aus Tabelle K)
mit `blocked_generation`, `not_before` und `refresh_posts` (Epochensekunden). Ein Token-Schreiben löscht sie,
nur `refresh_posts` gibt der Dienst-Refresh selbst weiter; ein Login löscht alle. Ältere Dateien ohne diese Felder
bleiben lesbar, `"refresh_posts": null` gilt als fehlend.

**Wertebereiche (zweites Review PR 4):** Zeitwerte (`not_before`, `refresh_posts`) und `exp` liegen in
`[0, MAX_EPOCH]` mit `MAX_EPOCH = 10^11`; `refresh_blocked` ist `K0` bis `K10`; `login_id` besteht aus 1 bis 64
Kleinbuchstaben-Hexziffern. Alles andere macht die Datei ungültig (`InvalidAuthFileError`), ein `OverflowError`
erreicht nie den Aufrufer. `AuthState` und `AuthUpdate` zeigen Tokens nicht in `repr`.

### Orchestrierung R-b

- Datenabrufe und Discovery laufen über Instanzen mit `auth_provider=False`. Dort ist `get_token()` ein No-op.
  Den Refresh macht nur eine Refresh-Instanz mit `auth_provider=True`. Alle Instanzen teilen sich dieselben
  `ConnectionOptions`. Das Feld `ConnectionOptions.auth_provider` wird in 1.8.2 nicht ausgewertet; die Fabrik
  setzt es trotzdem ausdrücklich auf `False`. Ein Test prüft, dass die Refresh-Instanz dennoch refresht.
- `ensure_fresh()` läuft vor jeder Discovery und jedem Poll und liest die Datei bei jedem Aufruf unter dem
  Lock (`read_locked()`). Die Datei wird übernommen, sobald Generation, `login_id` oder Refresh-Token von dem
  abweichen, was der Prozess zuletzt gelesen oder geschrieben hat, und nichts Ungespeichertes ansteht. Das deckt
  einen Login ab, eine gelöschte und neu angelegte Datei (Generation wieder 1) und ein zurückgespieltes Backup.
  Ein fehlender oder unlesbarer Access-Token gilt als abgelaufen; ist die halbe Laufzeit nicht positiv, ebenso.
- **Marge** = `min(BOSCH_POLL_TIMEOUT + 60 s, Laufzeit / 2)`, mit Laufzeit = `exp − iat`, ersatzweise
  `exp − Zeitpunkt des Erhalts`. Ist `BOSCH_POLL_TIMEOUT + 60 s` größer als die halbe Laufzeit, wird einmal
  gewarnt. `BOSCH_POLL_TIMEOUT` liegt zwischen 60 und 900 s, Default 300 s. Der Timeout umschließt nur
  Discovery und Poll, nie einen Refresh. Polls laufen nacheinander, das Intervall zählt ab dem Ende des
  vorherigen Polls; deshalb ist der Timeout nicht an das Intervall gekoppelt.
- `refresh_locked()` ist eine einzige Task je Prozess. Aufrufer warten per `asyncio.shield` auf sie. Unter dem
  Lock liest die Task zuerst die Datei. Hat ein Login eine höhere Generation mit frischem Access-Token
  geschrieben, geht kein POST raus. Andernfalls wird nur mit dem übernommenen Refresh-Token refresht, nie mit
  dem alten. Nach einem Erfolg wird sofort geschrieben (Generation + 1, `last_refresh_at`). Die neuen Tokens
  werden im Speicher gesichert, bevor ihre Claims gelesen werden; Typ und Inhalt werden vorher geprüft (Text,
  nicht leer, sonst K9 mit Rollback, geloggt wird nur der Typname).
- **Phasen der Task:** `acquiring` und `backoff` sind abbrechbar, `sending` und `writing` werden abgewartet.
  Zwischen dem erfolgreichen `LOCK_NB` und `sending` liegt kein `await`, der Lock wird im `finally` freigegeben.
  Nach `aclose()` beginnt kein neuer Versuch. `auth_required` und `disconnected` setzt die Task selbst.
- **Gemeinsamer Lock im Prozess:** Läuft die Refresh-Task, liest `ensure_fresh()` nicht selbst, sondern wartet
  auf die Task. So sperrt sich der Prozess nicht selbst aus.
- **Schreibfehler nach erfolgreichem Refresh:** Der Dienst läuft mit den Tokens im Speicher weiter und loggt
  einen ERROR. Vor jedem Poll versucht er das Schreiben erneut. Solange nicht geschrieben ist, startet nur dann
  ein weiterer Refresh, wenn der Access-Token fällig wird oder ein 401 kommt; dafür nimmt er den neuesten
  Refresh-Token aus dem Speicher. Schreibt ein Login zwischendurch, gewinnt der Login. Ein Schreibversuch, dessen
  Tokens inzwischen ersetzt oder geschrieben wurden, schreibt nichts. Lehnt die Validierung des Stores die Daten ab
  (`ValueError`), gilt dasselbe wie bei einem Schreibfehler.
- **401 bei einem Datenabruf:** Darauf folgen genau ein Refresh und genau eine Wiederholung des Polls. Ein
  zweiter 401 führt zu `auth_required`, wenn der verwendete Token laut `exp` noch gültig war. Sonst läuft
  `ensure_fresh()` einmal erneut.
- **`auth_required`:** Der Dienst stellt keine Cloud-Anfragen. Alle 10 s liest er die Datei unter dem Lock und
  übernimmt einen neuen Login. Beim Eintritt geht das Fehler-Event `AUTH_REQUIRED` raus. Braucht der übernommene
  Login noch einen Refresh, wechselt der Zustand zuerst nach `starting`; scheitert der Refresh erneut, ist das ein
  neuer Übergang nach `auth_required` mit eigenem Event.
- **Lange Wartezeiten (K3, K4):** Sie laufen in Schritten von 10 s; nach jedem Schritt wird die Datei unter dem
  Lock gelesen. Ein Login beendet die Wartezeit: Mit frischem Access-Token folgt `ready`, sonst sofort ein Versuch.
  Eine neue `login_id` öffnet ein neues K3-Fenster (`not_before` und gezählte POSTs im Speicher werden verworfen),
  und der K4-Backoff beginnt wieder bei 30 s.
- **Uhr geht vor:** Ist ein gerade erhaltener Token laut lokaler Uhr schon fällig, loggt der Dienst einmal eine
  WARNING und startet für 60 s (`CRASH_LOOP_GUARD`) keinen weiteren Refresh. So bleibt es bei höchstens einem POST
  je 60 s statt einem je Poll. Eine Rechnung über `iat` wurde verworfen: Für Tokens aus der Datei ist der
  Zeitpunkt des Erhalts nicht verlässlich bekannt.
- **Neustartfest (Nutzerentscheidung 2026-10-06):** Führt ein gesendeter Refresh zu `auth_required` (K0, K1, K2,
  K5–K10), schreibt der Dienst unter demselben Lock `refresh_blocked` mit der aktuellen Generation; Tokens und
  Generation bleiben unverändert, damit die Übernahme eines Logins nicht gestört wird. Nach einem Neustart geht
  kein POST raus, solange `blocked_generation` gleich der Generation der Datei ist; nur ein Login (neue
  Generation oder Datei-Identität) hebt das auf. K3 schreibt `not_before` und die POST-Zeitpunkte der letzten
  Stunde; nach einem Neustart wartet der Dienst bis `not_before` (höchstens 1 h, ein späterer Wert gilt als
  unplausibel). Scheitert dieses Schreiben, bleibt der Zustand im Speicher, der Versuch gilt nicht als K4, und der
  Dienst schreibt bei jeder 10-s-Prüfung erneut. Ein solches Nachschreiben ohne Block hebt einen inzwischen
  gesetzten Block derselben Generation nie auf: Der Store übernimmt ihn. Ein zweiter 401 eines frisch refreshten Tokens wird nicht
  gespeichert: Der Token selbst ist dann nicht als schlecht bekannt.
- **Crash-Loop-Schutz:** Liegt `last_refresh_at` weniger als 60 s zurück, wartet der erste Refresh nach dem
  Start, bis 60 s vergangen sind, höchstens 60 s (auch bei einem Zeitstempel in der Zukunft). Geprüft wird unter
  dem Lock, nachdem die Datei gelesen ist. Ein gültiger Access-Token aus der Datei macht den Refresh überflüssig.
- **Netz- und FUSE-Dateisysteme:** Liegt das Verzeichnis von `auth.json` laut `/proc/mounts` auf NFS, CIFS/SMB,
  Ceph, GlusterFS, 9p, virtiofs, AFS oder einem `fuse*`-Typ, wird gewarnt.

### D11: Option (b) – Retry nur bei K3 und K4

Fehler werden nur direkt um `get_token(force=True)` der Refresh-Instanz gefangen, und zwar `Exception`, nie
`BaseException`. Ein Abbruch ist also keine Fehlerklasse. Die Klassifikation folgt Tabelle K.

| K | Auslöser | Erscheint als | Rotationsstatus | Folge bei D11 (b) |
| --- | --- | --- | --- | --- |
| K0 | `get_token(force=True)` endet ohne Ausnahme, liefert aber nicht `True` | – | unklar oder verarbeitet | `auth_required`, Typ wird geloggt |
| K1 | HTTP 400; leeres JSON ist davon nicht unterscheidbar | `AuthFailedError` ohne `__cause__` | endgültig ungültig | `auth_required` |
| K2 | HTTP 401 am Token-Endpunkt | `AuthFailedError`, `__cause__` 401 | endgültig abgelehnt | `auth_required` |
| K3 | HTTP 429 | `NotRespondingError`, `__cause__` 429 | nicht verarbeitet (Annahme, Q-K3) | Retry nach `Retry-After`, mindestens 60 s, höchstens 1 h; ≤ 3 POSTs/h, danach `disconnected`, bis das Fenster frei ist |
| K4 | Verbindungsaufbau gescheitert oder beim Aufbau abgelaufen (Nutzerentscheidung 2026-10-06); ebenso Lock-Timeout und Dateisystemfehler beim Lesen vor dem POST | `NotRespondingError`, `__cause__` `ClientConnectorError` oder `ConnectionTimeoutError`; beide auch roh | nachweislich nicht gesendet | Backoff 30 s → max. 15 min, unbegrenzt, `disconnected` |
| K5 | Timeout nach dem Aufbau oder Gesamt-Timeout: `SocketTimeoutError`, sonstiger `ServerTimeoutError`, roher `TimeoutError` beim Lesen der Antwort | `NotRespondingError` mit `TimeoutError` oder roh | unklar | `auth_required` |
| K6 | Abbruch nach dem Senden | roh: `ServerDisconnectedError`, `ClientOSError`, `ClientPayloadError` | unklar oder verarbeitet | `auth_required` |
| K7 | HTTP 500 und sonstige Statuscodes | `ApiError` | unklar | `auth_required` |
| K8 | HTTP 403/404/502/504 | `{}` → `AttributeError` | unklar | `auth_required` |
| K9 | 200 mit unbrauchbarem Body, auch ein Token, der kein nicht leerer Text ist | `InvalidSensorDataError`, roh `ContentTypeError`, `KeyError`; Typprüfung im Token-Manager | verarbeitet, wahrscheinlich rotiert und verloren | `auth_required`, Typ wird geloggt |
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

**Unveränderter Refresh-Token (Review PR 4):** Liefert der Server einen neuen Access-Token, aber denselben
Refresh-Token, übernimmt der Dienst den Access-Token, behält den Refresh-Token, schreibt beide (Generation + 1)
und loggt eine WARNING. Der Server hat dann nicht rotiert; OAuth erlaubt das. `auth_required` würde einen
unnötigen Login erzwingen. Hat der Server doch rotiert und den alten Wert zurückgegeben, zeigt das der nächste
Refresh als K1/K2, und erst dann folgt `auth_required`. Die WARNING macht den Fall im 24-h-Lauf (PR 6, Q-ROT)
sichtbar.

**K3-Zählung:** Das Fenster „≤ 3 POSTs/h“ zählt nur tatsächlich gesendete Refresh-POSTs. K4, Lock-Timeout und
Dateisystemfehler vor dem POST zählen nicht.

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
wird. Die Docker-Voreinstellung von 10 s reicht dafür nicht. `stop_grace_period: 20s` in Compose bzw.
`docker run --stop-timeout 20` sind deshalb Pflicht; PR 6 setzt den Compose-Eintrag und dokumentiert beides in
`docs/operations.md`. Das reicht, weil `aclose()` das Warten auf den Lock abbricht; vor dem POST ist das sicher.
Signal-Handler folgen ebenfalls in PR 6.

**Shutdown-Vertrag:** `main` ruft `await token_manager.aclose()` im `finally` auf, bevor die `ClientSession`
geschlossen wird. Eine gescheiterte Refresh-Task loggt ein Done-Callback mit ihrem Typnamen, sodass keine Ausnahme
unabgeholt bleibt.

## Akzeptierte Restrisiken

- **Hängender Verbindungsaufbau (Accepted risk, Nutzerentscheidung 2026-10-06, Entscheidung A):** Mit
  `homecom_alt` 1.8.2 (`ClientTimeout(total=15)`) endet ein hängender Verbindungsaufbau als K5 und damit in
  `auth_required`, obwohl nichts gesendet wurde. Das ist als Restrisiko akzeptiert. PR 6 führt **keinen**
  `connect`-Timeout ein.
- **Absturz während des POST (Info I1):** Es gibt keinen Write-Ahead-Marker vor dem POST. Stirbt der Prozess
  zwischen Senden und Schreiben, kann ein rotierter Refresh-Token verloren gehen; nach dem Neustart folgt dann
  K1/K2 und ein Login. Ein Marker vor jedem POST würde jeden Neustart nach einem Absturz in `auth_required`
  schicken, auch wenn der POST nie rausging. Abgewogen ist das gegen die kurze Lücke; den geordneten Stopp deckt
  T2 ab (Docker-Voreinstellung 10 s < POST-Timeout 15 s, daher 20 s Pflicht).
- **Backup-Restore ersetzt die Tokens bewusst:** Ein zurückgespieltes `auth.json` wird übernommen (Datei-Identität).
  Backups enthalten Live-Secrets und gehören entsprechend geschützt. Weil Refresh-Tokens einmal verwendbar sind,
  ist der Token im Backup nach einem Restore meist schon verbraucht; dann folgt K1/K2 und ein neuer Login.

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
- **Neustartverhalten:** Ein Neustart umgeht weder `auth_required` noch das K3-Limit; beides steht in `auth.json`.
  Einen Block hebt nur ein Login auf. Wer die Datei von Hand bearbeitet, muss die Felder `refresh_blocked` und
  `blocked_generation` gemeinsam entfernen; ein Backup mit Block bleibt nach dem Zurückspielen blockiert.
