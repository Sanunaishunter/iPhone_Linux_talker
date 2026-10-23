# iPhone Shortcuts setup (verified working 2026-10-24)

Run `talker info` on Ubuntu for the URLs. Get the token onto the phone without a trailing newline:

    printf %s "$(talker token)" | qrencode -t ansiutf8     # then scan with the iPhone camera

The iPhone needs Tailscale connected. Ubuntu needs: `sudo ufw allow in on tailscale0 to any port 8787 proto tcp`.

## Shortcut A: "Sent To Ubuntu" (iPhone -> Ubuntu)
One action, **Get Contents of URL**:
- URL: `http://<TAILSCALE_IP>:8787/push`
- Method: **POST**
- Headers: key `X-Token`, value = the token only (no `Bearer`)
- Request Body: **JSON**, one field: type Text, key `text` (lowercase), value = **Clipboard** variable
- Optional: add **Show Content** with input **Contents of URL** to see `ok` / the error.

JSON (not File) matters: copied text is often rich text, and a File body would send raw RTF.

## Shortcut B: "Get from Ubuntu" (Ubuntu -> iPhone)
1. **Get Contents of URL**: `http://<TAILSCALE_IP>:8787/pull`, Method **GET**, header `X-Token` = token, no body.
2. **Copy to Clipboard**: input = **Contents of URL**.

On Ubuntu, queue text first: `talker send` (clipboard), `talker send "text"`, or `echo hi | talker send`.

## Back Tap
Settings -> Accessibility -> Touch -> Back Tap: Double Tap -> Sent To Ubuntu, Triple Tap -> Get from Ubuntu.

## Optional Ubuntu keybinding
Settings -> Keyboard -> Custom Shortcuts: command `~/.local/bin/talker send`.

## Gotchas found while debugging
- **No trailing space in a header key.** iOS keyboard autocomplete can leave `X-Token ` (space before the
  colon); the server's HTTP parser then drops every header and replies 401 even with a correct token.
- Authorization header: also accepted (`Bearer <token>`), plus `?t=<token>` in the URL; X-Token is simplest.
- Local curl tests bypass the firewall; only a real phone request proves ufw allows tailscale0:8787.
- 401 with a correct token -> check `journalctl --user -u iphone-linux-talker` ("auth failed: header names=...").
- Rotate the token: delete ~/.config/iphone_linux_talker/token, run `talker init`, restart the service,
  then update the header in both shortcuts.
