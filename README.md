# iPhone_Linux_talker

A tiny text relay between an iPhone and an Ubuntu (GNOME Wayland) desktop, like a small Universal Clipboard.
iOS Shortcuts talk to a local HTTP service over Tailscale; a chat page on the desktop shows who sent what and
lets you type messages to the phone.

```
iPhone Shortcut --POST /push--> server --> wl-copy + notification + chat history
iPhone Shortcut <--GET /pull--- server <-- chat page / `talker send`
```

## Install
Ubuntu with GNOME on Wayland, and Tailscale on the Ubuntu machine and the iPhone. Then one command:

    ./install.sh

It installs the missing packages (`wl-clipboard`, `libnotify-bin`, `qrencode`), the server as a systemd user service,
the `talker` command, the ufw rule for `tailscale0`, and the GNOME top-bar extension, then prints the URLs and token
(plus a QR code of the token) for the Shortcuts. Safe to re-run to update. The top-bar icon appears after you log out
and back in. Options: `--no-extension`, `--uninstall`, `--purge` (also deletes the token and history).

Then build the two Shortcuts: see [shortcuts/README.md](shortcuts/README.md). `talker info` reprints the URLs and token.

## Use
- **iPhone -> Ubuntu**: run the "Sent To Ubuntu" Shortcut (e.g. Back Tap). The text lands in the Ubuntu clipboard.
- **Ubuntu -> iPhone**: `talker send [text]` or type in the chat page; run "Get from Ubuntu" on the phone to pull
  everything not yet picked up (the page shows "waiting" / "picked up").
- **Chat page**: `talker ui` (http://127.0.0.1:8787/). Last 50 messages, kept locally in
  `~/.local/state/iphone_linux_talker/messages.json` (mode 600). Settings (gear): popup style
  (system notification / in-page banner / both / off), popup seconds, auto-copy. Other devices can open the page
  over Tailscale and sign in with the token.
- `talker log`, `talker clear`, `talker token`.

## GNOME top-bar extension (optional)
A phone icon in the top bar opens the chat (history, type + Enter to send, settings), turns blue when there are
unread messages, and in popup style **桌面頂端列橫條 (bar)** slides a popup in under the top bar for the configured
number of seconds, above all windows, including fullscreen. Hover keeps it open; click opens the chat.
If the extension is not running, the `bar` style falls back to a normal system notification.

    ./install.sh                  # also installs the extension; log out and back in afterwards (Wayland loads new/changed extensions at login)

`tools/` has an isolated headless GNOME Shell + a screenshot/input harness for testing the extension without
logging out (see the comments in `tools/nested-shell.sh`).

## Security notes
- Listens only on the Tailscale IP and 127.0.0.1. A token is required for `/push` and `/pull`.
- The web page signs the local browser in with an HttpOnly SameSite=Strict cookie; web API writes must be JSON and
  the Host header must be an IP, localhost, or a tailnet name (DNS-rebinding guard).
- Request logs never contain message text or the token.

See [DESIGN.md](DESIGN.md) for the design notes and roadmap.
