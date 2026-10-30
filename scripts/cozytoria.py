#!/usr/bin/env python3
"""Build-Tooling für Cozytoria: Craftoria als Upstream plus unser Overlay.

  build [--release]   Komplettes Pack bauen: Client- und Server-ZIP in dist/
  assemble            Nur das kombinierte packwiz-Pack in build/pack erzeugen
  packwiz <args>      packwiz auf dem kombinierten Pack ausführen; neue oder geänderte
                      Metadaten landen in overlay/, entfernte Craftoria-Mods in remove.txt
  check-upstream      Nach einem neuen Craftoria-Release suchen und upstream.json anpassen
  notify-discord      Das zuletzt gebaute Release im Discord ankündigen (Webhook aus DISCORD_WEBHOOK_URL,
                      Ergebnis des Malware-Scans aus MALWARE_SCAN)
  test-server         Server aus dist/ installieren, starten und wieder stoppen (braucht bash und Java 21)
  fetch-mods          Alle Dateien aus den Metadaten in build/pack nach build/scan/files laden und Prüfsummen prüfen
  scan-clamav         Neue Dateien aus build/pack und build/scan/files mit ClamAV scannen (braucht Docker oder
                      clamscan), --full scannt alle mit den aktuellen Signaturen

Braucht nur Python 3.9+ und git, packwiz wird automatisch heruntergeladen.
"""

import argparse
import concurrent.futures
import hashlib
import io
import json
import os
import platform
import queue
import re
import shutil
import signal
import ssl
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.parse
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
TEST_SERVER_DIR = BUILD / "test-server"
SCAN_DIR = BUILD / "scan"
SCAN_FILES_DIR = SCAN_DIR / "files"
SCAN_INDEX = SCAN_DIR / "files.json"
CLAMAV_CACHE = SCAN_DIR / "clamav.json"
CLAMAV_TODO_DIR = SCAN_DIR / "todo"
OVERLAY_DIR = ROOT / "overlay"
PATCHES_DIR = ROOT / "patches"
REMOVE_FILE = ROOT / "remove.txt"
CONFIG_FILE = ROOT / "cozytoria.json"
LOCK_FILE = ROOT / "upstream.json"
SCAN_ALLOWLIST = ROOT / "scan-allowlist.txt"

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
# Diese Zeile schreibt der Minecraft-Server, sobald er komplett gestartet ist
SERVER_DONE_PATTERN = re.compile(r"Done \(\d+[.,]\d+s\)! For help")

CURSEFORGE_CDN = "https://mediafilez.forgecdn.net/files"
# Offizielles Image mit vorinstallierten Signaturen, die Debian-Variante gibt es auch für arm64
CLAMAV_IMAGE = "clamav/clamav:1.5-debian"
CLAMAV_FRESHCLAM_FAILED = "freshclam fehlgeschlagen"
CLAMAV_LOG_PREFIX = "clamd-log: "
# Im Image läuft der Daemon statt clamscan: Er lädt die Signaturen einmal und scannt mit einem Thread pro Kern,
# clamscan allein bräuchte für alle Mods weit über eine halbe Stunde. Die Signaturen im Image sind ein paar Tage alt,
# freshclam lädt nur die Unterschiede nach. Drosselt das CDN (kommt bei den geteilten Adressen von CI-Runnern vor),
# wird mit den Signaturen aus dem Image gescannt. ScanHTML normalisiert jede Textdatei, auch die Tausenden JSONs in
# den Jars, und kostet so ein Drittel der Zeit. Für Java-Malware ist es ohne Bedeutung.
CLAMAV_SCRIPT = f"""
freshclam --stdout --quiet || echo "{CLAMAV_FRESHCLAM_FAILED}"
cat >> /etc/clamav/clamd.conf <<EOF
MaxThreads $(nproc)
MaxFileSize 1024M
MaxScanSize 4096M
MaxFiles 200000
MaxRecursion 32
ScanHTML no
LogClean yes
EOF
clamd
clamdscan --ping 300:1 > /dev/null || {{ tail -n 20 /var/log/clamav/clamd.log; exit 2; }}
clamdscan --version
# Für den Fortschritt: clamdscan meldet bei einem Verzeichnis nur Funde, clamd protokolliert jede fertige Datei
tail -n 0 -F /var/log/clamav/clamd.log | sed -u "s/^/{CLAMAV_LOG_PREFIX}/" &
clamdscan --multiscan --fdpass "$@"
code=$?
sleep 1
exit $code
"""
# Für ein lokal installiertes clamscan. Ohne höhere Grenzen bricht ClamAV bei großen Jars (über 100 MB oder
# 10000 Dateien) mittendrin ab, im Image stehen dieselben Grenzen in der clamd.conf.
CLAMSCAN_OPTIONS = ["--recursive", "--infected", "--max-filesize=1024M", "--max-scansize=4096M",
                    "--max-files=200000", "--max-recursion=32", "--scan-html=no"]

IN_CI = os.environ.get("GITHUB_ACTIONS") == "true"
warnings = []
curl_fallback_logged = False


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


def run(cmd, cwd=None, env=None, capture=False, capture_bytes=False):
    result = subprocess.run(
        [str(c) for c in cmd], cwd=cwd, env=env, check=True, text=not capture_bytes,
        stdout=subprocess.PIPE if capture or capture_bytes else None,
    )
    if capture_bytes:
        return result.stdout
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


def ssl_context():
    # Wie ssl.create_default_context(), aber ohne SSLKEYLOGFILE. Zeigt die Variable auf eine Datei, die
    # Python nicht öffnen darf (kommt auf manchen Windows-PCs vor), scheitert sonst jeder Download.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_default_certs()
    return context


def system_curl():
    # Das curl des Betriebssystems vertraut denselben Zertifikaten wie der Browser (unter Windows über Schannel)
    if platform.system() == "Windows":
        path = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe"
        return str(path) if path.exists() else None
    return shutil.which("curl")


def http_get(url):
    headers = {"User-Agent": "cozytoria-build"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=120, context=ssl_context()) as response:
            return response.read()
    except urllib.error.URLError as error:
        curl = system_curl()
        if not isinstance(error.reason, ssl.SSLCertVerificationError) or not curl:
            raise

    # Python kennt die Zertifizierungsstelle nicht, z. B. bei msys2-Python oder einem Virenscanner mit
    # HTTPS-Prüfung. curl prüft das Zertifikat weiterhin, nur gegen den Zertifikatsspeicher des Systems.
    global curl_fallback_logged
    if not curl_fallback_logged:
        log("Python vertraut dem Zertifikat nicht, lade mit dem curl des Systems")
        curl_fallback_logged = True
    return run([curl, "--fail", "--silent", "--show-error", "--location", "--max-time", "300",
                "--user-agent", headers["User-Agent"], url], capture_bytes=True)


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
    def field(key):
        # Strings stehen in "…" oder (von Prism geschrieben) in '…', Zahlen ohne Anführungszeichen
        match = re.search(rf"""^{key}\s*=\s*(?:"([^"]*)"|'([^']*)'|(\d+))""", text, re.M)
        return next((group for group in match.groups() if group), None) if match else None

    curseforge = "[update.curseforge]" in text
    return {
        "name": field("name") or fallback_name,
        "filename": field("filename"),
        "side": field("side") or "both",
        "cf": field("project-id") if curseforge else None,
        "cf_file": field("file-id") if curseforge else None,
        "mr": field("mod-id") if "[update.modrinth]" in text else None,
        "url": field("url"),
        "hash_format": field("hash-format"),
        "hash": field("hash"),
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


def craftoria_jars(removed):
    # Jar-Dateiname -> Metadaten-Pfad aller Mods, die Craftoria mitbringt und die nicht per remove.txt raus sind
    removed_paths = {entry["path"] for entry in removed}
    jars = {}
    for path in metadata_files(UPSTREAM_DIR):
        rel = path.relative_to(UPSTREAM_DIR).as_posix()
        if rel not in removed_paths:
            jars[read_metadata(path)["filename"]] = rel
    return jars


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

    # Update-Button im Hauptmenü: Craftoria vergleicht die lokale version_info.json mit der aus seinem Repo, wir mit
    # unserem neuesten Release. Die API antwortet direkt, FancyMenu folgt keinen Weiterleitungen, ein Release-Asset
    # unter releases/latest/download/ ginge also nicht. tag_name ist die Version, html_url die Release-Seite.
    if fancymenu.exists():
        latest = f"https://api.github.com/repos/{github_repo(cfg)}/releases/latest"
        fields = {"version": "tag_name", "version_url": "html_url"}
        text, count = re.subn(
            r'"source":"https://raw\.githubusercontent\.com/[^"]*/version_info\.json","json_path":"\$\.(version_url|version)"',
            lambda m: f'"source":"{latest}","json_path":"$.{fields[m.group(1)]}"',
            read_text(fancymenu),
        )
        if count == 0 or "/version_info.json" in text.replace('"short_path":"version_info.json"', ""):
            warn("Update-Button im Hauptmenü nicht (vollständig) auf unsere Releases umgestellt, Craftoria hat ihn geändert")
        write_text(fancymenu, text)


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
    jars = craftoria_jars(removed)
    for rel in overlay_meta:
        jar = read_metadata(PACK_DIR / rel)["filename"]
        if jar in jars:
            warn(f"overlay/{rel} enthält dasselbe Jar wie Craftoria ({jar}) und hält dessen Version fest, besser löschen")
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

    # Server-only Mods lässt packwiz aus der Client-ZIP weg, der ServerStarter lädt sie nach dem Pack direkt herunter
    additional_files = []
    for mod in info["overlay_mods"]:
        if mod["side"] != "server":
            continue
        url = download_url(mod)
        if not url or not mod["filename"]:
            fail(f"{mod['name']} hat side = \"server\", aber die Metadaten nennen keine Download-Adresse")
        destination = json.dumps(f"mods/{mod['filename']}", ensure_ascii=False)
        additional_files.append(f"{{url: {json.dumps(url, ensure_ascii=False)}, destination: {destination}}}")

    config = SERVER_DIR / SERVER_CONFIG
    text = read_text(config)
    text = yaml_set(text, "name", json.dumps(cfg["name"], ensure_ascii=False))
    text = yaml_set(text, "description", json.dumps(cfg["description"], ensure_ascii=False))
    text = yaml_set(text, "modpackUrl", modpack_url)
    text = yaml_append(text, "ignoreProject", [f"{mod['cf']} # {mod['name']}" for mod in client_only if mod["cf"]])
    text = yaml_append(text, "ignoreFiles", [f"mods/{mod['filename']}" for mod in client_only if not mod["cf"] and mod["filename"]])
    text = yaml_append(text, "additionalFiles", additional_files)
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

    side_notes = {"client": " (nur Client)", "server": " (nur Server)"}
    added_mods = [mod["name"] + side_notes.get(mod["side"], "") for mod in info["overlay_mods"]]
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


# --- Malware-Scan ----------------------------------------------------------------------------

def download_url(meta):
    if meta["url"]:
        return meta["url"]
    if meta["cf_file"] and meta["filename"]:
        # Dieselbe CDN-Adresse, die CurseForge selbst ausliefert, braucht keinen API-Key
        file_id = int(meta["cf_file"])
        return f"{CURSEFORGE_CDN}/{file_id // 1000}/{file_id % 1000}/{urllib.parse.quote(meta['filename'])}"
    return None


def hash_matches(data, meta):
    # None, wenn sich die Prüfsumme nicht prüfen lässt
    algorithm = (meta["hash_format"] or "").lower()
    if not meta["hash"] or algorithm not in hashlib.algorithms_available:
        return None
    return hashlib.new(algorithm, data).hexdigest() == meta["hash"].lower()


def download_with_retry(url, attempts=3):
    for attempt in range(1, attempts + 1):
        try:
            return http_get(url)
        except urllib.error.HTTPError as error:
            if attempt == attempts or (error.code < 500 and error.code != 429):
                raise
        except (urllib.error.URLError, OSError, subprocess.CalledProcessError):
            if attempt == attempts:
                raise
        time.sleep(5 * attempt)


def fetch_scan_file(meta, dest):
    # Liefert (Herkunft, SHA-256), die Datei liegt danach geprüft in dest
    if dest.exists():
        data = dest.read_bytes()
        if hash_matches(data, meta):
            return "cache", hashlib.sha256(data).hexdigest()
    url = download_url(meta)
    if not url:
        raise ValueError("keine Download-Adresse in den Metadaten")
    data = download_with_retry(url)
    if hash_matches(data, meta) is False:
        raise ValueError(f"Prüfsumme passt nicht zu den Metadaten, die Datei von {url} wurde verändert")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return "geladen", hashlib.sha256(data).hexdigest()


def describe_error(error):
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}"
    if isinstance(error, urllib.error.URLError):
        return str(error.reason)
    return str(error)


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan_index():
    if not SCAN_INDEX.exists():
        fail("build/scan/files.json fehlt, vorher fetch-mods ausführen")
    return load_json(SCAN_INDEX)


def load_allowlist():
    allowed = set()
    if not SCAN_ALLOWLIST.exists():
        return allowed
    for number, line in enumerate(read_text(SCAN_ALLOWLIST).splitlines(), 1):
        value = line.split("#", 1)[0].strip().lower()
        if not value:
            continue
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            fail(f"{SCAN_ALLOWLIST.name}, Zeile {number}: '{value}' ist kein SHA-256")
        allowed.add(value)
    return allowed


def write_summary(lines):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n\n")


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
    info = assemble(cfg, lock, dev_version(cfg, lock))
    jars = craftoria_jars(info["removed"])

    before = snapshot_metadata()
    packwiz(packwiz_args, interactive=True)
    after = snapshot_metadata()

    for rel in sorted(after):
        if before.get(rel) == after[rel]:
            continue
        dest = OVERLAY_DIR / rel
        jar = read_metadata(PACK_DIR / rel)["filename"]
        if jar in jars:
            # Dasselbe Jar wie in Craftoria, meist eine Abhängigkeit, die packwiz von der anderen Plattform neu
            # geholt hat. Im Overlay würde sie Craftorias Version dauerhaft festnageln.
            if dest.exists():
                dest.unlink()
                log(f"overlay/{rel} gelöscht")
            warn(f"{rel} nicht ins Overlay übernommen: {jar} kommt schon aus Craftoria ({jars[jar]})")
            continue
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


def print_crash_report():
    reports = sorted((TEST_SERVER_DIR / "crash-reports").glob("*.txt"), key=lambda path: path.stat().st_mtime)
    if reports:
        log(f"--- Anfang von crash-reports/{reports[-1].name} ---")
        log("\n".join(read_text(reports[-1]).splitlines()[:80]))


def cmd_test_server(args):
    if os.name == "nt":
        fail("Der Servertest läuft nur unter Linux und macOS")
    if not shutil.which("bash") or not shutil.which("java"):
        fail("Der Servertest braucht bash und Java 21 im PATH")
    cfg = load_json(CONFIG_FILE)
    server_zips = sorted(DIST.glob(f"{cfg['name']}-Server-*.zip"))
    client_zips = sorted(set(DIST.glob(f"{cfg['name']}-*.zip")) - set(server_zips))
    if len(server_zips) != 1 or len(client_zips) != 1:
        fail("dist/ braucht genau eine Client- und eine Server-ZIP, vorher build ausführen")
    client_zip = client_zips[0]

    if TEST_SERVER_DIR.exists():
        shutil.rmtree(TEST_SERVER_DIR)
    TEST_SERVER_DIR.mkdir(parents=True)
    with zipfile.ZipFile(server_zips[0]) as zf:
        zf.extractall(TEST_SERVER_DIR)
    # Die lokale Client-ZIP statt des Release-Downloads, so lassen sich auch unveröffentlichte Builds testen
    shutil.copy2(client_zip, TEST_SERVER_DIR / client_zip.name)
    config = TEST_SERVER_DIR / SERVER_CONFIG
    write_text(config, yaml_set(read_text(config), "modpackUrl", f"file://{client_zip.name}"))
    # Der ServerStarter erwartet die Zustimmung in der dritten Zeile
    write_text(TEST_SERVER_DIR / "eula.txt", "#Servertest von Cozytoria\n#\neula=true\n")

    log(f"Starte Testserver mit {client_zip.name} (Zeitlimit {args.timeout} Minuten)")
    # Eigene Prozessgruppe, damit bei einem Timeout auch der eigentliche Minecraft-Prozess beendet wird
    process = subprocess.Popen(
        ["bash", "startserver.sh"], cwd=TEST_SERVER_DIR, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, errors="replace", start_new_session=True,
    )
    lines = queue.Queue()

    def pump():
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()

    started = False
    deadline = time.monotonic() + args.timeout * 60
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            os.killpg(process.pid, signal.SIGKILL)
            if started:
                warn("Server hat sich nach stop nicht innerhalb von 5 Minuten beendet und wurde abgeschossen")
                break
            print_crash_report()
            fail(f"Server war nach {args.timeout} Minuten noch nicht gestartet")
        try:
            line = lines.get(timeout=min(remaining, 5))
        except queue.Empty:
            continue
        if line is None:
            break
        print(line, end="", flush=True)
        if not started and SERVER_DONE_PATTERN.search(line):
            started = True
            deadline = time.monotonic() + 5 * 60
            log("Server ist gestartet, schicke stop")
            try:
                process.stdin.write("stop\n")
                process.stdin.flush()
            except BrokenPipeError:
                pass

    process.wait()
    if not started:
        print_crash_report()
        fail("Server hat sich beendet, bevor er fertig gestartet war")
    log("Servertest bestanden")


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
    path = BUILD / "discord-payload.json"
    if not path.exists():
        fail("build/discord-payload.json fehlt, vorher build ausführen")
    payload = load_json(path)
    # Der Scan läuft parallel zum Build, sein Ergebnis kommt erst hier dazu
    scan = os.environ.get("MALWARE_SCAN", "").strip()
    if scan:
        payload["embeds"][0]["description"] += f"\n\n🛡️ Malware-Scan mit ClamAV: {scan}"

    request = urllib.request.Request(
        webhook + ("&" if "?" in webhook else "?") + "wait=true",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "cozytoria-build"},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=30, context=ssl_context()).close()
    except urllib.error.HTTPError as error:
        fail(f"Discord hat mit HTTP {error.code} geantwortet: {error.read().decode('utf-8', 'replace')[:500]}")
    except urllib.error.URLError as error:
        fail(f"Discord ist nicht erreichbar: {error.reason}")
    log("Release im Discord angekündigt")


def cmd_fetch_mods(args):
    if not (PACK_DIR / "pack.toml").exists():
        fail("build/pack fehlt, vorher assemble oder build ausführen")
    jobs = {}
    for path in metadata_files(PACK_DIR):
        rel = path.relative_to(PACK_DIR)
        meta = read_metadata(path)
        if not meta["filename"]:
            warn(f"{rel.as_posix()} nennt keine Datei und wird nicht gescannt")
            continue
        if hash_matches(b"", meta) is None:
            warn(f"{rel.as_posix()}: Prüfsumme ({meta['hash_format']}) lässt sich nicht prüfen")
        jobs[SCAN_FILES_DIR / rel.parent / meta["filename"]] = (rel.as_posix(), meta)

    log(f"Lade {len(jobs)} Dateien aus den Metadaten nach {SCAN_FILES_DIR.relative_to(ROOT)}")
    results, errors = {}, []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_scan_file, meta, dest): (dest, rel) for dest, (rel, meta) in jobs.items()}
        for future in concurrent.futures.as_completed(futures):
            dest, rel = futures[future]
            try:
                results[dest] = future.result()
            except (ValueError, urllib.error.URLError, OSError, subprocess.CalledProcessError) as error:
                errors.append(f"{rel}: {describe_error(error)}")

    # Dateien, die nicht mehr im Pack sind, fliegen raus, damit Scan und Cache nur den aktuellen Stand sehen
    if SCAN_FILES_DIR.exists():
        for path in sorted(SCAN_FILES_DIR.rglob("*"), reverse=True):
            if path.is_file() and path not in jobs:
                path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()

    if errors:
        fail(f"{len(errors)} Dateien konnten nicht geprüft geladen werden:\n" + "\n".join(sorted(errors)))

    index = [
        {
            "file": dest.relative_to(ROOT).as_posix(),
            "name": meta["name"],
            "sha256": results[dest][1],
        }
        for dest, (_, meta) in sorted(jobs.items())
    ]
    save_json(SCAN_INDEX, index)
    loaded = sum(1 for origin, _ in results.values() if origin == "geladen")
    log(f"Fertig: {loaded} geladen, {len(results) - loaded} aus dem Cache, alle Prüfsummen stimmen")


def cmd_scan_clamav(args):
    index = {entry["file"]: entry for entry in scan_index()}
    allowed = load_allowlist()
    # SHA-256 -> Datum aller Dateien, die schon einmal sauber waren. Builds scannen nur, was dort fehlt, also meist
    # nur neue Jars. --full (nächtlicher Lauf oder von Hand) scannt alles mit den aktuellen Signaturen und baut die
    # Liste neu auf, eine Datei mit Fund fliegt damit raus und wird ab dann bei jedem Build wieder gescannt.
    clean = {} if args.full or not CLAMAV_CACHE.exists() else load_json(CLAMAV_CACHE)

    # Die zu scannenden Dateien kommen als Hardlinks in ein eigenes Verzeichnis, durch das clamd parallel läuft
    if CLAMAV_TODO_DIR.exists():
        shutil.rmtree(CLAMAV_TODO_DIR)
    todo, total = {}, 0
    for name, root in {"pack": PACK_DIR, "files": SCAN_FILES_DIR}.items():
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            total += 1
            rel = path.relative_to(ROOT).as_posix()
            sha = index[rel]["sha256"] if rel in index else sha256_of(path)
            if sha in clean:
                continue
            link = CLAMAV_TODO_DIR / name / path.relative_to(root)
            link.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(path, link)
            except OSError:
                shutil.copy2(path, link)
            todo[link] = (path, sha)

    if not todo:
        result = f"keine neuen Dateien, alle {total} waren schon sauber"
        write_summary(["### ClamAV", result[0].upper() + result[1:] + "."])
        log(f"ClamAV: {result}")
        set_output("result", result)
        return

    scope = f"alle {total} Dateien" if len(todo) == total else f"{len(todo)} neue Datei" + ("en" if len(todo) > 1 else "")
    if shutil.which("docker"):
        cmd = ["docker", "run", "--rm", "--entrypoint", "sh", "--volume", f"{CLAMAV_TODO_DIR}:/scan:ro",
               CLAMAV_IMAGE, "-c", CLAMAV_SCRIPT, "sh", "/scan"]
        log(f"Scanne {scope} aus build/pack und build/scan/files mit {CLAMAV_IMAGE}")
    elif shutil.which("clamscan"):
        cmd = ["clamscan", *CLAMSCAN_OPTIONS, CLAMAV_TODO_DIR]
        log(f"Scanne {scope} aus build/pack und build/scan/files mit dem installierten clamscan")
    else:
        fail("Der ClamAV-Scan braucht Docker oder clamscan im PATH")

    def original(path):
        link = CLAMAV_TODO_DIR / path[len("/scan/"):] if path.startswith("/scan/") else Path(path)
        return todo.get(link, (link, None))

    def shown(path):
        return path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)

    process = subprocess.Popen(
        [str(c) for c in cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
    )
    hits, scanned, started = [], 0, time.monotonic()
    for line in process.stdout:
        line = line.rstrip("\r\n")
        if line.startswith(CLAMAV_LOG_PREFIX):
            # clamd protokolliert jede fertige Datei als "<Datum> -> <Pfad>: <Ergebnis>". Angezeigt werden nur
            # Jars und Zips, die dauern, die Tausenden Configs aus Craftoria würden das Log fluten.
            entry = re.search(r" -> (.+?): (?:OK|Empty file|.+ FOUND)$", line)
            if not entry:
                print(line[len(CLAMAV_LOG_PREFIX):], flush=True)
                continue
            scanned += 1
            path = original(entry.group(1))[0]
            if path.suffix in (".jar", ".zip"):
                elapsed = int(time.monotonic() - started)
                log(f"[{scanned:>{len(str(len(todo)))}}/{len(todo)} {elapsed // 60}:{elapsed % 60:02d}] {shown(path)}")
            continue
        if line == "/scan: OK":
            # Das Gesamtergebnis für das Verzeichnis, steht gleich darunter noch einmal in der Zusammenfassung
            continue
        print(line, flush=True)
        if line == CLAMAV_FRESHCLAM_FAILED:
            warn("freshclam konnte die Signaturen nicht aktualisieren, gescannt wird mit denen aus dem Image")
        # Funde zählen nur aus der Ausgabe von clamdscan selbst, das Protokoll könnte am Ende Zeilen verlieren
        found = re.match(r"^(.+?): (.+) FOUND$", line)
        if found:
            hits.append((*original(found.group(1)), found.group(2)))
    code = process.wait()
    shutil.rmtree(CLAMAV_TODO_DIR)

    blocking = []
    for path, sha, signature in hits:
        rel = shown(path)
        sha = sha or sha256_of(path)
        label = f"{index[rel]['name']} (`{rel}`)" if rel in index else f"`{rel}`"
        if sha in allowed:
            log(f"Freigegeben in {SCAN_ALLOWLIST.name}: {rel} ({signature})")
        else:
            blocking.append(f"{label}: {signature}, SHA-256 {sha}")
    # Bei einem Fehler weiß man nicht, welche Dateien wirklich gescannt wurden, dann bleibt die Liste, wie sie war
    if code in (0, 1) and (code == 0 or hits):
        today = datetime.now(timezone.utc).date().isoformat()
        hit_shas = {sha for _, sha, _ in hits}
        clean.update({sha: today for _, sha in todo.values() if sha not in hit_shas})
    SCAN_DIR.mkdir(parents=True, exist_ok=True)
    save_json(CLAMAV_CACHE, clean)

    if blocking:
        write_summary(["### ClamAV", "**Funde:**"] + [f"- {text}" for text in blocking])
        fail(f"ClamAV hat {len(blocking)} verdächtige Dateien gefunden:\n" + "\n".join(blocking))
    if code == 1 and not hits:
        fail("ClamAV meldet Funde, die sich keiner Datei zuordnen lassen, siehe Ausgabe oben")
    if code not in (0, 1):
        fail(f"ClamAV ist mit Code {code} abgebrochen, siehe Ausgabe oben")

    result = f"{scope} gescannt" + ("" if len(todo) == total else f" ({total - len(todo)} waren schon sauber)")
    result += ", " + ("nur freigegebene Fehlalarme" if hits else "keine Funde")
    write_summary(["### ClamAV", result[0].upper() + result[1:] + "."])
    log(f"ClamAV: {result}")
    # Für die Ankündigung im Discord
    set_output("result", result)


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
    test_server = commands.add_parser("test-server", help="Server aus dist/ installieren, starten und stoppen")
    test_server.add_argument("--timeout", type=float, default=40, help="Minuten bis zum fertigen Start (Standard 40)")
    commands.add_parser("fetch-mods", help="alle Dateien aus den Metadaten zum Scannen laden")
    clamav = commands.add_parser("scan-clamav", help="neue Dateien aus Pack und Downloads mit ClamAV scannen")
    clamav.add_argument("--full", action="store_true", help="alle Dateien scannen, auch die schon sauberen")
    args = parser.parse_args()

    handlers = {
        "build": cmd_build,
        "assemble": cmd_assemble,
        "check-upstream": cmd_check_upstream,
        "notify-discord": cmd_notify_discord,
        "test-server": cmd_test_server,
        "fetch-mods": cmd_fetch_mods,
        "scan-clamav": cmd_scan_clamav,
    }
    run_guarded(handlers[args.command], args)


def run_guarded(handler, args):
    try:
        handler(args)
    except subprocess.CalledProcessError as error:
        fail(f"Befehl fehlgeschlagen ({error.returncode}): {' '.join(map(str, error.cmd))}")
    except urllib.error.URLError as error:
        fail(f"Download fehlgeschlagen: {error.reason}")


if __name__ == "__main__":
    main()
