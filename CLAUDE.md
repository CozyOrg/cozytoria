# Cozytoria

Overlay auf [Craftoria](https://github.com/TeamAOF/Craftoria): Das Repo enthält nur unsere Änderungen, Craftoria wird
beim Build am Commit aus `upstream.json` geholt. Aufbau, Befehle und Abläufe stehen im README, das ist die Referenz.

## Grundsatz: Craftoria bleibt, wie es ist

- Konflikte, die durch unsere zusätzlichen Mods entstehen (Rezepte, Tags, Configs …), lösen wir auf unserer Seite:
  unsere Mod bzw. deren Rezept ändern, nie Craftorias Mods oder Skripte.
- Rezept-Fixes als JSON mit derselben Rezept-ID unter `overlay/kubejs/data/<namespace>/recipe/<pfad>.json`, das
  Original aus dem Mod-Jar als Vorlage. Keine KubeJS-Skripte dafür, die sieht der Rezept-Check nicht.
- Muss doch eine Craftoria-Datei geändert werden: als Patch in `patches/`, nie die ganze Datei ins Overlay kopieren.
  Dateien entfernen über `remove.txt`.
- Wie Craftoria ein Problem schon gelöst hat, ist das Vorbild (z. B.
  `build/upstream/kubejs/server_scripts/Mods/Craftoria/ConflictFixes.js`).

## Im Repo arbeiten

- Mods nur über `python3 scripts/cozytoria.py packwiz …` hinzufügen, entfernen oder aktualisieren, `.pw.toml` nicht
  von Hand schreiben.
- `build/` und `dist/` sind erzeugt. `build/upstream` ist ein Git-Checkout von Craftoria nur zum Erstellen von Patches,
  danach zurücksetzen.
- Nach `fetch-mods` liegen alle Mod-Jars in `build/scan/files/mods/`. Dort Rezepte, Tags und Configs nachschauen
  (`unzip -p`), statt zu raten.
- `scripts/cozytoria.py`: nur Python-Standardbibliothek, Python 3.9+. Kommentare, Ausgaben und README auf Deutsch.

## Prüfen

- `python3 scripts/cozytoria.py build` baut Client- und Server-ZIP.
- Nach jeder neuen Mod und jedem Rezept-Fix: `assemble`, `fetch-mods`, `check-recipes`. Der Check muss grün sein,
  sonst blockiert er in CI das Release.
- `test-server` (Java 21, rund 8 GB RAM) nur bei Bedarf, CI startet den Server bei jedem Lauf.

## Commits und Releases

- Conventional Commits auf Englisch. Betreffs von `feat:` und `fix:` landen als Changelog bei den Spielern (Release
  und Discord), also aus Spielersicht formulieren. `ci:`, `chore:`, `docs:`, `refactor:` usw. werden dort ausgeblendet.
- Spieler-Änderungen und Tooling/CI in getrennten Commits.
- Releases startet nur der Mensch, nie selbst `gh workflow run build.yml` aufrufen.
