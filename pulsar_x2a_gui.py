#!/usr/bin/env python3
"""
pulsar_x2a_gui.py — GTK4/libadwaita settings GUI + system-tray applet
                     for the Pulsar X2A mouse.

Run directly:
    python3 pulsar_x2a_gui.py

Single-instance: a second launch will focus the existing window.
The system-tray icon requires the GNOME AppIndicator extension:
    sudo apt install gnome-shell-extension-appindicator
    gnome-extensions enable appindicatorsupport@rgcjonas.gmail.com
"""

import sys
import os
import threading
import time

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Dbusmenu', '0.4')
from gi.repository import Gtk, Adw, GLib, Gio, Dbusmenu

# Import the backend from the same directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pulsar_x2a as px

APP_ID = 'io.github.packerlschupfer.PulsarX2A'

# Serialises all USB open/close operations so the tray and window don't collide.
_USB_LOCK = threading.Lock()

_SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category"     type="s" access="read"/>
    <property name="Id"           type="s" access="read"/>
    <property name="Title"        type="s" access="read"/>
    <property name="Status"       type="s" access="read"/>
    <property name="IconName"     type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="Menu"         type="o" access="read"/>
    <property name="ItemIsMenu"   type="b" access="read"/>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewStatus"><arg type="s"/></signal>
    <signal name="XAyatanaNewLabel"><arg type="s"/><arg type="s"/></signal>
    <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
  </interface>
</node>
"""


class _StatusNotifierItem:
    """Minimal StatusNotifierItem D-Bus service (no GTK3 dependency)."""

    _MENU_PATH = '/MenuBar'

    def __init__(self, app_id, icon_name, title):
        self._app_id    = app_id
        self._icon_name = icon_name
        self._title     = title
        self._label     = ''
        self._conn      = None
        self._obj_id    = 0
        self._sni_server = None   # Dbusmenu.Server
        self._poll_items = {}     # hz → Dbusmenu.Menuitem
        self._on_activate = None

    # ── public API ────────────────────────────────────────────────────────

    def start(self, dbus_conn):
        self._conn = dbus_conn
        node  = Gio.DBusNodeInfo.new_for_xml(_SNI_XML)
        iface = node.lookup_interface('org.kde.StatusNotifierItem')
        self._obj_id = self._conn.register_object(
            '/StatusNotifierItem', iface,
            self._on_method, self._on_get_prop, None)
        import os
        svc = f'org.kde.StatusNotifierItem-{os.getpid()}-1'
        Gio.bus_own_name_on_connection(
            self._conn, svc, Gio.BusNameOwnerFlags.NONE, None, None)
        self._conn.call(
            'org.kde.StatusNotifierWatcher', '/StatusNotifierWatcher',
            'org.kde.StatusNotifierWatcher', 'RegisterStatusNotifierItem',
            GLib.Variant('(s)', (svc,)), None,
            Gio.DBusCallFlags.NONE, -1, None, None, None)

    def set_label(self, label, guide=''):
        self._label = label
        if self._conn and self._obj_id:
            self._conn.emit_signal(
                None, '/StatusNotifierItem',
                'org.kde.StatusNotifierItem', 'XAyatanaNewLabel',
                GLib.Variant('(ss)', (label, guide)))

    def set_poll_items(self, items):
        self._poll_items = items

    def set_on_activate(self, cb):
        self._on_activate = cb

    def set_dbusmenu_server(self, server):
        self._sni_server = server

    # ── D-Bus handlers ────────────────────────────────────────────────────

    def _on_method(self, conn, sender, path, iface, method, params, inv):
        if method == 'Activate' and self._on_activate:
            GLib.idle_add(self._on_activate)
        inv.return_value(None)

    def _on_get_prop(self, conn, sender, path, iface, prop):
        return {
            'Category':     GLib.Variant('s', 'Hardware'),
            'Id':           GLib.Variant('s', self._app_id),
            'Title':        GLib.Variant('s', self._title),
            'Status':       GLib.Variant('s', 'Active'),
            'IconName':     GLib.Variant('s', self._icon_name),
            'IconThemePath':GLib.Variant('s', ''),
            'Menu':         GLib.Variant('o', self._MENU_PATH),
            'ItemIsMenu':   GLib.Variant('b', True),
        }.get(prop)


class PulsarX2AApp(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self.connect('activate', self._on_activate)
        self._sni = None
        self._poll_items = {}   # hz → Dbusmenu.Menuitem
        self._tray_refresh_busy = False

    def _on_activate(self, app):
        # Single-instance: re-use existing window if already open
        wins = self.get_windows()
        if wins:
            wins[0].present()
            return
        win = MainWindow(application=app)
        win.present()
        self._build_tray(win)
        self.hold()  # keep alive when window is hidden (quit via tray)

    # ── System-tray (direct D-Bus StatusNotifierItem + Dbusmenu) ─────────────

    def _build_tray(self, win: 'MainWindow'):
        sni = _StatusNotifierItem('pulsar-x2a', 'input-mouse', 'Pulsar X2A')
        sni.set_on_activate(win.present)
        self._sni = sni

        # Build Dbusmenu tree
        root = Dbusmenu.Menuitem.new()

        item_open = Dbusmenu.Menuitem.new()
        item_open.property_set(Dbusmenu.MENUITEM_PROP_LABEL, 'Open Settings')
        item_open.connect('item-activated', lambda _i, _t: win.present())
        root.child_append(item_open)

        sep1 = Dbusmenu.Menuitem.new()
        sep1.property_set(Dbusmenu.MENUITEM_PROP_TYPE, Dbusmenu.CLIENT_TYPES_SEPARATOR)
        root.child_append(sep1)

        dpi_root = Dbusmenu.Menuitem.new()
        dpi_root.property_set(Dbusmenu.MENUITEM_PROP_LABEL, 'Quick DPI (profile 1)')
        for dv in (400, 800, 1200, 1600, 3200):
            sub = Dbusmenu.Menuitem.new()
            sub.property_set(Dbusmenu.MENUITEM_PROP_LABEL, f'{dv} DPI')
            sub.connect('item-activated', lambda _i, _t, d=dv: self._set_dpi(d))
            dpi_root.child_append(sub)
        root.child_append(dpi_root)

        poll_root = Dbusmenu.Menuitem.new()
        poll_root.property_set(Dbusmenu.MENUITEM_PROP_LABEL, 'Polling Rate')
        for hz in (125, 250, 500, 1000):
            sub = Dbusmenu.Menuitem.new()
            sub.property_set(Dbusmenu.MENUITEM_PROP_LABEL, f'{hz} Hz')
            sub.property_set(Dbusmenu.MENUITEM_PROP_TOGGLE_TYPE,
                             Dbusmenu.MENUITEM_TOGGLE_RADIO)
            sub.property_set_int(Dbusmenu.MENUITEM_PROP_TOGGLE_STATE,
                                 Dbusmenu.MENUITEM_TOGGLE_STATE_UNCHECKED)
            sub.connect('item-activated', lambda _i, _t, h=hz: self._set_poll(h))
            poll_root.child_append(sub)
            self._poll_items[hz] = sub
        root.child_append(poll_root)

        sep2 = Dbusmenu.Menuitem.new()
        sep2.property_set(Dbusmenu.MENUITEM_PROP_TYPE, Dbusmenu.CLIENT_TYPES_SEPARATOR)
        root.child_append(sep2)

        item_auto = Dbusmenu.Menuitem.new()
        item_auto.property_set(Dbusmenu.MENUITEM_PROP_LABEL, 'Start on Login')
        item_auto.property_set(Dbusmenu.MENUITEM_PROP_TOGGLE_TYPE,
                               Dbusmenu.MENUITEM_TOGGLE_CHECK)
        item_auto.property_set_int(Dbusmenu.MENUITEM_PROP_TOGGLE_STATE,
                                   Dbusmenu.MENUITEM_TOGGLE_STATE_CHECKED
                                   if self._autostart_enabled() else
                                   Dbusmenu.MENUITEM_TOGGLE_STATE_UNCHECKED)
        item_auto.connect('item-activated', lambda _i, _t: self._toggle_autostart(_i))
        root.child_append(item_auto)

        item_quit = Dbusmenu.Menuitem.new()
        item_quit.property_set(Dbusmenu.MENUITEM_PROP_LABEL, 'Quit')
        item_quit.connect('item-activated', lambda _i, _t: self.quit())
        root.child_append(item_quit)

        server = Dbusmenu.Server.new(_StatusNotifierItem._MENU_PATH)
        server.set_root(root)
        sni.set_dbusmenu_server(server)

        conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        sni.start(conn)

        # Delay initial read so the SNI D-Bus registration completes first
        GLib.timeout_add(500, self._start_tray_updates)

    def _start_tray_updates(self):
        self._read_initial_state()
        threading.Thread(target=self._hidraw_listener, daemon=True).start()
        return False  # don't repeat

    def _read_initial_state(self):
        def _read():
            try:
                with _USB_LOCK:
                    dev = px.open_device()
                    hz = px.get_polling_rate(dev)
                    dpi_info = px.get_dpi_stages(dev, profile=1)
                    px.close_device(dev)
                dpi = dpi_info['stages'][dpi_info['active'] - 1][0]
                GLib.idle_add(self._update_tray_label, dpi, hz, True)
            except Exception:
                pass
        threading.Thread(target=_read, daemon=True).start()

    @staticmethod
    def _find_hidraw():
        """Find the hidraw device for the Pulsar X2A interface 1 (DPI events)."""
        import glob
        for path in sorted(glob.glob('/sys/class/hidraw/hidraw*/device/uevent')):
            try:
                text = open(path).read()
                if '3710' not in text:
                    continue
                phys_line = [l for l in text.splitlines() if 'HID_PHYS' in l]
                if phys_line and phys_line[0].endswith('/input1'):
                    return '/dev/' + path.split('/')[4]
            except OSError:
                continue
        return None

    def _hidraw_listener(self):
        """Listen for DPI change events on hidraw (USB interrupt, zero overhead)."""
        import struct as st
        path = self._find_hidraw()
        if not path:
            return
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            while True:
                data = os.read(fd, 256)
                if not data:
                    break
                # DPI change: report_id=0x05, cat=0x05, stage, dpi_x_le16, dpi_y_le16
                if len(data) >= 7 and data[0] == 0x05 and data[1] == 0x05:
                    dpi = st.unpack_from('<H', data, 3)[0]
                    GLib.idle_add(self._update_tray_label, dpi, None)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _update_tray_label(self, dpi=None, hz=None, initial=False):
        if not hasattr(self, '_cur_dpi'):
            self._cur_dpi = 0
            self._cur_hz = 0
            self._label_hide_seq = 0
        if dpi is not None:
            self._cur_dpi = dpi
        if hz is not None:
            self._cur_hz = hz

        # Build label: initial shows both, changes show only what changed
        if initial:
            label = f'{self._cur_dpi} DPI  {self._cur_hz} Hz'
            timeout = 3
        elif dpi is not None and hz is not None:
            label = f'{self._cur_dpi} DPI  {self._cur_hz} Hz'
            timeout = 3
        elif dpi is not None:
            label = f'{self._cur_dpi} DPI'
            timeout = 3
        else:
            label = f'{self._cur_hz} Hz'
            timeout = 3

        if self._sni:
            self._sni.set_label(label, '')

        # Schedule label hide
        self._label_hide_seq += 1
        seq = self._label_hide_seq
        GLib.timeout_add_seconds(timeout, self._hide_label, seq)

        if hz is not None:
            checked   = Dbusmenu.MENUITEM_TOGGLE_STATE_CHECKED
            unchecked = Dbusmenu.MENUITEM_TOGGLE_STATE_UNCHECKED
            for h, item in self._poll_items.items():
                item.property_set_int(Dbusmenu.MENUITEM_PROP_TOGGLE_STATE,
                                      checked if h == hz else unchecked)

    def _hide_label(self, seq):
        # Only hide if no newer label was shown since this timer was set
        if seq == self._label_hide_seq and self._sni:
            self._sni.set_label('', '')
        return False

    _AUTOSTART_PATH = os.path.expanduser('~/.config/autostart/pulsar-x2a.desktop')
    _AUTOSTART_CONTENT = """\
[Desktop Entry]
Name=Pulsar X2A
Comment=Pulsar X2A system-tray applet
Exec=pulsar-x2a-gui
Icon=input-mouse
Type=Application
X-GNOME-Autostart-enabled=true
"""

    def _autostart_enabled(self):
        return os.path.exists(self._AUTOSTART_PATH)

    def _toggle_autostart(self, menu_item):
        if self._autostart_enabled():
            os.remove(self._AUTOSTART_PATH)
            menu_item.property_set_int(Dbusmenu.MENUITEM_PROP_TOGGLE_STATE,
                                       Dbusmenu.MENUITEM_TOGGLE_STATE_UNCHECKED)
        else:
            os.makedirs(os.path.dirname(self._AUTOSTART_PATH), exist_ok=True)
            with open(self._AUTOSTART_PATH, 'w') as f:
                f.write(self._AUTOSTART_CONTENT)
            menu_item.property_set_int(Dbusmenu.MENUITEM_PROP_TOGGLE_STATE,
                                       Dbusmenu.MENUITEM_TOGGLE_STATE_CHECKED)

    def _set_dpi(self, dpi_val: int):
        def _write():
            try:
                with _USB_LOCK:
                    dev = px.open_device()
                    info = px.get_dpi_stages(dev, profile=1)
                    # Find existing stage with matching DPI
                    for i, (dx, _dy) in enumerate(info['stages']):
                        if dx == dpi_val:
                            px.set_active_dpi_stage(dev, i + 1, profile=1)
                            break
                    else:
                        # No match: update active stage's DPI, keep all others
                        stages = [dx for dx, _dy in info['stages']]
                        stages[info['active'] - 1] = dpi_val
                        px.set_dpi_stages(dev, stages, info['active'], profile=1)
                    px.close_device(dev)
                GLib.idle_add(self._update_tray_label, dpi_val, None)
            except Exception:
                pass
        threading.Thread(target=_write, daemon=True).start()

    def _set_poll(self, hz: int):
        def _write():
            try:
                with _USB_LOCK:
                    dev = px.open_device()
                    px.set_polling_rate(dev, hz)
                    px.close_device(dev)
                GLib.idle_add(self._update_tray_label, None, hz)
            except Exception:
                pass
        threading.Thread(target=_write, daemon=True).start()


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_title('Pulsar X2A')
        self.set_default_size(560, 740)
        self.set_icon_name('input-mouse')
        self.set_hide_on_close(True)  # X hides window; quit via tray menu
        self._dev = None
        self._profile = 1
        self._building = False  # suppress reactive callbacks while populating UI

        self._build_ui()
        GLib.idle_add(self._reload)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        # ── Header bar ────────────────────────────────────────────────────────
        header = Adw.HeaderBar()

        # Profile selector centred in the header
        profile_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        profile_box.append(Gtk.Label(label='Profile:'))
        self._profile_combo = Gtk.DropDown.new_from_strings(
            [f'Profile {i}' for i in range(1, 6)]
        )
        self._profile_combo.connect('notify::selected', self._on_profile_changed)
        profile_box.append(self._profile_combo)
        header.set_title_widget(profile_box)

        reload_btn = Gtk.Button(icon_name='view-refresh-symbolic')
        reload_btn.set_tooltip_text('Reload from mouse')
        reload_btn.connect('clicked', lambda _: self._reload())
        header.pack_start(reload_btn)

        apply_btn = Gtk.Button(label='Apply')
        apply_btn.add_css_class('suggested-action')
        apply_btn.connect('clicked', lambda _: self._apply())
        header.pack_end(apply_btn)

        toolbar_view.add_top_bar(header)

        # ── Toast overlay (for "Settings applied" notifications) ─────────────
        toast_overlay = Adw.ToastOverlay()
        toolbar_view.set_content(toast_overlay)
        self._toast_overlay = toast_overlay

        # ── Scrollable content ────────────────────────────────────────────────
        scroll = Gtk.ScrolledWindow()
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)
        toast_overlay.set_child(scroll)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(600)
        clamp.set_margin_top(24)
        clamp.set_margin_bottom(24)
        clamp.set_margin_start(12)
        clamp.set_margin_end(12)
        scroll.set_child(clamp)

        page_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        clamp.set_child(page_box)

        # Error banner
        self._banner = Adw.Banner()
        self._banner.set_revealed(False)
        page_box.append(self._banner)

        # ── Global settings ───────────────────────────────────────────────────
        global_group = Adw.PreferencesGroup()
        global_group.set_title('Global Settings')
        global_group.set_description('Applies to all profiles')
        page_box.append(global_group)

        self._poll_row = Adw.ComboRow()
        self._poll_row.set_title('Polling Rate')
        self._poll_row.set_subtitle('Hz')
        self._poll_row.set_model(
            Gtk.StringList.new(['125 Hz', '250 Hz', '500 Hz', '1000 Hz'])
        )
        global_group.add(self._poll_row)

        self._debounce_row = Adw.SpinRow.new_with_range(0, 20, 1)
        self._debounce_row.set_title('Debounce')
        self._debounce_row.set_subtitle('milliseconds (0 – 20)')
        global_group.add(self._debounce_row)

        self._angle_row = Adw.SwitchRow()
        self._angle_row.set_title('Angle Snap')
        self._angle_row.set_subtitle('Straightens cursor movement to horizontal/vertical lines')
        global_group.add(self._angle_row)

        self._ripple_row = Adw.SwitchRow()
        self._ripple_row.set_title('Ripple Control')
        self._ripple_row.set_subtitle('Smooths out sensor jitter at low speeds')
        global_group.add(self._ripple_row)

        self._motion_row = Adw.SwitchRow()
        self._motion_row.set_title('Motion Sync')
        self._motion_row.set_subtitle('Synchronises sensor data with USB polling interval')
        global_group.add(self._motion_row)

        # ── Per-profile settings ──────────────────────────────────────────────
        profile_group = Adw.PreferencesGroup()
        profile_group.set_title('Profile Settings')
        page_box.append(profile_group)

        self._lod_row = Adw.ComboRow()
        self._lod_row.set_title('Lift-off Distance')
        self._lod_row.set_subtitle('Height at which tracking stops when lifting the mouse')
        self._lod_row.set_model(Gtk.StringList.new(['1 mm (low)', '2 mm (high)']))
        profile_group.add(self._lod_row)

        self._bright_row = Adw.SpinRow.new_with_range(0, 255, 5)
        self._bright_row.set_title('LED Brightness')
        self._bright_row.set_subtitle('0 – 255')
        profile_group.add(self._bright_row)

        self._led_row = Adw.ComboRow()
        self._led_row.set_title('LED Effect')
        self._led_row.set_model(Gtk.StringList.new(['Off', 'Steady', 'Breath']))
        self._led_row.connect('notify::selected', self._on_led_changed)
        profile_group.add(self._led_row)

        self._breath_row = Adw.SpinRow.new_with_range(0, 100, 1)
        self._breath_row.set_title('Breath Speed')
        self._breath_row.set_subtitle('0 – 100')
        self._breath_row.set_visible(False)
        profile_group.add(self._breath_row)

        # ── DPI stages ────────────────────────────────────────────────────────
        dpi_group = Adw.PreferencesGroup()
        dpi_group.set_title('DPI Stages')
        page_box.append(dpi_group)

        self._stage_count_row = Adw.SpinRow.new_with_range(1, 6, 1)
        self._stage_count_row.set_title('Number of Stages')
        self._stage_count_row.connect('notify::value', self._on_stage_count_changed)
        dpi_group.add(self._stage_count_row)

        self._active_stage_row = Adw.ComboRow()
        self._active_stage_row.set_title('Active Stage')
        self._active_stage_row.set_model(
            Gtk.StringList.new([f'Stage {i}' for i in range(1, 7)])
        )
        dpi_group.add(self._active_stage_row)

        self._dpi_rows = []
        for i in range(1, 7):
            row = Adw.SpinRow.new_with_range(100, 26000, 100)
            row.set_title(f'Stage {i}')
            dpi_group.add(row)
            self._dpi_rows.append(row)

        # ── Button bindings (read-only display) ───────────────────────────────
        btn_group = Adw.PreferencesGroup()
        btn_group.set_title('Button Bindings')
        btn_group.set_description('Use the CLI (--button) to remap buttons')
        page_box.append(btn_group)

        self._btn_rows = {}
        btn_labels = {
            'left': 'Left Click', 'right': 'Right Click',
            'wheel': 'Wheel Click',
            'thumb1': 'Thumb 1 (forward)', 'thumb2': 'Thumb 2 (back)',
            'thumb3': 'Thumb 3 (forward)', 'thumb4': 'Thumb 4 (back)',
            'dpi': 'DPI Button',
        }
        for btn_name, btn_id in px.BUTTONS.items():
            row = Adw.ActionRow()
            row.set_title(btn_labels.get(btn_name, btn_name.capitalize()))
            row.set_subtitle('–')
            btn_group.add(row)
            self._btn_rows[btn_id] = row

        # ── Reset ─────────────────────────────────────────────────────────────
        reset_group = Adw.PreferencesGroup()
        page_box.append(reset_group)

        reset_row = Adw.ButtonRow()
        reset_row.set_title('Reset to Factory Defaults')
        reset_row.add_css_class('destructive-action')
        reset_row.connect('activated', self._on_reset_clicked)
        reset_group.add(reset_row)

        # ── Test Input ────────────────────────────────────────────────────────
        test_group = Adw.PreferencesGroup()
        page_box.append(test_group)

        test_row = Adw.ButtonRow()
        test_row.set_title('Test Input — click to test mouse buttons')
        test_row.connect('activated', self._on_test_clicked)
        test_group.add(test_row)

        # ── OS / Desktop Settings ────────────────────────────────────────────
        os_group = Adw.PreferencesGroup()
        os_group.set_title('Desktop Mouse Settings')
        os_group.set_description('System-wide GNOME settings (affects all mice)')
        page_box.append(os_group)

        # Acceleration profile
        self._accel_row = Adw.ComboRow()
        self._accel_row.set_title('Acceleration Profile')
        self._accel_row.set_subtitle('Use "Flat" for raw input (recommended for gaming)')
        self._accel_row.set_model(Gtk.StringList.new(['Flat (raw)', 'Adaptive (default)']))
        self._accel_row.connect('notify::selected', self._on_accel_changed)
        os_group.add(self._accel_row)

        # Speed / sensitivity
        adj_speed = Gtk.Adjustment(lower=-1.0, upper=1.0, step_increment=0.05,
                                   page_increment=0.1)
        self._speed_row = Adw.SpinRow(adjustment=adj_speed, digits=2)
        self._speed_row.set_title('Pointer Speed')
        self._speed_row.set_subtitle('Multiplier from -1.0 (slow) to 1.0 (fast), 0 = no change')
        os_group.add(self._speed_row)

        speed_apply = Adw.ButtonRow()
        speed_apply.set_title('Apply Desktop Settings')
        speed_apply.connect('activated', self._on_os_apply)
        os_group.add(speed_apply)

        self._load_os_settings()

    def _load_os_settings(self):
        try:
            import subprocess
            accel = subprocess.check_output(
                ['gsettings', 'get', 'org.gnome.desktop.peripherals.mouse', 'accel-profile'],
                text=True).strip().strip("'")
            speed = subprocess.check_output(
                ['gsettings', 'get', 'org.gnome.desktop.peripherals.mouse', 'speed'],
                text=True).strip()
            self._building = True
            self._accel_row.set_selected(0 if accel == 'flat' else 1)
            self._speed_row.set_value(float(speed))
            self._building = False
        except Exception:
            pass

    def _on_accel_changed(self, combo, _param):
        pass  # applied via button

    def _on_os_apply(self, _row):
        accel = 'flat' if self._accel_row.get_selected() == 0 else 'adaptive'
        speed = self._speed_row.get_value()
        import subprocess
        subprocess.run(['gsettings', 'set', 'org.gnome.desktop.peripherals.mouse',
                        'accel-profile', accel])
        subprocess.run(['gsettings', 'set', 'org.gnome.desktop.peripherals.mouse',
                        'speed', str(speed)])

    # ── UI event handlers ─────────────────────────────────────────────────────

    def _on_test_clicked(self, _row):
        dialog = InputTestDialog(transient_for=self)
        dialog.present()

    def _on_profile_changed(self, combo, _param):
        if self._building:
            return
        self._profile = combo.get_selected() + 1
        GLib.idle_add(self._reload_profile)

    def _on_led_changed(self, combo, _param):
        if self._building:
            return
        self._breath_row.set_visible(combo.get_selected() == 2)

    def _on_stage_count_changed(self, row, _param):
        if self._building:
            return
        n = int(row.get_value())
        for i, dpi_row in enumerate(self._dpi_rows):
            dpi_row.set_sensitive(i < n)

    def _on_reset_clicked(self, _row):
        dialog = Adw.AlertDialog()
        dialog.set_heading('Reset to Factory Defaults?')
        dialog.set_body(
            'This sends the firmware reset command to the mouse.\n'
            'All profiles will be restored to factory defaults.'
        )
        dialog.add_response('cancel', 'Cancel')
        dialog.add_response('reset', 'Reset')
        dialog.set_response_appearance('reset', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect('response', self._on_reset_response)
        dialog.present(self)

    def _on_reset_response(self, _dialog, response):
        if response == 'reset':
            self._run_bg(self._do_reset)

    # ── Thread management ─────────────────────────────────────────────────────

    def _run_bg(self, fn):
        threading.Thread(target=fn, daemon=True).start()

    def _reload(self):
        self._run_bg(self._do_reload)

    def _reload_profile(self):
        self._run_bg(self._do_reload_profile)

    def _apply(self):
        # Snapshot all widget values here in the main thread — GTK is not thread-safe.
        num_stages = int(self._stage_count_row.get_value())
        s = {
            'poll_hz':    [125, 250, 500, 1000][self._poll_row.get_selected()],
            'debounce':   int(self._debounce_row.get_value()),
            'angle':      self._angle_row.get_active(),
            'ripple':     self._ripple_row.get_active(),
            'motion':     self._motion_row.get_active(),
            'profile':    self._profile,
            'lod':        self._lod_row.get_selected() + 1,
            'brightness': int(self._bright_row.get_value()),
            'led':        ['off', 'steady', 'breath'][self._led_row.get_selected()],
            'breath':     int(self._breath_row.get_value()),
            'num_stages': num_stages,
            'active':     min(self._active_stage_row.get_selected() + 1, num_stages),
            'dpi_values': [int(r.get_value()) for r in self._dpi_rows],
        }
        self._run_bg(lambda: self._do_apply(s))

    # ── USB workers (run in background threads) ───────────────────────────────

    def _open_dev(self) -> bool:
        _USB_LOCK.acquire()
        try:
            self._dev = px.open_device()
            GLib.idle_add(self._banner.set_revealed, False)
            return True
        except SystemExit as e:
            _USB_LOCK.release()
            GLib.idle_add(self._show_error, str(e))
            return False
        except Exception as e:
            _USB_LOCK.release()
            GLib.idle_add(self._show_error, f'Error opening device: {e}')
            return False

    def _close_dev(self):
        if self._dev:
            try:
                px.close_device(self._dev)
            except Exception:
                pass
            self._dev = None
        _USB_LOCK.release()

    def _do_reload(self):
        if not self._open_dev():
            return
        try:
            poll_hz  = px.get_polling_rate(self._dev)
            debounce = px.get_debounce(self._dev)
            angle    = px.get_angle_snap(self._dev)
            ripple   = px.get_ripple_control(self._dev)
            motion   = px.get_motion_sync(self._dev)
            GLib.idle_add(self._populate_global, poll_hz, debounce, angle, ripple, motion)
        except Exception as e:
            GLib.idle_add(self._show_error, f'Read error (global): {e}')
        self._do_reload_profile_inner()
        self._close_dev()

    def _do_reload_profile(self):
        if not self._open_dev():
            return
        self._do_reload_profile_inner()
        self._close_dev()

    def _do_reload_profile_inner(self):
        p = self._profile
        try:
            lod       = px.get_lod(self._dev, p)
            brightness = px.get_brightness(self._dev, p)
            led       = px.get_led_effect(self._dev, p)
            breath    = px.get_breath_speed(self._dev, p)
            dpi_info  = px.get_dpi_stages(self._dev, p)
            buttons   = {bid: px.get_button(self._dev, bid, p)
                         for bid in px.BUTTONS.values()}
            GLib.idle_add(self._populate_profile,
                          lod, brightness, led, breath, dpi_info, buttons)
        except Exception as e:
            GLib.idle_add(self._show_error, f'Read error (profile {p}): {e}')

    def _do_apply(self, s: dict):
        if not self._open_dev():
            return
        try:
            px.set_polling_rate(self._dev, s['poll_hz'])
            px.set_debounce(self._dev, s['debounce'])
            px.set_angle_snap(self._dev, s['angle'])
            px.set_ripple_control(self._dev, s['ripple'])
            px.set_motion_sync(self._dev, s['motion'])

            p = s['profile']
            px.set_lod(self._dev, s['lod'], p)
            px.set_brightness(self._dev, s['brightness'], p)
            px.set_led_effect(self._dev, s['led'], p)
            if s['led'] == 'breath':
                px.set_breath_speed(self._dev, s['breath'], p)

            stages = s['dpi_values'][:s['num_stages']]
            px.set_dpi_stages(self._dev, stages, s['active'], p)

            GLib.idle_add(self._show_toast, 'Settings applied')
        except Exception as e:
            GLib.idle_add(self._show_error, f'Write error: {e}')
        self._close_dev()

    def _do_reset(self):
        if not self._open_dev():
            return
        try:
            px.reset_to_defaults(self._dev, self._profile)
            GLib.idle_add(self._show_toast, 'Reset to factory defaults')
        except Exception as e:
            GLib.idle_add(self._show_error, f'Reset error: {e}')
        self._close_dev()
        # Give the device ~1 s to finish its internal reset before reconnecting.
        time.sleep(1.0)
        GLib.idle_add(self._reload)

    # ── UI population helpers (called via GLib.idle_add in main thread) ───────

    def _populate_global(self, poll_hz, debounce, angle, ripple, motion):
        self._building = True
        self._poll_row.set_selected({125: 0, 250: 1, 500: 2, 1000: 3}.get(poll_hz, 3))
        self._debounce_row.set_value(debounce)
        self._angle_row.set_active(angle)
        self._ripple_row.set_active(ripple)
        self._motion_row.set_active(motion)
        self._building = False

    def _populate_profile(self, lod, brightness, led, breath, dpi_info, buttons):
        self._building = True
        self._lod_row.set_selected(lod - 1)
        self._bright_row.set_value(brightness)
        led_idx = {'off': 0, 'steady': 1, 'breath': 2}.get(led, 1)
        self._led_row.set_selected(led_idx)
        self._breath_row.set_value(breath)
        self._breath_row.set_visible(led == 'breath')

        stages = dpi_info['stages']
        num    = dpi_info['count']
        active = dpi_info['active']
        self._stage_count_row.set_value(num)
        self._active_stage_row.set_selected(max(0, active - 1))
        for i, row in enumerate(self._dpi_rows):
            row.set_value(stages[i][0] if i < len(stages) else 800)
            row.set_sensitive(i < num)

        for btn_id, (t, a1, a2) in buttons.items():
            if btn_id in self._btn_rows:
                self._btn_rows[btn_id].set_subtitle(px.describe_button(t, a1, a2))
        self._building = False

    def _show_error(self, msg: str):
        self._banner.set_title(msg)
        self._banner.set_revealed(True)

    def _show_toast(self, msg: str):
        toast = Adw.Toast.new(msg)
        toast.set_timeout(3)
        self._toast_overlay.add_toast(toast)


class InputTestDialog(Adw.Window):
    """Input test dialog with a mouse diagram and event log."""

    # Button zones: (x, y, w, h) as fractions of the drawing area
    _ZONES = {
        1: ('Left Click',    0.02, 0.02, 0.44, 0.35),
        2: ('Right Click',   0.54, 0.02, 0.44, 0.35),
        3: ('Wheel Click',   0.38, 0.06, 0.24, 0.20),
        8: ('Thumb Back',    0.00, 0.50, 0.30, 0.15),
        9: ('Thumb Forward', 0.00, 0.36, 0.30, 0.15),
    }
    _GTK_BTN_NAMES = {1: 'Left', 2: 'Middle', 3: 'Right',
                       8: 'Back (both sides)', 9: 'Forward (both sides)'}

    def __init__(self, **kwargs):
        super().__init__(**kwargs, title='Input Test', default_width=380, default_height=520)
        self.set_modal(True)

        # Detect left-handed mode to un-swap buttons for visualization
        self._left_handed = False
        try:
            import subprocess
            val = subprocess.check_output(
                ['gsettings', 'get', 'org.gnome.desktop.peripherals.mouse', 'left-handed'],
                text=True).strip()
            self._left_handed = val == 'true'
        except Exception:
            pass

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(vbox)

        header = Adw.HeaderBar()
        vbox.append(header)

        hint = 'Left-handed mode detected — buttons un-swapped for display' \
               if self._left_handed else 'Click any mouse button in the area below'
        label = Gtk.Label(label=hint)
        label.set_margin_top(8)
        label.set_margin_bottom(4)
        vbox.append(label)

        # Drawing area for mouse shape
        self._active_btn = 0
        self._drawing = Gtk.DrawingArea()
        self._drawing.set_content_width(300)
        self._drawing.set_content_height(280)
        self._drawing.set_halign(Gtk.Align.CENTER)
        self._drawing.set_draw_func(self._draw)
        vbox.append(self._drawing)

        # Capture clicks (button 0 = any button)
        click = Gtk.GestureClick.new()
        click.set_button(0)
        click.connect('pressed', self._on_press)
        click.connect('released', self._on_release)
        self._drawing.add_controller(click)

        # Capture scroll
        scroll_ctrl = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL |
            Gtk.EventControllerScrollFlags.HORIZONTAL)
        scroll_ctrl.connect('scroll', self._on_scroll)
        self._drawing.add_controller(scroll_ctrl)

        # Event log
        self._log_view = Gtk.TextView()
        self._log_view.set_editable(False)
        self._log_view.set_cursor_visible(False)
        self._log_view.set_monospace(True)
        self._log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._log_buf = self._log_view.get_buffer()

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_margin_start(12)
        scroll.set_margin_end(12)
        scroll.set_margin_top(8)
        scroll.set_margin_bottom(12)
        scroll.set_child(self._log_view)
        vbox.append(scroll)

        # Listen for DPI button via hidraw (firmware handles it, no GTK event)
        self._dpi_hide_seq = 0
        threading.Thread(target=self._dpi_listener, daemon=True).start()

    def _log(self, msg):
        end = self._log_buf.get_end_iter()
        self._log_buf.insert(end, msg + '\n')
        # Scroll to end
        end = self._log_buf.get_end_iter()
        self._log_view.scroll_to_iter(end, 0, False, 0, 0)

    def _physical_btn(self, gtk_btn):
        """Un-swap left/right if left-handed mode is active."""
        if self._left_handed and gtk_btn in (1, 3):
            return 4 - gtk_btn   # 1↔3
        return gtk_btn

    def _on_press(self, gesture, _n, x, y):
        gtk_btn = gesture.get_current_button()
        btn = self._physical_btn(gtk_btn)
        name = self._GTK_BTN_NAMES.get(btn, f'Button {btn}')
        self._active_btn = btn
        self._drawing.queue_draw()
        self._log(f'Press:   {name} (button {btn})')

    def _on_release(self, gesture, _n, x, y):
        gtk_btn = gesture.get_current_button()
        btn = self._physical_btn(gtk_btn)
        name = self._GTK_BTN_NAMES.get(btn, f'Button {btn}')
        self._active_btn = 0
        self._drawing.queue_draw()
        self._log(f'Release: {name} (button {btn})')

    def _on_scroll(self, ctrl, dx, dy):
        if dy < 0:
            self._active_btn = 'scroll_up'
            self._log('Scroll:  Up')
        elif dy > 0:
            self._active_btn = 'scroll_down'
            self._log('Scroll:  Down')
        if dx < 0:
            self._log('Scroll:  Left')
        elif dx > 0:
            self._log('Scroll:  Right')
        self._drawing.queue_draw()
        self._scroll_hide_seq = getattr(self, '_scroll_hide_seq', 0) + 1
        seq = self._scroll_hide_seq
        GLib.timeout_add(300, self._clear_scroll, seq)
        return True

    def _clear_scroll(self, seq):
        if seq == self._scroll_hide_seq:
            self._active_btn = 0
            self._drawing.queue_draw()
        return False

    def _dpi_listener(self):
        """Listen for DPI button presses via hidraw."""
        import struct as st
        path = PulsarX2AApp._find_hidraw()
        if not path:
            return
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            while True:
                data = os.read(fd, 256)
                if not data:
                    break
                if len(data) >= 7 and data[0] == 0x05 and data[1] == 0x05:
                    dpi = st.unpack_from('<H', data, 3)[0]
                    stage = data[2] + 1
                    GLib.idle_add(self._on_dpi_event, dpi, stage)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _on_dpi_event(self, dpi, stage):
        self._active_btn = 'dpi'
        self._drawing.queue_draw()
        self._log(f'DPI:     Stage {stage} → {dpi} DPI')
        self._dpi_hide_seq += 1
        seq = self._dpi_hide_seq
        GLib.timeout_add(500, self._clear_dpi, seq)

    def _clear_dpi(self, seq):
        if seq == self._dpi_hide_seq:
            self._active_btn = 0
            self._drawing.queue_draw()
        return False

    def _draw(self, area, cr, w, h):
        import math

        # Background
        cr.set_source_rgb(0.15, 0.15, 0.17)
        cr.paint()

        # Mouse body (rounded shape)
        mx, my, mw, mh = w * 0.15, h * 0.02, w * 0.7, h * 0.92
        r = mw * 0.35
        cr.set_source_rgb(0.30, 0.30, 0.33)
        cr.new_path()
        cr.arc(mx + r, my + r, r, math.pi, 1.5 * math.pi)
        cr.arc(mx + mw - r, my + r, r, 1.5 * math.pi, 2 * math.pi)
        cr.arc(mx + mw - r, my + mh - r, r, 0, 0.5 * math.pi)
        cr.arc(mx + r, my + mh - r, r, 0.5 * math.pi, math.pi)
        cr.close_path()
        cr.fill()

        # Divider line (left/right split)
        cr.set_source_rgb(0.20, 0.20, 0.22)
        cr.set_line_width(2)
        cr.move_to(w * 0.5, my)
        cr.line_to(w * 0.5, my + mh * 0.40)
        cr.stroke()

        # Scroll wheel
        ww, wh = mw * 0.12, mh * 0.12
        wx = w * 0.5 - ww / 2
        wy = my + mh * 0.08
        is_scroll = isinstance(self._active_btn, str) and self._active_btn.startswith('scroll')
        if is_scroll or self._active_btn == 2:
            cr.set_source_rgb(0.3, 0.7, 1.0)
        else:
            cr.set_source_rgb(0.45, 0.45, 0.50)
        self._rounded_rect(cr, wx, wy, ww, wh, ww * 0.3)
        cr.fill()
        # Scroll direction arrow
        if self._active_btn == 'scroll_up':
            cr.set_source_rgb(1, 1, 1)
            cr.move_to(wx + ww / 2, wy + 3)
            cr.line_to(wx + ww / 2 - 4, wy + wh / 2)
            cr.line_to(wx + ww / 2 + 4, wy + wh / 2)
            cr.close_path()
            cr.fill()
        elif self._active_btn == 'scroll_down':
            cr.set_source_rgb(1, 1, 1)
            cr.move_to(wx + ww / 2, wy + wh - 3)
            cr.line_to(wx + ww / 2 - 4, wy + wh / 2)
            cr.line_to(wx + ww / 2 + 4, wy + wh / 2)
            cr.close_path()
            cr.fill()

        # Thumb buttons — left side (thumb1=fwd, thumb2=back)
        #                right side (thumb3=fwd, thumb4=back)
        # Both sides share the same GTK button IDs (8=back, 9=fwd)
        # because firmware maps thumb3/4 to the same HID usage as thumb1/2
        thumb_buttons = [
            ('left',  0.38, 'FWD',  9),
            ('left',  0.52, 'BACK', 8),
            ('right', 0.38, 'FWD',  9),
            ('right', 0.52, 'BACK', 8),
        ]
        for side, ty, label, btn_id in thumb_buttons:
            if side == 'left':
                bx = mx - mw * 0.08
            else:
                bx = mx + mw * 0.90
            by = my + mh * ty
            bw = mw * 0.18
            bh = mh * 0.10
            if self._active_btn == btn_id:
                cr.set_source_rgb(0.3, 0.7, 1.0)
            else:
                cr.set_source_rgb(0.40, 0.40, 0.44)
            self._rounded_rect(cr, bx, by, bw, bh, 4)
            cr.fill()
            cr.set_source_rgb(0.9, 0.9, 0.9)
            cr.set_font_size(9)
            ext = cr.text_extents(label)
            cr.move_to(bx + bw / 2 - ext.width / 2, by + bh / 2 + ext.height / 2)
            cr.show_text(label)

        # Highlight active button zone on mouse body
        zones_on_body = {
            1: (mx, my, mw * 0.49, mh * 0.38),           # left
            3: (mx + mw * 0.51, my, mw * 0.49, mh * 0.38), # right
        }
        if self._active_btn in zones_on_body:
            zx, zy, zw, zh = zones_on_body[self._active_btn]
            cr.set_source_rgba(0.3, 0.7, 1.0, 0.35)
            self._rounded_rect(cr, zx, zy, zw, zh, r if self._active_btn != 3 else 6)
            cr.fill()

        # Labels on mouse body
        cr.set_source_rgb(0.85, 0.85, 0.85)
        cr.set_font_size(12)
        for label, lx_frac, ly_frac in [('L', 0.38, 0.22), ('R', 0.60, 0.22)]:
            ext = cr.text_extents(label)
            cr.move_to(w * lx_frac - ext.width / 2, my + mh * ly_frac)
            cr.show_text(label)

        # DPI button (below wheel)
        dx, dy = w * 0.5, my + mh * 0.28
        dr = 7 if self._active_btn == 'dpi' else 5
        if self._active_btn == 'dpi':
            cr.set_source_rgb(0.3, 0.7, 1.0)
        else:
            cr.set_source_rgb(0.50, 0.50, 0.55)
        cr.arc(dx, dy, dr, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgb(0.9, 0.9, 0.9) if self._active_btn == 'dpi' else cr.set_source_rgb(0.7, 0.7, 0.7)
        cr.set_font_size(8)
        ext = cr.text_extents('DPI')
        cr.move_to(dx - ext.width / 2, dy + dr + ext.height + 2)
        cr.show_text('DPI')

    @staticmethod
    def _rounded_rect(cr, x, y, w, h, r):
        import math
        r = min(r, w / 2, h / 2)
        cr.new_path()
        cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
        cr.arc(x + w - r, y + r, r, 1.5 * math.pi, 2 * math.pi)
        cr.arc(x + w - r, y + h - r, r, 0, 0.5 * math.pi)
        cr.arc(x + r, y + h - r, r, 0.5 * math.pi, math.pi)
        cr.close_path()


def main():
    app = PulsarX2AApp()
    sys.exit(app.run(sys.argv))


if __name__ == '__main__':
    main()
