#!/usr/bin/env bash
# Installs to ~/.local (the project may live on an external drive that is not always mounted).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
install -Dm755 "$here/server/talker_server.py" ~/.local/share/iphone_linux_talker/talker_server.py
install -Dm755 "$here/cli/talker" ~/.local/bin/talker
install -Dm644 "$here/systemd/iphone-linux-talker.service" ~/.config/systemd/user/iphone-linux-talker.service
~/.local/bin/talker init
systemctl --user daemon-reload
systemctl --user enable --now iphone-linux-talker.service
systemctl --user restart iphone-linux-talker.service
sleep 1
systemctl --user --no-pager status iphone-linux-talker.service | head -n 8
echo; ~/.local/bin/talker info
