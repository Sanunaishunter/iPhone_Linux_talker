#!/usr/bin/env bash
# Installs the GNOME Shell extension and marks it enabled. On Wayland a newly installed extension is only
# loaded after you log out and back in; later code changes also need a re-login.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
uuid="iphone-linux-talker@sanunaishunter.github.io"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
gnome-extensions pack --force --out-dir "$tmp" "$here/gnome-extension/$uuid"
gnome-extensions install --force "$tmp/$uuid.shell-extension.zip"
python3 - "$uuid" <<'PY'
import ast, subprocess, sys
uuid = sys.argv[1]
cur = subprocess.run(["gsettings", "get", "org.gnome.shell", "enabled-extensions"], capture_output=True, text=True).stdout.strip()
items = ast.literal_eval(cur.replace("@as ", "")) if cur else []
if uuid not in items:
    items.append(uuid)
    subprocess.run(["gsettings", "set", "org.gnome.shell", "enabled-extensions", str(items)], check=True)
    print("added to enabled-extensions")
PY
echo "Installed $uuid. Log out and back in to load it."
