# Cozytoria

[Craftoria](https://github.com/TeamAOF/Craftoria) mit ein paar Extras. Dieses Repo enthält nur **unsere Änderungen**.
Craftoria selbst wird beim Build am Commit aus [`upstream.json`](upstream.json) geholt, unser Overlay kommt
darüber, und GitHub Actions baut daraus Client- und Server-ZIP.

## Für Spieler

1. Neueste `Cozytoria-<version>.zip` unter [Releases](https://github.com/CozyOrg/cozytoria/releases) herunterladen.
2. CurseForge App → Minecraft → **Create Custom Profile** → **Import** → ZIP auswählen.
3. Update: neue ZIP genauso importieren. Wer seine Einstellungen behalten will, kopiert `options.txt` aus dem alten Profilordner rüber.

Die ZIP ist ein normaler CurseForge-Export, Prism Launcher oder ATLauncher können sie auch importieren.

## Für den Server

Java 21 nötig.

1. `Cozytoria-Server-<version>.zip` entpacken.
2. `startserver.sh` (Linux/macOS) bzw. `startserver.bat` (Windows) starten. Der ServerStarter lädt NeoForge,
   die Client-ZIP aus dem Release und alle Mods, Client-only Mods lässt er weg.

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
- **Client-only Mods:** in der `.pw.toml` `side = "client"` setzen. Der Build trägt sie dann automatisch in
  `ignoreProject` (CurseForge) bzw. `ignoreFiles` (andere Quellen) der Server-Config ein.
- `side = "server"` funktioniert nicht: Der Server installiert aus der Client-ZIP, und dort fehlt die Mod dann.
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
- Jedes Release wird zusätzlich im Discord angekündigt: Ping, großer Download-Link, Changelog und alle zusätzlichen
  Mods (siehe Einrichtung). Wer gepingt wird, steht in `cozytoria.json` unter `discord.mention`: `@everyone`,
  eine Rolle als `<@&ROLLEN-ID>` oder leer für keinen Ping.

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
