#!/usr/bin/env bash
# Run an isolated headless GNOME Shell (own D-Bus session, own settings) that loads the installed extension,
# so it can be tested without logging out of the real desktop. Stop it with: pkill -f '^gnome-shell --headless'
#   tools/nested-shell.sh &            # then, once it is up (~12 s):
#   PID=$(pgrep -f '^gnome-shell --headless' | head -n1)
#   ADDR=$(tr '\0' '\n' < /proc/$PID/environ | grep ^DBUS_SESSION_BUS_ADDRESS= | cut -d= -f2-)
#   python3 tools/shell_harness.py "$ADDR" /tmp/shots scene.py   # scene.py uses shot()/click()/type_text()/pump()
set -euo pipefail
uuid="iphone-linux-talker@sanunaishunter.github.io"
t="$(mktemp -d)"; mkdir -p "$t/config/glib-2.0/settings"
ln -s "$HOME/.config/iphone_linux_talker" "$t/config/iphone_linux_talker"   # the extension reads the token from here
cat > "$t/config/glib-2.0/settings/keyfile" <<EOS
[org/gnome/shell]
enabled-extensions=['$uuid']
disable-user-extensions=false
welcome-dialog-last-shown-version='99.0'
EOS
export XDG_CONFIG_HOME="$t/config" GSETTINGS_BACKEND=keyfile
exec timeout "${NESTED_SECONDS:-600}" dbus-run-session -- gnome-shell --headless --wayland --no-x11 \
    --wayland-display=wayland-talkertest --virtual-monitor 1280x720
