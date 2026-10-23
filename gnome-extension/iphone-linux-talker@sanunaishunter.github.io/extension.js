// iPhone Linux Talker: top-bar chat + always-on-top popups for the local talker service.
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Pango from 'gi://Pango';
import Soup from 'gi://Soup?version=3.0';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

Gio._promisify(Soup.Session.prototype, 'send_async', 'send_finish');
Gio._promisify(Soup.Session.prototype, 'send_and_read_async', 'send_and_read_finish');

const PORT = 8787;
const BASE = `http://127.0.0.1:${PORT}`;
const STREAM_STALE_SECONDS = 45;  // the server pings every 15 s
const TOAST_MAX_CHARS = 160;

const STYLES = [
    ['notification', '系統通知'],
    ['bar', '桌面頂端列橫條'],
    ['banner', '網頁橫條'],
    ['both', '系統通知 + 網頁橫條'],
    ['off', '不提示'],
];

function log(msg) { console.log(`[talker] ${msg}`); }

function fmtTime(ts) {
    return GLib.DateTime.new_from_unix_local(Math.floor(ts)).format('%H:%M');
}

/** Talks to the local service: JSON requests plus a server-sent-events stream with auto-reconnect. */
class TalkerClient {
    constructor(handlers) {
        this._h = handlers;  // {onState(state), onMessage(msg), onStatus(connected)}
        this._session = new Soup.Session({timeout: 0});
        this._cancel = new Gio.Cancellable();
        this._streamCancel = null;
        this._timers = new Set();
        this._destroyed = false;
        this._lastSeen = 0;
        this.state = {messages: [], settings: {}, me: 'Ubuntu'};
        this._token = '';
    }

    start() {
        this._run().catch(e => console.error(`[talker] stream loop ended: ${e}`));
    }

    destroy() {
        this._destroyed = true;
        this._cancel.cancel();
        if (this._streamCancel)
            this._streamCancel.cancel();
        for (const id of this._timers)
            GLib.source_remove(id);
        this._timers.clear();
        this._session.abort();
        this._session = null;
    }

    _readToken() {
        const path = GLib.build_filenamev([GLib.get_user_config_dir(), 'iphone_linux_talker', 'token']);
        const [, bytes] = GLib.file_get_contents(path);
        return new TextDecoder().decode(bytes).trim();
    }

    _sleep(seconds) {
        return new Promise(resolve => {
            const id = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, seconds, () => {
                this._timers.delete(id);
                resolve();
                return GLib.SOURCE_REMOVE;
            });
            this._timers.add(id);
        });
    }

    async request(method, path, body) {
        const msg = Soup.Message.new(method, BASE + path);
        msg.get_request_headers().append('X-Token', this._token);
        if (body !== undefined) {
            msg.set_request_body_from_bytes('application/json',
                new GLib.Bytes(new TextEncoder().encode(JSON.stringify(body))));
        }
        const bytes = await this._session.send_and_read_async(msg, GLib.PRIORITY_DEFAULT, this._cancel);
        if (msg.get_status() !== Soup.Status.OK)
            throw new Error(`HTTP ${msg.get_status()} for ${path}`);
        return JSON.parse(new TextDecoder().decode(bytes.get_data()));
    }

    async refreshState() {
        this.state = await this.request('GET', '/api/state');
        this._h.onState(this.state);
    }

    async _run() {
        let backoff = 1;
        while (!this._destroyed) {
            try {
                this._token = this._readToken();
                await this._stream();
            } catch (e) {
                if (this._destroyed)
                    return;
                log(`connection problem: ${e.message ?? e}`);
            }
            this._h.onStatus(false);
            await this._sleep(backoff);
            backoff = Math.min(backoff * 2, 30);
        }
    }

    async _stream() {
        const cancel = new Gio.Cancellable();
        this._streamCancel = cancel;
        const msg = Soup.Message.new('GET', `${BASE}/api/events?client=bar`);
        msg.get_request_headers().append('X-Token', this._token);
        const stream = await this._session.send_async(msg, GLib.PRIORITY_DEFAULT, cancel);
        if (msg.get_status() !== Soup.Status.OK)
            throw new Error(`events: HTTP ${msg.get_status()}`);

        this._lastSeen = GLib.get_monotonic_time();
        const watchdog = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 15, () => {
            if ((GLib.get_monotonic_time() - this._lastSeen) / 1e6 > STREAM_STALE_SECONDS) {
                log('stream went quiet; reconnecting');
                cancel.cancel();
            }
            return GLib.SOURCE_CONTINUE;
        });
        this._timers.add(watchdog);

        try {
            await this.refreshState();
            this._h.onStatus(true);
            const data = new Gio.DataInputStream({base_stream: stream});
            let event = 'message';
            let payload = [];
            for (;;) {
                const line = await this._readLine(data, cancel);
                if (line === null)
                    break;  // server closed the stream
                this._lastSeen = GLib.get_monotonic_time();
                if (line === '') {
                    if (payload.length)
                        this._dispatch(event, payload.join('\n'));
                    event = 'message';
                    payload = [];
                } else if (line.startsWith('event: ')) {
                    event = line.slice(7);
                } else if (line.startsWith('data: ')) {
                    payload.push(line.slice(6));
                }  // ": ping" comments only refresh _lastSeen
            }
        } finally {
            GLib.source_remove(watchdog);
            this._timers.delete(watchdog);
            this._streamCancel = null;
        }
    }

    _readLine(data, cancel) {
        return new Promise((resolve, reject) => {
            data.read_line_async(GLib.PRIORITY_DEFAULT, cancel, (src, res) => {
                try {
                    const [line] = src.read_line_finish_utf8(res);  // null at end of stream
                    resolve(line);
                } catch (e) {
                    reject(e);
                }
            });
        });
    }

    _dispatch(event, data) {
        if (event === 'msg') {
            const m = JSON.parse(data);
            if (!this.state.messages.some(x => x.id === m.id))
                this.state.messages.push(m);
            this._h.onMessage(m);
        } else if (event === 'refresh') {
            this.refreshState().catch(e => log(`refresh failed: ${e.message ?? e}`));
        }
    }
}

/** Always-on-top popup that slides down under the top bar. */
class Toast {
    constructor(onActivate) {
        this._onActivate = onActivate;
        this._timeout = 0;
        this._actor = new St.BoxLayout({
            style_class: 'talker-toast', vertical: true, reactive: true, track_hover: true,
            visible: false, opacity: 0,
        });
        this._title = new St.Label({style_class: 'talker-toast-title'});
        this._body = new St.Label({style_class: 'talker-toast-body'});
        this._body.clutter_text.set_line_wrap(true);
        this._body.clutter_text.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR);
        this._actor.add_child(this._title);
        this._actor.add_child(this._body);
        this._actor.connect('button-press-event', () => {
            this.hide();
            this._onActivate();
            return Clutter.EVENT_STOP;
        });
        this._actor.connect('notify::hover', () => {
            if (this._actor.hover)
                this._clearTimeout();
            else if (this._actor.visible)
                this._arm(1.5);
        });
        Main.layoutManager.addTopChrome(this._actor);
        this._seconds = 3;
    }

    show(msg, seconds) {
        this._seconds = seconds;
        this._title.text = `${msg.from} → ${msg.to}`;
        const t = msg.text.length > TOAST_MAX_CHARS ? `${msg.text.slice(0, TOAST_MAX_CHARS)}…` : msg.text;
        this._body.text = t;

        const mon = Main.layoutManager.primaryMonitor;
        const width = Math.min(Math.round(mon.width * 0.9), 540);
        this._actor.set_width(width);
        this._actor.set_position(mon.x + Math.round((mon.width - width) / 2), mon.y + Main.panel.height + 8);

        this._clearTimeout();
        this._actor.remove_all_transitions();
        this._actor.visible = true;
        this._actor.translation_y = -(Main.panel.height + 220);
        this._actor.opacity = 0;
        this._actor.ease({
            translation_y: 0, opacity: 255, duration: 220, mode: Clutter.AnimationMode.EASE_OUT_QUAD,
        });
        this._arm(seconds);
        log(`toast shown (${seconds}s): ${msg.from} -> ${msg.to}`);
    }

    _arm(seconds) {
        this._clearTimeout();
        this._timeout = GLib.timeout_add(GLib.PRIORITY_DEFAULT, Math.round(seconds * 1000), () => {
            this._timeout = 0;
            this.hide();
            return GLib.SOURCE_REMOVE;
        });
    }

    _clearTimeout() {
        if (this._timeout) {
            GLib.source_remove(this._timeout);
            this._timeout = 0;
        }
    }

    hide() {
        this._clearTimeout();
        if (!this._actor.visible)
            return;
        this._actor.remove_all_transitions();
        this._actor.ease({
            translation_y: -(Main.panel.height + 220), opacity: 0, duration: 180,
            mode: Clutter.AnimationMode.EASE_IN_QUAD,
            onComplete: () => { this._actor.visible = false; },
        });
    }

    destroy() {
        this._clearTimeout();
        this._actor.remove_all_transitions();
        Main.layoutManager.removeChrome(this._actor);
        this._actor.destroy();
    }
}

const TalkerIndicator = GObject.registerClass(
class TalkerIndicator extends PanelMenu.Button {
    _init(client) {
        super._init(0.5, 'iPhone Linux Talker');
        this._client = client;
        this._unread = 0;
        this._connected = false;

        this._icon = new St.Icon({icon_name: 'phone-symbolic', style_class: 'system-status-icon'});
        this.add_child(this._icon);
        this._updateIcon();

        this._buildPanel();
        this._buildSettings();

        this.menu.connect('open-state-changed', (_m, open) => {
            if (!open)
                return;
            this._unread = 0;
            this._updateIcon();
            this._renderList();
            this._entry.grab_key_focus();
        });
    }

    _buildPanel() {
        const item = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
        const box = new St.BoxLayout({style_class: 'talker-panel', vertical: true, x_expand: true});
        item.add_child(box);

        const header = new St.BoxLayout({style_class: 'talker-header'});
        this._status = new St.Label({style_class: 'talker-status', text: '連線中…', x_expand: true,
            y_align: Clutter.ActorAlign.CENTER});
        const web = new St.Button({label: '網頁版', style_class: 'button talker-open-web', can_focus: true});
        web.connect('clicked', () => {
            this.menu.close();
            Gio.AppInfo.launch_default_for_uri(`${BASE}/`, null);
        });
        header.add_child(this._status);
        header.add_child(web);
        box.add_child(header);

        this._scroll = new St.ScrollView({
            style_class: 'talker-scroll', overlay_scrollbars: true,
            hscrollbar_policy: St.PolicyType.NEVER, vscrollbar_policy: St.PolicyType.AUTOMATIC,
        });
        this._list = new St.BoxLayout({style_class: 'talker-list', vertical: true});
        this._scroll.set_child(this._list);
        box.add_child(this._scroll);

        const compose = new St.BoxLayout({style_class: 'talker-compose'});
        this._entry = new St.Entry({hint_text: '傳給 iPhone…（Enter 傳送）', can_focus: true, x_expand: true,
            style_class: 'talker-entry'});
        this._entry.clutter_text.connect('activate', () => this._send());
        const send = new St.Button({label: '傳送', style_class: 'button talker-send', can_focus: true});
        send.connect('clicked', () => this._send());
        compose.add_child(this._entry);
        compose.add_child(send);
        box.add_child(compose);

        this.menu.addMenuItem(item);
    }

    _buildSettings() {
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._settingsMenu = new PopupMenu.PopupSubMenuMenuItem('設定');
        this.menu.addMenuItem(this._settingsMenu);

        this._styleItems = new Map();
        for (const [value, label] of STYLES) {
            const it = new PopupMenu.PopupMenuItem(label);
            it.connect('activate', () => this._saveSettings({popup_style: value}));
            this._settingsMenu.menu.addMenuItem(it);
            this._styleItems.set(value, it);
        }

        const stepper = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
        this._secLabel = new St.Label({text: '提示停留 3 秒', x_expand: true, y_align: Clutter.ActorAlign.CENTER});
        const minus = new St.Button({label: '−', style_class: 'button talker-step', can_focus: true});
        const plus = new St.Button({label: '+', style_class: 'button talker-step', can_focus: true});
        minus.connect('clicked', () => this._stepSeconds(-1));
        plus.connect('clicked', () => this._stepSeconds(1));
        stepper.add_child(this._secLabel);
        stepper.add_child(minus);
        stepper.add_child(plus);
        this._settingsMenu.menu.addMenuItem(stepper);

        this._copySwitch = new PopupMenu.PopupSwitchMenuItem('收到後自動複製到剪貼簿', true);
        this._copySwitch.connect('toggled', (_i, on) => this._saveSettings({auto_copy: on}));
        this._settingsMenu.menu.addMenuItem(this._copySwitch);

        this._clearItem = new PopupMenu.PopupMenuItem('清除全部紀錄');
        this._clearArmed = false;
        this._clearItem.connect('activate', () => {
            if (!this._clearArmed) {
                this._clearArmed = true;
                this._clearItem.label.text = '再按一次確認清除';
                this._clearTimer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 3, () => {
                    this._clearArmed = false;
                    this._clearItem.label.text = '清除全部紀錄';
                    this._clearTimer = 0;
                    return GLib.SOURCE_REMOVE;
                });
                return;
            }
            this._clearArmed = false;
            this._clearItem.label.text = '清除全部紀錄';
            this._client.request('POST', '/api/clear', {}).catch(e => log(`clear failed: ${e.message ?? e}`));
        });
        this._settingsMenu.menu.addMenuItem(this._clearItem);
        // Keep the settings submenu open after picking an item.
        this._settingsMenu.menu.itemActivated = () => {};
    }

    _stepSeconds(delta) {
        const cur = this._client.state.settings.toast_seconds ?? 3;
        this._saveSettings({toast_seconds: Math.min(Math.max(cur + delta, 1), 30)});
    }

    _saveSettings(patch) {
        this._client.request('POST', '/api/settings', patch)
            .then(s => { this._client.state.settings = s; this._applySettings(); })
            .catch(e => log(`saving settings failed: ${e.message ?? e}`));
    }

    _applySettings() {
        const s = this._client.state.settings;
        for (const [value, it] of this._styleItems)
            it.setOrnament(value === s.popup_style ? PopupMenu.Ornament.DOT : PopupMenu.Ornament.NONE);
        this._secLabel.text = `提示停留 ${s.toast_seconds} 秒`;
        this._copySwitch.setToggleState(!!s.auto_copy);
    }

    _send() {
        const text = this._entry.get_text();
        if (!text.trim())
            return;
        this._entry.set_text('');
        this._client.request('POST', '/api/send', {text}).then(m => {
            if (!this._client.state.messages.some(x => x.id === m.id))
                this._client.state.messages.push(m);
            this._renderList();
        }).catch(e => {
            log(`send failed: ${e.message ?? e}`);
            this._entry.set_text(text);  // do not lose what was typed
        });
    }

    _renderList() {
        if (!this.menu.isOpen)
            return;
        this._list.destroy_all_children();
        const {messages, me} = this._client.state;
        if (!messages.length) {
            this._list.add_child(new St.Label({style_class: 'talker-empty',
                text: '還沒有訊息。\niPhone 傳來的和你傳出的都會顯示在這裡。'}));
            return;
        }
        for (const m of messages) {
            const out = m.to !== me;
            const row = new St.BoxLayout({
                style_class: `talker-msg ${out ? 'talker-out' : 'talker-in'}`, vertical: true,
                x_align: out ? Clutter.ActorAlign.END : Clutter.ActorAlign.START,
            });
            const bubble = new St.Label({style_class: 'talker-bubble', text: m.text, reactive: true,
                track_hover: true});
            bubble.clutter_text.set_line_wrap(true);
            bubble.clutter_text.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR);
            bubble.clutter_text.set_ellipsize(Pango.EllipsizeMode.NONE);

            let status = '';
            let cls = 'talker-meta';
            if (out) {
                status = m.delivered ? ' · iPhone 已取走' : ' · 等待 iPhone 取走';
                cls += m.delivered ? ' talker-meta-done' : ' talker-meta-pending';
            } else if (m.copied) {
                status = ' · 已複製';
                cls += ' talker-meta-done';
            }
            const meta = new St.Label({style_class: cls, text: `${m.from} → ${m.to} · ${fmtTime(m.ts)}${status}`});
            bubble.connect('button-press-event', () => {
                St.Clipboard.get_default().set_text(St.ClipboardType.CLIPBOARD, m.text);
                meta.text = '已複製這則訊息';
                return Clutter.EVENT_STOP;
            });
            row.add_child(bubble);
            row.add_child(meta);
            this._list.add_child(row);
        }
        // Scroll to the newest message once the layout has settled.
        this._scrollIdle = GLib.idle_add(GLib.PRIORITY_LOW, () => {
            this._scrollIdle = 0;
            const adj = this._scroll.vadjustment ?? this._scroll.vscroll?.adjustment;
            if (adj)
                adj.value = adj.upper - adj.page_size;
            return GLib.SOURCE_REMOVE;
        });
    }

    _updateIcon() {
        this._icon.opacity = this._connected ? 255 : 110;
        if (this._unread > 0)
            this._icon.add_style_class_name('talker-unread');
        else
            this._icon.remove_style_class_name('talker-unread');
    }

    setConnected(on) {
        this._connected = on;
        this._status.text = on ? '已連線' : '無法連線到 talker 服務';
        if (on)
            this._status.add_style_class_name('talker-status-on');
        else
            this._status.remove_style_class_name('talker-status-on');
        this._updateIcon();
    }

    onState() {
        this._applySettings();
        this._renderList();
    }

    onIncoming() {
        if (!this.menu.isOpen) {
            this._unread++;
            this._updateIcon();
        }
        this._renderList();
    }

    onOutgoing() {
        this._renderList();
    }

    destroy() {
        if (this._scrollIdle)
            GLib.source_remove(this._scrollIdle);
        if (this._clearTimer)
            GLib.source_remove(this._clearTimer);
        super.destroy();
    }
});

export default class TalkerExtension extends Extension {
    enable() {
        this._toast = new Toast(() => this._indicator?.menu.open());
        this._client = new TalkerClient({
            onState: () => this._indicator?.onState(),
            onStatus: on => this._indicator?.setConnected(on),
            onMessage: m => this._onMessage(m),
        });
        this._indicator = new TalkerIndicator(this._client);
        Main.panel.addToStatusArea(this.uuid, this._indicator);
        this._client.start();
        log('enabled');
    }

    _onMessage(m) {
        const me = this._client.state.me;
        if (m.to !== me) {  // sent by us (here or from the web page)
            this._indicator?.onOutgoing();
            return;
        }
        this._indicator?.onIncoming();
        const s = this._client.state.settings;
        if (s.popup_style === 'bar' && !this._indicator.menu.isOpen)
            this._toast.show(m, s.toast_seconds ?? 3);
    }

    disable() {
        this._client?.destroy();
        this._client = null;
        this._toast?.destroy();
        this._toast = null;
        this._indicator?.destroy();
        this._indicator = null;
        log('disabled');
    }
}
