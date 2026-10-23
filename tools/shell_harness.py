"""Drive the isolated headless gnome-shell: screenshots + input via mutter ScreenCast/RemoteDesktop."""
import json, os, subprocess, sys, time
import gi
gi.require_version('Gio', '2.0')
from gi.repository import Gio, GLib

ADDR = sys.argv[1]
OUT = sys.argv[2]
SCENARIO = sys.argv[3]

conn = Gio.DBusConnection.new_for_address_sync(
    ADDR, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)

def call(dest, path, iface, method, args=None, reply=None):
    return conn.call_sync(dest, path, iface, method, args, GLib.VariantType(reply) if reply else None, 0, 10000, None)

RD = 'org.gnome.Mutter.RemoteDesktop'; SC = 'org.gnome.Mutter.ScreenCast'
rd_path = call(RD, '/org/gnome/Mutter/RemoteDesktop', RD, 'CreateSession', None, '(o)')[0]
sid = call(RD, rd_path, 'org.freedesktop.DBus.Properties', 'Get',
           GLib.Variant('(ss)', (RD + '.Session', 'SessionId')), '(v)')[0]
sc_path = call(SC, '/org/gnome/Mutter/ScreenCast', SC, 'CreateSession',
               GLib.Variant('(a{sv})', ({'remote-desktop-session-id': GLib.Variant('s', sid)},)), '(o)')[0]
stream_path = call(SC, sc_path, SC + '.Session', 'RecordMonitor',
                   GLib.Variant('(sa{sv})', ('Meta-0', {})), '(o)')[0]

node = {}
def on_signal(_c, _s, _p, _i, _n, params):
    node['id'] = params[0]
conn.signal_subscribe(SC, SC + '.Stream', 'PipeWireStreamAdded', stream_path, None, Gio.DBusSignalFlags.NONE, on_signal)
call(RD, rd_path, RD + '.Session', 'Start')
ctx = GLib.MainContext.default()
t0 = time.time()
while 'id' not in node and time.time() - t0 < 10:
    ctx.iteration(False); time.sleep(0.05)
if 'id' not in node:
    sys.exit('no pipewire node')
print('pipewire node', node['id'], flush=True)

def pump(sec):
    end = time.time() + sec
    while time.time() < end:
        ctx.iteration(False); time.sleep(0.03)

def shot(name):
    path = os.path.join(OUT, name + '.png')
    subprocess.run(['gst-launch-1.0', '-q', 'pipewiresrc', f'path={node["id"]}', 'num-buffers=3', '!', 'videoconvert', '!',
                    'pngenc', 'snapshot=true', '!', 'filesink', f'location={path}'], timeout=20,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print('shot', path, os.path.exists(path), flush=True)

def sess(method, args):
    call(RD, rd_path, RD + '.Session', method, args)

def move(x, y): sess('NotifyPointerMotionAbsolute', GLib.Variant('(sdd)', (stream_path, float(x), float(y))))
def click(x, y):
    move(x, y); pump(0.2)
    sess('NotifyPointerButton', GLib.Variant('(ib)', (0x110, True))); pump(0.08)
    sess('NotifyPointerButton', GLib.Variant('(ib)', (0x110, False))); pump(0.3)
def key(sym):
    sess('NotifyKeyboardKeysym', GLib.Variant('(ub)', (sym, True))); pump(0.04)
    sess('NotifyKeyboardKeysym', GLib.Variant('(ub)', (sym, False))); pump(0.06)
def type_text(s):
    for ch in s:
        key(ord(ch) if ord(ch) < 0x100 else 0x1000000 + ord(ch))
ENTER = 0xff0d

exec(open(SCENARIO).read())
sess('Stop') if False else call(RD, rd_path, RD + '.Session', 'Stop')
