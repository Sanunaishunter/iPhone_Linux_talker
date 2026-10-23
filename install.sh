#!/usr/bin/env bash
# One-command installer for iPhone_Linux_talker (Ubuntu / GNOME Wayland). Safe to re-run: it updates in place.
#
#   ./install.sh                  install or update everything
#   ./install.sh --no-extension   skip the GNOME top-bar extension
#   ./install.sh --uninstall      remove the service, CLI and extension (keeps token and history)
#   ./install.sh --purge          like --uninstall, and also delete the token and message history
#
# Installs to ~/.local (the project may live on an external drive that is not always mounted).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
uuid="iphone-linux-talker@sanunaishunter.github.io"
port="${TALKER_PORT:-8787}"
app=~/.local/share/iphone_linux_talker
unit=~/.config/systemd/user/iphone-linux-talker.service

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m!! %s\033[0m\n' "$*"; }

with_extension=1
case "${1:-}" in
    "") ;;
    --no-extension) with_extension=0 ;;
    --uninstall|--purge)
        step "Removing iPhone_Linux_talker"
        systemctl --user disable --now iphone-linux-talker.service 2>/dev/null || true
        rm -rf "$app" ~/.local/bin/talker "$unit"
        systemctl --user daemon-reload
        gnome-extensions uninstall "$uuid" 2>/dev/null || true
        if [ "$1" = "--purge" ]; then
            rm -rf ~/.config/iphone_linux_talker ~/.local/state/iphone_linux_talker
            echo "Deleted token and message history."
        else
            echo "Kept the token and history (use --purge to delete them)."
        fi
        echo "Done. If the ufw rule for port $port was added, remove it with: sudo ufw delete allow in on tailscale0 to any port $port proto tcp"
        exit 0 ;;
    -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
esac

[ "$(id -u)" -ne 0 ] || { echo "Run as your normal user, not root (sudo is asked for only when needed)." >&2; exit 1; }

# ---- 1. dependencies --------------------------------------------------------
step "Checking dependencies"
missing=()
command -v python3    >/dev/null || missing+=(python3)
command -v wl-copy    >/dev/null || missing+=(wl-clipboard)
command -v notify-send >/dev/null || missing+=(libnotify-bin)
command -v qrencode   >/dev/null || missing+=(qrencode)   # only used to show the token as a QR code for the phone
if [ "${#missing[@]}" -gt 0 ]; then
    echo "Installing: ${missing[*]}"
    if command -v apt-get >/dev/null; then
        sudo apt-get install -y "${missing[@]}"
    else
        warn "No apt-get found; install these yourself: ${missing[*]}"
    fi
else
    echo "All present."
fi

# ---- 2. program files, token, service ---------------------------------------
step "Installing the service and the 'talker' command"
install -Dm755 "$here/server/talker_server.py" "$app/talker_server.py"
install -Dm644 "$here/server/accounting_bridge.py" "$app/accounting_bridge.py"
install -Dm644 "$here/server/ui.html" "$app/ui.html"
install -Dm755 "$here/cli/talker" ~/.local/bin/talker
install -Dm644 "$here/systemd/iphone-linux-talker.service" "$unit"
~/.local/bin/talker init
systemctl --user daemon-reload
systemctl --user enable iphone-linux-talker.service >/dev/null 2>&1
systemctl --user restart iphone-linux-talker.service
sleep 1
if systemctl --user is-active --quiet iphone-linux-talker.service; then
    echo "Service is running."
else
    warn "Service failed to start. Details: journalctl --user -u iphone-linux-talker -n 30"
fi
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) warn "~/.local/bin is not on your PATH; open a new terminal or add it to ~/.profile." ;; esac

# ---- 3. Tailscale + firewall ------------------------------------------------
step "Checking Tailscale and the firewall"
ts_ip=""
if command -v tailscale >/dev/null; then
    ts_ip="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
    [ -n "$ts_ip" ] || warn "Tailscale is installed but not connected. Run: sudo tailscale up"
else
    warn "Tailscale is not installed. Install it from https://tailscale.com/download, run 'sudo tailscale up', then re-run ./install.sh"
fi
if command -v ufw >/dev/null; then
    rule="ufw allow in on tailscale0 to any port $port proto tcp"
    if ufw_status="$(sudo ufw status 2>/dev/null)"; then
        if grep -q "Status: active" <<<"$ufw_status"; then
            sudo $rule >/dev/null
            echo "ufw: allowed port $port on tailscale0."
        else
            echo "ufw is not active; nothing to open."
        fi
    else
        warn "Could not read the ufw status (sudo needed). If ufw is on, run: sudo $rule"
    fi
fi

# ---- 4. GNOME Shell extension -----------------------------------------------
if [ "$with_extension" = 1 ]; then
    step "Installing the GNOME top-bar extension"
    if command -v gnome-extensions >/dev/null; then
        bash "$here/install-extension.sh"
    else
        warn "gnome-extensions not found; skipping the extension (use --no-extension to silence this)."
    fi
fi

# ---- 5. what to do on the phone ---------------------------------------------
step "Done. Connection details for the iPhone Shortcuts"
~/.local/bin/talker info
if command -v qrencode >/dev/null; then
    printf '\nToken as a QR code (scan with the iPhone camera, then copy it into the Shortcuts):\n'
    printf %s "$(~/.local/bin/talker token)" | qrencode -t ansiutf8
fi
echo
echo "Next: build the two Shortcuts, see shortcuts/README.md"
[ "$with_extension" = 1 ] && echo "The top-bar icon appears after you log out and back in (GNOME on Wayland loads new extensions at login)."
exit 0
