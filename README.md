# Cozytoria

[Craftoria](https://github.com/TeamAOF/Craftoria) mit ein paar Extras. Dieses Repo enthält nur **unsere Änderungen**.
Craftoria selbst wird beim Build am Commit aus [`upstream.json`](upstream.json) geholt, unser Overlay kommt
darüber, und GitHub Actions baut daraus Client- und Server-ZIP.

## Für Spieler

Wir spielen mit [Prism Launcher](https://prismlauncher.org/).

1. **Installieren:** Prism Launcher → **Instanz hinzufügen** → **Importieren** → die neueste `Cozytoria-<version>.zip`
   aus den [Releases](https://github.com/CozyOrg/cozytoria/releases) auswählen oder deren Download-Link einfügen → **OK**.
2. **Updaten:** Gibt es ein neues Release, erscheint im Hauptmenü oben links ein **Update**-Button, der zur
   Release-Seite führt. Die neue Version genauso importieren. Eigene Einstellungen aus der alten Instanz übernehmen
   (`options.txt` in deren `minecraft`-Ordner), danach die alte Instanz löschen.

Fragt Prism beim Import nach manuellen Downloads (manche Mods sperren Downloads über Launcher): die angezeigten Links
im Browser öffnen und herunterladen, Prism findet die Dateien im Downloads-Ordner von selbst.

Die ZIP ist ein normaler CurseForge-Export und lässt sich auch in der CurseForge App importieren.

## Für den Server

Java 21 nötig.

1. `Cozytoria-Server-<version>.zip` entpacken.
2. `startserver.sh` (Linux/macOS) bzw. `startserver.bat` (Windows) starten. Der ServerStarter lädt NeoForge,
   die Client-ZIP aus dem Release und alle Mods, Client-only Mods lässt er weg, Server-only Mods lädt er zusätzlich.

**Update:** die neue `server-setup-config.yaml` (oder die ganze Server-ZIP) über die alte kopieren und neu starten.
Der ServerStarter erkennt an der geänderten `modpackUrl`, dass er neu installieren muss, und schiebt den alten
`mods/`-Ordner nach `oldfiles/`. Configs aus dem Pack werden überschrieben, Welten und `server.properties` bleiben.

## Aufbau

```
cozytoria.json     Name, Autor, Versions-Suffix, Server-Einstellungen
upstream.json      Craftoria-Commit, auf dem wir aufbauen (pflegt der Upstream-Workflow)
overlay/           Dateien, die über Craftoria gelegt werden (gleiche Ordnerstruktur wie Craftoria)
  mods/*.pw.toml   unsere zusätzlichen Mods als packwiz-Metadaten
  config/ …        eigene oder komplett ersetzte Configs
  server_files/ …  z. B. eigenes server-icon.png
patches/*.patch    gezielte Änderungen an Craftoria-Dateien
remove.txt         Craftoria-Dateien, die rausfliegen
scan-allowlist.txt geprüfte Fehlalarme des Malware-Scans
scripts/cozytoria.py  Build-Tooling (Python 3.9+, git; packwiz lädt es selbst)
```

Beim Build passiert in dieser Reihenfolge: Craftoria holen → `remove.txt` → `patches/` → `overlay/` → Version
eintragen → `packwiz refresh` → `packwiz curseforge export` → Server-ZIP bauen.

## Mods hinzufügen und entfernen

packwiz-Befehle laufen über das Skript. Es baut erst das komplette Pack (Craftoria + Overlay), damit packwiz sieht,
welche Abhängigkeiten schon da sind, und überträgt das Ergebnis zurück:

```bash
python3 scripts/cozytoria.py packwiz curseforge add <slug-oder-url>
python3 scripts/cozytoria.py packwiz modrinth add <slug>
python3 scripts/cozytoria.py packwiz remove <mod>
python3 scripts/cozytoria.py packwiz update <mod>
```

- Neue oder aktualisierte `.pw.toml` landen in `overlay/`, entfernte Craftoria-Mods in `remove.txt`.
- Holt packwiz eine Mod, die Craftoria schon mitbringt, noch einmal (z. B. eine Abhängigkeit von Modrinth statt
  CurseForge), übernimmt der Helfer sie nicht ins Overlay und warnt. Sonst würde sie Craftorias Version festhalten.
  Liegt so eine Datei trotzdem im Overlay, warnt auch der Build.
- **Client-only Mods:** in der `.pw.toml` `side = "client"` setzen. Der Build trägt sie dann automatisch in
  `ignoreProject` (CurseForge) bzw. `ignoreFiles` (andere Quellen) der Server-Config ein.
- **Server-only Mods** (z. B. BlueMap): `side = "server"`. packwiz lässt sie aus der Client-ZIP weg, der Build trägt
  sie stattdessen mit ihrer Download-Adresse in `additionalFiles` der Server-Config ein. Der ServerStarter lädt sie
  dann nach dem Pack direkt nach `mods/`.
- Bringt das Overlay eine Mod mit, die Craftoria auch hat, gewinnt **immer** unsere Version, auch wenn Craftoria
  später eine neuere hat. Nach `packwiz update` auf eine Craftoria-Mod also im Blick behalten.

## Configs und Skripte ändern

**Neue Dateien** (eigene KubeJS-Skripte, Configs neuer Mods): einfach an den passenden Pfad unter `overlay/` legen.

**Bestehende Craftoria-Dateien ändern:** besser als Patch, weil eine komplett kopierte Datei spätere Änderungen
von Craftoria stillschweigend überschreibt. Ein Patch bricht dagegen laut ab, wenn er nicht mehr passt.

```bash
python3 scripts/cozytoria.py assemble      # holt Craftoria nach build/upstream (ein normales Git-Checkout)
# Datei in build/upstream/... bearbeiten, dann:
git -C build/upstream diff > patches/0001-kurze-beschreibung.patch
git -C build/upstream checkout -- .
```

Patches werden alphabetisch angewendet. Wenn ein Craftoria-Update einen Patch bricht, schlägt der Build mit dem
Patch-Namen fehl. Dann die Änderung in `build/upstream` (jetzt auf dem neuen Stand) neu machen und den Patch ersetzen.

Tipp: Mit `overlay/config/desiredservers/desiredservers.json` steht euer Server automatisch in der Serverliste.

## Lokal bauen

```bash
python3 scripts/cozytoria.py build
```

Ergebnis in `dist/` mit Version `<craftoria>-cozy.dev`. Die `modpackUrl` der Server-ZIP zeigt dabei auf ein Release,
das es nicht gibt, zum Testen also die Client-ZIP importieren oder `modpackSource` (siehe unten) nutzen.

## Releases

- **Releases gibt es nur per Hand:** Actions → **Build** → **Run workflow** auf `main`, oder
  `gh workflow run build.yml -R CozyOrg/cozytoria --ref main`. Das baut und veröffentlicht ein Release mit der
  nächsten Nummer, z. B. `1.37.0-cozy.1`, `1.37.0-cozy.2`, nach einem Craftoria-Update wieder ab `1.38.0-cozy.1`.
- **Pushes und Pull Requests** bauen nur zur Kontrolle, die ZIPs hängen als Artefakt am Workflow-Run.
- Die Release-Notes enthalten einen Changelog seit dem letzten Release: Craftoria-Update, neue, aktualisierte und
  entfernte Mods sowie die Commit-Betreffs. Gute Commit-Messages landen also direkt bei den Spielern.
- Jedes Release wird zusätzlich im Discord angekündigt: Ping, großer Download-Link, Changelog, alle zusätzlichen
  Mods und in einer Zeile das Ergebnis des Malware-Scans (siehe Einrichtung). Wer gepingt wird, steht in
  `cozytoria.json` unter `discord.mention`: `@everyone`, eine Rolle als `<@&ROLLEN-ID>` oder leer für keinen Ping.
  Unter „Was ist neu“ stehen dort nur Commits, die Spieler betreffen: `feat: Mehr Quests` wird zu „Mehr Quests“,
  `ci:`, `docs:`, `chore:`, `refactor:`, `style:`, `test:` und `build:` werden ausgeblendet.

## Servertest

Jeder Build-Lauf auf GitHub (Push, PR und Release) installiert den Server einmal mit dem ServerStarter, so wie er
auch bei euch läuft, und startet ihn. Sobald „Done“ erscheint, wird er wieder gestoppt. Stürzt er ab oder ist er
nach 40 Minuten nicht oben, wird der Lauf rot. Logs und Crash-Reports hängen dann als Artefakt `server-test-logs` am
Run, und es entsteht kein Release.

Der Test prüft nur die Serverseite. Abstürze, die nur im Client auftreten (Grafik, Shader, Client-only Mods),
findet er nicht.

Lokal (Linux oder macOS, Java 21, etwa 8 GB freier RAM, lädt alle Mods herunter):

```bash
python3 scripts/cozytoria.py build
python3 scripts/cozytoria.py test-server
```

## Malware-Scan

Jeder Build-Lauf scannt parallel zum Servertest die Dateien des Packs (Workflow **Malware-Scan**), aber nur die, die
noch nie sauber gescannt wurden, meist also nur neue oder aktualisierte Mods. Das dauert ein paar Minuten statt einer
halben Stunde. Welche Dateien schon sauber waren, merkt sich der Scan im Actions-Cache (`build/scan/clamav.json`).

Jede Nacht auf `main` läuft ein **Vollscan** über alles mit den aktuellen Signaturen. Dort ändern sich die Jars nicht,
die Signaturen aber schon. Wird eine Mod, die ihr längst ausliefert, nachträglich als Malware bekannt, wird der
nächtliche Lauf rot, und GitHub schickt eine Mail. Die Datei fliegt dabei aus der Liste der sauberen Dateien, ab dann
schlägt jeder Build an, bis sie raus oder freigegeben ist.

Vollscan von Hand: Actions → Malware-Scan → Run workflow (Haken „Alle Dateien scannen“ ist gesetzt). Beim Release
(Actions → Build → Run workflow) gibt es denselben Haken, dort ist er standardmäßig aus.

1. **Alles laden:** Alle Mods, Resourcepacks und Shaderpacks kommen direkt von CurseForge bzw. Modrinth, auch die
   reinen Client-Mods, die der Servertest überspringt. Jede Datei wird gegen die Prüfsumme aus ihrer `.pw.toml`
   geprüft. Passt eine nicht, wird der Lauf rot.
2. **ClamAV** scannt neue Dateien bzw. beim Vollscan alles, auch Craftorias Configs und Skripte und Jars, die
   Craftoria direkt mitbringt (z. B. `InstanceSync.jar`). Jeder Fund macht den Lauf rot. Ein Vollscan dauert wegen
   der vielen Dateien in den Jars rund eine halbe Stunde. Das Log zeigt jedes fertige Jar.

Ein Release gibt es nur, wenn der Scan grün ist. Funde stehen in der Zusammenfassung des Runs, das Ergebnis
außerdem in der Discord-Ankündigung.
Ein Scanner kennt nur Malware, die schon bekannt ist. Ganz neue Malware erkennt er am ersten Tag meist nicht.
Craftoria-Updates also lieber erst mergen, wenn das Release ein paar Tage alt ist.

**Fehlalarm:** Erst genau hinsehen: Welche Signatur schlägt an, und ist die Mod bekannt? Ist die Datei sicher harmlos,
ihren SHA-256 (steht in der Meldung) mit Kommentar in `scan-allowlist.txt` eintragen. Die Freigabe gilt nur für genau
diese Datei, ein Update der Mod wird wieder geprüft.

Lokal (Docker oder ein installiertes ClamAV, lädt rund 1,3 GB):

```bash
python3 scripts/cozytoria.py assemble
python3 scripts/cozytoria.py fetch-mods
python3 scripts/cozytoria.py scan-clamav          # nur neue Dateien
python3 scripts/cozytoria.py scan-clamav --full   # alles
```

## Craftoria-Updates

Der Workflow **Craftoria upstream** schaut jeden Morgen nach. Craftoria hat keine GitHub-Releases; als Release gilt
der Commit, der in `server_files/server-setup-config.yaml` die `modpackUrl` auf den neuen CurseForge-Download setzt
(das macht deren Upload-Skript erst nach dem Upload). Findet er einen neuen, öffnet er einen PR mit dem neuen
`upstream.json` und stößt dafür einen Build an. Ist der grün und laufen eure Mods noch: mergen und das Release per Hand
starten (siehe oben). Den Check selbst manuell auslösen geht unter Actions → Craftoria upstream → Run workflow.

## Einmalige Einrichtung auf GitHub

- **Settings → Actions → General → Workflow permissions:** „Read and write permissions“ und
  „Allow GitHub Actions to create and approve pull requests“ aktivieren. Bei einer Organisation muss das
  zusätzlich in den Organisations-Einstellungen erlaubt sein.
- **Discord:** Webhook-URL als Secret `DISCORD_WEBHOOK_URL` hinterlegen (Settings → Secrets and variables →
  Actions → New repository secret, oder `gh secret set DISCORD_WEBHOOK_URL -R CozyOrg/cozytoria` und URL einfügen).
  Die URL gehört nicht ins Repo: Wer sie kennt, kann in euren Kanal schreiben. Ohne Secret wird die Ankündigung
  übersprungen.
- **Sichtbarkeit:** Der ServerStarter lädt die Client-ZIP ohne Login aus dem Release, das Repo muss dafür
  **öffentlich** sein. Bei einem privaten Repo in `cozytoria.json` `"modpackSource": "bundled"` setzen: Dann liegt
  die Client-ZIP in der Server-ZIP und wird per `file://` gelesen (Server-ZIP wird dadurch ~65 MB groß).
