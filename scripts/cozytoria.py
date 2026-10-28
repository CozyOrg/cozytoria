#!/usr/bin/env python3
"""Build-Tooling für Cozytoria: Craftoria als Upstream plus unser Overlay.

  build [--release]   Komplettes Pack bauen: Client- und Server-ZIP in dist/
  assemble            Nur das kombinierte packwiz-Pack in build/pack erzeugen
  packwiz <args>      packwiz auf dem kombinierten Pack ausführen; neue oder geänderte
                      Metadaten landen in overlay/, entfernte Craftoria-Mods in remove.txt
  check-upstream      Nach einem neuen Craftoria-Release suchen und upstream.json anpassen
  notify-discord      Das zuletzt gebaute Release im Discord ankündigen (Webhook aus DISCORD_WEBHOOK_URL)

Braucht nur Python 3.9+ und git, packwiz wird automatisch heruntergeladen.
"""

import argparse
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
DIST = ROOT / "dist"
UPSTREAM_DIR = BUILD / "upstream"
PACK_DIR = BUILD / "pack"
SERVER_DIR = BUILD / "server"
OVERLAY_DIR = ROOT / "overlay"
PATCHES_DIR = ROOT / "patches"
REMOVE_FILE = ROOT / "remove.txt"
CONFIG_FILE = ROOT / "cozytoria.json"
LOCK_FILE = ROOT / "upstream.json"

# Der packwiz-Fork, mit dem auch Craftoria gebaut wird. Er schreibt für jede Mod eine
# downloadUrl in die manifest.json, ohne die der TeamAOF-ServerStarter nichts laden kann.
PACKWIZ_REPO = "WhitePhant0m/packwiz"
PACKWIZ_VERSION = "0.0.1751897637"

METADATA_DIRS = ("mods", "resourcepacks", "shaderpacks")
SERVER_CONFIG = "server-setup-config.yaml"
UPSTREAM_SERVER_CONFIG = "server_files/" + SERVER_CONFIG
IGNORED_OVERLAY_FILES = (".gitkeep", ".DS_Store")
# Discord erlaubt 4096 Zeichen pro Embed, der Rest bleibt für den Hinweis aufs vollständige Release
DISCORD_DESCRIPTION_LIMIT = 3900
# Conventional-Commit-Typen, die Spieler nicht interessieren und im Discord ausgeblendet werden
TECHNICAL_COMMIT_TYPES = {"build", "chore", "ci", "docs", "refactor", "style", "test"}

IN_CI = os.environ.get("GITHUB_ACTIONS") == "true"
warnings = []


def log(message):
    print(message, flush=True)


def warn(message):
    warnings.append(message)
    print(("::warning::" if IN_CI else "WARNUNG: ") + message, flush=True)


def fail(message):
    print(("::error::" if IN_CI else "FEHLER: ") + message, file=sys.stderr, flush=True)
    sys.exit(1)


def set_output(key, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")


def run(cmd, cwd=None, env=None, capture=False):
    result = subprocess.run(
        [str(c) for c in cmd], cwd=cwd, env=env, check=True, text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout.strip() if capture else None


def git_output(*args):
    # Wie run(), liefert aber None statt abzubrechen, z. B. wenn es noch keinen Release-Tag gibt
    result = subprocess.run(["git", *args], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return result.stdout if result.returncode == 0 else None


def read_text(path):
    # newline="" erhält CRLF/LF, damit wir nur die Zeilen anfassen, die wir wirklich ändern
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def load_json(path):
    return json.loads(read_text(path))


def save_json(path, data):
    write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def http_get(url):
    headers = {"User-Agent": "cozytoria-build"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as response:
        return response.read()


def github_repo(cfg):
    return os.environ.get("GITHUB_REPOSITORY") or cfg["github"]


def dev_version(cfg, lock):
    return f"{lock['version']}-{cfg['versionSuffix']}.dev"


def next_version(cfg, lock):
    prefix = f"{lock['version']}-{cfg['versionSuffix']}."
    tags = run(["git", "tag", "--list", prefix + "*"], cwd=ROOT, capture=True).split()
    numbers = [int(tag[len(prefix):]) for tag in tags if tag[len(prefix):].isdigit()]
    return f"{prefix}{max(numbers, default=0) + 1}"


# --- packwiz -----------------------------------------------------------------------------

def packwiz_binary():
    override = os.environ.get("PACKWIZ")
    if override:
        return override

    system = platform.system().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine().lower())
    if system not in ("linux", "darwin", "windows") or not arch:
        fail(f"Kein packwiz-Build für {platform.system()}/{platform.machine()}, setze PACKWIZ auf eine eigene Binary")

    exe = "packwiz.exe" if system == "windows" else "packwiz"
    target = BUILD / "tools" / f"packwiz-{PACKWIZ_VERSION}" / exe
    if not target.exists():
        archive = f"packwiz_{system}_{arch}." + ("zip" if system == "windows" else "tar.gz")
        url = f"https://github.com/{PACKWIZ_REPO}/releases/download/{PACKWIZ_VERSION}/{archive}"
        log(f"Lade packwiz: {url}")
        data = http_get(url)
        target.parent.mkdir(parents=True, exist_ok=True)
        if archive.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                target.write_bytes(zf.read(exe))
        else:
            with tarfile.open(fileobj=io.BytesIO(data)) as tf:
                target.write_bytes(tf.extractfile(exe).read())
        target.chmod(0o755)
    return str(target)


def packwiz(args, interactive=False):
    cmd = [packwiz_binary(), "--cache", BUILD / "cache"]
    if not interactive:
        cmd.append("--yes")
    run(cmd + list(args), cwd=PACK_DIR)


def metadata_files(base):
    for folder in METADATA_DIRS:
        yield from sorted((base / folder).rglob("*.pw.toml"))


def read_metadata(path):
    return parse_metadata(read_text(path), path.name)


def parse_metadata(text, fallback_name):
    def field(pattern):
        match = re.search(pattern, text, re.M)
        return match.group(1) if match else None

    return {
        "name": field(r'^name\s*=\s*"([^"]+)"') or fallback_name,
        "filename": field(r'^filename\s*=\s*"([^"]+)"'),
        "side": field(r'^side\s*=\s*"(\w+)"') or "both",
        "cf": field(r"^project-id\s*=\s*(\d+)") if "[update.curseforge]" in text else None,
        "mr": field(r'^mod-id\s*=\s*"([^"]+)"') if "[update.modrinth]" in text else None,
    }


def metadata_keys(meta):
    keys = {("curseforge", meta["cf"]), ("modrinth", meta["mr"]), ("file", meta["filename"])}
    return {key for key in keys if key[1]}


# --- Upstream + Overlay zusammensetzen ---------------------------------------------------

def fetch_upstream(lock):
    commit = lock["commit"]
    if not (UPSTREAM_DIR / ".git").exists():
        UPSTREAM_DIR.mkdir(parents=True, exist_ok=True)
        run(["git", "-c", "init.defaultBranch=upstream", "init", "--quiet"], cwd=UPSTREAM_DIR)
        run(["git", "remote", "add", "origin", f"https://github.com/{lock['repo']}.git"], cwd=UPSTREAM_DIR)

    head = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "HEAD"],
        cwd=UPSTREAM_DIR, text=True, stdout=subprocess.PIPE,
    ).stdout.strip()
    if head != commit:
        log(f"Hole {lock['repo']}@{commit[:7]} (Craftoria {lock['version']})")
        run(["git", "fetch", "--quiet", "--depth", "1", "origin", commit], cwd=UPSTREAM_DIR)
        run(["git", "checkout", "--quiet", "--detach", commit], cwd=UPSTREAM_DIR)


def export_upstream():
    # git archive nimmt den Commit selbst, lokale Änderungen in build/upstream (zum Patches bauen) bleiben draußen
    proc = subprocess.Popen(["git", "archive", "--format=tar", "HEAD"], cwd=UPSTREAM_DIR, stdout=subprocess.PIPE)
    with tarfile.open(fileobj=proc.stdout, mode="r|") as tf:
        if hasattr(tarfile, "data_filter"):
            tf.extractall(PACK_DIR, filter="data")
        else:
            tf.extractall(PACK_DIR)
    if proc.wait() != 0:
        fail("git archive für Craftoria ist fehlgeschlagen")


def removal_patterns(text):
    patterns = []
    for line in text.splitlines():
        pattern = line.split("#", 1)[0].strip().lstrip("/")
        if pattern:
            patterns.append(pattern)
    return patterns


def apply_removals():
    removed = []
    if not REMOVE_FILE.exists():
        return removed
    for pattern in removal_patterns(read_text(REMOVE_FILE)):
        matches = sorted(PACK_DIR.glob(pattern))
        if not matches:
            warn(f"remove.txt: '{pattern}' gibt es in Craftoria nicht (mehr)")
        for path in matches:
            # Bei ** kann der Pfad schon mit seinem Elternordner verschwunden sein
            if not path.exists():
                continue
            rel = path.relative_to(PACK_DIR).as_posix()
            if path.is_dir():
                name = f"`{rel}/`"
                shutil.rmtree(path)
            else:
                name = read_metadata(path)["name"] if rel.endswith(".pw.toml") else f"`{rel}`"
                path.unlink()
            removed.append({"pattern": pattern, "path": rel, "name": name})
    return removed


def apply_patches(lock):
    patches = sorted(PATCHES_DIR.glob("*.patch"))
    # Ohne Ceiling findet git apply das Cozytoria-Repo um build/pack herum und löst die Pfade falsch auf
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(BUILD))
    for patch in patches:
        log(f"Wende {patch.name} an")
        try:
            run(["git", "apply", "--whitespace=nowarn", patch], cwd=PACK_DIR, env=env)
        except subprocess.CalledProcessError:
            fail(f"patches/{patch.name} passt nicht mehr auf Craftoria {lock['version']}, bitte neu erstellen (siehe README)")
    return [patch.name for patch in patches]


def apply_overlay():
    copied, overridden = [], []
    for src in sorted(OVERLAY_DIR.rglob("*")):
        if src.is_dir() or src.name in IGNORED_OVERLAY_FILES:
            continue
        rel = src.relative_to(OVERLAY_DIR).as_posix()
        dest = PACK_DIR / rel
        if dest.exists():
            overridden.append(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied.append(rel)
    return copied, overridden


def drop_replaced_upstream(overlay_meta):
    # Bringt das Overlay eine Mod mit, die Craftoria schon hat, gewinnt die Overlay-Version
    claimed = {}
    for rel in overlay_meta:
        for key in metadata_keys(read_metadata(PACK_DIR / rel)):
            claimed[key] = rel

    replaced = []
    for path in metadata_files(PACK_DIR):
        rel = path.relative_to(PACK_DIR).as_posix()
        if rel in overlay_meta:
            continue
        hits = metadata_keys(read_metadata(path)) & claimed.keys()
        if hits:
            path.unlink()
            replaced.append((rel, claimed[hits.pop()]))
            log(f"{rel} aus Craftoria wird durch overlay/{replaced[-1][1]} ersetzt")
    return replaced


def replace_in_file(path, pattern, value):
    rel = path.relative_to(PACK_DIR).as_posix()
    if not path.exists():
        warn(f"{rel} fehlt, Version wurde dort nicht eingetragen")
        return
    text, count = re.subn(pattern, lambda m: m.group(1) + value, read_text(path), count=1, flags=re.M)
    if count == 0:
        warn(f"{rel}: '{pattern}' nicht gefunden")
    write_text(path, text)


def stamp_version(cfg, version):
    pack_toml = PACK_DIR / "pack.toml"
    replace_in_file(pack_toml, r'^(name\s*=\s*)"[^"]*"', f'"{cfg["name"]}"')
    replace_in_file(pack_toml, r'^(author\s*=\s*)"[^"]*"', f'"{cfg["author"]}"')
    replace_in_file(pack_toml, r'^(version\s*=\s*)"[^"]*"', f'"{version}"')

    version_info = PACK_DIR / "version_info.json"
    if version_info.exists():
        save_json(version_info, {"version": version, "version_url": f"https://github.com/{github_repo(cfg)}/releases"})

    # Better Compatibility Checker zeigt Name und Version im Menü und warnt bei Client/Server-Unterschieden
    bcc = PACK_DIR / "config" / "bcc-common.toml"
    replace_in_file(bcc, r'^(\s*modpackName\s*=\s*)"[^"]*"', f'"{cfg["name"]}"')
    replace_in_file(bcc, r'^(\s*modpackVersion\s*=\s*)"[^"]*"', f'"{version}"')
    replace_in_file(bcc, r"^(\s*modpackProjectID\s*=\s*)\d+", "0")

    # Versionstext im Hauptmenü, der Teil nach %n% (Minecraft-/Loader-Version) bleibt stehen
    fancymenu = PACK_DIR / "config" / "fancymenu" / "customization" / "craftoria.txt"
    replace_in_file(fancymenu, r"^(\s*source\s*=\s*)[^%\r\n]*(?=%n%)", f"{cfg['name']} - {version}")


def assemble(cfg, lock, version):
    fetch_upstream(lock)
    if PACK_DIR.exists():
        shutil.rmtree(PACK_DIR)
    PACK_DIR.mkdir(parents=True)
    export_upstream()

    removed = apply_removals()
    patches = apply_patches(lock)
    copied, overridden = apply_overlay()
    overlay_meta = [rel for rel in copied if rel.endswith(".pw.toml") and rel.split("/", 1)[0] in METADATA_DIRS]
    replaced = drop_replaced_upstream(overlay_meta)
    stamp_version(cfg, version)

    log("packwiz refresh")
    packwiz(["refresh"])

    return {
        "removed": removed,
        "patches": patches,
        "replaced": replaced,
        "overlay_mods": [read_metadata(PACK_DIR / rel) for rel in overlay_meta],
        "overlay_files": [
            rel + (" (ersetzt Craftoria-Datei)" if rel in overridden else "")
            for rel in copied if rel not in overlay_meta
        ],
    }


# --- Server-Dateien ------------------------------------------------------------------------

def yaml_set(text, key, value):
    text, count = re.subn(rf"^(\s*{key}:)[^\r\n]*", lambda m: f"{m.group(1)} {value}", text, count=1, flags=re.M)
    if count == 0:
        warn(f"{SERVER_CONFIG}: '{key}' nicht gefunden")
    return text


def yaml_append(text, key, items):
    if not items:
        return text
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        match = re.match(rf"^(\s*){key}:\s*$", line)
        if not match:
            continue
        newline = "\r\n" if line.endswith("\r\n") else "\n"
        key_indent = len(match.group(1))
        item_indent = match.group(1) + "  "
        end = i + 1
        while end < len(lines):
            stripped = lines[end].lstrip()
            indent = len(lines[end]) - len(stripped)
            if not stripped.startswith("- ") or indent <= key_indent:
                break
            item_indent = lines[end][:indent]
            end += 1
        lines[end:end] = [f"{item_indent}- {item}{newline}" for item in items]
        return "".join(lines)
    warn(f"{SERVER_CONFIG}: Liste '{key}' nicht gefunden, {len(items)} Einträge fehlen")
    return text


def zip_dir(src, dest):
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(src.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(src).as_posix())


def build_server(cfg, version, client_zip, info):
    if SERVER_DIR.exists():
        shutil.rmtree(SERVER_DIR)
    shutil.copytree(PACK_DIR / "server_files", SERVER_DIR)

    if cfg["server"]["modpackSource"] == "bundled":
        # Für private Repos: Client-ZIP liegt neben dem ServerStarter, der sie per file:// liest
        shutil.copy2(client_zip, SERVER_DIR / client_zip.name)
        modpack_url = f"file://{client_zip.name}"
    else:
        modpack_url = f"https://github.com/{github_repo(cfg)}/releases/download/{version}/{client_zip.name}"

    # Client-only Mods aus dem Overlay haben auf dem Server nichts verloren
    client_only = [mod for mod in info["overlay_mods"] if mod["side"] == "client"]

    config = SERVER_DIR / SERVER_CONFIG
    text = read_text(config)
    text = yaml_set(text, "name", json.dumps(cfg["name"], ensure_ascii=False))
    text = yaml_set(text, "description", json.dumps(cfg["description"], ensure_ascii=False))
    text = yaml_set(text, "modpackUrl", modpack_url)
    text = yaml_append(text, "ignoreProject", [f"{mod['cf']} # {mod['name']}" for mod in client_only if mod["cf"]])
    text = yaml_append(text, "ignoreFiles", [f"mods/{mod['filename']}" for mod in client_only if not mod["cf"] and mod["filename"]])
    write_text(config, text)

    server_zip = DIST / f"{cfg['name']}-Server-{version}.zip"
    zip_dir(SERVER_DIR, server_zip)
    return server_zip


def changelog(cfg, lock, info):
    # Vergleicht mit dem letzten Release-Tag: (Tag, Änderungen an Craftoria/Mods, Commit-Betreffs)
    previous = git_output("describe", "--tags", "--abbrev=0", "--match", f"*-{cfg['versionSuffix']}.*", "HEAD")
    if not previous:
        return None, [], []
    previous = previous.strip()
    changes = []

    repo = lock["repo"]
    previous_lock = json.loads(git_output("show", f"{previous}:upstream.json") or "{}")
    if previous_lock and previous_lock["commit"] != lock["commit"]:
        changes.append(
            f"Craftoria {previous_lock['version']} → {lock['version']} "
            f"([Änderungen](https://github.com/{repo}/compare/{previous_lock['commit']}...{lock['commit']}))"
        )

    labels = {"A": "Neu", "M": "Aktualisiert", "D": "Entfernt"}
    diff = git_output("diff", "--name-status", "--no-renames", previous, "HEAD", "--", "overlay") or ""
    for line in diff.splitlines():
        status, path = line.split("\t", 1)
        rel = path[len("overlay/"):]
        if status not in labels or not rel.endswith(".pw.toml") or rel.split("/", 1)[0] not in METADATA_DIRS:
            continue
        text = git_output("show", f"{previous if status == 'D' else 'HEAD'}:{path}") or ""
        changes.append(f"{labels[status]}: {parse_metadata(text, Path(rel).name)['name']}")

    removed_names = {}
    for entry in info["removed"]:
        removed_names.setdefault(entry["pattern"], []).append(entry["name"])
    previous_patterns = removal_patterns(git_output("show", f"{previous}:remove.txt") or "")
    current_patterns = removal_patterns(read_text(REMOVE_FILE)) if REMOVE_FILE.exists() else []
    for pattern in current_patterns:
        if pattern not in previous_patterns:
            changes.append("Aus Craftoria entfernt: " + ", ".join(removed_names.get(pattern, [f"`{pattern}`"])))
    for pattern in previous_patterns:
        if pattern not in current_patterns:
            changes.append(f"Wieder wie in Craftoria: `{pattern}`")

    commits = (git_output("log", "--no-merges", "--format=%s", f"{previous}..HEAD") or "").splitlines()
    return previous, changes, commits


def player_facing(subject):
    # "feat(quests): neue Kapitel" wird zu "Neue Kapitel", technische Commits fallen ganz raus
    match = re.match(r"^(\w+)(\([^)]*\))?!?:\s*(.+)$", subject)
    if not match:
        return subject
    if match.group(1).lower() in TECHNICAL_COMMIT_TYPES:
        return None
    text = match.group(3)
    return text[0].upper() + text[1:]


def write_release_notes(cfg, lock, version, info, client_zip, server_zip):
    repo, commit = lock["repo"], lock["commit"]
    release_url = f"https://github.com/{github_repo(cfg)}/releases/tag/{version}"
    download_url = f"https://github.com/{github_repo(cfg)}/releases/download/{version}"
    intro = (
        f"Basiert auf **Craftoria {lock['version']}** "
        f"([{commit[:7]}](https://github.com/{repo}/commit/{commit}), "
        f"[Changelog](https://github.com/{repo}/blob/{commit}/changelogs/CHANGELOG.md))."
    )
    previous, changes, commits = changelog(cfg, lock, info)

    added_mods = [mod["name"] + (" (nur Client)" if mod["side"] == "client" else "") for mod in info["overlay_mods"]]
    removed = [entry["name"] for entry in info["removed"]]

    sections = [
        ("Downloads", [
            f"`{client_zip.name}`: Client, in Prism Launcher unter Instanz hinzufügen → Importieren",
            f"`{server_zip.name}`: Server, entpacken und `startserver.sh` bzw. `startserver.bat` starten",
        ]),
        (f"Änderungen seit {previous}", changes),
        (f"Commits seit {previous}", [f"`{subject}`" for subject in commits]),
        ("Zusätzliche Mods und Packs", added_mods),
        ("Ersetzt", [f"`{old}` durch `{new}`" for old, new in info["replaced"]]),
        ("Aus Craftoria entfernt", removed),
        ("Eigene Dateien", [f"`{rel}`" for rel in info["overlay_files"]]),
        ("Patches", [f"`{name}`" for name in info["patches"]]),
        ("Warnungen beim Build", warnings),
    ]

    lines = [intro]
    for title, items in sections:
        if items:
            lines.extend(["", f"### {title}"] + [f"- {item}" for item in items])
    write_text(DIST / "release-notes.md", "\n".join(lines) + "\n")

    # Discord: alles in einem Embed, nur der Ping steht im content (Erwähnungen in Embeds pingen nie).
    # Fett statt Überschriften und Mods in einer Zeile, damit die Nachricht ruhig und kompakt bleibt.
    news = changes + [text for text in map(player_facing, commits) if text]
    discord = [
        f"**[⬇️ Download für Prism Launcher]({download_url}/{client_zip.name})**",
        f"Basiert auf Craftoria {lock['version']} · "
        f"[Craftoria-Changelog](https://github.com/{repo}/blob/{commit}/changelogs/CHANGELOG.md)",
    ]
    if news:
        discord += ["", "**Was ist neu**"] + [f"- {item}" for item in news]
    if added_mods:
        discord += ["", "**Zusätzliche Mods**", " · ".join(added_mods)]
    if removed:
        discord += ["", "**Aus Craftoria entfernt**", " · ".join(removed)]
    description = "\n".join(discord)
    if len(description) > DISCORD_DESCRIPTION_LIMIT:
        description = description[:DISCORD_DESCRIPTION_LIMIT].rsplit("\n", 1)[0] + f"\n… [alles im Release]({release_url})"

    payload = {
        "username": cfg["name"],
        "allowed_mentions": {"parse": ["everyone", "roles"]},
        "embeds": [{
            "title": f"🎉 {cfg['name']} {version} ist da!",
            "url": release_url,
            "description": description,
            "color": 0x6AA84F,
            "footer": {
                "text": "Installieren: Prism Launcher → Instanz hinzufügen → Importieren → ZIP oder Download-Link · "
                        "Updaten: neue Version genauso importieren, options.txt aus der alten Instanz übernehmen",
            },
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }],
    }
    mention = cfg.get("discord", {}).get("mention", "")
    if mention:
        payload["content"] = mention
    save_json(BUILD / "discord-payload.json", payload)


# --- Befehle ---------------------------------------------------------------------------------

def cmd_build(args):
    cfg, lock = load_json(CONFIG_FILE), load_json(LOCK_FILE)
    version = args.version or (next_version(cfg, lock) if args.release else dev_version(cfg, lock))
    log(f"Baue {cfg['name']} {version}")
    info = assemble(cfg, lock, version)

    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir()
    client_zip = DIST / f"{cfg['name']}-{version}.zip"
    log("Exportiere Client-ZIP")
    packwiz(["curseforge", "export", "--output", client_zip])
    if not client_zip.exists():
        fail("packwiz hat keine Client-ZIP erzeugt")

    server_zip = build_server(cfg, version, client_zip, info)
    write_release_notes(cfg, lock, version, info, client_zip, server_zip)

    set_output("name", cfg["name"])
    set_output("version", version)
    log(f"Fertig: dist/{client_zip.name}, dist/{server_zip.name}")


def cmd_assemble(args):
    cfg, lock = load_json(CONFIG_FILE), load_json(LOCK_FILE)
    assemble(cfg, lock, dev_version(cfg, lock))
    log(f"Kombiniertes Pack liegt in {PACK_DIR.relative_to(ROOT)}")


def snapshot_metadata():
    return {
        path.relative_to(PACK_DIR).as_posix(): hashlib.sha1(path.read_bytes()).hexdigest()
        for path in metadata_files(PACK_DIR)
    }


def cmd_packwiz(packwiz_args):
    cfg, lock = load_json(CONFIG_FILE), load_json(LOCK_FILE)
    assemble(cfg, lock, dev_version(cfg, lock))

    before = snapshot_metadata()
    packwiz(packwiz_args, interactive=True)
    after = snapshot_metadata()

    for rel in sorted(after):
        if before.get(rel) != after[rel]:
            dest = OVERLAY_DIR / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(PACK_DIR / rel, dest)
            log(f"overlay/{rel} geschrieben")

    removals = []
    for rel in sorted(set(before) - set(after)):
        if (OVERLAY_DIR / rel).exists():
            (OVERLAY_DIR / rel).unlink()
            log(f"overlay/{rel} gelöscht")
        if (UPSTREAM_DIR / rel).exists():
            removals.append(rel)
    if removals:
        text = read_text(REMOVE_FILE) if REMOVE_FILE.exists() else ""
        if text and not text.endswith("\n"):
            text += "\n"
        write_text(REMOVE_FILE, text + "".join(rel + "\n" for rel in removals))
        for rel in removals:
            log(f"{rel} in remove.txt eingetragen")


def modpack_url_of(config_text):
    match = re.search(r"^\s*modpackUrl:\s*(\S+)", config_text, re.M)
    return match.group(1) if match else None


def cmd_check_upstream(args):
    lock = load_json(LOCK_FILE)
    repo, branch = lock["repo"], lock.get("branch", "main")

    def raw(ref, path):
        return http_get(f"https://raw.githubusercontent.com/{repo}/{ref}/{path}").decode("utf-8")

    # Craftorias Upload-Skript schreibt modpackUrl erst nach dem CurseForge-Upload um.
    # Der Commit, der die aktuelle URL eingeführt hat, ist damit der Stand des Releases.
    current_url = modpack_url_of(raw(branch, UPSTREAM_SERVER_CONFIG))
    commits = json.loads(http_get(
        f"https://api.github.com/repos/{repo}/commits?sha={branch}&path={UPSTREAM_SERVER_CONFIG}&per_page=50"
    ))
    release = None
    for commit in commits:
        parents = commit["parents"]
        if not parents or modpack_url_of(raw(parents[0]["sha"], UPSTREAM_SERVER_CONFIG)) != current_url:
            release = commit["sha"]
            break
    if not release:
        fail(f"Kein Commit gefunden, der modpackUrl auf {current_url} gesetzt hat")

    version = re.search(r'^version\s*=\s*"([^"]+)"', raw(release, "pack.toml"), re.M).group(1)
    if not current_url.endswith(f"-{version}.zip"):
        warn(f"modpackUrl {current_url} passt nicht zur Version {version} in pack.toml")

    set_output("version", version)
    set_output("commit", release)
    if release == lock["commit"]:
        log(f"Craftoria {version} ({release[:7]}) ist bereits eingetragen")
        set_output("changed", "false")
        return

    old_commit, old_version = lock["commit"], lock["version"]
    lock.update(commit=release, version=version, modpackUrl=current_url)
    save_json(LOCK_FILE, lock)

    BUILD.mkdir(exist_ok=True)
    write_text(BUILD / "upstream-pr.md", "\n".join([
        f"Craftoria hat ein neues Release: **{version}**",
        "",
        f"- Vorher: {old_version} ([{old_commit[:7]}](https://github.com/{repo}/commit/{old_commit}))",
        f"- Jetzt: {version} ([{release[:7]}](https://github.com/{repo}/commit/{release}))",
        f"- [Alle Änderungen in Craftoria](https://github.com/{repo}/compare/{old_commit}...{release})",
        f"- [Changelog](https://github.com/{repo}/blob/{release}/changelogs/CHANGELOG.md)",
        "",
        "Vor dem Mergen prüfen:",
        "- [ ] Der Build für diesen Branch ist grün (Patches passen, keine Warnungen zu remove.txt)",
        "- [ ] Eigene Mods laufen noch (Testserver mit dem Build-Artefakt starten)",
        "",
        "Nach dem Merge das Release unter Actions → Build → Run workflow (main) starten.",
        "",
    ]))
    set_output("changed", "true")
    log(f"Neues Craftoria-Release: {version} ({release[:7]})")


def cmd_notify_discord(args):
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        log("DISCORD_WEBHOOK_URL ist nicht gesetzt, keine Discord-Nachricht")
        return
    payload = BUILD / "discord-payload.json"
    if not payload.exists():
        fail("build/discord-payload.json fehlt, vorher build ausführen")

    request = urllib.request.Request(
        webhook + ("&" if "?" in webhook else "?") + "wait=true",
        data=payload.read_bytes(),
        headers={"Content-Type": "application/json", "User-Agent": "cozytoria-build"},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=30).close()
    except urllib.error.HTTPError as error:
        fail(f"Discord hat mit HTTP {error.code} geantwortet: {error.read().decode('utf-8', 'replace')[:500]}")
    except urllib.error.URLError as error:
        fail(f"Discord ist nicht erreichbar: {error.reason}")
    log("Release im Discord angekündigt")


def main():
    # packwiz-Argumente unverändert durchreichen, argparse würde sich an deren Flags verschlucken
    if len(sys.argv) > 1 and sys.argv[1] == "packwiz":
        if len(sys.argv) == 2:
            fail("Aufruf: cozytoria.py packwiz <packwiz-Argumente>, z. B. packwiz curseforge add jei")
        return run_guarded(cmd_packwiz, sys.argv[2:])

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="Client- und Server-ZIP nach dist/ bauen")
    build.add_argument("--release", action="store_true", help="nächste freie Versionsnummer aus den Git-Tags nehmen")
    build.add_argument("--version", help="Version explizit setzen")
    commands.add_parser("assemble", help="nur das kombinierte Pack in build/pack erzeugen")
    commands.add_parser("packwiz", help="packwiz auf dem kombinierten Pack ausführen")
    commands.add_parser("check-upstream", help="nach neuem Craftoria-Release suchen")
    commands.add_parser("notify-discord", help="zuletzt gebautes Release im Discord ankündigen")
    args = parser.parse_args()

    handlers = {
        "build": cmd_build,
        "assemble": cmd_assemble,
        "check-upstream": cmd_check_upstream,
        "notify-discord": cmd_notify_discord,
    }
    run_guarded(handlers[args.command], args)


def run_guarded(handler, args):
    try:
        handler(args)
    except subprocess.CalledProcessError as error:
        fail(f"Befehl fehlgeschlagen ({error.returncode}): {' '.join(map(str, error.cmd))}")


if __name__ == "__main__":
    main()
