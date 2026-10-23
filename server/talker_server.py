#!/usr/bin/env python3
"""iPhone_Linux_talker server: message relay between an iPhone (Shortcuts) and this Ubuntu desktop.

Shortcut-facing (token via `X-Token`, `Authorization: Bearer`, or `?t=`):
  POST /push        iPhone -> Ubuntu. Body: raw text or {"text": "..."}.
  GET  /pull        Ubuntu -> iPhone. Returns every unpulled message (newline-joined), marks them delivered.
Web UI (loopback is logged in automatically; other devices sign in with the token):
  GET  /            chat page
  GET  /api/state   messages + settings          GET /api/events  server-sent events
  POST /api/send    {"text"}                      POST /api/settings  POST /api/clear   POST /login
GET /health is an unauthenticated liveness probe.
"""
import hashlib
import hmac
import html
import ipaddress
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import accounting_bridge  # noqa: E402
CONF_DIR = Path(os.environ.get("TALKER_CONF", Path.home() / ".config/iphone_linux_talker"))
STATE_DIR = Path(os.environ.get("TALKER_STATE", Path.home() / ".local/state/iphone_linux_talker"))
TOKEN_FILE = CONF_DIR / "token"
SETTINGS_FILE = CONF_DIR / "settings.json"
MESSAGES_FILE = STATE_DIR / "messages.json"
PORT = int(os.environ.get("TALKER_PORT", "8787"))
BIND = os.environ.get("TALKER_BIND", "tailscale,127.0.0.1")
MAX_BODY = 1024 * 1024
MAX_TEXT = 100_000
LOCAL = "Ubuntu"
REMOTE = "iPhone"

DEFAULTS = {"toast_seconds": 3.0, "popup_style": "notification", "auto_copy": True, "history_limit": 50,
            "accounting_mode": False}
POPUP_STYLES = ("notification", "banner", "bar", "both", "off")  # bar = GNOME Shell extension top-bar popup

TOKEN = ""


def log(msg):
    print(msg, flush=True)


def write_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    tmp.replace(path)


def clean_settings(raw):
    out = {}
    try:
        if "toast_seconds" in raw:
            out["toast_seconds"] = min(max(float(raw["toast_seconds"]), 0.5), 60.0)
        if "history_limit" in raw:
            out["history_limit"] = min(max(int(raw["history_limit"]), 1), 1000)
    except (TypeError, ValueError):
        pass
    if raw.get("popup_style") in POPUP_STYLES:
        out["popup_style"] = raw["popup_style"]
    if isinstance(raw.get("auto_copy"), bool):
        out["auto_copy"] = raw["auto_copy"]
    if isinstance(raw.get("accounting_mode"), bool):
        out["accounting_mode"] = raw["accounting_mode"]
    return out


class Store:
    """Message history (persisted, newest last), settings, and SSE subscribers."""

    def __init__(self):
        self.lock = threading.Lock()
        self.subs = set()
        self.bar_clients = 0  # connected GNOME top-bar extensions (?client=bar)
        self.settings = dict(DEFAULTS)
        self.messages = []
        self.next_id = 1
        try:
            self.settings.update(clean_settings(json.loads(SETTINGS_FILE.read_text())))
        except (OSError, ValueError):
            pass
        try:
            self.messages = json.loads(MESSAGES_FILE.read_text(encoding="utf-8"))
            self.next_id = max((m["id"] for m in self.messages), default=0) + 1
        except (OSError, ValueError, KeyError):
            self.messages = []

    def _save_messages(self):
        write_private(MESSAGES_FILE, json.dumps(self.messages, ensure_ascii=False))

    def broadcast(self, kind, data):
        payload = f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        for q in list(self.subs):
            q.put(payload)

    def add(self, frm, to, text, **extra):
        with self.lock:
            msg = {"id": self.next_id, "ts": time.time(), "from": frm, "to": to, "text": text, **extra}
            self.next_id += 1
            self.messages.append(msg)
            del self.messages[:-self.settings["history_limit"]]
            self._save_messages()
        self.broadcast("msg", msg)
        return msg

    def pull(self):
        with self.lock:
            pending = [m for m in self.messages if m["to"] == REMOTE and not m.get("delivered")]
            for m in pending:
                m["delivered"] = True
            if pending:
                self._save_messages()
        if pending:
            self.broadcast("refresh", {})
        return "\n".join(m["text"] for m in pending)

    def mark_copied(self, msg_id):
        with self.lock:
            for m in self.messages:
                if m["id"] == msg_id:
                    m["copied"] = True
                    self._save_messages()
                    break
            else:
                return
        self.broadcast("refresh", {})

    def clear(self):
        with self.lock:
            self.messages = []
            self._save_messages()
        self.broadcast("refresh", {})

    def update_settings(self, raw):
        with self.lock:
            self.settings.update(clean_settings(raw))
            write_private(SETTINGS_FILE, json.dumps(self.settings, indent=2))
            del self.messages[:-self.settings["history_limit"]]
            snap = dict(self.settings)
        self.broadcast("refresh", {})
        return snap

    def state(self):
        with self.lock:
            return {"messages": list(self.messages), "settings": dict(self.settings), "me": LOCAL}


STORE = None
_peers = {"at": 0.0, "map": {}}


def tailscale_status():
    try:
        out = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=5)
        return json.loads(out.stdout) if out.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}


def tailscale_ip():
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5)
        return out.stdout.split()[0] if out.returncode == 0 and out.stdout.split() else None
    except (OSError, subprocess.SubprocessError):
        return None


def peer_name(ip):
    """Friendly device name for a client address (iOS -> 'iPhone', this machine -> 'Ubuntu')."""
    if ip in ("127.0.0.1", "::1"):
        return LOCAL
    if ip not in _peers["map"] and time.time() - _peers["at"] > 10:
        _peers["at"] = time.time()
        peers = {}
        status = tailscale_status()
        for node in list(status.get("Peer", {}).values()) + [status.get("Self", {})]:
            os_name = (node.get("OS") or "").lower()
            name = {"ios": REMOTE, "android": "Android"}.get(os_name, node.get("HostName") or "")
            for addr in node.get("TailscaleIPs") or []:
                peers[addr] = name
        _peers["map"] = peers
    return _peers["map"].get(ip) or ip


def set_clipboard(text, timeout=4):
    # wl-copy forks into the background to serve the selection, so only stdin is piped. GNOME has no data-control
    # protocol, so wl-copy needs a focused helper window and can stall for seconds: never wait on it forever.
    try:
        p = subprocess.Popen(["wl-copy"], stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    try:
        p.stdin.write(text.encode("utf-8"))
        p.stdin.close()
        return p.wait(timeout=timeout) == 0
    except (OSError, subprocess.TimeoutExpired):
        p.kill()
        p.wait()
        return False


_copy = {"lock": threading.Lock(), "seq": 0}


def copy_in_background(msg_id, text, tries=4):
    """Copy off the request thread so /push answers at once; only the newest push may win the clipboard."""
    _copy["seq"] += 1
    seq = _copy["seq"]

    def job():
        with _copy["lock"]:
            for attempt in range(tries):
                if seq != _copy["seq"]:
                    return  # a newer message superseded this one
                if set_clipboard(text):
                    return STORE.mark_copied(msg_id)
                log(f"wl-copy failed or timed out (attempt {attempt + 1}/{tries})")
                time.sleep(0.5)

    threading.Thread(target=job, daemon=True).start()


def notify_text(msg):
    """(title, body) for the system notification / GNOME bar toast."""
    acct = msg.get("account")
    if acct:
        if acct.get("error"):
            return "記帳失敗", html.escape(acct["error"])
        parts = []
        if acct.get("recorded"):
            parts.append(f"✅ 已記帳 {acct['recorded']} 筆")
        if acct.get("pending"):
            parts.append(f"⚠️ 待審核 {acct['pending']} 筆")
        if acct.get("memos"):
            parts.append(f"📝 備忘 {acct['memos']} 筆")
        if acct.get("skipped"):
            parts.append(f"⏭ 重複已略過 {acct['skipped']} 筆")
        return "記帳", "、".join(parts) if parts else "沒有可記錄的項目"
    body = html.escape(msg["text"][:300] + ("…" if len(msg["text"]) > 300 else ""))
    return f"{msg['from']} → {msg['to']}", body


def desktop_notify(msg, settings):
    style = settings["popup_style"]
    # "bar" is drawn by the GNOME extension; fall back to a system notification if it is not running.
    if style not in ("notification", "both") and not (style == "bar" and STORE.bar_clients == 0):
        return
    title, body = notify_text(msg)
    try:
        subprocess.Popen(["notify-send", "-t", str(int(settings["toast_seconds"] * 1000)),
                          "-i", "phone-symbolic",
                          "-h", "string:transient:1", "--", title, body],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def host_ok(host_header):
    """Reject DNS-rebinding: a Host header must be an IP literal, localhost, or a tailnet name."""
    host = host_header.rsplit(":", 1)[0].strip("[]").lower() if host_header else ""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return host == "localhost" or host.endswith(".ts.net") or host == socket.gethostname().lower()


def session_cookie():
    return hashlib.sha256(("talker-ui:" + TOKEN).encode()).hexdigest()


class Handler(BaseHTTPRequestHandler):
    server_version = "talker"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # request line only (method, path, status); never bodies or queries
        line = fmt % args
        log(f"{self.client_address[0]} {line.split('?')[0]}{' ?<redacted>' if '?' in line else ''}")

    # ---- helpers -------------------------------------------------------
    def _reply(self, code, body=b"", ctype="text/plain; charset=utf-8", headers=()):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj, headers=()):
        self._reply(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8", headers)

    def _token_ok(self):
        # Token from `Authorization: Bearer`, `X-Token`, or `?t=`. iOS Shortcuts is picky about hand-typed headers.
        parts = self.headers.get("Authorization", "").split(None, 1)
        cands = [parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else "",
                 self.headers.get("X-Token", ""),
                 parse_qs(urlsplit(self.path).query).get("t", [""])[0]]
        want = TOKEN.encode()
        return any(c.strip() and hmac.compare_digest(c.strip().encode(), want) for c in cands)

    def _cookie_ok(self):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "talker" and hmac.compare_digest(v.encode(), session_cookie().encode()):
                return host_ok(self.headers.get("Host", ""))
        return False

    def _authed(self, cookie=False):
        if self._token_ok():
            return True
        # Cookie sessions are for the web UI. POSTs must be JSON so a cross-site form cannot forge them.
        if cookie and self._cookie_ok() and (self.command != "POST" or "json" in self.headers.get("Content-Type", "")):
            return True
        log(f"auth failed: header names={sorted(self.headers.keys())}")
        time.sleep(0.5)
        self._reply(401, "unauthorized\n")
        return False

    def _read_json(self, raw):
        try:
            obj = json.loads(raw.decode("utf-8"))
            return obj if isinstance(obj, dict) else None
        except (UnicodeDecodeError, ValueError):
            return None

    # ---- GET -----------------------------------------------------------
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            return self._reply(200, "ok\n")
        if path in ("/", "/index.html"):
            if not host_ok(self.headers.get("Host", "")):
                return self._reply(400, "bad host\n")
            try:
                page = (HERE / "ui.html").read_bytes()
            except OSError:
                return self._reply(500, "ui.html missing\n")
            cookie = ()
            if self.client_address[0] in ("127.0.0.1", "::1"):  # local browser: logged in automatically
                cookie = (("Set-Cookie", f"talker={session_cookie()}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000"),)
            return self._reply(200, page, "text/html; charset=utf-8", cookie)
        if path == "/pull":
            if not self._authed():
                return
            return self._reply(200, STORE.pull())
        if path == "/api/state":
            if not self._authed(cookie=True):
                return
            return self._json(200, STORE.state())
        if path == "/api/events":
            if not self._authed(cookie=True):
                return
            return self._events()
        self._reply(404, "not found\n")

    def _events(self):
        q = queue.Queue()
        STORE.subs.add(q)
        is_bar = parse_qs(urlsplit(self.path).query).get("client") == ["bar"]
        if is_bar:
            STORE.bar_clients += 1
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b": hello\n\n")
            self.wfile.flush()
            while True:
                try:
                    chunk = q.get(timeout=15)
                except queue.Empty:
                    chunk = ": ping\n\n"
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            STORE.subs.discard(q)
            if is_bar:
                STORE.bar_clients -= 1

    # ---- POST ----------------------------------------------------------
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

        if path == "/push":
            return self._push(raw)
        if path == "/login":
            body = self._read_json(raw) or {}
            if hmac.compare_digest(str(body.get("token", "")).strip().encode(), TOKEN.encode()):
                return self._json(200, {"ok": True}, (
                    ("Set-Cookie", f"talker={session_cookie()}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000"),))
            time.sleep(0.5)
            return self._json(401, {"ok": False})
        if path in ("/api/send", "/api/settings", "/api/clear"):
            if not self._authed(cookie=True):
                return
            body = self._read_json(raw)
            if body is None:
                return self._reply(400, "bad body\n")
            if path == "/api/clear":
                STORE.clear()
                return self._json(200, {"ok": True})
            if path == "/api/settings":
                return self._json(200, STORE.update_settings(body))
            text = str(body.get("text", ""))
            if not text.strip() or len(text) > MAX_TEXT:
                return self._reply(400, "empty or too long\n")
            return self._json(200, STORE.add(LOCAL, REMOTE, text, delivered=False))
        self._reply(404, "not found\n")

    def _push(self, raw):
        if not self._authed():
            return
        try:
            text = raw.decode("utf-8")
            if "json" in self.headers.get("Content-Type", ""):
                text = json.loads(text)["text"]
        except (UnicodeDecodeError, ValueError, KeyError, TypeError):
            return self._reply(400, "bad body\n")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            return self._reply(400, "empty or too long\n")
        settings = STORE.state()["settings"]
        account = None
        if settings["accounting_mode"] and accounting_bridge.looks_like_accounting(text):
            account = accounting_bridge.ingest(text)
        extra = {"account": account} if account else {}
        msg = STORE.add(peer_name(self.client_address[0]), LOCAL, text, copied=False, **extra)
        desktop_notify(msg, settings)
        self._reply(200, "ok\n")
        if settings["auto_copy"]:
            copy_in_background(msg["id"], text)


def serve(addr):
    srv = ThreadingHTTPServer((addr, PORT), Handler)
    log(f"listening on {addr}:{PORT}")
    srv.serve_forever()


def main():
    global TOKEN, STORE
    if not TOKEN_FILE.exists():
        sys.exit(f"missing token: {TOKEN_FILE} (run install.sh or `talker init`)")
    TOKEN = TOKEN_FILE.read_text().strip()
    STORE = Store()
    for name in [b.strip() for b in BIND.split(",") if b.strip()]:
        def run(name=name):
            while True:  # tailscale may not be up yet at login; keep retrying
                addr = tailscale_ip() if name == "tailscale" else name
                if addr:
                    try:
                        return serve(addr)
                    except OSError as e:
                        log(f"bind {addr} failed: {e}")
                else:
                    log("tailscale IP not available yet")
                time.sleep(5)
        threading.Thread(target=run, daemon=True).start()
    threading.Event().wait()


if __name__ == "__main__":
    main()
