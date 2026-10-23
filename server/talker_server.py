#!/usr/bin/env python3
"""iPhone_Linux_talker server: relay text between an iPhone (Shortcuts) and the Wayland clipboard.

POST /push  (Bearer token)  body = raw text or {"text": "..."}  -> wl-copy + desktop notification
GET  /pull  (Bearer token)  returns the latest text queued by `talker send` (empty body if none)
GET  /health                unauthenticated liveness probe, no data
"""
import hmac
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

CONF_DIR = Path(os.environ.get("TALKER_CONF", Path.home() / ".config/iphone_linux_talker"))
STATE_DIR = Path(os.environ.get("TALKER_STATE", Path.home() / ".local/state/iphone_linux_talker"))
TOKEN_FILE = CONF_DIR / "token"
OUTBOX = STATE_DIR / "outbox.txt"
PORT = int(os.environ.get("TALKER_PORT", "8787"))
BIND = os.environ.get("TALKER_BIND", "tailscale,127.0.0.1")
MAX_BODY = 1024 * 1024


def log(msg):
    print(msg, flush=True)


def load_token():
    return TOKEN_FILE.read_text().strip()


def tailscale_ip():
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5)
        return out.stdout.split()[0] if out.returncode == 0 and out.stdout.split() else None
    except (OSError, subprocess.SubprocessError):
        return None


def set_clipboard(text):
    # wl-copy forks into the background to serve the selection, so only stdin is piped.
    p = subprocess.Popen(["wl-copy"], stdin=subprocess.PIPE,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p.stdin.write(text.encode("utf-8"))
    p.stdin.close()
    return p.wait(timeout=5) == 0


def notify(text):
    preview = text.replace("\n", " ")
    preview = preview[:40] + ("…" if len(preview) > 40 else "")
    try:
        subprocess.Popen(["notify-send", "-t", "3000", "-h", "string:transient:1",
                          "iPhone → Ubuntu", f"{len(text)} 字已複製：{preview}"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = "talker"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # request line only (method, path, status); never the body
        line = fmt % args
        log(f"{self.client_address[0]} {line.split('?')[0]}{' ?<redacted>' if '?' in line else ''}")

    def handle_one_request(self):
        if os.environ.get("TALKER_DEBUG_RAW"):  # opt-in: dump the first bytes of each request, token redacted
            import socket
            raw = self.connection.recv(1500, socket.MSG_PEEK)
            log("RAW " + repr(raw.replace(self.server.token.encode(), b"<TOKEN>"))[:900])
        super().handle_one_request()

    def _reply(self, code, body=b"", ctype="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        # Accept the token from `Authorization: Bearer`, an `X-Token` header, or a `?t=` query
        # parameter, because iOS Shortcuts does not always deliver a hand-typed Authorization header.
        parts = self.headers.get("Authorization", "").split(None, 1)
        cands = [parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else "",
                 self.headers.get("X-Token", ""),
                 parse_qs(urlsplit(self.path).query).get("t", [""])[0]]
        want = self.server.token.encode()
        if not any(c.strip() and hmac.compare_digest(c.strip().encode(), want) for c in cands):
            log(f"auth failed: header names={sorted(self.headers.keys())} "
                f"candidate_lens={[len(c.strip()) for c in cands]} expected={len(self.server.token)}")
            time.sleep(0.5)
            self._reply(401, "unauthorized\n")
            return False
        return True

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            return self._reply(200, "ok\n")
        if path == "/pull":
            if not self._authed():
                return
            try:
                text = OUTBOX.read_text(encoding="utf-8")
            except FileNotFoundError:
                text = ""
            return self._reply(200, text)
        self._reply(404, "not found\n")

    def do_POST(self):
        path = urlsplit(self.path).path
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.close_connection = True
            return self._reply(400, "bad length\n")
        if length > MAX_BODY:
            self.close_connection = True
            return self._reply(413, "too large\n")
        raw = self.rfile.read(length)  # drain first: keep-alive would otherwise parse it as a request
        if path != "/push":
            return self._reply(404, "not found\n")
        if not self._authed():
            return
        try:
            text = raw.decode("utf-8")
            if "json" in self.headers.get("Content-Type", ""):
                text = json.loads(text)["text"]
        except (UnicodeDecodeError, ValueError, KeyError, TypeError):
            return self._reply(400, "bad body\n")
        if not text:
            return self._reply(400, "empty\n")
        if not set_clipboard(text):
            return self._reply(500, "wl-copy failed\n")
        notify(text)
        self._reply(200, "ok\n")


def serve(addr, token):
    srv = ThreadingHTTPServer((addr, PORT), Handler)
    srv.token = token
    log(f"listening on {addr}:{PORT}")
    srv.serve_forever()


def main():
    if not TOKEN_FILE.exists():
        sys.exit(f"missing token: {TOKEN_FILE} (run install.sh or `talker init`)")
    token = load_token()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    for name in [b.strip() for b in BIND.split(",") if b.strip()]:
        def run(name=name):
            while True:  # tailscale may not be up yet at login; keep retrying
                addr = tailscale_ip() if name == "tailscale" else name
                if addr:
                    try:
                        return serve(addr, token)
                    except OSError as e:
                        log(f"bind {addr} failed: {e}")
                else:
                    log("tailscale IP not available yet")
                time.sleep(5)
        threading.Thread(target=run, daemon=True).start()
    threading.Event().wait()


if __name__ == "__main__":
    main()
